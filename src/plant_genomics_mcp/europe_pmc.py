"""Europe PMC REST client — async httpx wrapper around www.ebi.ac.uk/europepmc.

Europe PMC indexes PubMed + PMC + preprints + agricultural literature. The
REST API is free, no auth, no per-IP rate limit documented (the team asks
that bulk pipelines be polite — we retry on 429/5xx with backoff).

We query the ``/search`` endpoint with the locus identifier as a free-text
query. Locus IDs like ``AT1G01010`` are unique enough that an unqualified
query returns relevant papers; for non-Arabidopsis species we also append
the species common name to help disambiguate. Endpoint docs:
https://europepmc.org/RestfulWebService.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from plant_genomics_mcp import _http, cache, organisms, validators
from plant_genomics_mcp.errors import PlantGenomicsError

BASE_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3
DEFAULT_PAGE_SIZE = 10
MAX_PAGE_SIZE = 25  # cap to keep the wire payload bounded

# Per-module response cache. See plant_genomics_mcp.cache for env knobs.
_CACHE = cache.TTLCache()


# Subset of Europe PMC result fields we surface. The upstream record carries
# ~50 fields including affiliations, dateOfRevision, fullTextUrlList, etc. —
# clients that want the full record can re-fetch by id or pmid.
_HIT_FIELDS = (
    "id",
    "source",
    "pmid",
    "pmcid",
    "doi",
    "title",
    "authorString",
    "journalTitle",
    "pubYear",
    "firstPublicationDate",
    "citedByCount",
    "isOpenAccess",
    "hasPDF",
    "abstractText",
)


def _search_shape_problem(raw: Any) -> str | None:
    """Why a /search body cannot be read as an answer, or None if it can.

    Issue #141: Europe PMC intermittently answers 200 with ``{"version":"6.9"}``
    and nothing else. Read with defaults, that is hitCount 0 and no hits — a
    false "no papers" — so a body is an answer only if it states its count.
    A genuine zero states ``"hitCount": 0`` and an empty ``resultList``.
    """
    if not isinstance(raw, dict):
        return f"non-dict payload: {type(raw).__name__}"
    hit_count = raw.get("hitCount")
    if isinstance(hit_count, bool) or not isinstance(hit_count, int):
        return f"no integer hitCount in {str(raw)[:120]}"
    result_list = raw.get("resultList")
    if not isinstance(result_list, dict) or not isinstance(result_list.get("result"), list):
        return f"no resultList.result list in {str(raw)[:120]}"
    return None


async def _get(
    client: httpx.AsyncClient,
    path: str,
    params: dict[str, Any] | None = None,
    shape_problem: Callable[[Any], str | None] | None = None,
) -> Any:
    """GET an Europe PMC endpoint with retry on 429/5xx.

    With ``shape_problem``, a body it rejects is asked for once more, then
    raised as :class:`UpstreamUnavailableError` — and is never cached, so one
    malformed answer cannot be served as the answer for the cache TTL.
    """
    return await _http.cached_get(
        client,
        _CACHE,
        f"{BASE_URL}{path}",
        service=f"Europe PMC {path}",
        params=params,
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
        reject=shape_problem,
    )


_FLAGS = {"Y": True, "N": False}


def _year(value: Any) -> int | None:
    """Europe PMC's string pubYear as an int (#134); None stays None."""
    if value is None:
        return None
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise PlantGenomicsError(f"Europe PMC pubYear is not a year: {value!r}")


def _flag(name: str, value: Any) -> bool | None:
    """Europe PMC's "Y"/"N" flag as a bool (#134); None stays None."""
    if value is None:
        return None
    if value in _FLAGS:
        return _FLAGS[value]
    raise PlantGenomicsError(f"Europe PMC {name} is neither 'Y' nor 'N': {value!r}")


