"""Read-only MCP resources for the plant-genomics server (P2.17).

Three resources, all JSON, all derived from in-process state:

  * ``pgmcp://cache/stats``        — per-backend ``TTLCache`` stats rollup
                                     (hits / misses / size). Useful for an
                                     operator to confirm caching is doing
                                     work and to spot a hot-key pattern.
  * ``pgmcp://organisms/phytozome`` — the ``phytozome.KNOWN_ORGANISMS`` slug
                                     → integer-id map. Lets a client enumerate
                                     supported organisms without having to
                                     parse the docstring.
  * ``pgmcp://backends/status``    — per-backend liveness rollup
                                     (name, base_url, kind=live|stub,
                                     subscription_gated, probed_at). Mirrors
                                     the catalog in ``server.py``'s module
                                     docstring but in a parseable form.

Wiring sits in ``server.py``: ``@server.list_resources()`` returns
``RESOURCES``, ``@server.read_resource()`` dispatches to ``read_resource``.

Why a separate module: keeps the server file focused on tool dispatch,
makes the URI → payload mapping unit-testable without spinning up MCP,
and concentrates the read-only metadata so future resources (e.g. tool
graph, recent calls) have a home.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from mcp import types
from mcp.server.lowlevel.helper_types import ReadResourceContents
from pydantic import AnyUrl

from plant_genomics_mcp import (
    ensembl_plants,
    europe_pmc,
    gramene,
    kegg,
    phytozome,
    plantcyc,
    quickgo,
    tair,
    uniprot,
)

CACHE_STATS_URI = "pgmcp://cache/stats"
PHYTOZOME_ORGANISMS_URI = "pgmcp://organisms/phytozome"
BACKENDS_STATUS_URI = "pgmcp://backends/status"


RESOURCES: list[types.Resource] = [
    types.Resource(
        uri=AnyUrl(CACHE_STATS_URI),
        name="Cache statistics",
        description=(
            "Per-backend TTL+LRU cache stats (hits / misses / size). "
            "Sourced from each backend module's process-local _CACHE."
        ),
        mimeType="application/json",
    ),
    types.Resource(
        uri=AnyUrl(PHYTOZOME_ORGANISMS_URI),
        name="Phytozome known organisms",
        description=(
            "Map of Ensembl-style species slug → Phytozome organism_id. "
            "Only arabidopsis_thaliana (167) is controller-verified; the "
            "rest are unverified BioMart-registry hints."
        ),
        mimeType="application/json",
    ),
    types.Resource(
        uri=AnyUrl(BACKENDS_STATUS_URI),
        name="Backend status",
        description=(
            "Per-backend rollup (name, base_url, kind=live|stub, "
            "subscription_gated, probed_at). Lets a client enumerate the "
            "live and stubbed backends without parsing the server "
            "docstring."
        ),
        mimeType="application/json",
    ),
]


def _cache_stats_payload() -> dict[str, dict[str, int]]:
    """Live snapshot of each backend's per-module ``_CACHE.stats()``.

    Read fresh on every call — we do NOT cache the cache stats (callers
    want a current reading, not a stale snapshot).
    """
    return {
        "ensembl_plants": ensembl_plants._CACHE.stats(),
        "europe_pmc": europe_pmc._CACHE.stats(),
        "gramene": gramene._CACHE.stats(),
        "kegg": kegg._CACHE.stats(),
        "phytozome": phytozome._CACHE.stats(),
        "quickgo": quickgo._CACHE.stats(),
        "uniprot": uniprot._CACHE.stats(),
    }


def _backends_status_payload() -> list[dict[str, object]]:
    """Per-backend liveness + subscription-gating rollup.

    ``probed_at`` is included on stub entries — it's the last time the
    controller verified the subscription gate, so a client can decide
    whether to retry.
    """
    return [
        {
            "name": "ensembl_plants",
            "base_url": ensembl_plants.BASE_URL,
            "kind": "live",
            "subscription_gated": False,
        },
        {
            "name": "phytozome",
            "base_url": phytozome.BASE_URL,
            "kind": "live",
            "subscription_gated": False,
        },
        {
            "name": "uniprot",
            "base_url": uniprot.BASE_URL,
            "kind": "live",
            "subscription_gated": False,
        },
        {
            "name": "europe_pmc",
            "base_url": europe_pmc.BASE_URL,
            "kind": "live",
            "subscription_gated": False,
        },
        {
            "name": "quickgo",
            "base_url": quickgo.BASE_URL,
            "kind": "live",
            "subscription_gated": False,
        },
        {
            "name": "gramene",
            "base_url": gramene.BASE_URL,
            "kind": "live",
            "subscription_gated": False,
        },
        {
            "name": "kegg",
            "base_url": kegg.BASE_URL,
            "kind": "live",
            "subscription_gated": False,
        },
        {
            "name": "tair",
            "base_url": "https://www.arabidopsis.org/",
            "kind": "stub",
            "subscription_gated": True,
            "probed_at": tair._PROBED_AT,
        },
        {
            "name": "plantcyc",
            "base_url": "https://pmn.plantcyc.org/",
            "kind": "stub",
            "subscription_gated": True,
            "probed_at": plantcyc._PROBED_AT,
        },
    ]


def _payload_for(uri: str) -> object:
    """Map a resource URI to its serializable Python payload."""
    if uri == CACHE_STATS_URI:
        return _cache_stats_payload()
    if uri == PHYTOZOME_ORGANISMS_URI:
        return phytozome.KNOWN_ORGANISMS
    if uri == BACKENDS_STATUS_URI:
        return _backends_status_payload()
    raise ValueError(f"unknown resource URI: {uri}")


async def read_resource(uri: AnyUrl) -> Iterable[ReadResourceContents]:
    """Resolve ``uri`` to a single JSON ``ReadResourceContents`` entry.

    The MCP SDK accepts an iterable of ``ReadResourceContents``; we always
    return exactly one item per resource (no multi-part payloads). JSON is
    rendered with sorted keys and 2-space indent so a client can diff
    snapshots over time without spurious key-order churn.
    """
    payload = _payload_for(str(uri))
    text = json.dumps(payload, sort_keys=True, indent=2)
    return [ReadResourceContents(content=text, mime_type="application/json")]
