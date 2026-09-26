"""Tests for the OrthoDB orthology backend.

Two tiers:
  1. Unit tests with mocked HTTP via pytest-httpx across the search → group →
     orthologs three-hop flow.
  2. Live integration tests gated by PLANT_GENOMICS_MCP_LIVE=1.
"""

from __future__ import annotations

import asyncio
import os

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import _http, orthodb
from plant_genomics_mcp.errors import PlantGenomicsError

LIVE = os.environ.get("PLANT_GENOMICS_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set PLANT_GENOMICS_MCP_LIVE=1 to run")

_GID = "444580at33090"
_SEARCH_URL = f"{orthodb.BASE_URL}/v12/search?query=AT1G01060&level=33090&limit=1"
_GROUP_URL = f"{orthodb.BASE_URL}/v12/group?id={_GID}"
_ORTHO_URL = f"{orthodb.BASE_URL}/v12/orthologs?id={_GID}"

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
    # The release every request named is the release reported (#121 contract).
    assert r["upstream_version"] == orthodb.ORTHODB_RELEASE == "v12"
    assert {req.url.path.split("/")[1] for req in httpx_mock.get_requests()} == {"v12"}


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
    # A not-found answer came from the pinned release too.
    assert r["upstream_version"] == "v12"


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
    # #123: the schema said member_count was "returned (post-cap)" and COUNT_SPECS
    # agreed, while the code above reports the pre-cap total. Run the three-way
    # check on this capped payload, which a live sweep only sees by chance.
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from semantic_invariants import COUNT_SPECS, Verdict, check_count_semantics

    from plant_genomics_mcp.models import OrthoDbOrthologs

    props = OrthoDbOrthologs.model_json_schema()["properties"]
    specs = [s for s in COUNT_SPECS if s.tool == "orthodb_orthologs" and s.list_field]
    results = [check_count_semantics(s, r, props[s.field]["description"]) for s in specs]
    assert {s.field for s in specs} == {"member_count", "total", "returned"}
    assert all(x.verdict is Verdict.PASS for x in results), [x.detail for x in results]


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


