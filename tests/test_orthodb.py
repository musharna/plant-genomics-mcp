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
    # Was `== 1`, which pinned member_count to the number of rows RETURNED and
    # so encoded the bug: under a cap the count could never exceed the list, and
    # a caller could not tell how many orthologs actually existed. The fixture
    # holds 2 members, so the true pre-cap total is 2 while 1 row comes back.
    assert r["member_count"] == 2
    assert len(r["members"]) == 1


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


# --- member_count must describe the DATA, not the returned list -------------
# It previously reported len(members), i.e. the number of rows RETURNED. With
# the cap engaged a caller saw `member_count: 100, truncated: true` and could
# not tell whether 101 or 10,000 orthologs existed — the count agreed with the
# list instead of describing the data. Same class as the lying counts the
# 2026-07-25 semantic audit fixed three of.


def _clusters(n_orgs: int, genes_per_org: int) -> list[dict[str, object]]:
    return [
        {
            "organism": {"name": f"org{o}"},
            "genes": [
                {"gene_id": {"id": f"g{o}_{i}", "param": "x"}, "description": "d"}
                for i in range(genes_per_org)
            ],
        }
        for o in range(n_orgs)
    ]


def test_member_count_is_the_true_total_not_the_row_count() -> None:
    rows, total = orthodb._members(_clusters(10, 30), orthodb.MAX_MEMBERS)
    assert total == 300, "count must describe the data, not the truncated list"
    assert len(rows) == orthodb.MAX_MEMBERS
    assert total > len(rows)


def test_members_counts_everything_past_the_cap() -> None:
    """The loop must keep counting after the cap, or the total is just the cap."""
    _, total = orthodb._members(_clusters(1, orthodb.MAX_MEMBERS + 57), orthodb.MAX_MEMBERS)
    assert total == orthodb.MAX_MEMBERS + 57


def test_members_untruncated_reports_equal_counts() -> None:
    """Positive control: total == rows when nothing is capped."""
    rows, total = orthodb._members(_clusters(2, 3), orthodb.MAX_MEMBERS)
    assert total == 6 and len(rows) == 6


def test_orthodb_limit_is_clamped() -> None:
    assert orthodb._resolve_limit(None) == orthodb.MAX_MEMBERS
    assert orthodb._resolve_limit(0) == 1
    assert orthodb._resolve_limit(10_000) == orthodb.MAX_MEMBERS
