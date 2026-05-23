"""Gramene compara homology backend — async httpx wrapper around data.gramene.org.

Gramene is the EBI/Cornell-hosted plant comparative-genomics resource. We
query the ``/v69/genes?idList={locus}&fl=homology`` endpoint to retrieve
orthologs and paralogs for a plant locus. Free, no API key.

Default release is v69 (released Sept 2025). v69 is a frozen release; the
24h cache TTL reflects that — homology assignments don't change mid-release.

Endpoint docs (swagger): https://github.com/warelab/gramene-swagger.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from plant_genomics_mcp import cache, progress, validators
from plant_genomics_mcp.errors import (
    NotFoundError,
    PlantGenomicsError,
    RateLimitError,
    UpstreamUnavailableError,
)

BASE_URL = "https://data.gramene.org"
GRAMENE_RELEASE = "v69"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3
CACHE_TTL_SECONDS = 86400.0  # 24h — Gramene v69 is a frozen release.

_CACHE = cache.TTLCache(default_ttl=CACHE_TTL_SECONDS)


async def _get(
    client: httpx.AsyncClient,
    path: str,
    params: dict[str, Any] | None = None,
) -> Any:
    """GET a Gramene endpoint with retry on 429/5xx and per-call caching."""
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
            retry_after = min(float(resp.headers.get("Retry-After", delay)), 60.0)
            await progress.notify(
                f"Gramene {path}: HTTP {resp.status_code}, retrying in "
                f"{retry_after:.1f}s (attempt {attempt + 2}/{MAX_RETRIES})"
            )
            await asyncio.sleep(retry_after)
            delay *= 2
            continue
        if resp.status_code == 429:
            raise RateLimitError(f"Gramene {path} rate-limited (HTTP 429): {resp.text[:200]}")
        if resp.status_code in (500, 502, 503, 504):
            raise UpstreamUnavailableError(
                f"Gramene {path} → HTTP {resp.status_code}: {resp.text[:200]}"
            )
        raise PlantGenomicsError(f"Gramene {path} → HTTP {resp.status_code}: {resp.text[:200]}")
    if last_status == 429:
        raise RateLimitError(f"Gramene {path} exhausted {MAX_RETRIES} retries (429)")
    raise UpstreamUnavailableError(
        f"Gramene {path} exhausted {MAX_RETRIES} retries (last HTTP {last_status})"
    )


# Gramene homology categories as KEYS of homologous_genes.
# Probed against AT1G01010 on 2026-05-21: live response contained
# ortholog_one2many, ortholog_many2many, within_species_paralog. Other
# categories (ortholog_one2one, between_species_paralog) appear for other
# loci; we keep them in the filter map but tolerate their absence.
_HOMOLOGY_FILTERS: dict[str, tuple[str, ...]] = {
    "ortholog": ("ortholog_one2one", "ortholog_one2many", "ortholog_many2many"),
    "paralog": ("within_species_paralog", "between_species_paralog"),
    "all": (),
}


def _normalize(
    category: str,
    target_locus: str,
    gene_tree_id: str | None,
) -> dict[str, Any]:
    return {
        "target_locus": target_locus,
        "type": category,
        "gene_tree_id": gene_tree_id,
    }


async def lookup_homologs(
    client: httpx.AsyncClient,
    locus: str,
    homology_type: str = "ortholog",
) -> dict[str, Any]:
    """Fetch Gramene compara homologs for a plant locus.

    ``homology_type`` is one of ``"ortholog"``, ``"paralog"``, ``"all"``.
    Unknown values default to ``"all"`` — we prefer permissive filtering
    over raising on a typo, since the upstream homology_type strings
    occasionally drift.

    Live response shape (Gramene v69, fl=homology):
        homology = {
            gene_tree: {id, root_taxon_id, root_taxon_name, duplications},
            homologous_genes: {<category>: [locus_str, ...], ...},
        }
    Categories are keys; values are flat lists of locus-ID strings. The
    fl=homology projection does NOT carry per-row taxon, identity, protein
    ID, dn/ds, or goc_score — so we only surface what's there.
    """
    validators.assert_valid_locus(locus, backend="Gramene")
    raw = await _get(
        client,
        f"/{GRAMENE_RELEASE}/genes",
        params={"idList": locus, "fl": "homology"},
    )
    if not isinstance(raw, list) or not raw:
        raise NotFoundError(f"Gramene: no record for locus {locus} in {GRAMENE_RELEASE}")
    record = raw[0]
    if not isinstance(record, dict):
        raise PlantGenomicsError(
            f"Gramene: unexpected payload shape for {locus}: {type(record).__name__}"
        )
    homology = record.get("homology") or {}
    if not isinstance(homology, dict):
        raise PlantGenomicsError(
            f"Gramene: homology field is not a dict for {locus}: {type(homology).__name__}"
        )
    gene_tree = homology.get("gene_tree") or {}
    gene_tree_id = gene_tree.get("id") if isinstance(gene_tree, dict) else None
    homologous_genes = homology.get("homologous_genes") or {}
    if not isinstance(homologous_genes, dict):
        raise PlantGenomicsError(
            f"Gramene: homologous_genes is not a dict for {locus}: {type(homologous_genes).__name__}"
        )
    allowed = _HOMOLOGY_FILTERS.get(homology_type, ())
    normalized: list[dict[str, Any]] = []
    for category, loci in homologous_genes.items():
        if allowed and category not in allowed:
            continue
        if not isinstance(loci, list):
            continue
        for target_locus in loci:
            if isinstance(target_locus, str):
                normalized.append(_normalize(category, target_locus, gene_tree_id))
    return {
        "locus": locus,
        "release": GRAMENE_RELEASE,
        "total": len(normalized),
        "homologs": normalized,
    }
