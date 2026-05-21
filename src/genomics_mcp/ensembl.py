"""Ensembl REST client — async httpx wrapper around rest.ensembl.org.

Endpoints documented at https://rest.ensembl.org. No auth required. Server
asks for a 1 req/sec ceiling per IP for sustained use; bursts above are
tolerated. We retry on 429 and 5xx with exponential backoff.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

BASE_URL = "https://rest.ensembl.org"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3


class EnsemblError(RuntimeError):
    pass


async def _get(
    client: httpx.AsyncClient,
    path: str,
    params: dict[str, Any] | None = None,
) -> Any:
    """GET an Ensembl REST endpoint with retry on 429/5xx."""
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
        raise EnsemblError(f"Ensembl {path} → HTTP {resp.status_code}: {resp.text[:200]}")
    raise EnsemblError(f"Ensembl {path} exhausted {MAX_RETRIES} retries")


async def lookup_id(
    client: httpx.AsyncClient, ensembl_id: str, expand: bool = False
) -> dict[str, Any]:
    """Fetch metadata for an Ensembl stable ID (gene, transcript, protein)."""
    params = {"expand": 1} if expand else None
    return await _get(client, f"/lookup/id/{ensembl_id}", params=params)


async def lookup_symbol(
    client: httpx.AsyncClient, species: str, symbol: str, expand: bool = False
) -> dict[str, Any]:
    """Resolve a gene symbol to its Ensembl record for a given species."""
    params = {"expand": 1} if expand else None
    return await _get(client, f"/lookup/symbol/{species}/{symbol}", params=params)


async def sequence_by_id(
    client: httpx.AsyncClient,
    ensembl_id: str,
    seq_type: str = "genomic",
) -> dict[str, Any]:
    """Retrieve a sequence by Ensembl ID. seq_type ∈ {genomic, cds, cdna, protein}."""
    if seq_type not in {"genomic", "cds", "cdna", "protein"}:
        raise EnsemblError(f"invalid seq_type: {seq_type}")
    return await _get(client, f"/sequence/id/{ensembl_id}", params={"type": seq_type})


async def xrefs_by_id(client: httpx.AsyncClient, ensembl_id: str) -> list[dict[str, Any]]:
    """List external database cross-references for an Ensembl ID."""
    return await _get(client, f"/xrefs/id/{ensembl_id}")


async def homology_by_id(
    client: httpx.AsyncClient,
    ensembl_id: str,
    target_species: str | None = None,
    homology_type: str | None = None,
) -> dict[str, Any]:
    """Get orthologs/paralogs for an Ensembl gene ID.

    homology_type ∈ {orthologues, paralogues, projections} or None for all.
    """
    params: dict[str, Any] = {}
    if target_species:
        params["target_species"] = target_species
    if homology_type:
        params["type"] = homology_type
    return await _get(client, f"/homology/id/{ensembl_id}", params=params or None)
