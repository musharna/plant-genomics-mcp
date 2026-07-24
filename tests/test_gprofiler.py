"""Tests for the g:Profiler g:GOSt over-representation client.

Two tiers (mirrors the quickgo / europe_pmc pattern):
  1. Unit tests with mocked HTTP via pytest-httpx.
  2. Live integration test gated by PLANT_GENOMICS_MCP_LIVE=1.
"""

from __future__ import annotations

import json
import os

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import gprofiler
from plant_genomics_mcp.errors import PlantGenomicsError

LIVE = os.environ.get("PLANT_GENOMICS_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set PLANT_GENOMICS_MCP_LIVE=1 to run")

PROFILE_URL = f"{gprofiler.BASE_URL}{gprofiler.PROFILE_PATH}"


def _term(source: str, native: str, p_value: float, **overrides) -> dict:
    """Synthetic g:Profiler result row shaped like the wire format."""
    base = {
        "source": source,
        "native": native,
        "name": f"term {native}",
        "description": f"description of {native}",
        "p_value": p_value,
        "significant": True,
        "term_size": 100,
        "query_size": 8,
        "intersection_size": 5,
        "precision": 0.625,
        "recall": 0.05,
        "group_id": 1,
        "source_order": 1,
        "goshv": 0.0,
        "effective_domain_size": 20000,
        "parents": [],
        "query": "query_1",
    }
    base.update(overrides)
    return base


def _payload(result: list[dict], *, failed: list | None = None) -> dict:
    """Wrap result rows in the g:Profiler response envelope."""
    return {
        "result": result,
        "meta": {"genes_metadata": {"failed": failed if failed is not None else []}},
    }


# ---------- mocked unit tests ----------


@pytest.mark.asyncio
async def test_go_enrichment_basic(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        method="POST",
        url=PROFILE_URL,
        json=_payload(
            [
                _term("GO:BP", "GO:0007623", 7.0e-16, name="circadian rhythm"),
                _term("KEGG", "KEGG:04712", 1.2e-14, name="Circadian rhythm - plant"),
            ]
        ),
    )
    async with httpx.AsyncClient() as client:
        result = await gprofiler.go_enrichment(client, ["AT2G46830", "AT1G01060"], "arabidopsis")
    assert result["organism"] == "arabidopsis_thaliana"
    assert result["gprofiler_id"] == "athaliana"
    assert result["query_size"] == 2
    assert result["mapped"] == 2
    assert result["unmapped"] == []
    assert result["total_terms"] == 2
    assert result["returned"] == 2
    first = result["enriched"][0]
    # native → term_id projection
    assert first["term_id"] == "GO:0007623"
    assert "native" not in first
    assert first["name"] == "circadian rhythm"
    # plotting-only fields are dropped
    assert "group_id" not in first
    assert "parents" not in first


@pytest.mark.asyncio
async def test_go_enrichment_sorts_by_pvalue_and_caps_top_n(httpx_mock: HTTPXMock) -> None:
    """Rows arrive out of p-order; result is ascending and capped at top_n."""
    httpx_mock.add_response(
        method="POST",
        url=PROFILE_URL,
        json=_payload(
            [
                _term("GO:BP", "GO:AAA", 1.0e-2),
                _term("GO:BP", "GO:BBB", 1.0e-8),
                _term("GO:MF", "GO:CCC", 1.0e-5),
            ]
        ),
    )
    async with httpx.AsyncClient() as client:
        result = await gprofiler.go_enrichment(client, ["AT1G01010"], top_n=2)
    assert result["total_terms"] == 3
    assert result["returned"] == 2
    ids = [t["term_id"] for t in result["enriched"]]
    assert ids == ["GO:BBB", "GO:CCC"], "expected ascending-p order, top 2"


@pytest.mark.asyncio
async def test_go_enrichment_surfaces_unmapped(httpx_mock: HTTPXMock) -> None:
    """genes_metadata.failed loci appear in unmapped[] and shrink mapped."""
    httpx_mock.add_response(
        method="POST",
        url=PROFILE_URL,
        json=_payload([_term("GO:BP", "GO:0007623", 1e-10)], failed=["NOTAREALLOCUS"]),
    )
    async with httpx.AsyncClient() as client:
        result = await gprofiler.go_enrichment(
            client, ["AT2G46830", "NOTAREALLOCUS"], "arabidopsis"
        )
    assert result["query_size"] == 2
    assert result["unmapped"] == ["NOTAREALLOCUS"]
    assert result["mapped"] == 1


@pytest.mark.asyncio
async def test_go_enrichment_defaults_sources(httpx_mock: HTTPXMock) -> None:
    """No sources arg → the four defaults are sent to g:Profiler."""
    httpx_mock.add_response(method="POST", url=PROFILE_URL, json=_payload([]))
    async with httpx.AsyncClient() as client:
        await gprofiler.go_enrichment(client, ["AT1G01010"])
    sent = json.loads(httpx_mock.get_requests()[0].content)
    assert sent["sources"] == ["GO:BP", "GO:MF", "GO:CC", "KEGG"]
    assert sent["organism"] == "athaliana"
    assert sent["query"] == ["AT1G01010"]
    # No custom background → domain_scope is left to the g:Profiler default.
    assert "background" not in sent
    assert "domain_scope" not in sent


