"""Tests for the read-only MCP resources surface (P2.17)."""

from __future__ import annotations

import json

import pytest
from pydantic import AnyUrl

from plant_genomics_mcp import phytozome, plantcyc, resources, tair


def test_resources_catalog_has_three_entries() -> None:
    uris = {str(r.uri) for r in resources.RESOURCES}
    assert uris == {
        resources.CACHE_STATS_URI,
        resources.PHYTOZOME_ORGANISMS_URI,
        resources.BACKENDS_STATUS_URI,
    }


def test_resources_catalog_all_json_mime() -> None:
    for r in resources.RESOURCES:
        assert r.mimeType == "application/json"
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
async def test_read_phytozome_organisms_matches_known_organisms_dict() -> None:
    out = list(await resources.read_resource(AnyUrl(resources.PHYTOZOME_ORGANISMS_URI)))
    payload = json.loads(out[0].content)
    assert payload == phytozome.KNOWN_ORGANISMS
    # Spot-check the only verified entry.
    assert payload["arabidopsis_thaliana"] == 167


@pytest.mark.asyncio
async def test_read_backends_status_lists_live_and_stub_backends() -> None:
    out = list(await resources.read_resource(AnyUrl(resources.BACKENDS_STATUS_URI)))
    payload = json.loads(out[0].content)
    by_name = {entry["name"]: entry for entry in payload}
    # Live backends.
    for name in (
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
