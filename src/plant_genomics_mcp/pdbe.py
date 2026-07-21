"""PDBe experimental-structure client — locus → UniProt → deposited PDB entries.

PDBe (www.ebi.ac.uk/pdbe) serves experimentally-determined protein structures
(X-ray, cryo-EM, NMR) via its ``best_structures`` mapping, keyed by UniProt
accession and ranked best-first (resolution / coverage). Its API is free and
needs no key. Plant loci aren't indexed directly, so we resolve the locus to a
UniProt accession via ``plant_genomics_mcp.uniprot.lookup_locus`` (the same seam
quickgo / alphafold / interpro use), then fetch the deposited structures.

Complements ``alphafold_structure`` (a *predicted* model): this is the
experimentally-*solved* view. Most plant proteins have NO deposited structure —
PDBe answers HTTP 404, surfaced here as ``found=False`` (a normal outcome, not
an error). A locus that resolves to no UniProt entry propagates ``NotFoundError``.

Endpoint: https://www.ebi.ac.uk/pdbe/api/mappings/best_structures/{accession}
(JSON ``{accession: [structure, ...]}``).
"""

from __future__ import annotations

from typing import Any

import httpx

from plant_genomics_mcp import _http, cache, organisms, uniprot, validators
from plant_genomics_mcp.errors import PlantGenomicsError

BASE_URL = "https://www.ebi.ac.uk"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3

# Cap structures returned — a well-studied protein (e.g. RuBisCO) can have
# dozens. PDBe already ranks the list best-first; ``structure_count`` reports the
# true total even when the returned list is capped.
MAX_STRUCTURES = 25

_CACHE = cache.TTLCache()


def _empty(accession: str) -> dict[str, Any]:
    """Result for an accession with no deposited experimental structure."""
    return {
        "accession": accession,
        "found": False,
        "structure_count": 0,
        "truncated": False,
        "structures": [],
    }


def _project(entry: dict[str, Any]) -> dict[str, Any]:
    """Project one PDBe best_structures entry to the surfaced field set."""
    start, end = entry.get("unp_start"), entry.get("unp_end")
    return {
        "pdb_id": entry.get("pdb_id"),
        "chain_id": entry.get("chain_id"),
        "experimental_method": entry.get("experimental_method"),
        "resolution": entry.get("resolution"),
        "coverage": entry.get("coverage"),
        "residue_range": {"start": start, "end": end} if start is not None else None,
    }


async def lookup_by_uniprot(client: httpx.AsyncClient, accession: str) -> dict[str, Any]:
    """Fetch PDBe experimentally-solved structures for a UniProt accession.

    Returns ``found=False`` (empty list) when the accession has no deposited
    structure — a 404 (the common plant case) or an empty mapping. Reusable by
    synthesis tools that have already resolved an accession. ``structure_count``
    is the true total even when the ``structures`` list is capped at
    ``MAX_STRUCTURES``.
    """
    path = f"/pdbe/api/mappings/best_structures/{accession}"
    key = cache.make_key("GET", BASE_URL, path, None)
    cached = _CACHE.get(key)
    if cached is None:
        resp = await _http.request_with_retry(
            client,
            "GET",
            f"{BASE_URL}{path}",
            service=f"PDBe {path}",
            headers={"Accept": "application/json"},
            timeout=DEFAULT_TIMEOUT,
            max_retries=MAX_RETRIES,
            not_found_returns=None,
        )
        if resp is None:  # 404 — no deposited structure
            return _empty(accession)
        cached = resp.json()
        _CACHE.set(key, cached)
    if not isinstance(cached, dict):
        raise PlantGenomicsError(
            f"PDBe {path} returned unexpected payload: {type(cached).__name__}"
        )
    entries = cached.get(accession)
    if not isinstance(entries, list) or not entries:
        return _empty(accession)
    total = len(entries)
    structures = [_project(s) for s in entries[:MAX_STRUCTURES] if isinstance(s, dict)]
    return {
        "accession": accession,
        "found": True,
        "structure_count": total,
        "truncated": total > MAX_STRUCTURES,
        "structures": structures,
    }


async def lookup_locus(
    client: httpx.AsyncClient,
    locus: str,
    organism: str = organisms.DEFAULT_ORGANISM,
) -> dict[str, Any]:
    """Resolve a locus to UniProt, then fetch its PDBe experimental structures.

    Propagates ``NotFoundError`` when the locus has no UniProt entry (it can't be
    keyed into PDBe), mirroring the locus→UniProt→AlphaFold path.
    """
    validators.assert_valid_locus(locus, backend="PDBe")
    up = await uniprot.lookup_locus(client, locus, organism=organism)
    accession = up["primaryAccession"]
    result = await lookup_by_uniprot(client, accession)
    return {"locus": locus, **result}
