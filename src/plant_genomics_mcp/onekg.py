"""1001 Genomes client — Arabidopsis locus → natural SNP variation + effects.

The 1001 Genomes project (tools.1001genomes.org) resequenced 1135 natural
*Arabidopsis thaliana* accessions. Its effects API returns, for a gene, every
SNP effect observed across the panel: the variant's molecular consequence,
impact class, amino-acid change, and which accession carries it.

Arabidopsis-only by construction, so any other organism raises
``OrganismNotSupported``. The gene id is transcript-scoped (``.1`` appended when
the caller passes a bare AGI).

The effects endpoint returns **headerless positional arrays**; the column order
is fixed by the API docs (verified 2026-07-20) and mapped in ``_EFFECT_COLUMNS``.

Two hops:
    /api/v2/gi2coords/TAIR10/{AGI}.1                      → genomic region span
    /api/v1.1/effects.json?type=snps;accs=all;gid={AGI}.1 → per-accession effects
"""

from __future__ import annotations

from typing import Any

import httpx

from plant_genomics_mcp import _http, cache, organisms, validators
from plant_genomics_mcp.errors import OrganismNotSupported, PlantGenomicsError

BASE_URL = "https://tools.1001genomes.org"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3

# Reference annotation for the gene→coords lookup.
ANNOTATION = "TAIR10"

# Cap projected effect rows (a gene × 1135 accessions can be thousands).
# ``variant_count`` reports the true total returned by the API even when capped.
MAX_EFFECTS = 300

# Fixed column order of an effects.json row (per API docs, verified 2026-07-20).
_EFFECT_COLUMNS = (
    "chr",
    "position",
    "accession_id",
    "effect",
    "impact",
    "functional_class",
    "codon_change",
    "amino_acid_change",
    "amino_acid_length",
    "gene",
    "transcript_biotype",
    "gene_coding",
    "transcript",
    "exon_rank",
)

_CACHE = cache.TTLCache()


async def _get(client: httpx.AsyncClient, url: str) -> dict[str, Any]:
    """GET a 1001 Genomes endpoint (cached by full URL), returning the parsed dict."""
    key = cache.make_key("GET", url, "", None)
    cached = _CACHE.get(key)
    if cached is None:
        resp = await _http.request_with_retry(
            client,
            "GET",
            url,
            service="1001 Genomes",
            headers={"Accept": "application/json"},
            timeout=DEFAULT_TIMEOUT,
            max_retries=MAX_RETRIES,
        )
        cached = resp.json()
        _CACHE.set(key, cached)
    if not isinstance(cached, dict):
        raise PlantGenomicsError(
            f"1001 Genomes returned unexpected payload: {type(cached).__name__}"
        )
    return cached


def _project_effect(row: Any) -> dict[str, Any] | None:
    """Map one headerless effects row to named fields (``None`` if not a list)."""
    if not isinstance(row, list):
        return None
    return {col: (row[i] if i < len(row) else None) for i, col in enumerate(_EFFECT_COLUMNS)}


async def lookup_locus(
    client: httpx.AsyncClient,
    locus: str,
    organism: str | int = organisms.DEFAULT_ORGANISM,
) -> dict[str, Any]:
    """Fetch 1001 Genomes natural-variation effects for an Arabidopsis locus.

    Raises ``OrganismNotSupported`` for any non-Arabidopsis organism. A bare AGI
    is transcript-scoped to ``{AGI}.1``. ``variant_count`` is the true row total;
    ``variants`` is capped at ``MAX_EFFECTS`` with ``truncated`` flagged.
    """
    canonical = organisms.resolve(organism).canonical
    if canonical != "arabidopsis_thaliana":
        raise OrganismNotSupported(
            backend="1001genomes", organism=canonical, supported=["arabidopsis_thaliana"]
        )
    validators.assert_valid_agi(locus, backend="1001genomes")
    tx = locus if "." in locus else f"{locus}.1"

    coords = await _get(client, f"{BASE_URL}/api/v2/gi2coords/{ANNOTATION}/{tx}")
    regions = coords.get("regions") or []
    region = regions[0].get("reg_str") if regions and isinstance(regions[0], dict) else None

    eff = await _get(client, f"{BASE_URL}/api/v1.1/effects.json?type=snps;accs=all;gid={tx}")
    data = eff.get("data")
    data = data if isinstance(data, list) else []
    total = len(data)
    variants = [p for row in data[:MAX_EFFECTS] if (p := _project_effect(row)) is not None]

    return {
        "locus": locus,
        "organism": canonical,
        "found": True,
        "transcript": tx,
        "region": region,
        "variant_count": total,
        "returned": len(variants),
        "truncated": total > MAX_EFFECTS,
        "variants": variants,
    }
