"""Tests for the in-memory TTL + LRU cache.

Two layers:
  1. Unit tests against ``cache.TTLCache`` directly (lazy expiry, LRU
     eviction, disabled flag, stats counters, key canonicalization).
  2. Integration tests proving the backend modules ACTUALLY consult the
     cache. The integration tests register exactly ONE httpx_mock
     response per URL+params combination and call the wrapped helper
     twice. If the second call bypasses the cache it would either
     attempt to consume a second (unregistered) mock — and pytest-httpx
     would fail the test — or the second mock would go unconsumed,
     which would also fail. So a passing integration test is a real
     mechanism-level check, not just a state assertion.
"""

from __future__ import annotations

import time

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import cache, ensembl_plants, europe_pmc, quickgo

# ---------- TTLCache unit tests ----------


def test_get_miss_returns_none_and_bumps_misses() -> None:
    c = cache.TTLCache()
    assert c.get("nope") is None
    assert c.stats()["misses"] == 1
    assert c.stats()["hits"] == 0


def test_set_then_get_round_trips_value() -> None:
    c = cache.TTLCache()
    c.set("k", {"v": 1})
    assert c.get("k") == {"v": 1}
    assert c.stats()["hits"] == 1
    assert c.stats()["size"] == 1


def test_lazy_expiry_drops_entry_on_read() -> None:
    c = cache.TTLCache(default_ttl=0.05)
    c.set("k", "v")
    time.sleep(0.06)
    assert c.get("k") is None
    # Expired-then-evicted should not still be counted as a stored entry.
    assert c.stats()["size"] == 0
    assert c.stats()["misses"] == 1


def test_per_call_ttl_overrides_default() -> None:
    c = cache.TTLCache(default_ttl=600)
    c.set("short", "v", ttl=0.05)
    c.set("long", "v")
    time.sleep(0.06)
    assert c.get("short") is None
    assert c.get("long") == "v"


def test_lru_eviction_drops_oldest_first() -> None:
    c = cache.TTLCache(max_entries=2)
    c.set("a", 1)
    c.set("b", 2)
    c.set("c", 3)
    assert c.get("a") is None  # evicted
    assert c.get("b") == 2
    assert c.get("c") == 3


def test_get_promotes_to_mru() -> None:
    c = cache.TTLCache(max_entries=2)
    c.set("a", 1)
    c.set("b", 2)
    # Touch 'a' so it becomes most-recently-used.
    assert c.get("a") == 1
    c.set("c", 3)  # should evict 'b', not 'a'
    assert c.get("a") == 1
    assert c.get("b") is None


def test_disabled_flag_makes_cache_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLANT_GENOMICS_MCP_CACHE_DISABLED", "1")
    c = cache.TTLCache()
    c.set("k", "v")
    assert c.get("k") is None
    # Nothing was stored because set() short-circuited under disabled.
    assert c.stats()["size"] == 0


def test_clear_resets_store_and_counters() -> None:
    c = cache.TTLCache()
    c.set("k", "v")
    c.get("k")
    c.clear()
    assert c.stats() == {"hits": 0, "misses": 0, "size": 0}


def test_make_key_is_order_invariant_for_params() -> None:
    k1 = cache.make_key("GET", "https://x", "/p", params={"a": 1, "b": 2})
    k2 = cache.make_key("GET", "https://x", "/p", params={"b": 2, "a": 1})
    assert k1 == k2


def test_make_key_distinguishes_method_and_body() -> None:
    k_get = cache.make_key("GET", "https://x", "/p")
    k_post = cache.make_key("POST", "https://x", "/p")
    k_body = cache.make_key("POST", "https://x", "/p", body={"q": 1})
    assert k_get != k_post != k_body
    # JSON-stable body serialization → dict order doesn't matter.
    k_b1 = cache.make_key("POST", "https://x", "/p", body={"a": 1, "b": 2})
    k_b2 = cache.make_key("POST", "https://x", "/p", body={"b": 2, "a": 1})
    assert k_b1 == k_b2


