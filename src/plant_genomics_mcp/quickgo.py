"""QuickGO REST client — async httpx wrapper around www.ebi.ac.uk/QuickGO.

QuickGO is EBI's Gene Ontology annotation browser. Free, no API key. We
query the ``/annotation/search`` endpoint with a UniProt accession as
``geneProductId``. QuickGO doesn't index plant locus identifiers
(AT1G01010, Os01g0100100, ...) directly — those must first be resolved
to a UniProt accession via ``plant_genomics_mcp.uniprot.lookup_locus``.

We request the documented ``goName`` and ``taxonName`` includeFields so
the wire payload carries human-readable labels alongside the GO IDs.
Endpoint docs: https://www.ebi.ac.uk/QuickGO/api/index.html.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from plant_genomics_mcp import cache, progress
from plant_genomics_mcp.errors import (
    PlantGenomicsError,
    RateLimitError,
    UpstreamUnavailableError,
)

BASE_URL = "https://www.ebi.ac.uk/QuickGO/services"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3
DEFAULT_LIMIT = 50
MAX_LIMIT = 100  # QuickGO documents a 100/page upper bound on /search

# Per-module response cache. See plant_genomics_mcp.cache for env knobs.
_CACHE = cache.TTLCache()


# Fields we surface from each annotation row. QuickGO returns ~18 fields per
# row; we drop verbose ones (`id`, `name`, `synonyms`, `targetSets`,
# `extensions`) and keep the GO-centric core.
_ANN_FIELDS = (
    "geneProductId",
    "symbol",
    "qualifier",
    "goId",
    "goName",
    "goAspect",
    "goEvidence",
    "evidenceCode",
    "reference",
    "assignedBy",
    "taxonId",
    "taxonName",
    "date",
    "withFrom",
)


async def _get(
    client: httpx.AsyncClient,
    path: str,
    params: dict[str, Any] | None = None,
) -> Any:
    """GET a QuickGO endpoint with retry on 429/5xx."""
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
            retry_after = float(resp.headers.get("Retry-After", delay))
            await progress.notify(
                f"QuickGO {path}: HTTP {resp.status_code}, retrying in "
                f"{retry_after:.1f}s (attempt {attempt + 2}/{MAX_RETRIES})"
            )
            await asyncio.sleep(retry_after)
            delay *= 2
            continue
        if resp.status_code == 429:
            raise RateLimitError(f"QuickGO {path} rate-limited (HTTP 429): {resp.text[:200]}")
        if resp.status_code in (500, 502, 503, 504):
            raise UpstreamUnavailableError(
                f"QuickGO {path} → HTTP {resp.status_code}: {resp.text[:200]}"
            )
        raise PlantGenomicsError(f"QuickGO {path} → HTTP {resp.status_code}: {resp.text[:200]}")
    if last_status == 429:
        raise RateLimitError(f"QuickGO {path} exhausted {MAX_RETRIES} retries (429)")
    raise UpstreamUnavailableError(
        f"QuickGO {path} exhausted {MAX_RETRIES} retries (last HTTP {last_status})"
    )


def _normalize(row: dict[str, Any]) -> dict[str, Any]:
    """Project a QuickGO annotation row to the surfaced field set."""
    return {k: row.get(k) for k in _ANN_FIELDS}


def _rollup_by_aspect(annotations: list[dict[str, Any]]) -> dict[str, list[dict[str, str]]]:
    """Group annotations by GO aspect, deduping on (goId, goName).

    Each annotation row appears once per (goId, evidence_code, reference)
    triple — a single GO term can have multiple supporting annotations.
    The aspect rollup collapses these so an LLM client can see "the term
    set" at a glance without the evidence-level repetition.
    """
    seen: dict[str, set[str]] = {}
    grouped: dict[str, list[dict[str, str]]] = {}
    for ann in annotations:
        aspect = ann.get("goAspect")
        go_id = ann.get("goId")
        if not aspect or not go_id:
            continue
        bucket = seen.setdefault(aspect, set())
        if go_id in bucket:
            continue
        bucket.add(go_id)
        grouped.setdefault(aspect, []).append({"goId": go_id, "goName": ann.get("goName") or ""})
    return grouped


async def lookup_by_uniprot(
    client: httpx.AsyncClient,
    accession: str,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Fetch GO annotations for a UniProt accession.

    ``limit`` is clamped to [1, MAX_LIMIT]. Returns a dict with raw
    ``annotations[]`` plus a ``by_aspect`` rollup keyed on GO aspect
    (molecular_function / biological_process / cellular_component).
    """
    limit = max(1, min(limit, MAX_LIMIT))
    params: dict[str, Any] = {
        "geneProductId": accession,
        "limit": limit,
        "includeFields": "goName,taxonName",
    }
    raw = await _get(client, "/annotation/search", params=params)
    if not isinstance(raw, dict):
        raise PlantGenomicsError(
            f"QuickGO /annotation/search returned non-dict payload: {type(raw).__name__}"
        )
    results = raw.get("results") or []
    if not isinstance(results, list):
        raise PlantGenomicsError(
            f"QuickGO /annotation/search results is not a list: {type(results).__name__}"
        )
    annotations = [_normalize(r) for r in results if isinstance(r, dict)]
    return {
        "uniprot_accession": accession,
        "numberOfHits": int(raw.get("numberOfHits", 0)),
        "returned": len(annotations),
        "annotations": annotations,
        "by_aspect": _rollup_by_aspect(annotations),
    }
