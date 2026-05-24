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

from typing import Any

import httpx

from plant_genomics_mcp import _http, cache, organisms, validators
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

    Thin cache wrapper over the shared :func:`_http.request_with_retry`
    helper. The retry/cap/error-classification policy is shared with the
    other 8 backends.
    """
    key = cache.make_key("GET", BASE_URL, path, params)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    resp = await _http.request_with_retry(
        client,
        "GET",
        f"{BASE_URL}{path}",
        service=f"Ensembl Plants {path}",
        params=params,
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
    )
    result = resp.json()
    _CACHE.set(key, result)
    return result


async def lookup_locus(
    client: httpx.AsyncClient,
    locus: str,
    organism: str | int = organisms.DEFAULT_ORGANISM,
) -> dict[str, Any]:
    """Fetch metadata for a plant locus identifier.

    ``locus`` is the species-specific gene identifier — e.g. TAIR locus
    ``AT1G01010`` for Arabidopsis, ``Os01g0100100`` for rice. Ensembl
    looks these up via ``/lookup/id/{locus}`` with the ``species=`` query
    parameter constraining the namespace. ``organism=`` accepts any alias
    or NCBI taxid the resolver understands; we translate to the Ensembl
    slug before hitting the wire.
    """
    validators.assert_valid_locus(locus, backend="Ensembl Plants")
    slug = organisms.ensembl_slug_for(organism)
    params: dict[str, Any] = {"species": slug, "expand": 0}
    raw = await _get(client, f"/lookup/id/{locus}", params=params)
    if isinstance(raw, dict) and "species" in raw:
        raw["organism"] = raw.pop("species")
    return raw


async def lookup_xrefs(
    client: httpx.AsyncClient,
    locus: str,
    organism: str | int = organisms.DEFAULT_ORGANISM,
) -> dict[str, Any]:
    """Fetch cross-references (UniProt, NCBI Gene, TAIR, etc.) for a locus.

    Ensembl ``/xrefs/id/{locus}`` returns a list of records mapping the
    locus to other databases. We wrap the raw array in an object so the
    MCP outputSchema can validate it (top-level must be type=object) and
    add a ``by_db`` rollup keyed on Ensembl's ``dbname`` for quick lookup
    without walking the full list. ``organism=`` accepts any alias or
    NCBI taxid the resolver understands; we translate to the Ensembl
    slug before hitting the wire.
    """
    validators.assert_valid_locus(locus, backend="Ensembl Plants")
    slug = organisms.ensembl_slug_for(organism)
    params: dict[str, Any] = {"species": slug}
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
        "organism": slug,
        "count": len(raw),
        "xrefs": raw,
        "by_db": by_db,
    }
