"""ATTED-II coexpression backend unit tests.

v1.1.0 T6: ``organism`` is keyword-only and required. The ATTED-II
release id (``Ath-u.c4-0`` for Arabidopsis, ``Osa-u.c1-0`` for rice, …)
is resolved through ``organisms.atted_release_for`` rather than the
old module-level ``ATTED_RELEASE`` constant.
"""

from __future__ import annotations

import os

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import atted, organisms
from plant_genomics_mcp.errors import NotFoundError, OrganismNotSupported


@pytest.fixture(autouse=True)
def _clear_cache():
    atted._CACHE.clear()
    yield
    atted._CACHE.clear()


# ---------- v1.1.0 T6 — organism= contract on lookup_coexpression ----------


@pytest.mark.asyncio
async def test_lookup_coexpression_requires_organism() -> None:
    """v1.1.0 BREAKING: ``organism`` is keyword-only and required.
    Calling without it must TypeError before any HTTP.
    """
    async with httpx.AsyncClient() as client:
        with pytest.raises(TypeError):
            # organism is keyword-only + required; omitting it is the point of the
            # test. mypy doesn't flag it (decorator-wrapped signature), so a
            # `type: ignore` would be "unused"; silence pyright's stricter view instead.
            await atted.lookup_coexpression(client, "AT1G01010")  # pyright: ignore[reportCallIssue]


@pytest.mark.asyncio
async def test_lookup_coexpression_arabidopsis_uses_known_release(
    httpx_mock: HTTPXMock,
) -> None:
    expected_release = organisms.atted_release_for("arabidopsis_thaliana")
    httpx_mock.add_response(
        url=f"https://atted.jp/api5/?gene=AT1G01010&topN=25&db={expected_release}",
        json={"result_set": [{"results": [{"gene": 839580, "other_id": ["AT1G01020"], "z": 4.2}]}]},
    )
    async with httpx.AsyncClient() as client:
        out = await atted.lookup_coexpression(client, "AT1G01010", organism="arabidopsis_thaliana")
    assert out["atted_release"] == expected_release
    assert out["neighbors"][0]["locus"] == "AT1G01020"


@pytest.mark.asyncio
async def test_lookup_coexpression_rice_uses_osa_release(
    httpx_mock: HTTPXMock,
) -> None:
    """Non-Arabidopsis organism with a populated ATTED-II release threads
    through end-to-end. Asserts the per-organism release is spliced into
    the ``db=`` query param, not the Arabidopsis default.
    """
    expected_release = organisms.atted_release_for("oryza_sativa")
    httpx_mock.add_response(
        url=f"https://atted.jp/api5/?gene=Os01g0100100&topN=10&db={expected_release}",
        json={
            "result_set": [{"results": [{"gene": 4326732, "other_id": ["Os01g0100200"], "z": 5.1}]}]
        },
    )
    async with httpx.AsyncClient() as client:
        out = await atted.lookup_coexpression(
            client, "Os01g0100100", organism="oryza_sativa", top_n=10
        )
    assert out["atted_release"] == expected_release
    assert out["neighbors"][0]["locus"] == "Os01g0100200"


@pytest.mark.asyncio
async def test_lookup_coexpression_unsupported_organism_raises() -> None:
    """Organisms with ``atted_release=None`` in the matrix (wheat, sorghum,
    barley, poplar, brachypodium as of 2026-05-24) must raise
    OrganismNotSupported before any HTTP fires.
    """
    unsupported = next(
        (c for c, r in organisms.ORGANISMS.items() if r.atted_release is None),
        None,
    )
    if unsupported is None:
        pytest.skip("ATTED covers all populated organisms")
    async with httpx.AsyncClient() as client:
        with pytest.raises(OrganismNotSupported):
            await atted.lookup_coexpression(client, "AT1G01010", organism=unsupported)


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
        result = await atted.lookup_coexpression(
            client, "AT1G01010", organism="arabidopsis_thaliana", top_n=5
        )
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
            await atted.lookup_coexpression(client, "ATNOPE", organism="arabidopsis_thaliana")


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
        result = await atted.lookup_coexpression(
            client, "AT1G01010", organism="arabidopsis_thaliana", top_n=9999
        )
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
            await atted.lookup_coexpression(client, "AT1G01010", organism="arabidopsis_thaliana")


@pytest.mark.skipif(
    not os.environ.get("PLANT_GENOMICS_MCP_LIVE"),
    reason="set PLANT_GENOMICS_MCP_LIVE=1 to hit atted.jp",
)
@pytest.mark.asyncio
async def test_live_atted_at1g01010_has_neighbors():
    async with httpx.AsyncClient() as client:
        result = await atted.lookup_coexpression(
            client, "AT1G01010", organism="arabidopsis_thaliana", top_n=5
        )
    assert result["locus"] == "AT1G01010"
    assert result["atted_release"] == organisms.atted_release_for("arabidopsis_thaliana")
    assert len(result["neighbors"]) > 0
    assert result["neighbors"][0]["z_score"] is not None
    assert result["neighbors"][0]["locus"]
