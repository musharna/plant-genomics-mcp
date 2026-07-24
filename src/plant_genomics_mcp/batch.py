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
import json
import os
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from plant_genomics_mcp import (
    _http,
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

# Cap concurrent per-locus fan-out (env-tunable). 8 keeps a full 50-locus
# two-stage batch well under httpx's default 100-connection pool and polite to
# upstreams, while still overlapping most of the latency.
try:
    _CONCURRENCY = int(os.environ.get("PLANT_GENOMICS_MCP_BATCH_CONCURRENCY", "8"))
except ValueError:
    _CONCURRENCY = 8


def _bound(loci: list[str]) -> list[str]:
    if not loci:
        raise ValueError("loci must be a non-empty list")
    if len(loci) > MAX_BATCH:
        raise ValueError(f"loci length {len(loci)} exceeds MAX_BATCH={MAX_BATCH}")
    # De-duplicate, preserving first-seen order. ``_gather`` keys results/errors
    # by locus, so a duplicate would overwrite its twin — leaving the envelope's
    # ``count`` (len of this list) disagreeing with len(results)+len(errors) and
    # silently coalescing two possibly-different outcomes. Dedup also spares the
    # duplicate upstream calls. Length cap is checked against the raw input above.
    return list(dict.fromkeys(loci))


async def _gather(
    loci: list[str],
    fn: Callable[[str], Awaitable[dict[str, Any]]],
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Run ``fn(locus)`` for each locus in parallel and split successes/errors.

    PlantGenomicsError subclasses go to ``errors`` with the [ClassName]
    prefix from PlantGenomicsError.__str__; other exceptions re-raise.
    """
    # Bound concurrent per-locus work: a 50-locus two-stage batch (e.g.
    # UniProt-resolve → QuickGO per locus) would otherwise open ~100 sockets at
    # once, at httpx's default pool cap and hammering two upstreams (audit M3).
    # Fresh semaphore per call so it binds to the running loop (plantcyc pattern).
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def _guarded(locus: str) -> dict[str, Any]:
        async with sem:
            return await fn(locus)

    raw = await asyncio.gather(*(_guarded(locus) for locus in loci), return_exceptions=True)
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

    v1.1.0: now retries 429/5xx via the shared ``_http.request_with_retry``
    helper (Retry-After capped at 60 s, Wave B2 contract). Misses (null
    record per ID) still surface as ``[NotFoundError]`` entries in
    ``errors``; the whole batch only fails when the upstream call
    exhausts the retry budget.
    """
    loci = _bound(loci)
    slug = organisms.ensembl_slug_for(organism)
    payload: dict[str, Any] = {"ids": loci, "species": slug, "expand": 0}
    resp = await _http.request_with_retry(
        client,
        "POST",
        f"{ensembl_plants.BASE_URL}/lookup/id",
        service="Ensembl Plants /lookup/id (batch)",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=ensembl_plants.DEFAULT_TIMEOUT,
    )
    # request_with_retry returns the raw httpx.Response on 2xx.
    try:
        raw = resp.json()
    except ValueError as e:
        raise PlantGenomicsError(
            f"Ensembl Plants /lookup/id (batch) returned non-JSON: {resp.text[:200]}"
        ) from e
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
    *,
    organism: str | int = organisms.DEFAULT_ORGANISM,
) -> dict[str, Any]:
    loci = _bound(loci)
    results, errors = await _gather(
        loci, lambda locus: kegg.lookup_pathways(client, locus, organism=organism)
    )
    return _envelope("kegg_pathways", loci, results, errors)


async def batch_bar_gene_summary(
    client: httpx.AsyncClient,
    loci: list[str],
) -> dict[str, Any]:
    loci = _bound(loci)
    results, errors = await _gather(loci, lambda locus: bar.gene_summary(client, locus))
    return _envelope("bar_gene_summary", loci, results, errors)


async def batch_bar_aiv_interactions(
    client: httpx.AsyncClient,
    loci: list[str],
    organism: str | int = organisms.DEFAULT_ORGANISM,
) -> dict[str, Any]:
    loci = _bound(loci)
    results, errors = await _gather(
        loci,
        lambda locus: bar.aiv_interactions(client, locus, organism=organism),
    )
    return _envelope("bar_aiv_interactions", loci, results, errors)


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
    *,
    organism: str | int = organisms.DEFAULT_ORGANISM,
    top_n: int = atted.DEFAULT_TOP_N,
) -> dict[str, Any]:
    loci = _bound(loci)
    results, errors = await _gather(
        loci,
        lambda locus: atted.lookup_coexpression(client, locus, organism=organism, top_n=top_n),
    )
    return _envelope("atted_coexpression", loci, results, errors)
