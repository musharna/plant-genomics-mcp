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

from typing import Any

import httpx

from plant_genomics_mcp import _http, cache
from plant_genomics_mcp.errors import (
    PlantGenomicsError,
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
    resp = await _http.request_with_retry(
        client,
        "GET",
        f"{BASE_URL}{path}",
        service=f"QuickGO {path}",
        params=params,
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
    )
    result = resp.json()
    _CACHE.set(key, result)
    return result


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
    cursor: str | None = None,
) -> dict[str, Any]:
    """Fetch GO annotations for a UniProt accession.

    ``cursor`` is a ``next_cursor`` from the previous page (#123); it resumes
    at QuickGO's own 1-based ``page``. ``by_aspect`` rolls up THIS page.

    ``limit`` is clamped to [1, MAX_LIMIT]. Returns a dict with raw
    ``annotations[]`` plus a ``by_aspect`` rollup keyed on GO aspect
    (molecular_function / biological_process / cellular_component).
    """
    limit = max(1, min(limit, MAX_LIMIT))
    query = {"accession": accession, "limit": limit}
    page = int(_http.decode_cursor("locus_go_annotations", query, cursor).get("page", 1))
    params: dict[str, Any] = {
        "geneProductId": accession,
        "limit": limit,
        "includeFields": "goName,taxonName",
    }
    if page > 1:
        params["page"] = page
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
    total = _http.stated_count(raw, "numberOfHits", service="QuickGO /annotation/search")
    return {
        "uniprot_accession": accession,
        "numberOfHits": total,
        # Issue #132: 51 upstream / 50 returned shipped with no flag.
        **_http.counted(total, annotations, offset=(page - 1) * limit),
        "next_cursor": _http.encode_cursor("locus_go_annotations", query, {"page": page + 1})
        if total > (page - 1) * limit + len(annotations)
        else None,
        "annotations": annotations,
        "by_aspect": _rollup_by_aspect(annotations),
        # Issue #132: the rollup is a dedup of annotations[], not a cut of it;
        # say so in the payload, not only in the tool description.
        "by_aspect_deduped_on": "goId",
        # Issue #121: uniform key; null because this backend states no release on
        # the answering response (headers probed live 2026-09-22).
        "upstream_version": None,
    }
