"""Gramene compara homology backend — async httpx wrapper around data.gramene.org.

Gramene is the EBI/Cornell-hosted plant comparative-genomics resource. We
query the ``/v69/genes?idList={locus}&fl=homology`` endpoint to retrieve
orthologs and paralogs for a plant locus. Free, no API key.

Default release is v69 (released Sept 2025). v69 is a frozen release; the
24h cache TTL reflects that — homology assignments don't change mid-release.

Endpoint docs (swagger): https://github.com/warelab/gramene-swagger.
"""

from __future__ import annotations

from typing import Any

import httpx

from plant_genomics_mcp import _http, cache, validators
from plant_genomics_mcp.errors import (
    NotFoundError,
    PlantGenomicsError,
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
    resp = await _http.request_with_retry(
        client,
        "GET",
        f"{BASE_URL}{path}",
        service=f"Gramene {path}",
        params=params,
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
    )
    result = resp.json()
    _CACHE.set(key, result)
    return result


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


_UNIPROT_DB_PRIORITY = ("Uniprot/SWISSPROT", "Uniprot/SPTREMBL")


def _pick_uniprot_acc(xrefs: Any) -> str | None:
    """Return preferred UniProt accession from a Gramene xrefs payload.

    SWISSPROT wins over SPTREMBL — mirrors UniProt's own reviewed-first
    heuristic in ``uniprot.lookup_locus``. Tolerant of missing/malformed
    xref entries (Gramene occasionally returns ``ids: []`` or omits
    fields entirely).
    """
    if not isinstance(xrefs, list):
        return None
    by_db: dict[str, list[str]] = {}
    for entry in xrefs:
        if not isinstance(entry, dict):
            continue
        db = entry.get("db")
        ids = entry.get("ids")
        if not isinstance(db, str) or not isinstance(ids, list):
            continue
        flat = [x for x in ids if isinstance(x, str) and x]
        if flat:
            by_db[db] = flat
    for db in _UNIPROT_DB_PRIORITY:
        if db in by_db:
            return by_db[db][0]
    return None


async def fetch_homolog_enrichment_batch(
    client: httpx.AsyncClient,
    loci: list[str],
    *,
    chunk_size: int = 100,
) -> dict[str, dict[str, str | None]]:
    """Enrich a batch of Gramene loci with UniProt acc + species slug.

    For each locus in ``loci``, returns a dict
    ``{"uniprot_acc": <SWISSPROT or SPTREMBL or None>, "system_name": <organism slug or None>}``.
    Loci absent from Gramene's response (404 or filtered upstream) also map
    to ``{"uniprot_acc": None, "system_name": None}`` — the output dict is
    total over the input list so the caller's join doesn't need to handle
    KeyError.

    Batched via comma-separated ``idList``, chunked to ``chunk_size`` loci
    per call for URL-length safety on long homology lists. Uses the same
    24h cache as ``lookup_homologs`` (each chunk URL keyed independently).

    Used by ``synthesis.consensus_homologs`` to project Gramene's locus-
    space onto UniProt-accession-space so it can dedup with BLAST hits —
    BLAST returns bare UniProt accessions natively, so accession is the
    only stable cross-source join key for Gramene v69.
    """
    if not loci:
        return {}
    result: dict[str, dict[str, str | None]] = {
        locus: {"uniprot_acc": None, "system_name": None} for locus in loci
    }
    for i in range(0, len(loci), chunk_size):
        chunk = loci[i : i + chunk_size]
        raw = await _get(
            client,
            f"/{GRAMENE_RELEASE}/genes",
            params={"idList": ",".join(chunk), "fl": "_id,xrefs,system_name"},
        )
        if not isinstance(raw, list):
            continue
        for record in raw:
            if not isinstance(record, dict):
                continue
            rid = record.get("_id")
            if not isinstance(rid, str) or rid not in result:
                continue
            sys_name = record.get("system_name")
            result[rid] = {
                "uniprot_acc": _pick_uniprot_acc(record.get("xrefs")),
                "system_name": sys_name if isinstance(sys_name, str) and sys_name else None,
            }
    return result


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
