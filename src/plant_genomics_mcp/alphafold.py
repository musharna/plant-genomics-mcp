"""AlphaFold DB structure client — locus → UniProt → predicted 3D model.

AlphaFold DB (alphafold.ebi.ac.uk) serves predicted protein structures keyed by
UniProt accession. Its prediction API is free and needs no key. Plant loci
(AT1G01010, Os01g0100100, …) aren't indexed directly, so we first resolve the
locus to a UniProt accession via ``plant_genomics_mcp.uniprot.lookup_locus``
(the same seam quickgo uses), then fetch the model metadata.

A valid protein with no deposited model returns HTTP 404 — surfaced as
``found=False`` (a normal outcome), not an error. A locus that resolves to no
UniProt entry propagates the typed ``NotFoundError`` from the resolve step.

Endpoint: https://alphafold.ebi.ac.uk/api/prediction/{accession} (JSON array).
"""

from __future__ import annotations

from typing import Any

import httpx

from plant_genomics_mcp import _http, cache, organisms, uniprot, validators
from plant_genomics_mcp.errors import PlantGenomicsError

BASE_URL = "https://alphafold.ebi.ac.uk"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3

# Per-module response cache. See plant_genomics_mcp.cache for env knobs.
_CACHE = cache.TTLCache()


def _empty(accession: str) -> dict[str, Any]:
    """Result for an accession with no predicted model (404 / empty array)."""
    return {
        "accession": accession,
        "found": False,
        "model_entity_id": None,
        "mean_plddt": None,
        "plddt_bands": None,
        "latest_version": None,
        "model_created": None,
        "residue_range": None,
        "organism": None,
        "gene": None,
        "description": None,
        "cif_url": None,
        "pdb_url": None,
        "pae_image_url": None,
    }


def _project(accession: str, entry: dict[str, Any]) -> dict[str, Any]:
    """Project one AlphaFold prediction entry to the surfaced field set."""
    start = entry.get("sequenceStart")
    end = entry.get("sequenceEnd")
    return {
        "accession": accession,
        "found": True,
        "model_entity_id": entry.get("modelEntityId"),
        "mean_plddt": entry.get("globalMetricValue"),
        "plddt_bands": {
            "very_low": entry.get("fractionPlddtVeryLow"),
            "low": entry.get("fractionPlddtLow"),
            "confident": entry.get("fractionPlddtConfident"),
            "very_high": entry.get("fractionPlddtVeryHigh"),
        },
        "latest_version": entry.get("latestVersion"),
        "model_created": entry.get("modelCreatedDate"),
        "residue_range": {"start": start, "end": end} if start is not None else None,
        "organism": entry.get("organismScientificName"),
        "gene": entry.get("gene"),
        "description": entry.get("uniprotDescription"),
        "cif_url": entry.get("cifUrl"),
        "pdb_url": entry.get("pdbUrl"),
        "pae_image_url": entry.get("paeImageUrl"),
    }


async def lookup_by_uniprot(client: httpx.AsyncClient, accession: str) -> dict[str, Any]:
    """Fetch the AlphaFold predicted-structure summary for a UniProt accession.

    Returns ``found=False`` (with null fields) when no model exists — a 404 or
    an empty response array. Reusable directly by synthesis tools that have
    already resolved an accession (e.g. ``gene_report``).
    """
    path = f"/api/prediction/{accession}"
    key = cache.make_key("GET", BASE_URL, path, None)
    cached = _CACHE.get(key)
    if cached is None:
        resp = await _http.request_with_retry(
            client,
            "GET",
            f"{BASE_URL}{path}",
            service=f"AlphaFold {path}",
            headers={"Accept": "application/json"},
            timeout=DEFAULT_TIMEOUT,
            max_retries=MAX_RETRIES,
            not_found_returns=None,
        )
        if resp is None:  # 404 sentinel — no deposited model
            return _empty(accession)
        cached = resp.json()
        _CACHE.set(key, cached)
    if not isinstance(cached, list):
        raise PlantGenomicsError(
            f"AlphaFold {path} returned unexpected payload: {type(cached).__name__}"
        )
    if not cached:
        return _empty(accession)
    return _project(accession, cached[0])


async def lookup_locus(
    client: httpx.AsyncClient,
    locus: str,
    organism: str = organisms.DEFAULT_ORGANISM,
) -> dict[str, Any]:
    """Resolve a locus to UniProt, then fetch its AlphaFold model summary.

    Propagates ``NotFoundError`` when the locus has no UniProt entry (it can't
    be keyed into AlphaFold), mirroring the locus→UniProt→QuickGO path.
    """
    validators.assert_valid_locus(locus, backend="AlphaFold")
    up = await uniprot.lookup_locus(client, locus, organism=organism)
    accession = up["primaryAccession"]
    result = await lookup_by_uniprot(client, accession)
    return {"locus": locus, **result}
