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

import asyncio
from typing import Any

import httpx

from plant_genomics_mcp import cache, progress
from plant_genomics_mcp.errors import (
    PlantGenomicsError,
    RateLimitError,
    UpstreamUnavailableError,
)

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


async def _get(
    client: httpx.AsyncClient,
    path: str,
    params: dict[str, Any] | None = None,
) -> Any:
    """GET an Europe PMC endpoint with retry on 429/5xx."""
    key = cache.make_key("GET", BASE_URL, path, params)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    headers = {"Accept": "application/json"}
    delay = 1.0
    last_status: int | None = None
    for attempt in range(MAX_RETRIES):
        resp = await client.get(
            f"{BASE_URL}{path}",
            params=params,
            headers=headers,
            timeout=DEFAULT_TIMEOUT,
        )
        last_status = resp.status_code
        if resp.status_code == 200:
            result = resp.json()
            _CACHE.set(key, result)
            return result
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES - 1:
            retry_after = float(resp.headers.get("Retry-After", delay))
            await progress.notify(
                f"Europe PMC {path}: HTTP {resp.status_code}, retrying in "
                f"{retry_after:.1f}s (attempt {attempt + 2}/{MAX_RETRIES})"
            )
            await asyncio.sleep(retry_after)
            delay *= 2
            continue
        if resp.status_code == 429:
            raise RateLimitError(f"Europe PMC {path} rate-limited (HTTP 429): {resp.text[:200]}")
        if resp.status_code in (500, 502, 503, 504):
            raise UpstreamUnavailableError(
                f"Europe PMC {path} → HTTP {resp.status_code}: {resp.text[:200]}"
            )
        raise PlantGenomicsError(f"Europe PMC {path} → HTTP {resp.status_code}: {resp.text[:200]}")
    if last_status == 429:
        raise RateLimitError(f"Europe PMC {path} exhausted {MAX_RETRIES} retries (429)")
    raise UpstreamUnavailableError(
        f"Europe PMC {path} exhausted {MAX_RETRIES} retries (last HTTP {last_status})"
    )


def _normalize(hit: dict[str, Any]) -> dict[str, Any]:
    """Project an Europe PMC result row down to the surfaced field set.

    Adds ``web_url`` derived from pmcid (preferred — open access) or pmid.
    Keeps null fields explicit so the outputSchema's optional-field contract
    is observable in the wire payload.
    """
    normalized: dict[str, Any] = {k: hit.get(k) for k in _HIT_FIELDS}
    pmcid = hit.get("pmcid")
    pmid = hit.get("pmid")
    if pmcid:
        normalized["web_url"] = f"https://europepmc.org/article/PMC/{pmcid}"
    elif pmid:
        normalized["web_url"] = f"https://europepmc.org/article/MED/{pmid}"
    else:
        normalized["web_url"] = None
    return normalized


# Common name suffixes for the species slugs we already support elsewhere.
# Appended to the query for non-Arabidopsis species to disambiguate locus IDs
# that could overlap with unrelated literature.
_SPECIES_COMMON_NAME = {
    "arabidopsis_thaliana": None,  # AT-prefixed IDs are already unambiguous
    "oryza_sativa": "rice",
    "zea_mays": "maize",
    "solanum_lycopersicum": "tomato",
    "glycine_max": "soybean",
    "sorghum_bicolor": "sorghum",
    "triticum_aestivum": "wheat",
    "hordeum_vulgare": "barley",
    "brachypodium_distachyon": "Brachypodium",
}


async def lookup_locus(
    client: httpx.AsyncClient,
    locus: str,
    species: str = "arabidopsis_thaliana",
    size: int = DEFAULT_PAGE_SIZE,
) -> dict[str, Any]:
    """Search Europe PMC for literature mentioning a plant locus.

    ``size`` is clamped to [1, MAX_PAGE_SIZE] to bound the wire payload.
    Returns a dict shaped per ``LocusLiterature``: locus, species, hitCount
    (total available in Europe PMC), returned (len(hits)), hits[].
    """
    size = max(1, min(size, MAX_PAGE_SIZE))
    query = locus
    suffix = _SPECIES_COMMON_NAME.get(species)
    if suffix:
        query = f"{locus} AND {suffix}"
    params: dict[str, Any] = {
        "query": query,
        "format": "json",
        "resultType": "core",
        "pageSize": size,
    }
    raw = await _get(client, "/search", params=params)
    if not isinstance(raw, dict):
        raise PlantGenomicsError(
            f"Europe PMC /search returned non-dict payload: {type(raw).__name__}"
        )
    result_list = raw.get("resultList") or {}
    results = result_list.get("result") or []
    if not isinstance(results, list):
        raise PlantGenomicsError(
            f"Europe PMC /search resultList.result is not a list: {type(results).__name__}"
        )
    hits = [_normalize(r) for r in results if isinstance(r, dict)]
    return {
        "locus": locus,
        "species": species,
        "query": query,
        "hitCount": int(raw.get("hitCount", 0)),
        "returned": len(hits),
        "hits": hits,
    }