@pytest.mark.asyncio
async def test_go_enrichment_custom_background(httpx_mock: HTTPXMock) -> None:
    """A background list sets domain_scope=custom and passes the genes through."""
    httpx_mock.add_response(method="POST", url=PROFILE_URL, json=_payload([]))
    bg = ["AT1G01010", "AT1G01020", "AT1G01030"]
    async with httpx.AsyncClient() as client:
        await gprofiler.go_enrichment(client, ["AT2G46830"], background=bg)
    sent = json.loads(httpx_mock.get_requests()[0].content)
    assert sent["domain_scope"] == "custom"
    assert sent["background"] == bg


@pytest.mark.asyncio
async def test_go_enrichment_subset_sources(httpx_mock: HTTPXMock) -> None:
    """Caller-supplied sources are normalized (upper) and de-duped, order kept."""
    httpx_mock.add_response(method="POST", url=PROFILE_URL, json=_payload([]))
    async with httpx.AsyncClient() as client:
        await gprofiler.go_enrichment(client, ["AT1G01010"], sources=["kegg", "GO:BP", "kegg"])
    sent = json.loads(httpx_mock.get_requests()[0].content)
    assert sent["sources"] == ["KEGG", "GO:BP"]


@pytest.mark.asyncio
async def test_go_enrichment_rejects_bad_source() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="unsupported source"):
            await gprofiler.go_enrichment(client, ["AT1G01010"], sources=["REAC"])


@pytest.mark.asyncio
async def test_go_enrichment_rejects_empty_loci() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="at least one"):
            await gprofiler.go_enrichment(client, ["", "  "])


@pytest.mark.asyncio
async def test_go_enrichment_rejects_bad_threshold() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="user_threshold"):
            await gprofiler.go_enrichment(client, ["AT1G01010"], user_threshold=1.5)


@pytest.mark.asyncio
async def test_go_enrichment_rejects_non_list_loci() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="must be a list"):
            await gprofiler.go_enrichment(client, "AT1G01010")


@pytest.mark.asyncio
async def test_go_enrichment_rejects_oversized_query() -> None:
    big = [f"GENE{i}" for i in range(gprofiler.MAX_QUERY + 1)]
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="the cap is"):
            await gprofiler.go_enrichment(client, big)


@pytest.mark.asyncio
async def test_go_enrichment_empty_sources_falls_back_to_default(httpx_mock: HTTPXMock) -> None:
    """sources=['', '  '] normalizes to empty → the four defaults are used."""
    httpx_mock.add_response(method="POST", url=PROFILE_URL, json=_payload([]))
    async with httpx.AsyncClient() as client:
        await gprofiler.go_enrichment(client, ["AT1G01010"], sources=["", "  "])
    sent = json.loads(httpx_mock.get_requests()[0].content)
    assert sent["sources"] == ["GO:BP", "GO:MF", "GO:CC", "KEGG"]


@pytest.mark.asyncio
async def test_go_enrichment_non_dict_payload_raises(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="POST", url=PROFILE_URL, json=["not", "a", "dict"])
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlantGenomicsError, match="non-dict payload"):
            await gprofiler.go_enrichment(client, ["AT1G01010"])


@pytest.mark.asyncio
async def test_go_enrichment_result_not_list_raises(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="POST", url=PROFILE_URL, json={"result": {"oops": 1}})
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlantGenomicsError, match="'result' is not a list"):
            await gprofiler.go_enrichment(client, ["AT1G01010"])


# ---------- live integration (real-execution check) ----------


@live_only
@pytest.mark.asyncio
async def test_live_circadian_set_enriches_for_circadian_rhythm() -> None:
    """Real g:Profiler call — Arabidopsis clock genes enrich for circadian rhythm."""
    clock = [
        "AT2G46830",  # CCA1
        "AT1G01060",  # LHY
        "AT5G61380",  # TOC1
        "AT5G02810",  # PRR7
        "AT2G46790",  # PRR9
        "AT1G22770",  # GI
        "AT2G25930",  # ELF3
        "AT2G40080",  # ELF4
    ]
    async with httpx.AsyncClient() as client:
        result = await gprofiler.go_enrichment(client, clock, "arabidopsis", top_n=20)
    assert result["mapped"] >= 7
    assert result["total_terms"] > 0
    term_ids = {t["term_id"] for t in result["enriched"]}
    assert "GO:0007623" in term_ids, "circadian rhythm (GO:0007623) expected"


@live_only
@pytest.mark.asyncio
async def test_live_rice_organism_resolves() -> None:
    """Rice maps to g:Profiler osativa and returns without error."""
    async with httpx.AsyncClient() as client:
        result = await gprofiler.go_enrichment(
            client, ["Os03g0718100", "Os01g0100100"], "rice", top_n=10
        )
    assert result["gprofiler_id"] == "osativa"
    assert result["query_size"] == 2
