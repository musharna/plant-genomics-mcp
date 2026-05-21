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

BASE_URL = "https://rest.ensembl.org"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3


class PlantGenomicsError(RuntimeError):
    """Raised when an Ensembl Plants call fails (HTTP error, retry exhaustion)."""


async def _get(
    client: httpx.AsyncClient,
    path: str,
    params: dict[str, Any] | None = None,
) -> Any:
    """GET an Ensembl REST endpoint with retry on 429/5xx.

    Mirrors the genomics-mcp sibling's retry shape: bounded retries with
    exponential backoff, honors ``Retry-After`` if present.
    """
    headers = {"Accept": "application/json"}
    delay = 1.0
    for attempt in range(MAX_RETRIES):
        resp = await client.get(
            f"{BASE_URL}{path}",
            params=params,
            headers=headers,
            timeout=DEFAULT_TIMEOUT,
        )
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES - 1:
            retry_after = float(resp.headers.get("Retry-After", delay))
            await asyncio.sleep(retry_after)
            delay *= 2
            continue
        raise PlantGenomicsError(
            f"Ensembl Plants {path} → HTTP {resp.status_code}: {resp.text[:200]}"
        )
    raise PlantGenomicsError(f"Ensembl Plants {path} exhausted {MAX_RETRIES} retries")


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
