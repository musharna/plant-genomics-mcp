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

# Issue #135: the pLDDT span behind each band AlphaFold DB reports a fraction
# for, [lower, upper] on the 0-100 scale. EMBL-EBI's AlphaFold course:
# very high "pLDDT > 90", confident "90 > pLDDT > 70", low "70 > pLDDT > 50",
# very low "pLDDT < 50" (ebi.ac.uk/training/online/courses/alphafold, section
# "pLDDT: understanding local confidence", read 2026-09-22). The source does
# not say which band a value of exactly 50/70/90 falls in, so neither do we.
PLDDT_BAND_RANGES: dict[str, list[int]] = {
    "very_low": [0, 50],
    "low": [50, 70],
    "confident": [70, 90],
    "very_high": [90, 100],
}


def _version_str(value: Any) -> str | None:
    """AlphaFold sends latestVersion as an int (6); the shared key is a string."""
    if value is None or value == "":
        return None
    return str(value)


def _empty(accession: str) -> dict[str, Any]:
    """Result for an accession with no predicted model (404 / empty array)."""
    return {
        "accession": accession,
        "found": False,
        "model_entity_id": None,
        "mean_plddt": None,
        "plddt_bands": None,
        "plddt_band_ranges": PLDDT_BAND_RANGES,
        "latest_version": None,
        "model_created": None,
        "residue_range": None,
        "organism": None,
        "gene": None,
        "description": None,
        "cif_url": None,
        "pdb_url": None,
        "pae_image_url": None,
        "upstream_version": None,
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
        "plddt_band_ranges": PLDDT_BAND_RANGES,
        "latest_version": entry.get("latestVersion"),
        "model_created": entry.get("modelCreatedDate"),
        "residue_range": {"start": start, "end": end} if start is not None else None,
        "organism": entry.get("organismScientificName"),
        "gene": entry.get("gene"),
        "description": entry.get("uniprotDescription"),
        "cif_url": entry.get("cifUrl"),
        "pdb_url": entry.get("pdbUrl"),
        "pae_image_url": entry.get("paeImageUrl"),
        # Issue #121: the entry's own latestVersion is the AlphaFold DB release
        # of THIS model, stated by the answering payload; stringified so the
        # key reads the same across tools.
        "upstream_version": _version_str(entry.get("latestVersion")),
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
    if cached is cache.NEGATIVE:  # cached 404 — checked before the miss test
        return _empty(accession)
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
            _CACHE.set(key, cache.NEGATIVE)
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
    locus = validators.assert_valid_locus(locus, backend="AlphaFold")
    up = await uniprot.lookup_locus(client, locus, organism=organism)
    accession = up["primaryAccession"]
    result = await lookup_by_uniprot(client, accession)
    return {"locus": locus, **result}
