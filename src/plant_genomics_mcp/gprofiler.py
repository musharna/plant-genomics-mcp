"""g:Profiler g:GOSt client — GO/KEGG over-representation for a gene LIST.

Unlike the per-locus GO tool (``quickgo.py``), this backend answers the
downstream question "what is my differentially-expressed / co-expressed
gene *set* enriched for?" via functional over-representation analysis.

g:Profiler (biit.cs.ut.ee/gprofiler) is free, needs no API key, and covers
every organism in our registry through Ensembl Plants. We POST the query
set to the documented ``/api/gost/profile/`` endpoint and surface the
significant terms plus the loci g:Profiler could not map (so a locus-
namespace mismatch fails loud instead of silently shrinking the query).

Organism codes come from ``organisms.gprofiler_id_for`` (NOT the taxid —
g:Profiler indexes specific assemblies/cultivars). Endpoint docs:
https://biit.cs.ut.ee/gprofiler/page/apis.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import httpx

from plant_genomics_mcp import _http, cache, organisms
from plant_genomics_mcp.errors import PlantGenomicsError

BASE_URL = "https://biit.cs.ut.ee/gprofiler"
PROFILE_PATH = "/api/gost/profile/"
DEFAULT_TIMEOUT = 60.0
MAX_RETRIES = 3

# GO + KEGG only for v1 (matches the feature backlog). g:Profiler also serves
# REAC/TF/MIRNA/WP/CORUM/HP, but those have thin plant coverage; a source
# outside this set is rejected rather than silently dropped by the upstream.
ALLOWED_SOURCES: tuple[str, ...] = ("GO:BP", "GO:MF", "GO:CC", "KEGG")
DEFAULT_SOURCES: tuple[str, ...] = ("GO:BP", "GO:MF", "GO:CC", "KEGG")

DEFAULT_THRESHOLD = 0.05
# g:SCS is g:Profiler's native multiple-testing correction (tighter than
# Bonferroni, recommended by the authors). We don't expose the method knob in
# v1 — the default is the scientifically-sound choice for over-representation.
SIGNIFICANCE_METHOD = "g_SCS"

DEFAULT_TOP_N = 50
MAX_TOP_N = 200
# g:Profiler caps a single query at 100k genes; we bound well below that so an
# accidental whole-genome paste fails loud instead of timing out the endpoint.
MAX_QUERY = 10_000

# Fields projected from each g:Profiler result row. The wire row carries ~17
# fields; we drop the plotting-only ones (`group_id`, `source_order`, `goshv`,
# `effective_domain_size`, `parents`, `query`).
_TERM_FIELDS = (
    "source",
    "native",
    "name",
    "description",
    "p_value",
    "significant",
    "term_size",
    "query_size",
    "intersection_size",
    "precision",
    "recall",
)

# Per-module response cache. See plant_genomics_mcp.cache for env knobs.
_CACHE = cache.TTLCache()


def _clean_loci(loci: Sequence[str], *, field: str = "loci") -> list[str]:
    """Strip/validate a gene list: non-empty, all strings, <= MAX_QUERY."""
    if not isinstance(loci, (list, tuple)):
        raise ValueError(f"{field} must be a list of gene identifiers")
    cleaned = [str(x).strip() for x in loci if str(x).strip()]
    if not cleaned:
        raise ValueError(f"{field} must contain at least one non-empty identifier")
    if len(cleaned) > MAX_QUERY:
        raise ValueError(
            f"{field} has {len(cleaned)} identifiers; the cap is {MAX_QUERY} per query"
        )
    return cleaned


def _validate_sources(sources: Sequence[str] | None) -> list[str]:
    """Default to GO+KEGG; reject any source outside the v1 allow-list."""
    if not sources:
        return list(DEFAULT_SOURCES)
    chosen = [str(s).strip().upper() for s in sources if str(s).strip()]
    if not chosen:
        return list(DEFAULT_SOURCES)
    bad = [s for s in chosen if s not in ALLOWED_SOURCES]
    if bad:
        raise ValueError(f"unsupported source(s) {bad}; allowed: {list(ALLOWED_SOURCES)}")
    # De-dup, preserve caller order.
    seen: set[str] = set()
    deduped: list[str] = []
    for s in chosen:
        if s not in seen:
            seen.add(s)
            deduped.append(s)
    return deduped


def _project_term(row: dict[str, Any]) -> dict[str, Any]:
    """Project a g:Profiler result row to the surfaced field set (native→term_id)."""
    term = {k: row.get(k) for k in _TERM_FIELDS}
    term["term_id"] = term.pop("native")
    return term


async def go_enrichment(
    client: httpx.AsyncClient,
    loci: Sequence[str],
    organism: str = organisms.DEFAULT_ORGANISM,
    *,
    sources: Sequence[str] | None = None,
    background: Sequence[str] | None = None,
    user_threshold: float = DEFAULT_THRESHOLD,
    top_n: int = DEFAULT_TOP_N,
) -> dict[str, Any]:
    """GO/KEGG over-representation for a gene set via g:Profiler g:GOSt.

    ``loci`` is the query gene list. ``organism`` accepts any form the
    registry resolves (slug / scientific / common / taxid) and is mapped to
    a g:Profiler organism ID. ``sources`` defaults to GO:BP/MF/CC + KEGG.
    ``background`` supplies a custom statistical domain (defaults to all
    annotated genes). ``top_n`` caps the returned term count (sorted by
    p-value); the pre-cap total is reported as ``total_terms``.

    Returns a dict with ``enriched[]`` (significant terms) and ``unmapped[]``
    (query loci g:Profiler could not recognize — surfaced, not dropped).
    """
    record = organisms.resolve(organism)
    gp_id = organisms.gprofiler_id_for(organism)
    query = _clean_loci(loci)
    chosen_sources = _validate_sources(sources)
    top_n = max(1, min(int(top_n), MAX_TOP_N))
    if not (0.0 < float(user_threshold) <= 1.0):
        raise ValueError("user_threshold must be in (0, 1]")

    payload: dict[str, Any] = {
        "organism": gp_id,
        "query": query,
        "sources": chosen_sources,
        "user_threshold": float(user_threshold),
        "significance_threshold_method": SIGNIFICANCE_METHOD,
        "no_evidences": True,
        "no_iea": False,
    }
    if background:
        payload["background"] = _clean_loci(background, field="background")
        payload["domain_scope"] = "custom"

    key = cache.make_key("POST", BASE_URL, PROFILE_PATH, body=payload)
    cached = _CACHE.get(key)
    if cached is None:
        resp = await _http.request_with_retry(
            client,
            "POST",
            f"{BASE_URL}{PROFILE_PATH}",
            service="g:Profiler g:GOSt",
            json=payload,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=DEFAULT_TIMEOUT,
            max_retries=MAX_RETRIES,
        )
        cached = resp.json()
        _CACHE.set(key, cached)

    if not isinstance(cached, dict):
        raise PlantGenomicsError(
            f"g:Profiler {PROFILE_PATH} returned non-dict payload: {type(cached).__name__}"
        )
    results = cached.get("result")
    if not isinstance(results, list):
        raise PlantGenomicsError(
            f"g:Profiler {PROFILE_PATH} 'result' is not a list: {type(results).__name__}"
        )

    terms = [_project_term(r) for r in results if isinstance(r, dict)]
    terms.sort(key=lambda t: (t.get("p_value") is None, t.get("p_value", 1.0)))

    genes_meta = (cached.get("meta") or {}).get("genes_metadata") or {}
    failed = genes_meta.get("failed")
    unmapped = [str(g) for g in failed] if isinstance(failed, list) else []

    return {
        "organism": record.canonical,
        "gprofiler_id": gp_id,
        "sources": chosen_sources,
        "query_size": len(query),
        "mapped": len(query) - len(unmapped),
        "unmapped": unmapped,
        "total_terms": len(terms),
        "returned": min(len(terms), top_n),
        "enriched": terms[:top_n],
    }