def test_make_key_param_value_with_separators_no_collision() -> None:
    """audit P6: a param value containing the old hand-join separators must not
    alias a different param set. ``{"x": "1&y=2"}`` and ``{"x": "1", "y": "2"}``
    both serialized to ``x=1&y=2`` under the previous '&'/'=' join."""
    k_one = cache.make_key("GET", "https://x", "/p", params={"x": "1&y=2"})
    k_two = cache.make_key("GET", "https://x", "/p", params={"x": "1", "y": "2"})
    assert k_one != k_two


def test_get_returns_isolated_copy_not_shared_reference() -> None:
    """audit P5: mutating a value returned by get (or the value passed to set)
    must not corrupt the cached entry for the next reader."""
    c = cache.TTLCache()
    original = {"nested": {"k": "v"}, "items": [1, 2]}
    c.set("key", original)

    # Mutating the object we handed to set must not reach into the cache.
    original["nested"]["k"] = "MUTATED"
    original["items"].append(99)
    assert c.get("key") == {"nested": {"k": "v"}, "items": [1, 2]}

    # Mutating a returned value must not affect a subsequent get.
    first = c.get("key")
    first["nested"]["k"] = "ALSO_MUTATED"
    first["items"].append(7)
    assert c.get("key") == {"nested": {"k": "v"}, "items": [1, 2]}


# ---------- backend integration: cache actually short-circuits HTTP ----------


@pytest.mark.asyncio
async def test_ensembl_get_caches_second_call(httpx_mock: HTTPXMock) -> None:
    """Two identical calls; only ONE mock registered. If the second call
    bypasses the cache it would fail (no mock) — and if the cache were
    never consulted the single mock would be consumed once and the
    second call would fail. So a passing test is real mechanism-level
    evidence.
    """
    httpx_mock.add_response(
        url="https://rest.ensembl.org/lookup/id/AT1G01010?species=arabidopsis_thaliana&expand=0",
        json={"id": "AT1G01010", "display_name": "NAC001"},
    )
    async with httpx.AsyncClient() as client:
        a = await ensembl_plants.lookup_locus(client, "AT1G01010")
        b = await ensembl_plants.lookup_locus(client, "AT1G01010")
    assert a == b
    assert ensembl_plants._CACHE.stats()["hits"] >= 1


@pytest.mark.asyncio
async def test_europe_pmc_get_caches_second_call(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=AT1G01010&format=json&resultType=core&pageSize=10",
        json={"hitCount": 1, "resultList": {"result": [{"id": "PMC1", "pmcid": "PMC1"}]}},
    )
    async with httpx.AsyncClient() as client:
        a = await europe_pmc.lookup_locus(client, "AT1G01010")
        b = await europe_pmc.lookup_locus(client, "AT1G01010")
    assert a == b
    assert europe_pmc._CACHE.stats()["hits"] >= 1


@pytest.mark.asyncio
async def test_quickgo_get_caches_second_call(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://www.ebi.ac.uk/QuickGO/services/annotation/search?geneProductId=Q0WV96&limit=50&includeFields=goName%2CtaxonName",
        json={"numberOfHits": 0, "results": []},
    )
    async with httpx.AsyncClient() as client:
        a = await quickgo.lookup_by_uniprot(client, "Q0WV96")
        b = await quickgo.lookup_by_uniprot(client, "Q0WV96")
    assert a == b
    assert quickgo._CACHE.stats()["hits"] >= 1


@pytest.mark.asyncio
async def test_disabled_cache_forces_second_http_call(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the disabled flag set, two identical calls must consume two
    separate mocks. Register exactly two responses.
    """
    monkeypatch.setenv("PLANT_GENOMICS_MCP_CACHE_DISABLED", "1")
    url = "https://rest.ensembl.org/lookup/id/AT1G01010?species=arabidopsis_thaliana&expand=0"
    payload = {"id": "AT1G01010", "display_name": "NAC001"}
    httpx_mock.add_response(url=url, json=payload)
    httpx_mock.add_response(url=url, json=payload)
    async with httpx.AsyncClient() as client:
        await ensembl_plants.lookup_locus(client, "AT1G01010")
        await ensembl_plants.lookup_locus(client, "AT1G01010")
    # Both mocks consumed → pytest-httpx teardown would otherwise fail.
