"""Tests for the OrthoDB orthology backend.

Two tiers:
  1. Unit tests with mocked HTTP via pytest-httpx across the search → group →
     orthologs three-hop flow.
  2. Live integration tests gated by PLANT_GENOMICS_MCP_LIVE=1.
"""

from __future__ import annotations

import os

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import orthodb
from plant_genomics_mcp.errors import PlantGenomicsError

LIVE = os.environ.get("PLANT_GENOMICS_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set PLANT_GENOMICS_MCP_LIVE=1 to run")

_GID = "444580at33090"
_SEARCH_URL = f"{orthodb.BASE_URL}/current/search?query=AT1G01060&level=33090&limit=1"
_GROUP_URL = f"{orthodb.BASE_URL}/current/group?id={_GID}"
_ORTHO_URL = f"{orthodb.BASE_URL}/current/orthologs?id={_GID}"

# Real-shaped group + orthologs payloads (key names verified live 2026-07-20).
_GROUP = {
    "data": {
        "id": _GID,
        "public_id": _GID,
        "name": "LHY protein",
        "evolutionary_rate": 1.451,
        "level_name": "Viridiplantae",
        "tax_id": 33090,
    }
}
_ORTHO = {
    "status": "ok",
    "data": [
        "unexpected-non-dict-cluster",
        {
            "organism": {"name": "Abrus precatorius"},
            "genes": [
                {
                    "gene_id": {"id": "113863481", "param": "3816_0:0021f1"},
                    "description": "LHY protein",
                },
                "unexpected-non-dict-gene",
            ],
        },
        {
            "organism": {"name": "Arabidopsis thaliana"},
            "genes": [
                {"gene_id": {"id": "AT1G01060", "param": "3702_0:004abc"}, "description": "LHY"}
            ],
        },
    ],
}


@pytest.mark.asyncio
async def test_lookup_full(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=_SEARCH_URL, json={"count": "1", "data": [_GID]})
    httpx_mock.add_response(url=_GROUP_URL, json=_GROUP)
    httpx_mock.add_response(url=_ORTHO_URL, json=_ORTHO)
    async with httpx.AsyncClient() as client:
        r = await orthodb.lookup_locus(client, "AT1G01060", "arabidopsis")
    assert r["found"] is True
    assert r["organism"] == "arabidopsis_thaliana"
    assert r["group"]["name"] == "LHY protein"
    assert r["group"]["evolutionary_rate"] == 1.451
    assert r["group"]["level_name"] == "Viridiplantae"
    assert r["organism_count"] == 3  # includes the non-dict cluster in the raw total
    assert r["member_count"] == 2  # two valid genes; non-dict gene skipped
    assert r["truncated"] is False
    assert r["members"][0]["organism"] == "Abrus precatorius"
    assert r["members"][0]["gene_id"] == "113863481"
    assert r["members"][1]["organism"] == "Arabidopsis thaliana"


@pytest.mark.asyncio
async def test_lookup_no_group_is_found_false(httpx_mock: HTTPXMock) -> None:
    """Empty search result → found=False, no group/orthologs calls."""
    httpx_mock.add_response(url=_SEARCH_URL, json={"count": "0", "data": []})
    async with httpx.AsyncClient() as client:
        r = await orthodb.lookup_locus(client, "AT1G01060", "arabidopsis")
    assert r["found"] is False
    assert r["group"] is None
    assert r["members"] == []
    assert r["organism_count"] == 0


@pytest.mark.asyncio
async def test_lookup_truncates(httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orthodb, "MAX_MEMBERS", 1)
    httpx_mock.add_response(url=_SEARCH_URL, json={"count": "1", "data": [_GID]})
    httpx_mock.add_response(url=_GROUP_URL, json=_GROUP)
    httpx_mock.add_response(url=_ORTHO_URL, json=_ORTHO)
    async with httpx.AsyncClient() as client:
        r = await orthodb.lookup_locus(client, "AT1G01060", "arabidopsis")
    assert r["truncated"] is True
    assert r["member_count"] == 1


@pytest.mark.asyncio
async def test_lookup_malformed_raises(httpx_mock: HTTPXMock) -> None:
    """A 200 whose body is not a JSON object → typed PlantGenomicsError."""
    httpx_mock.add_response(url=_SEARCH_URL, json=["unexpected", "list"])
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlantGenomicsError, match="unexpected payload"):
            await orthodb.lookup_locus(client, "AT1G01060", "arabidopsis")


@pytest.mark.asyncio
async def test_lookup_orthologs_non_list_data(httpx_mock: HTTPXMock) -> None:
    """orthologs data that isn't a list → zero members, still found=True."""
    httpx_mock.add_response(url=_SEARCH_URL, json={"count": "1", "data": [_GID]})
    httpx_mock.add_response(url=_GROUP_URL, json=_GROUP)
    httpx_mock.add_response(url=_ORTHO_URL, json={"status": "ok", "data": None})
    async with httpx.AsyncClient() as client:
        r = await orthodb.lookup_locus(client, "AT1G01060", "arabidopsis")
    assert r["found"] is True
    assert r["organism_count"] == 0
    assert r["members"] == []


@live_only
@pytest.mark.asyncio
async def test_live_arabidopsis_orthologs() -> None:
    """Real OrthoDB call — AT1G01060 maps to a Viridiplantae ortholog group."""
    async with httpx.AsyncClient() as client:
        r = await orthodb.lookup_locus(client, "AT1G01060", "arabidopsis")
    assert r["found"] is True
    assert r["group"]["id"]
    assert r["organism_count"] > 0
