"""The release each backend reports as current — the ``upstream_release`` tool.

``upstream_version`` on a tool's answer is the release THAT answer states
(issue #121) and stays null where the answer states none. Most of those
backends do publish their current release, but only on a separate endpoint:
a DIFFERENT request that may describe a different release than the one that
answered a data call. This module reads those endpoints and reports what they
say, labelled as current at query time — never copied into ``upstream_version``.

Read it before and after a run: equal values mean no release changed in
between. Nothing here is cached, because a cached value would make that
comparison compare a value with itself.

Backends whose answers already state a release (UniProt, InterPro, PANTHER,
AlphaFold) or whose requests pin one (Gramene, ATTED-II, OrthoDB) are not
here: their ``upstream_version`` is already the release that answered. Three
backends publish no data release at all and are listed with the reason.
Endpoints and payload shapes probed live 2026-09-26.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import httpx

from plant_genomics_mcp import _http, ensembl_plants, jaspar, kegg, quickgo, string_db
from plant_genomics_mcp.errors import InvalidArguments, PlantGenomicsError

DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3

# Backends that publish no data release: why, as probed 2026-09-26.
_NONE_PUBLISHED = {
    "pdbe": (
        "PDBe states no archive release: its answers carry no release header or "
        "field, and /pdbe/api/status, /pdbe/api/pdb/release and /pdbe/api/v2/status "
        "are 404 (probed 2026-09-26). Per-entry release dates are not an archive release."
    ),
    "aragwas": (
        "AraGWAS states no release: its answers carry no release header or field, "
        "and its /api/ root lists resources only (probed 2026-09-26)."
    ),
    "europe_pmc": (
        "Europe PMC's index is updated continuously and has no data release; the "
        "'version' on its answers (6.9 on 2026-09-26) is the REST API's version."
    ),
}

BACKENDS = ("ensembl_plants", "string", "quickgo", "jaspar", "kegg", *_NONE_PUBLISHED)

# KEGG /info/kegg: one tab-indented line per database, "<db>  <entries>  <yyyy/mm/dd>".
_KEGG_LINE = re.compile(r"^\t(pathway|genes)\s+[\d,]+\s+(\d{4}/\d{2}/\d{2})$")
_KEGG_DATABASES = ("pathway", "genes")  # the two kegg_pathways reads


# (release, components, the URLs actually requested, what the value is)
_Found = tuple[str, dict[str, str], list[str], str]


async def _get(client: httpx.AsyncClient, url: str, service: str, **kw: Any) -> httpx.Response:
    return await _http.request_with_retry(
        client,
        "GET",
        url,
        service=service,
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
        **kw,
    )


def _text(value: Any, what: str) -> str:
    """A release component as a non-empty string; anything else is a changed shape."""
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).strip():
        raise PlantGenomicsError(f"{what} is unusable: {value!r}")
    return str(value).strip()


async def _ensembl(client: httpx.AsyncClient) -> _Found:
    resp = await _get(
        client,
        f"{ensembl_plants.BASE_URL}/info/eg_version",
        "Ensembl /info/eg_version",
        headers={"Accept": "application/json"},
    )
    body = _http.json_body(resp, "Ensembl /info/eg_version")
    version = _text(
        (body or {}).get("version") if isinstance(body, dict) else None, "Ensembl eg_version"
    )
    return (
        version,
        {"eg_version": version},
        [str(resp.url)],
        "The Ensembl Genomes release rest.ensembl.org serves (Ensembl Plants and "
        "Compara plants), current at query time.",
    )


async def _string(client: httpx.AsyncClient) -> _Found:
    resp = await _get(client, f"{string_db.BASE_URL}/api/json/version", "STRING /api/json/version")
    body = _http.json_body(resp, "STRING version")
    if not isinstance(body, list) or len(body) != 1 or not isinstance(body[0], dict):
        raise PlantGenomicsError(
            f"STRING /api/json/version returned an unexpected shape: {body!r:.200}"
        )
    version = _text(body[0].get("string_version"), "STRING string_version")
    return (
        version,
        {"string_version": version},
        [str(resp.url)],
        "The STRING release string-db.org serves, current at query time.",
    )


async def _quickgo(client: httpx.AsyncClient) -> _Found:
    parts: dict[str, str] = {}
    urls = []
    for part, path in (("annotation", "/annotation/about"), ("go", "/ontology/go/about")):
        resp = await _get(
            client,
            f"{quickgo.BASE_URL}{path}",
            f"QuickGO {path}",
            headers={"Accept": "application/json"},
        )
        body = _http.json_body(resp, f"QuickGO {path}")
        block = body.get(part) if isinstance(body, dict) else None
        stamp = block.get("timestamp") if isinstance(block, dict) else None
        parts[part] = _text(stamp, f"QuickGO {path} {part}.timestamp")
        urls.append(str(resp.url))
    return (
        f"annotation {parts['annotation']}; go {parts['go']}",
        parts,
        urls,
        "QuickGO's two dates at query time: 'annotation' is the GO annotation (GOA) "
        "load locus_go_annotations reads, 'go' the GO ontology release.",
    )


async def _jaspar(client: httpx.AsyncClient) -> _Found:
    resp = await _get(
        client,
        f"{jaspar.BASE_URL}/api/v1/releases/",
        "JASPAR /releases",
        params={"format": "json", "page_size": 100},
    )
    body = _http.json_body(resp, "JASPAR /releases")
    rows = body.get("results") if isinstance(body, dict) else None
    if not isinstance(rows, list) or body.get("next") is not None:
        raise PlantGenomicsError(f"JASPAR /releases returned an unexpected shape: {body!r:.200}")
    active = [r for r in rows if isinstance(r, dict) and r.get("active") == "Yes"]
    if not active:
        raise PlantGenomicsError("JASPAR /releases lists no active release")
    newest = max(active, key=lambda r: int(_text(r.get("release_number"), "JASPAR release_number")))
    number = _text(newest.get("release_number"), "JASPAR release_number")
    return (
        number,
        {
            "release_number": number,
            "year": _text(newest.get("year"), "JASPAR year"),
            "active_releases": str(len(active)),
        },
        [str(resp.url)],
        "The newest ACTIVE JASPAR release at query time. Several releases are "
        "active at once and a matrix answer names none, so this is the newest one "
        "on offer, not proof of the one that answered.",
    )


async def _kegg(client: httpx.AsyncClient) -> _Found:
    resp = await _get(client, f"{kegg.BASE_URL}/info/kegg", "KEGG /info/kegg")
    text = resp.text
    dates = {m.group(1): m.group(2) for m in map(_KEGG_LINE.match, text.splitlines()) if m}
    missing = [db for db in _KEGG_DATABASES if db not in dates]
    if missing:
        raise PlantGenomicsError(f"KEGG /info/kegg lists no date for {missing}: {text[:200]!r}")
    return (
        "; ".join(f"{db} {dates[db]}" for db in _KEGG_DATABASES),
        {db: dates[db] for db in _KEGG_DATABASES},
        [str(resp.url)],
        "KEGG publishes no release number, only each database's last-update date at "
        "query time: 'pathway' and 'genes' are the two kegg_pathways reads.",
    )


_FETCHERS = {
    "ensembl_plants": _ensembl,
    "string": _string,
    "quickgo": _quickgo,
    "jaspar": _jaspar,
    "kegg": _kegg,
}


async def upstream_release(client: httpx.AsyncClient, backend: str) -> dict[str, Any]:
    """The release ``backend`` reports as current, read now from its own endpoint."""
    if backend not in BACKENDS:
        raise InvalidArguments(f"backend must be one of {list(BACKENDS)}, got {backend!r}")
    if backend in _NONE_PUBLISHED:
        release: str | None = None
        components: dict[str, str] = {}
        endpoints: list[str] = []
        reason = _NONE_PUBLISHED[backend]
    else:
        release, components, endpoints, reason = await _FETCHERS[backend](client)
    return {
        "backend": backend,
        "release": release,
        "components": components,
        "endpoints": endpoints,
        "observed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "reason": reason,
    }
