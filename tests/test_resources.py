"""Tests for the read-only MCP resources surface (P2.17)."""

from __future__ import annotations

import json

import pytest
from pydantic import AnyUrl

from plant_genomics_mcp import organisms, plantcyc, resources, tair


def test_resources_catalog_has_four_entries() -> None:
    uris = {str(r.uri) for r in resources.RESOURCES}
    assert uris == {
        resources.CACHE_STATS_URI,
        resources.PHYTOZOME_ORGANISMS_URI,
        resources.BACKENDS_STATUS_URI,
        resources.COVERAGE_MATRIX_URI,
    }


def test_resources_catalog_all_named_with_known_mime() -> None:
    """Every resource carries a name + description; mime is JSON or markdown."""
    for r in resources.RESOURCES:
        assert r.mimeType in ("application/json", "text/markdown")
        assert r.name
        assert r.description


@pytest.mark.asyncio
async def test_read_cache_stats_returns_per_backend_rollup() -> None:
    out = list(await resources.read_resource(AnyUrl(resources.CACHE_STATS_URI)))
    assert len(out) == 1
    item = out[0]
    assert item.mime_type == "application/json"
    payload = json.loads(item.content)
    assert set(payload) == {
        "atted",
        "bar",
        "ensembl_plants",
        "europe_pmc",
        "gramene",
        "kegg",
        "phytozome",
        "quickgo",
        "string_db",
        "uniprot",
    }
    # Each per-backend block carries the canonical TTLCache stats shape.
    for stats in payload.values():
        assert set(stats) == {"hits", "misses", "size"}
        for v in stats.values():
            assert isinstance(v, int)


@pytest.mark.asyncio
async def test_read_phytozome_organisms_matches_organisms_registry() -> None:
    """Phytozome resource derives its slug→int map from organisms.ORGANISMS.

    The legacy phytozome.KNOWN_ORGANISMS module-level dict was deprecated
    in T11 when the multi-organism registry landed; the resource now
    filters ORGANISMS to records with a non-None phytozome_int.
    """
    from plant_genomics_mcp import organisms

    out = list(await resources.read_resource(AnyUrl(resources.PHYTOZOME_ORGANISMS_URI)))
    payload = json.loads(out[0].content)
    expected = {
        canonical: r.phytozome_int
        for canonical, r in organisms.ORGANISMS.items()
        if r.phytozome_int is not None
    }
    assert payload == expected
    assert payload["arabidopsis_thaliana"] == 167


@pytest.mark.asyncio
async def test_read_coverage_matrix_lists_all_organisms() -> None:
    """Markdown coverage matrix mentions every organism in ORGANISMS."""
    from plant_genomics_mcp import organisms

    out = list(await resources.read_resource(AnyUrl(resources.COVERAGE_MATRIX_URI)))
    assert len(out) == 1
    item = out[0]
    assert item.mime_type == "text/markdown"
    body = item.content
    for canonical in organisms.ORGANISMS:
        assert canonical in body, f"coverage matrix missing {canonical}"
    # Header + key column names so a client can parse it.
    assert "canonical" in body
    assert "phytozome" in body
    assert "europe_pmc" in body


@pytest.mark.asyncio
async def test_coverage_matrix_renders_missing_slots_as_dash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backends with no ID for an organism render as `—`, not 'None' or empty.

    Wave A2 (2026-05-23) populated every phytozome_int / ensembl_slug /
    string_taxid in the live registry, so we shadow one record with a
    None phytozome_int to exercise the dash-rendering contract.
    """
    from dataclasses import replace

    record = organisms.ORGANISMS["vitis_vinifera"]
    shadowed = dict(organisms.ORGANISMS)
    shadowed["vitis_vinifera"] = replace(record, phytozome_int=None)
    monkeypatch.setattr(organisms, "ORGANISMS", shadowed)

    out = list(await resources.read_resource(AnyUrl(resources.COVERAGE_MATRIX_URI)))
    body = out[0].content
    grape_row = next(line for line in body.splitlines() if line.startswith("| vitis_vinifera "))
    assert "—" in grape_row
    # The europe_pmc "None (no strip)" sentinel is allowed; bare "None" is not.
    cleaned = grape_row.replace("None (no strip)", "")
    assert "None" not in cleaned


@pytest.mark.asyncio
async def test_read_backends_status_lists_live_and_stub_backends() -> None:
    out = list(await resources.read_resource(AnyUrl(resources.BACKENDS_STATUS_URI)))
    payload = json.loads(out[0].content)
    by_name = {entry["name"]: entry for entry in payload}
    # Live backends.
    for name in (
        "atted",
        "bar",
        "ensembl_plants",
        "phytozome",
        "uniprot",
        "europe_pmc",
        "quickgo",
        "gramene",
        "kegg",
        "string_db",
    ):
        e = by_name[name]
        assert e["kind"] == "live"
        assert e["subscription_gated"] is False
        assert e["base_url"].startswith("http")
    # Stub backends carry probed_at + subscription_gated=True.
    for name, mod in [("tair", tair), ("plantcyc", plantcyc)]:
        e = by_name[name]
        assert e["kind"] == "stub"
        assert e["subscription_gated"] is True
        assert e["probed_at"] == mod._PROBED_AT


@pytest.mark.asyncio
async def test_read_unknown_uri_raises() -> None:
    with pytest.raises(ValueError, match="unknown resource URI"):
        await resources.read_resource(AnyUrl("pgmcp://does/not/exist"))


@pytest.mark.asyncio
async def test_cache_stats_reflect_live_counters(monkeypatch: pytest.MonkeyPatch) -> None:
    """If a backend cache records hits/misses, the resource reflects it.

    Real-execution check at the system boundary — proves we're reading
    the actual ``_CACHE.stats()`` and not a stale snapshot.
    """
    from plant_genomics_mcp import ensembl_plants

    before = ensembl_plants._CACHE.stats()
    ensembl_plants._CACHE.set("test-resource-key", {"x": 1})
    ensembl_plants._CACHE.get("test-resource-key")
    ensembl_plants._CACHE.get("missing-key")
    try:
        out = list(await resources.read_resource(AnyUrl(resources.CACHE_STATS_URI)))
        payload = json.loads(out[0].content)
        assert payload["ensembl_plants"]["hits"] == before["hits"] + 1
        assert payload["ensembl_plants"]["misses"] == before["misses"] + 1
    finally:
        ensembl_plants._CACHE.clear()
