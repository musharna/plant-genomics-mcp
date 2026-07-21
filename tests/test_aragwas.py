"""Tests for the AraGWAS association backend (Arabidopsis-only).

Two tiers:
  1. Unit tests with mocked HTTP via pytest-httpx (pagination + projection +
     organism gating).
  2. Live integration tests gated by PLANT_GENOMICS_MCP_LIVE=1.
"""

from __future__ import annotations

import os

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import aragwas
from plant_genomics_mcp.errors import OrganismNotSupported, PlantGenomicsError

LIVE = os.environ.get("PLANT_GENOMICS_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set PLANT_GENOMICS_MCP_LIVE=1 to run")

_URL = f"{aragwas.BASE_URL}/api/genes/AT1G01060/associations/"
_NEXT = f"{_URL}?limit=25&offset=25"

# Real-shaped association (key names verified live 2026-07-20, AT1G01060).
_ASSOC = {
    "score": 30.386,
    "maf": 0.00199,
    "mac": 1,
    "overBonferroni": True,
    "overFDR": True,
    "overPermutation": True,
    "snp": {
        "chr": "chr1",
        "position": 35574,
        "ref": "G",
        "anc": "G",
        "alt": "A",
        "coding": True,
        "geneName": "AT1G01060",
        "annotations": [
            {
                "function": "MISSENSE",
                "geneName": "AT9G00000",  # non-matching first entry
                "impact": "LOW",
                "transcriptId": "AT9G00000.1",
                "aminoAcidChange": "X0X",
                "effect": "SOMETHING_ELSE",
            },
            {
                "function": "MISSENSE",
                "geneName": "AT1G01060",  # matching entry — should win
                "impact": "MODERATE",
                "transcriptId": "AT1G01060.5",
                "aminoAcidChange": "P172L",
                "effect": "NON_SYNONYMOUS_CODING",
            },
        ],
    },
    "study": {
        "name": "clim-pet12_raw_amm",
        "method": "amm",
        "phenotype": {
            "name": "clim-pet12",
            "description": "Potential evapotranspiration of December (mm)",
        },
    },
}


@pytest.mark.asyncio
async def test_lookup_full_matches_gene_annotation(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=_URL, json={"count": 1, "links": {"next": None}, "results": [_ASSOC]}
    )
    async with httpx.AsyncClient() as client:
        r = await aragwas.lookup_locus(client, "AT1G01060", "arabidopsis")
    assert r["found"] is True
    assert r["organism"] == "arabidopsis_thaliana"
    assert r["association_count"] == 1
    assert r["truncated"] is False
    a = r["associations"][0]
    assert a["score"] == 30.386
    assert a["over_bonferroni"] is True
    assert a["snp"]["gene"] == "AT1G01060"
    assert a["snp"]["position"] == 35574
    # the annotation matching this gene wins over the first (non-matching) entry
    assert a["snp"]["effect"] == "NON_SYNONYMOUS_CODING"
    assert a["snp"]["amino_acid_change"] == "P172L"
    assert a["study"]["phenotype"] == "clim-pet12"


@pytest.mark.asyncio
async def test_lookup_paginates(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=_URL, json={"count": 2, "links": {"next": _NEXT}, "results": [_ASSOC]}
    )
    httpx_mock.add_response(
        url=_NEXT, json={"count": 2, "links": {"next": None}, "results": [_ASSOC]}
    )
    async with httpx.AsyncClient() as client:
        r = await aragwas.lookup_locus(client, "AT1G01060", "arabidopsis")
    assert r["association_count"] == 2
    assert r["returned"] == 2
    assert r["truncated"] is False


@pytest.mark.asyncio
async def test_lookup_page_cap_truncates(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(aragwas, "MAX_PAGES", 1)
    httpx_mock.add_response(
        url=_URL, json={"count": 200, "links": {"next": _NEXT}, "results": [_ASSOC]}
    )
    async with httpx.AsyncClient() as client:
        r = await aragwas.lookup_locus(client, "AT1G01060", "arabidopsis")
    assert r["association_count"] == 200
    assert r["returned"] == 1
    assert r["truncated"] is True


@pytest.mark.asyncio
async def test_lookup_empty_is_found_true(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=_URL, json={"count": 0, "links": {"next": None}, "results": []})
    async with httpx.AsyncClient() as client:
        r = await aragwas.lookup_locus(client, "AT1G01060", "arabidopsis")
    assert r["found"] is True
    assert r["associations"] == []
    assert r["association_count"] == 0


@pytest.mark.asyncio
async def test_lookup_annotation_fallback_and_empty(httpx_mock: HTTPXMock) -> None:
    """No gene-matching annotation → first; no annotations → null effect fields."""
    no_match = {**_ASSOC, "snp": {**_ASSOC["snp"], "geneName": "AT1G01060"}}
    no_match["snp"] = {**no_match["snp"], "annotations": [{"geneName": "OTHER", "effect": "E1"}]}
    no_ann = {**_ASSOC, "snp": {"chr": "chr1", "position": 1, "annotations": []}}
    httpx_mock.add_response(
        url=_URL, json={"count": 2, "links": {"next": None}, "results": [no_match, no_ann]}
    )
    async with httpx.AsyncClient() as client:
        r = await aragwas.lookup_locus(client, "AT1G01060", "arabidopsis")
    assert r["associations"][0]["snp"]["effect"] == "E1"  # fell back to first annotation
    assert r["associations"][1]["snp"]["effect"] is None  # no annotations at all


@pytest.mark.asyncio
async def test_lookup_malformed_raises(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=_URL, json=["unexpected", "list"])
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlantGenomicsError, match="unexpected payload"):
            await aragwas.lookup_locus(client, "AT1G01060", "arabidopsis")


@pytest.mark.asyncio
async def test_lookup_non_arabidopsis_raises() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(OrganismNotSupported):
            await aragwas.lookup_locus(client, "Os01g0100100", "rice")


@live_only
@pytest.mark.asyncio
async def test_live_arabidopsis_associations() -> None:
    """Real AraGWAS call — AT1G01060 has GWAS associations."""
    async with httpx.AsyncClient() as client:
        r = await aragwas.lookup_locus(client, "AT1G01060", "arabidopsis")
    assert r["found"] is True
    assert r["association_count"] > 0
    assert r["associations"][0]["snp"]["position"]