@live_only
@pytest.mark.asyncio
async def test_live_the_pinned_release_selects_the_data_and_an_unknown_one_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pin is only provenance if it changes what answers (probed 2026-09-26).

    /v11/ and /v12/ answer the same search with different group ids, so the
    reported release follows the data; /v99/ is refused, not served as current.
    """
    async with httpx.AsyncClient() as client:
        v12 = await orthodb.lookup_locus(client, "AT1G01010", "arabidopsis", limit=1)
        monkeypatch.setattr(orthodb, "ORTHODB_RELEASE", "v11")
        v11 = await orthodb.lookup_locus(client, "AT1G01010", "arabidopsis", limit=1)
        monkeypatch.setattr(orthodb, "ORTHODB_RELEASE", "v99")
        with pytest.raises(PlantGenomicsError) as refused:
            await orthodb.lookup_locus(client, "AT1G01010", "arabidopsis", limit=1)
    assert (v12["found"], v12["upstream_version"]) == (True, "v12"), v12
    assert (v11["found"], v11["upstream_version"]) == (True, "v11"), v11
    assert v12["group"]["id"] != v11["group"]["id"], (v12["group"], v11["group"])
    assert "302" in str(refused.value), refused.value


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


# --- target_organism (#125) ---------------------------------------------------
# Members come back ordered by organism name, so a cap of 100 over a
# 1,986-member Viridiplantae group ends at 'Lupinus albus' and never reaches
# Oryza or Triticum. ``target_organism`` filters the clusters BEFORE the cap.


def _ortho_with(organisms_and_genes: list[tuple[str, int]]) -> dict:
    data = []
    n = 0
    for org, k in organisms_and_genes:
        genes = []
        for _ in range(k):
            n += 1
            genes.append({"gene_id": {"id": str(n), "param": f"g{n}"}, "description": org})
        data.append({"organism": {"name": org}, "genes": genes})
    return {"status": "ok", "data": data}


@pytest.mark.asyncio
async def test_target_organism_filters_members_before_the_cap(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orthodb, "MAX_MEMBERS", 3)
    ortho = _ortho_with([("Abrus precatorius", 2), ("Lupinus albus", 2), ("Oryza sativa", 2)])
    httpx_mock.add_response(url=_SEARCH_URL, json={"count": "1", "data": [_GID]})
    httpx_mock.add_response(url=_GROUP_URL, json=_GROUP)
    httpx_mock.add_response(url=_ORTHO_URL, json=ortho)
    async with httpx.AsyncClient() as client:
        r = await orthodb.lookup_locus(client, "AT1G01060", "arabidopsis", target_organism="rice")
    assert [m["organism"] for m in r["members"]] == ["Oryza sativa", "Oryza sativa"]
    assert r["target_organism"] == "oryza_sativa"
    assert r["member_count"] == 2 and r["member_count_all_organisms"] == 6
    assert r["organism_count"] == 3  # the group's true organism total is unchanged
    assert r["truncated"] is False
    # Positive control: without the filter the cap of 3 stops inside Lupinus.
    orthodb._CACHE.clear()
    httpx_mock.add_response(url=_SEARCH_URL, json={"count": "1", "data": [_GID]})
    httpx_mock.add_response(url=_GROUP_URL, json=_GROUP)
    httpx_mock.add_response(url=_ORTHO_URL, json=ortho)
    async with httpx.AsyncClient() as client:
        r0 = await orthodb.lookup_locus(client, "AT1G01060", "arabidopsis")
    assert [m["organism"] for m in r0["members"]] == [
        "Abrus precatorius",
        "Abrus precatorius",
        "Lupinus albus",
    ]
    assert r0["truncated"] is True and "target_organism" not in r0


def test_organism_name_match_is_prefix_and_case_insensitive() -> None:
    assert orthodb._organism_matches("Oryza sativa Japonica Group", "Oryza sativa")
    assert orthodb._organism_matches("oryza sativa", "Oryza sativa")
    assert not orthodb._organism_matches("Oryza brachyantha", "Oryza sativa")
    assert not orthodb._organism_matches(None, "Oryza sativa")


def _crowd_refusing_transport(refused: list[str]) -> httpx.MockTransport:
    """OrthoDB as probed live (2026-09-23): any request arriving while another
    is in flight gets the 403 refusal page; alone, every request answers."""
    in_flight = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight
        in_flight += 1
        try:
            crowded = in_flight > 1
            await asyncio.sleep(0.01)
            if crowded:
                refused.append(request.url.path)
                return httpx.Response(
                    403, text="<html>Your query was rejected.<li> too high request rate</li>"
                )
            path = request.url.path
            if path.endswith("/search"):
                return httpx.Response(200, json={"count": "1", "data": [_GID]})
            if path.endswith("/group"):
                return httpx.Response(200, json=_GROUP)
            return httpx.Response(200, json=_ORTHO)
        finally:
            in_flight -= 1

    return httpx.MockTransport(handler)


async def _eight_lookups() -> tuple[int, list[str]]:
    orthodb._CACHE.clear()
    refused: list[str] = []
    loci = [f"AT1G0{i}060" for i in range(1, 9)]
    async with httpx.AsyncClient(transport=_crowd_refusing_transport(refused)) as client:
        out = await asyncio.gather(
            *(orthodb.lookup_locus(client, locus, "arabidopsis") for locus in loci),
            return_exceptions=True,
        )
    return sum(1 for r in out if isinstance(r, dict) and r["found"]), refused


@pytest.mark.asyncio
async def test_eight_parallel_lookups_stay_under_orthodb_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #153: batch_locus_call ran eight OrthoDB lookups at once and
    OrthoDB refused the excess. The client's own limit keeps it to one."""

    answered, refused = await _eight_lookups()
    assert (answered, refused) == (8, [])

    # Positive control: the same eight with the limit widened to eight are
    # refused by this transport, so the pass above is the limit's doing.
    monkeypatch.setattr(orthodb, "_LIMIT", _http.UpstreamLimit(8))
    monkeypatch.setattr(orthodb, "MAX_RETRIES", 1)
    answered, refused = await _eight_lookups()
    assert answered < 8 and refused