def _normalize(hit: dict[str, Any], include_abstract: bool = True) -> dict[str, Any]:
    """Project an Europe PMC result row down to the surfaced field set.

    Adds ``web_url`` derived from pmcid (preferred — open access) or pmid.
    Keeps null fields explicit so the outputSchema's optional-field contract
    is observable in the wire payload.
    """
    normalized: dict[str, Any] = {k: hit.get(k) for k in _HIT_FIELDS}
    # Issue #134: one type per kind of value. Upstream sends the year and the
    # Y/N flags as strings beside an int citedByCount; identifiers stay strings.
    normalized["pubYear"] = _year(hit.get("pubYear"))
    normalized["isOpenAccess"] = _flag("isOpenAccess", hit.get("isOpenAccess"))
    normalized["hasPDF"] = _flag("hasPDF", hit.get("hasPDF"))
    # Issue #134: resultType=core carries the journal under journalInfo; the
    # flat journalTitle is a lite-only field and was null on every real hit.
    journal = (hit.get("journalInfo") or {}).get("journal") or {}
    normalized["journalTitle"] = hit.get("journalTitle") or journal.get("title")
    if not include_abstract:
        # Measured on AT3G51240: abstractText is 10,968 of 16,360 bytes — 67% of
        # the payload at the default page size.
        #
        # Nulling it does NOT by itself distinguish "not requested" from "this
        # article has no abstract" — nothing in the row says which. The response
        # therefore carries a top-level ``abstracts_included`` flag, so a reader
        # who did not make the call can still tell the difference.
        normalized["abstractText"] = None
    pmcid = hit.get("pmcid")
    pmid = hit.get("pmid")
    if pmcid:
        normalized["web_url"] = f"https://europepmc.org/article/PMC/{pmcid}"
    elif pmid:
        normalized["web_url"] = f"https://europepmc.org/article/MED/{pmid}"
    else:
        normalized["web_url"] = None
    return normalized


def _next_cursor(bound: dict[str, Any], raw: dict[str, Any], end: int) -> str | None:
    """The cursor after this page: Europe PMC's nextCursorMark while hits remain."""
    mark = raw.get("nextCursorMark")
    if raw["hitCount"] <= end or not isinstance(mark, str) or not mark:
        return None
    return _http.encode_cursor("locus_literature", bound, {"mark": mark, "offset": end})


async def lookup_locus(
    client: httpx.AsyncClient,
    locus: str,
    organism: str | int = organisms.DEFAULT_ORGANISM,
    size: int = DEFAULT_PAGE_SIZE,
    include_abstract: bool = True,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Search Europe PMC for literature mentioning a plant locus.

    ``cursor`` is a ``next_cursor`` from the previous page (#123); it carries
    Europe PMC's own ``cursorMark`` and how many hits came before.

    ``size`` is clamped to [1, MAX_PAGE_SIZE] to bound the wire payload.
    ``include_abstract=False`` nulls ``abstractText``, which is ~67% of the
    payload at the default page size; the response's ``abstracts_included``
    flag records which mode produced it.
    ``organism`` accepts any form resolvable by :mod:`organisms` (canonical
    slug, scientific name, common name, NCBI taxid, alias). Returns a dict
    shaped per ``LocusLiterature``: locus, organism (resolved canonical),
    hitCount (total available in Europe PMC), returned (len(hits)), hits[].
    """
    # Audit 2026-09-22 L12: an empty locus became an empty query, which Europe
    # PMC answers with 200 {"errCode":404,"errMsg":"No search criteria
    # provided..."} — no hitCount, so it read as an outage. Validated like
    # every other locus tool (and the AGI recased), so it is refused as input.
    locus = validators.assert_valid_locus(locus, backend="Europe PMC")
    size = max(1, min(size, MAX_PAGE_SIZE))
    record = organisms.resolve(organism)
    query = locus
    suffix = organisms.europe_pmc_slug_for(organism)
    if suffix:
        query = f"{locus} AND {suffix}"
    bound = {"query": query, "size": size}
    position = _http.decode_cursor("locus_literature", bound, cursor)
    offset = int(position.get("offset", 0))
    params: dict[str, Any] = {
        "query": query,
        "format": "json",
        "resultType": "core",
        "pageSize": size,
    }
    # The first page is the same with or without cursorMark=* (live
    # 2026-09-23: same ids, same nextCursorMark), so it is sent only to continue.
    if "mark" in position:
        params["cursorMark"] = position["mark"]
    raw = await _get(client, "/search", params=params, shape_problem=_search_shape_problem)
    # _search_shape_problem has vouched for both: no defaults here, because a
    # defaulted missing count is exactly how #141's false zeros were made.
    results = raw["resultList"]["result"]
    hits = [_normalize(r, include_abstract) for r in results if isinstance(r, dict)]
    return {
        "locus": locus,
        "organism": record.canonical,
        "query": query,
        "hitCount": raw["hitCount"],
        **_http.counted(raw["hitCount"], hits, offset=offset),
        "next_cursor": _next_cursor(bound, raw, offset + len(hits)),
        # Makes the payload self-describing: without this, a null abstractText
        # is ambiguous between "not requested" and "this article has none", and
        # only the original caller would know which.
        "abstracts_included": include_abstract,
        "hits": hits,
        # Issue #121: uniform key; null because this backend states no release on
        # the answering response (headers probed live 2026-09-22).
        "upstream_version": None,
    }
