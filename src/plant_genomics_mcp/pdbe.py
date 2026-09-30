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

import re
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

# An accession with no structure is 404 naming no data; a missing route is 404
# ``{"detail":"Not Found"}`` (live, 2026-09-29), an outage, not "no structure".
NO_STRUCTURE_404_RE = re.compile(r"Requested endpoint does not contain any data")


def _empty(accession: str) -> dict[str, Any]:
    """Result for an accession with no deposited experimental structure."""
    return {
        "accession": accession,
        "found": False,
        "structure_count": 0,
        "entry_count": 0,
        **_http.counted(0, []),
        "structures": [],
        "upstream_version": None,
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
    if cached is cache.NEGATIVE:  # cached 404 — checked before the miss test
        return _empty(accession)
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
            not_found_404_pattern=NO_STRUCTURE_404_RE,
        )
        if resp is None:  # 404 — no deposited structure
            # The overwhelmingly common answer for a plant protein, so caching
            # it is what keeps a repeated lookup off the wire.
            _CACHE.set(key, cache.NEGATIVE)
            return _empty(accession)
        body = _http.json_body(resp, f"PDBe {path}")
        # Checked before it is stored: a body stored first was served back
        # as the same failure for the whole TTL without asking again (#96).
        if not isinstance(body, dict):
            raise PlantGenomicsError(
                f"PDBe {path} returned unexpected payload: {type(body).__name__}"
            )
        # A row that is not an object was skipped, so a list of only such
        # rows answered "no structures" and any other undercounted (#96);
        # every live row is one.
        rows = body.get(accession)
        if isinstance(rows, list):
            try:
                _http.object_rows(rows)
            except _http.UnreadableBody as e:
                raise PlantGenomicsError(
                    f"PDBe {path} returned unexpected payload: {e.args[0]}"
                ) from None
        _CACHE.set(key, body)
        cached = body
    entries = cached.get(accession)
    if not isinstance(entries, list):
        return _empty(accession)
    # Every row is an object: checked before the store.
    valid: list[dict[str, Any]] = entries
    if not valid:
        return _empty(accession)
    total = len(valid)
    structures = [_project(s) for s in valid[:MAX_STRUCTURES]]
    return {
        "accession": accession,
        "found": True,
        # PDBe best_structures rows are per CHAIN (4chk chains A and B are two
        # rows); entry_count is the distinct PDB entries among them (#123).
        "structure_count": total,
        "entry_count": len({s.get("pdb_id") for s in valid}),
        **_http.counted(total, structures),
        "structures": structures,
        # Issue #121: uniform key; null because this backend states no release on
        # the answering response (headers probed live 2026-09-22).
        "upstream_version": None,
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
    locus = validators.assert_valid_locus(locus, backend="PDBe")
    up = await uniprot.lookup_locus(client, locus, organism=organism)
    accession = up["primaryAccession"]
    result = await lookup_by_uniprot(client, accession)
    return {"locus": locus, **result}
