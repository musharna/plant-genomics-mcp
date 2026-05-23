"""Batch helpers — fan out per-locus calls in parallel and shape the result.

All batch tools share the same envelope shape::

    {
      "tool": "<tool_name>",
      "count": <len(loci)>,
      "results": {locus: per-locus dict, ...},
      "errors":  {locus: "[ClassName] message", ...},
    }

Loci that succeed land in ``results``; those that raise a PlantGenomicsError
subclass land in ``errors`` with the same ``[ClassName] message`` wire format
the single-locus tools produce. Non-PlantGenomicsError exceptions propagate
so the SDK's outer except handler still hits and the caller sees the same
error shape it would for a single-locus call.

For Ensembl Plants ``/lookup/id`` we use the native POST batch endpoint —
one HTTP round-trip for N loci, materially cheaper than N parallel GETs.
Everything else fans out via ``asyncio.gather`` of single-locus calls.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

import httpx

from plant_genomics_mcp import (
    atted,
    bar,
    ensembl_plants,
    europe_pmc,
    gramene,
    kegg,
    organisms,
    phytozome,
    quickgo,
    string_db,
    uniprot,
)
from plant_genomics_mcp.errors import PlantGenomicsError

MAX_BATCH = 50  # bound the wire payload; matches Ensembl's documented limit


def _bound(loci: list[str]) -> list[str]:
    if not loci:
        raise ValueError("loci must be a non-empty list")
    if len(loci) > MAX_BATCH:
        raise ValueError(f"loci length {len(loci)} exceeds MAX_BATCH={MAX_BATCH}")
    return loci


async def _gather(
    loci: list[str],
    fn: Callable[[str], Awaitable[dict[str, Any]]],
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Run ``fn(locus)`` for each locus in parallel and split successes/errors.

    PlantGenomicsError subclasses go to ``errors`` with the [ClassName]
    prefix from PlantGenomicsError.__str__; other exceptions re-raise.
    """
    coros = [fn(locus) for locus in loci]
    raw = await asyncio.gather(*coros, return_exceptions=True)
    results: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    for locus, outcome in zip(loci, raw, strict=True):
        if isinstance(outcome, PlantGenomicsError):
            errors[locus] = str(outcome)
        elif isinstance(outcome, BaseException):
            raise outcome
        else:
            results[locus] = outcome
    return results, errors


def _envelope(
    tool: str,
    loci: list[str],
    results: dict[str, dict[str, Any]],
    errors: dict[str, str],
) -> dict[str, Any]:
    return {
        "tool": tool,
        "count": len(loci),
        "results": results,
        "errors": errors,
    }


# ---- Ensembl Plants lookup — native POST batch -----------------------------


async def batch_ensembl_plants_lookup_locus(
    client: httpx.AsyncClient,
    loci: list[str],
    organism: str | int = organisms.DEFAULT_ORGANISM,
) -> dict[str, Any]:
    """One HTTP round-trip via Ensembl's POST /lookup/id batch endpoint.

    Ensembl returns a dict keyed on the input ID with the per-locus record
    or ``null`` for misses. We translate nulls into [NotFoundError] entries
    so the wire shape matches the gather-based batch tools.
    """
    loci = _bound(loci)
    slug = organisms.ensembl_slug_for(organism)
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    payload: dict[str, Any] = {"ids": loci, "species": slug, "expand": 0}
    resp = await client.post(
        f"{ensembl_plants.BASE_URL}/lookup/id",
        json=payload,
        headers=headers,
        timeout=ensembl_plants.DEFAULT_TIMEOUT,
    )
    if resp.status_code != 200:
        # The single-locus call has retry; the batch endpoint is documented
        # as idempotent so the same retry policy would apply, but for now we
        # surface the failure directly with the typed wrapper.
        raise PlantGenomicsError(
            f"Ensembl Plants /lookup/id (batch) → HTTP {resp.status_code}: {resp.text[:200]}"
        )
    raw = resp.json()
    if not isinstance(raw, dict):
        raise PlantGenomicsError(
            f"Ensembl Plants /lookup/id (batch) returned non-dict payload: {type(raw).__name__}"
        )
    results: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    for locus in loci:
        record = raw.get(locus)
        if record is None:
            errors[locus] = f"[NotFoundError] Ensembl Plants /lookup/id: no record for {locus}"
        elif isinstance(record, dict):
            results[locus] = record
        else:
            errors[locus] = (
                f"[PlantGenomicsError] Ensembl Plants returned non-dict for {locus}: "
                f"{type(record).__name__}"
            )
    return _envelope("ensembl_plants_lookup_locus", loci, results, errors)


# ---- gather-based fanouts --------------------------------------------------


