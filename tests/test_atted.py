"""ATTED-II coexpression backend unit tests."""

from __future__ import annotations

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import atted
from plant_genomics_mcp.errors import NotFoundError


@pytest.fixture(autouse=True)
def _clear_cache():
    atted._CACHE.clear()
    yield
    atted._CACHE.clear()


@pytest.mark.asyncio
async def test_lookup_coexpression_happy(httpx_mock: HTTPXMock):
    # Live shape (probed 2026-05-21 against ATTED-II API v5,
    # see /tmp/p3_probes_2026-05-21.txt and https://atted.jp/static/help/API.shtml):
    #   GET /api5/?gene={locus}&topN={n}&db=Ath-u.c4-0
    # Response: {request: {...}, result_set: [{entrez_gene_id, type: "z",
    #   results: [{gene: int, other_id: [locus_str], z: float}, ...],
    #   other_id: locus_str}]}
    httpx_mock.add_response(
        url="https://atted.jp/api5/?gene=AT1G01010&topN=5&db=Ath-u.c4-0",
        json={
            "request": {
                "query_id": "AT1G01010",
                "id_type": "agi",
                "entrez_gene_id": [839580],
                "value": "AT1G01010",
                "topN": 5,
                "database": "Ath-u",
                "database_version": "c4-0",
            },
            "result_set": [
                {
                    "entrez_gene_id": 839580,
                    "type": "z",
                    "results": [
                        {"gene": 842367, "other_id": ["At4g36990"], "z": 4.58},
                        {"gene": 838288, "other_id": ["At2g46270"], "z": 4.28},
                    ],
                    "other_id": "At1g01010",
                }
            ],
        },
    )
    async with httpx.AsyncClient() as client:
        result = await atted.lookup_coexpression(client, "AT1G01010", top_n=5)
    assert result["locus"] == "AT1G01010"
    assert result["atted_release"] == "Ath-u.c4-0"
    assert len(result["neighbors"]) == 2
    n0 = result["neighbors"][0]
    assert n0["locus"] == "At4g36990"
    assert n0["entrez_gene_id"] == 842367
    assert n0["z_score"] == 4.58


@pytest.mark.asyncio
async def test_lookup_coexpression_empty_array_raises_not_found(httpx_mock: HTTPXMock):
    # ATTED-II API v5 returns a result_set with an empty results array when
    # no neighbors exist for the query gene; we treat that as NotFound.
    httpx_mock.add_response(
        url="https://atted.jp/api5/?gene=ATNOPE&topN=25&db=Ath-u.c4-0",
        json={
            "request": {"query_id": "ATNOPE"},
            "result_set": [
                {
                    "entrez_gene_id": 0,
                    "type": "z",
                    "results": [],
                    "other_id": "ATNOPE",
                }
            ],
        },
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError):
            await atted.lookup_coexpression(client, "ATNOPE")


@pytest.mark.asyncio
async def test_lookup_coexpression_top_n_capped(httpx_mock: HTTPXMock):
    # We pass 9999, expect the call to use 300 (MAX_TOP_N).
    httpx_mock.add_response(
        url="https://atted.jp/api5/?gene=AT1G01010&topN=300&db=Ath-u.c4-0",
        json={
            "request": {"query_id": "AT1G01010", "topN": 300},
            "result_set": [
                {
                    "entrez_gene_id": 839580,
                    "type": "z",
                    "results": [{"gene": 842367, "other_id": ["At4g36990"], "z": 4.58}],
                    "other_id": "At1g01010",
                }
            ],
        },
    )
    async with httpx.AsyncClient() as client:
        result = await atted.lookup_coexpression(client, "AT1G01010", top_n=9999)
    assert len(result["neighbors"]) == 1


@pytest.mark.asyncio
async def test_lookup_coexpression_500_exhausts(httpx_mock: HTTPXMock):
    from plant_genomics_mcp.errors import UpstreamUnavailableError

    for _ in range(3):
        httpx_mock.add_response(
            url="https://atted.jp/api5/?gene=AT1G01010&topN=25&db=Ath-u.c4-0",
            status_code=503,
            text="upstream",
        )
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamUnavailableError):
            await atted.lookup_coexpression(client, "AT1G01010")
