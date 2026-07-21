"""Ensembl variation client — natural variants over a locus, plus VEP.

Two views onto the Ensembl (Plants) variation data at ``rest.ensembl.org``:

* ``locus_variants`` — resolve a gene locus to its genomic span (reusing
  ``ensembl_plants.lookup_locus``), then list the germline variants overlapping
  it via ``/overlap/region/{species}/{region}?feature=variation``. Answers
  "what natural variation sits in this gene" (EVA/dbSNP-sourced SNPs + indels
  with allele, consequence class, and clinical significance).
* ``vep_annotate`` — predict the molecular consequence of an *arbitrary* variant
  via ``/vep/{species}/region/{region}/{allele}`` (SIFT / consequence terms per
  overlapping transcript). Variant-first, not locus-first: the caller supplies
  the region + allele, so this opens a capability the locus tools can't.

Both ride the same REST host and retry policy as ``ensembl_plants``; this module
keeps its own response cache. No auth. 12/12 organisms.
"""

from __future__ import annotations

from typing import Any

import httpx

from plant_genomics_mcp import _http, cache, ensembl_plants, organisms
from plant_genomics_mcp.errors import PlantGenomicsError

BASE_URL = "https://rest.ensembl.org"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3

# Cap the variant rows returned from a single gene span. A dense locus in a
# highly-resequenced species (rice / Arabidopsis) can overlap thousands of EVA
# records; ``variant_count`` always reports the true total even when the row
# list is capped and ``truncated`` is set.
MAX_VARIANTS = 500

_CACHE = cache.TTLCache()


async def _get(client: httpx.AsyncClient, path: str, params: dict[str, Any] | None = None) -> Any:
    """GET an Ensembl REST endpoint (own cache), returning parsed JSON."""
    key = cache.make_key("GET", BASE_URL, path, params)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    resp = await _http.request_with_retry(
        client,
        "GET",
        f"{BASE_URL}{path}",
        service=f"Ensembl variation {path}",
        params=params,
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
    )
    result = resp.json()
    _CACHE.set(key, result)
    return result


def _project_variant(v: dict[str, Any]) -> dict[str, Any]:
    """Project one ``/overlap`` variation feature to the surfaced field set."""
    return {
        "id": v.get("id"),
        "source": v.get("source"),
        "consequence_type": v.get("consequence_type"),
        "alleles": v.get("alleles"),
        "clinical_significance": v.get("clinical_significance"),
        "seq_region_name": v.get("seq_region_name"),
        "start": v.get("start"),
        "end": v.get("end"),
        "strand": v.get("strand"),
    }


async def locus_variants(
    client: httpx.AsyncClient,
    locus: str,
    organism: str | int = organisms.DEFAULT_ORGANISM,
) -> dict[str, Any]:
    """List natural variants overlapping a locus's genomic span.

    Resolves the locus to its coordinates via ``ensembl_plants.lookup_locus``
    (propagating ``NotFoundError`` for an unknown locus), then queries
    ``/overlap/region``. ``variant_count`` is the true overlap total; ``variants``
    is capped at ``MAX_VARIANTS`` with ``truncated`` flagged when the cap bites.
    """
    slug = organisms.ensembl_slug_for(organism)
    gene = await ensembl_plants.lookup_locus(client, locus, organism=organism)
    seq_region = gene.get("seq_region_name")
    start, end = gene.get("start"), gene.get("end")
    if seq_region is None or start is None or end is None:
        raise PlantGenomicsError(
            f"Ensembl lookup for {locus} returned no genomic coordinates "
            f"(seq_region_name/start/end); cannot query variants"
        )
    region_str = f"{seq_region}:{start}-{end}"
    raw = await _get(
        client, f"/overlap/region/{slug}/{region_str}", params={"feature": "variation"}
    )
    if not isinstance(raw, list):
        raise PlantGenomicsError(
            f"Ensembl /overlap/region/{region_str} returned non-list payload: {type(raw).__name__}"
        )
    total = len(raw)
    rows = [_project_variant(v) for v in raw[:MAX_VARIANTS] if isinstance(v, dict)]
    return {
        "locus": locus,
        "organism": slug,
        "region": region_str,
        "gene_start": start,
        "gene_end": end,
        "variant_count": total,
        "truncated": total > MAX_VARIANTS,
        "variants": rows,
    }


def _project_consequence(c: dict[str, Any]) -> dict[str, Any]:
    """Project one VEP transcript_consequence to the surfaced field set."""
    return {
        "gene_id": c.get("gene_id"),
        "transcript_id": c.get("transcript_id"),
        "biotype": c.get("biotype"),
        "impact": c.get("impact"),
        "consequence_terms": c.get("consequence_terms"),
        "variant_allele": c.get("variant_allele"),
        "sift_prediction": c.get("sift_prediction"),
        "sift_score": c.get("sift_score"),
        "polyphen_prediction": c.get("polyphen_prediction"),
        "polyphen_score": c.get("polyphen_score"),
        "distance": c.get("distance"),
    }


async def vep_annotate(
    client: httpx.AsyncClient,
    region: str,
    allele: str,
    organism: str | int = organisms.DEFAULT_ORGANISM,
) -> dict[str, Any]:
    """Predict a variant's consequences via Ensembl VEP (region + allele form).

    ``region`` is Ensembl's ``chr:start-end:strand`` (e.g. ``"1:10000-10000:1"``)
    and ``allele`` the alternate base(s) (e.g. ``"C"``). Returns the most-severe
    consequence plus one projected row per overlapping transcript (consequence
    terms, IMPACT, and SIFT/PolyPhen when the variant is missense in a coding
    transcript). ``found=False`` when Ensembl reports no overlapping feature.
    """
    if not region or not allele:
        raise ValueError("vep_annotate requires non-empty region and allele")
    slug = organisms.ensembl_slug_for(organism)
    raw = await _get(client, f"/vep/{slug}/region/{region}/{allele}")
    if not isinstance(raw, list):
        raise PlantGenomicsError(
            f"Ensembl /vep/{slug}/region returned non-list payload: {type(raw).__name__}"
        )
    if not raw or not isinstance(raw[0], dict):
        return {
            "organism": slug,
            "region": region,
            "allele": allele,
            "found": False,
            "most_severe_consequence": None,
            "assembly_name": None,
            "transcript_consequences": [],
        }
    entry = raw[0]
    cons = [
        _project_consequence(c)
        for c in entry.get("transcript_consequences") or []
        if isinstance(c, dict)
    ]
    return {
        "organism": slug,
        "region": region,
        "allele": allele,
        "found": True,
        "input": entry.get("input"),
        "most_severe_consequence": entry.get("most_severe_consequence"),
        "assembly_name": entry.get("assembly_name"),
        "seq_region_name": entry.get("seq_region_name"),
        "start": entry.get("start"),
        "end": entry.get("end"),
        "allele_string": entry.get("allele_string"),
        "transcript_consequences": cons,
    }
