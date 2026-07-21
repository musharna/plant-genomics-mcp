"""Read-only MCP resources for the plant-genomics server (P2.17, extended in v0.9).

Four resources, all derived from in-process state:

  * ``pgmcp://cache/stats``         — per-backend ``TTLCache`` stats rollup
                                      (hits / misses / size). Useful for an
                                      operator to confirm caching is doing
                                      work and to spot a hot-key pattern.
  * ``pgmcp://organisms/phytozome`` — canonical slug → Phytozome organism_id,
                                      derived from the ORGANISMS registry
                                      (filtered to records with a non-None
                                      phytozome_int). Replaces the v0.8-era
                                      ``phytozome.KNOWN_ORGANISMS`` module dict.
  * ``pgmcp://backends/status``     — per-backend liveness rollup
                                      (name, base_url, kind=live,
                                      subscription_gated). Mirrors
                                      the catalog in ``server.py``'s module
                                      docstring but in a parseable form.
  * ``pgmcp://organisms/coverage``  — markdown table of the full 12-organism
                                      × 8-backend coverage matrix. Lets a
                                      client introspect supported coverage
                                      in one read instead of probing
                                      ``resolve_organism`` per organism.

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
    alphafold,
    aragwas,
    atted,
    bar,
    blast,
    ensembl_plants,
    ensembl_variation,
    europe_pmc,
    gprofiler,
    gramene,
    interpro,
    jaspar,
    kegg,
    onekg,
    organisms,
    orthodb,
    panther,
    pdbe,
    phytozome,
    plantcyc,
    planteome,
    quickgo,
    string_db,
    thalemine,
    uniprot,
)

CACHE_STATS_URI = "pgmcp://cache/stats"
PHYTOZOME_ORGANISMS_URI = "pgmcp://organisms/phytozome"
BACKENDS_STATUS_URI = "pgmcp://backends/status"
COVERAGE_MATRIX_URI = "pgmcp://organisms/coverage"


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
        name="Phytozome organisms",
        description=(
            "Map of canonical slug → Phytozome organism_id, derived from "
            "the ORGANISMS registry. Only includes organisms with a "
            "non-None phytozome_int. See pgmcp://organisms/coverage for "
            "the full coverage matrix across all backends."
        ),
        mimeType="application/json",
    ),
    types.Resource(
        uri=AnyUrl(BACKENDS_STATUS_URI),
        name="Backend status",
        description=(
            "Per-backend rollup (name, base_url, kind=live, "
            "subscription_gated). Lets a client enumerate the "
            "live backends without parsing the server "
            "docstring."
        ),
        mimeType="application/json",
    ),
    types.Resource(
        uri=AnyUrl(COVERAGE_MATRIX_URI),
        name="Organism coverage matrix",
        description=(
            "Markdown table of all 12 supported plants × 9 ID slots "
            "(ncbi_taxid, ensembl, phytozome, string, europe_pmc, kegg, atted, "
            "gprofiler, plantcyc). "
            "Missing slots render as em-dash. Lets a client introspect "
            "coverage in one read instead of probing resolve_organism "
            "per organism."
        ),
        mimeType="text/markdown",
    ),
]


def _cache_stats_payload() -> dict[str, dict[str, int]]:
    """Live snapshot of each backend's per-module ``_CACHE.stats()``.

    Read fresh on every call — we do NOT cache the cache stats (callers
    want a current reading, not a stale snapshot). BLAST is intentionally
    absent: it's an async submit/poll workflow with no client-side cache,
    so a stats entry would be a misleading row of zeros. See
    ``pgmcp://backends/status`` for the full backend roster including
    BLAST.
    """
    return {
        "alphafold": alphafold._CACHE.stats(),
        "aragwas": aragwas._CACHE.stats(),
        "atted": atted._CACHE.stats(),
        "bar": bar._CACHE.stats(),
        "ensembl_plants": ensembl_plants._CACHE.stats(),
        "ensembl_variation": ensembl_variation._CACHE.stats(),
        "europe_pmc": europe_pmc._CACHE.stats(),
        "gprofiler": gprofiler._CACHE.stats(),
        "gramene": gramene._CACHE.stats(),
        "interpro": interpro._CACHE.stats(),
        "kegg": kegg._CACHE.stats(),
        "onekg": onekg._CACHE.stats(),
        "orthodb": orthodb._CACHE.stats(),
        "panther": panther._CACHE.stats(),
        "pdbe": pdbe._CACHE.stats(),
        "jaspar": jaspar._CACHE.stats(),
        "phytozome": phytozome._CACHE.stats(),
        "plantcyc": plantcyc._CACHE.stats(),
        "planteome": planteome._CACHE.stats(),
        "quickgo": quickgo._CACHE.stats(),
        "string_db": string_db._CACHE.stats(),
        "thalemine": thalemine._CACHE.stats(),
        "uniprot": uniprot._CACHE.stats(),
    }


def _backends_status_payload() -> list[dict[str, object]]:
    """Per-backend liveness + subscription-gating rollup.

    Each entry is ``{name, base_url, kind, subscription_gated}`` (BLAST also
    carries ``concurrency_cap``). All backends are currently ``kind="live"``.
    """
    return [
        {
            "name": "atted",
            "base_url": atted.BASE_URL,
            "kind": "live",
            "subscription_gated": False,
        },
        {
            "name": "bar",
            "base_url": bar.BASE_URL,
            "kind": "live",
            "subscription_gated": False,
        },
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
            "name": "planteome",
            "base_url": planteome.BASE_URL,
            "kind": "live",
            "subscription_gated": False,
        },
        {
            "name": "gprofiler",
            "base_url": gprofiler.BASE_URL,
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
            "name": "string_db",
            "base_url": string_db.BASE_URL,
            "kind": "live",
            "subscription_gated": False,
        },
        {
            "name": "blast",
            "base_url": blast.BASE_URL,
            "kind": "live",
            "subscription_gated": False,
            # Concurrency-capped (NCBI etiquette, Wave B4); see
            # PLANT_GENOMICS_MCP_BLAST_CONCURRENCY for the operator knob.
            "concurrency_cap": blast.MAX_CONCURRENT_BLAST,
        },
        {
            "name": "plantcyc",
            "base_url": plantcyc.BASE_URL,
            "kind": "live",
            "subscription_gated": False,
        },
        {
            "name": "alphafold",
            "base_url": alphafold.BASE_URL,
            "kind": "live",
            "subscription_gated": False,
        },
        {
            "name": "pdbe",
            "base_url": pdbe.BASE_URL,
            "kind": "live",
            "subscription_gated": False,
        },
        {
            "name": "interpro",
            "base_url": interpro.BASE_URL,
            "kind": "live",
            "subscription_gated": False,
        },
        {
            "name": "jaspar",
            "base_url": jaspar.BASE_URL,
            "kind": "live",
            "subscription_gated": False,
        },
        {
            "name": "thalemine",
            "base_url": thalemine.BASE_URL,
            "kind": "live",
            "subscription_gated": False,
        },
        {
            "name": "ensembl_variation",
            "base_url": ensembl_variation.BASE_URL,
            "kind": "live",
            "subscription_gated": False,
        },
        {
            "name": "panther",
            "base_url": panther.BASE_URL,
            "kind": "live",
            "subscription_gated": False,
        },
        {
            "name": "orthodb",
            "base_url": orthodb.BASE_URL,
            "kind": "live",
            "subscription_gated": False,
        },
        {
            "name": "aragwas",
            "base_url": aragwas.BASE_URL,
            "kind": "live",
            "subscription_gated": False,
        },
        {
            "name": "onekg",
            "base_url": onekg.BASE_URL,
            "kind": "live",
            "subscription_gated": False,
        },
    ]


def _phytozome_organisms_payload() -> dict[str, int]:
    """Slug → Phytozome organism_id, filtered to organisms Phytozome covers.

    Sourced from ``organisms.ORGANISMS`` — ``phytozome.KNOWN_ORGANISMS``
    was deprecated when the multi-organism registry landed (v0.9 T11).
    """
    return {
        canonical: record.phytozome_int
        for canonical, record in organisms.ORGANISMS.items()
        if record.phytozome_int is not None
    }


def _coverage_matrix_payload() -> str:
    """Markdown table of every organism × every backend ID slot.

    Missing backend slots render as em-dash (—). europe_pmc's sentinel
    contract — ``None`` means "no slug strip needed, locus IDs already
    unambiguous" — renders as ``"None (no strip)"`` so a client can
    distinguish it from a genuine coverage gap.
    """
    lines = [
        "# Organism Coverage Matrix",
        "",
        "| canonical | scientific | ncbi_taxid | ensembl | phytozome | string | europe_pmc | kegg | atted | gprofiler | plantcyc |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for canonical, r in organisms.ORGANISMS.items():
        ensembl = r.ensembl_slug or "—"
        phyto = str(r.phytozome_int) if r.phytozome_int is not None else "—"
        string = str(r.string_taxid) if r.string_taxid is not None else "—"
        epmc = r.europe_pmc_slug if r.europe_pmc_slug is not None else "None (no strip)"
        kegg = r.kegg_org_code or "—"
        atted = r.atted_release or "—"
        gprof = r.gprofiler_id or "—"
        pcyc = r.plantcyc_orgid or "—"
        lines.append(
            f"| {canonical} | {r.scientific} | {r.ncbi_taxid} | "
            f"{ensembl} | {phyto} | {string} | {epmc} | {kegg} | {atted} | {gprof} | {pcyc} |"
        )
    return "\n".join(lines) + "\n"


def _payload_for(uri: str) -> object:
    """Map a resource URI to its serializable Python payload."""
    if uri == CACHE_STATS_URI:
        return _cache_stats_payload()
    if uri == PHYTOZOME_ORGANISMS_URI:
        return _phytozome_organisms_payload()
    if uri == BACKENDS_STATUS_URI:
        return _backends_status_payload()
    if uri == COVERAGE_MATRIX_URI:
        return _coverage_matrix_payload()
    raise ValueError(f"unknown resource URI: {uri}")


async def read_resource(uri: AnyUrl) -> Iterable[ReadResourceContents]:
    """Resolve ``uri`` to a single ``ReadResourceContents`` entry.

    The MCP SDK accepts an iterable of ``ReadResourceContents``; we always
    return exactly one item per resource (no multi-part payloads). JSON
    resources render with sorted keys and 2-space indent so a client can
    diff snapshots over time without spurious key-order churn. The
    coverage-matrix resource is already a rendered markdown string and
    passes through unchanged with ``text/markdown`` mime.
    """
    uri_str = str(uri)
    payload = _payload_for(uri_str)
    if uri_str == COVERAGE_MATRIX_URI:
        # _coverage_matrix_payload() returns rendered markdown; pass through.
        assert isinstance(payload, str)
        return [ReadResourceContents(content=payload, mime_type="text/markdown")]
    text = json.dumps(payload, sort_keys=True, indent=2)
    return [ReadResourceContents(content=text, mime_type="application/json")]