async def batch_get_gene_xrefs(
    client: httpx.AsyncClient,
    loci: list[str],
    organism: str | int = organisms.DEFAULT_ORGANISM,
) -> dict[str, Any]:
    loci = _bound(loci)
    results, errors = await _gather(
        loci, lambda locus: ensembl_plants.lookup_xrefs(client, locus, organism=organism)
    )
    return _envelope("get_gene_xrefs", loci, results, errors)


async def batch_phytozome_lookup_locus(
    client: httpx.AsyncClient,
    loci: list[str],
    organism: str | int = organisms.DEFAULT_ORGANISM,
) -> dict[str, Any]:
    loci = _bound(loci)
    results, errors = await _gather(
        loci, lambda locus: phytozome.lookup_locus(client, locus, organism=organism)
    )
    return _envelope("phytozome_lookup_locus", loci, results, errors)


async def batch_resolve_locus_to_uniprot(
    client: httpx.AsyncClient,
    loci: list[str],
    organism: str | int = organisms.DEFAULT_ORGANISM,
) -> dict[str, Any]:
    loci = _bound(loci)
    results, errors = await _gather(
        loci, lambda locus: uniprot.lookup_locus(client, locus, organism=organism)
    )
    return _envelope("resolve_locus_to_uniprot", loci, results, errors)


async def batch_locus_literature(
    client: httpx.AsyncClient,
    loci: list[str],
    organism: str | int = organisms.DEFAULT_ORGANISM,
    size: int = europe_pmc.DEFAULT_PAGE_SIZE,
) -> dict[str, Any]:
    loci = _bound(loci)
    results, errors = await _gather(
        loci,
        lambda locus: europe_pmc.lookup_locus(client, locus, organism=organism, size=size),
    )
    return _envelope("locus_literature", loci, results, errors)


async def batch_locus_go_annotations(
    client: httpx.AsyncClient,
    loci: list[str],
    organism: str | int = organisms.DEFAULT_ORGANISM,
    limit: int = quickgo.DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Two-stage fanout: resolve each locus to UniProt then query QuickGO.

    Resolution and QuickGO calls happen per-locus inside a single gather;
    a NotFoundError from either stage lands in ``errors`` with the typed
    prefix preserved.
    """
    loci = _bound(loci)

    async def _one(locus: str) -> dict[str, Any]:
        up = await uniprot.lookup_locus(client, locus, organism=organism)
        accession = up["primaryAccession"]
        go = await quickgo.lookup_by_uniprot(client, accession, limit=limit)
        return {
            "locus": locus,
            "uniprot_accession": accession,
            "numberOfHits": go["numberOfHits"],
            "returned": go["returned"],
            "annotations": go["annotations"],
            "by_aspect": go["by_aspect"],
        }

    results, errors = await _gather(loci, _one)
    return _envelope("locus_go_annotations", loci, results, errors)


async def batch_gramene_homologs(
    client: httpx.AsyncClient,
    loci: list[str],
    homology_type: str = "ortholog",
) -> dict[str, Any]:
    loci = _bound(loci)
    results, errors = await _gather(
        loci,
        lambda locus: gramene.lookup_homologs(client, locus, homology_type=homology_type),
    )
    return _envelope("gramene_homologs", loci, results, errors)


async def batch_kegg_pathways(
    client: httpx.AsyncClient,
    loci: list[str],
) -> dict[str, Any]:
    loci = _bound(loci)
    results, errors = await _gather(loci, lambda locus: kegg.lookup_pathways(client, locus))
    return _envelope("kegg_pathways", loci, results, errors)


async def batch_bar_gene_summary(
    client: httpx.AsyncClient,
    loci: list[str],
) -> dict[str, Any]:
    loci = _bound(loci)
    results, errors = await _gather(loci, lambda locus: bar.gene_summary(client, locus))
    return _envelope("bar_gene_summary", loci, results, errors)


async def batch_string_interactions(
    client: httpx.AsyncClient,
    loci_or_accessions: list[str],
    limit: int = string_db.DEFAULT_LIMIT,
    organism: str | int = organisms.DEFAULT_ORGANISM,
) -> dict[str, Any]:
    loci = _bound(loci_or_accessions)
    results, errors = await _gather(
        loci,
        lambda q: string_db.lookup_partners(client, q, limit=limit, organism=organism),
    )
    return _envelope("string_interactions", loci, results, errors)


async def batch_atted_coexpression(
    client: httpx.AsyncClient,
    loci: list[str],
    top_n: int = atted.DEFAULT_TOP_N,
) -> dict[str, Any]:
    loci = _bound(loci)
    results, errors = await _gather(
        loci, lambda locus: atted.lookup_coexpression(client, locus, top_n=top_n)
    )
    return _envelope("atted_coexpression", loci, results, errors)
