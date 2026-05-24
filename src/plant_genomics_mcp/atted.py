"""ATTED-II coexpression backend — async httpx wrapper around atted.jp.

ATTED-II is the Tohoku/Yamagata-hosted plant coexpression database.
Returns co-expressed gene neighbors with a z-score (higher = stronger
coexpression). Free, no API key.

We use API v5 (canonical docs https://atted.jp/static/help/API.shtml,
last updated 2024-01-25). The DB string (e.g. ``Ath-u.c4-0`` for
Arabidopsis, ``Osa-u.c1-0`` for rice) selects the per-organism release
and is resolved through ``organisms.atted_release_for`` — v1.1.0
BREAKING dropped the module-level ``ATTED_RELEASE`` constant. Within a
release, data is frozen — 24h cache TTL is conservative.

The main atted.jp site is JS-gated, but ``/api5/`` returns plain JSON.
Set a friendly User-Agent header.

Live response shape:
    {request: {...},
     result_set: [{entrez_gene_id: int,
                   type: "z",
                   results: [{gene: int, other_id: [locus_str], z: float}, ...],
                   other_id: locus_str}]}

We assume a single query gene per call and project ``result_set[0].results``
into a flat list of neighbors.
"""

from __future__ import annotations

from typing import Any

import httpx

from plant_genomics_mcp import __version__, _http, cache, organisms
from plant_genomics_mcp.errors import (
    NotFoundError,
    PlantGenomicsError,
)

BASE_URL = "https://atted.jp"
API_PATH = "/api5/"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3
CACHE_TTL_SECONDS = 86400.0  # 24h — ATTED-II releases are versioned + frozen.

DEFAULT_TOP_N = 25
MAX_TOP_N = 300

_CACHE = cache.TTLCache(default_ttl=CACHE_TTL_SECONDS)


def _user_agent() -> str:
    return f"plant-genomics-mcp/{__version__}"


async def _get(
    client: httpx.AsyncClient,
    path: str,
    params: dict[str, Any] | None = None,
) -> Any:
    key = cache.make_key("GET", BASE_URL, path, params)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    resp = await _http.request_with_retry(
        client,
        "GET",
        f"{BASE_URL}{path}",
        service=f"ATTED-II {path}",
        params=params,
        headers={"Accept": "application/json", "User-Agent": _user_agent()},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
    )
    try:
        result = resp.json()
    except ValueError as e:
        raise PlantGenomicsError(f"ATTED-II {path} returned non-JSON: {resp.text[:200]}") from e
    _CACHE.set(key, result)
    return result


def _normalize(row: dict[str, Any]) -> dict[str, Any]:
    """Project one ATTED-II result row → flat neighbor dict.

    Input row shape: ``{"gene": <entrez_int>, "other_id": [locus_str], "z": float}``
    The ``other_id`` field is a list; we take the first entry as the
    canonical locus and tolerate missing/empty cases.
    """
    other_id = row.get("other_id") or []
    locus = other_id[0] if isinstance(other_id, list) and other_id else None
    return {
        "locus": locus,
        "entrez_gene_id": row.get("gene"),
        "z_score": row.get("z"),
    }


async def lookup_coexpression(
    client: httpx.AsyncClient,
    locus: str,
    *,
    organism: str | int,
    top_n: int = DEFAULT_TOP_N,
) -> dict[str, Any]:
    """Fetch ATTED-II co-expression neighbors for a plant locus.

    v1.1.0 BREAKING: ``organism`` is keyword-only and required. The
    ATTED-II release identifier (e.g. ``Ath-u.c4-0`` for Arabidopsis,
    ``Osa-u.c1-0`` for rice) is resolved via
    ``organisms.atted_release_for(organism)``; organisms not covered by
    ATTED-II (wheat, sorghum, barley, poplar, brachypodium as of the
    2026-05-24 probe) raise :class:`OrganismNotSupported` before any
    HTTP fires.
    """
    release = organisms.atted_release_for(organism)
    top_n = max(1, min(top_n, MAX_TOP_N))
    raw = await _get(
        client,
        API_PATH,
        params={"gene": locus, "topN": top_n, "db": release},
    )
    if not isinstance(raw, dict):
        raise PlantGenomicsError(f"ATTED-II {API_PATH} returned non-dict: {type(raw).__name__}")
    result_set = raw.get("result_set") or []
    if not isinstance(result_set, list) or not result_set:
        raise NotFoundError(f"ATTED-II: no co-expression neighbors for {locus}")
    first = result_set[0]
    if not isinstance(first, dict):
        raise PlantGenomicsError(
            f"ATTED-II {API_PATH}: result_set[0] not a dict ({type(first).__name__})"
        )
    rows = first.get("results") or []
    if not isinstance(rows, list) or not rows:
        raise NotFoundError(f"ATTED-II: no co-expression neighbors for {locus}")
    neighbors = [_normalize(r) for r in rows if isinstance(r, dict)]
    return {
        "locus": locus,
        "atted_release": release,
        "neighbors": neighbors,
    }
