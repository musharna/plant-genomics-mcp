"""Ensembl Plants REST client — async httpx wrapper around rest.ensembl.org.

Ensembl Plants uses the same REST host as Ensembl (``rest.ensembl.org``); plant
species (``arabidopsis_thaliana``, ``oryza_sativa``, ``zea_mays``, ...) live
alongside vertebrates in the same lookup namespace. We constrain calls to
plant species via the ``species=`` query parameter.

Endpoints documented at https://rest.ensembl.org. No auth required. Server
asks for a ~15 req/sec ceiling per IP for sustained use; bursts above are
tolerated. We retry on 429 and 5xx with exponential backoff.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from plant_genomics_mcp import cache, progress
from plant_genomics_mcp.errors import (
    NotFoundError,
    PlantGenomicsError,
    RateLimitError,
    UpstreamUnavailableError,
)

BASE_URL = "https://rest.ensembl.org"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3

# Per-module response cache. See plant_genomics_mcp.cache for env knobs.
_CACHE = cache.TTLCache()

# Re-export so existing imports (`from plant_genomics_mcp.ensembl_plants import
# PlantGenomicsError`) keep working. New code should import from
# ``plant_genomics_mcp.errors`` directly.
__all__ = ["PlantGenomicsError", "RateLimitError", "NotFoundError", "UpstreamUnavailableError"]


async def _get(
    client: httpx.AsyncClient,
    path: str,
    params: dict[str, Any] | None = None,
) -> Any:
    """GET an Ensembl REST endpoint with retry on 429/5xx.

    Mirrors the genomics-mcp sibling's retry shape: bounded retries with
    exponential backoff, honors ``Retry-After`` if present. Each retry
    sleep emits an MCP progress notification so clients that opted in see
    "still working" updates instead of a silent stall.
    """
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
                f"Ensembl Plants {path}: HTTP {resp.status_code}, retrying in "
                f"{retry_after:.1f}s (attempt {attempt + 2}/{MAX_RETRIES})"
            )
            await asyncio.sleep(retry_after)
            delay *= 2
            continue
        # Non-retryable / final attempt — pick the most informative subclass.
        if resp.status_code == 404:
            raise NotFoundError(f"Ensembl Plants {path} → HTTP 404: {resp.text[:200]}")
        if resp.status_code == 429:
            raise RateLimitError(
                f"Ensembl Plants {path} rate-limited (HTTP 429): {resp.text[:200]}"
            )
        if resp.status_code in (500, 502, 503, 504):
            raise UpstreamUnavailableError(
                f"Ensembl Plants {path} → HTTP {resp.status_code}: {resp.text[:200]}"
            )
        raise PlantGenomicsError(
            f"Ensembl Plants {path} → HTTP {resp.status_code}: {resp.text[:200]}"
        )
    # Loop exhausted retries on a retryable status.
    if last_status == 429:
        raise RateLimitError(f"Ensembl Plants {path} exhausted {MAX_RETRIES} retries (429)")
    raise UpstreamUnavailableError(
        f"Ensembl Plants {path} exhausted {MAX_RETRIES} retries (last HTTP {last_status})"
    )


async def lookup_locus(
    client: httpx.AsyncClient,
    locus: str,
    species: str = "arabidopsis_thaliana",
) -> dict[str, Any]:
    """Fetch metadata for a plant locus identifier.

    ``locus`` is the species-specific gene identifier — e.g. TAIR locus
    ``AT1G01010`` for Arabidopsis, ``Os01g0100100`` for rice. Ensembl
    looks these up via ``/lookup/id/{locus}`` with the ``species=`` query
    parameter constraining the namespace.
    """
    params: dict[str, Any] = {"species": species, "expand": 0}
    return await _get(client, f"/lookup/id/{locus}", params=params)


async def lookup_xrefs(
    client: httpx.AsyncClient,
    locus: str,
    species: str = "arabidopsis_thaliana",
) -> dict[str, Any]:
    """Fetch cross-references (UniProt, NCBI Gene, TAIR, etc.) for a locus.

    Ensembl ``/xrefs/id/{locus}`` returns a list of records mapping the
    locus to other databases. We wrap the raw array in an object so the
    MCP outputSchema can validate it (top-level must be type=object) and
    add a ``by_db`` rollup keyed on Ensembl's ``dbname`` for quick lookup
    without walking the full list.
    """
    params: dict[str, Any] = {"species": species}
    raw = await _get(client, f"/xrefs/id/{locus}", params=params)
    if not isinstance(raw, list):
        raise PlantGenomicsError(
            f"Ensembl /xrefs/id/{locus} returned non-list payload: {type(raw).__name__}"
        )
    by_db: dict[str, list[str]] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        dbname = entry.get("dbname")
        primary_id = entry.get("primary_id")
        if dbname and primary_id:
            by_db.setdefault(dbname, []).append(primary_id)
    return {
        "locus": locus,
        "species": species,
        "count": len(raw),
        "xrefs": raw,
        "by_db": by_db,
    }
