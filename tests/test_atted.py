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
from plant_genomics_mcp.errors import NotFoundError, OrganismNotSupported, PlantGenomicsError


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
        json={
            "result_set": [
                {"type": "z", "results": [{"gene": 839580, "other_id": ["AT1G01020"], "z": 4.2}]}
            ]
        },
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
    # The rice release scores by LSmr, not z (live, 2026-09-25: Os08g0520550 →
    # {"type": "LSmr", "results": [{"gene": 4343083, "other_id":
    # ["Os07g0438550"], "LSmr": 8.57}, ...]}). The earlier fixture sent "z"
    # here, which is why no test caught every rice score coming back null.
    httpx_mock.add_response(
        url=f"https://atted.jp/api5/?gene=Os01g0100100&topN=10&db={expected_release}",
        json={
            "result_set": [
                {
                    "type": "LSmr",
                    "results": [{"gene": 4326732, "other_id": ["Os01g0100200"], "LSmr": 8.57}],
                }
            ]
        },
    )
    async with httpx.AsyncClient() as client:
        out = await atted.lookup_coexpression(
            client, "Os01g0100100", organism="oryza_sativa", top_n=10
        )
    assert out["atted_release"] == expected_release
    assert out["score_type"] == "LSmr"
    n0 = out["neighbors"][0]
    assert n0["locus"] == "Os01g0100200"
    assert n0["score"] == 8.57
    assert n0["z_score"] is None


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
    assert n0["locus"] == "AT4G36990"  # upstream "At4g36990", recased (#137)
    assert n0["entrez_gene_id"] == 842367
    assert n0["z_score"] == 4.58
    assert result["score_type"] == "z"
    assert n0["score"] == 4.58


@pytest.mark.asyncio
async def test_a_score_type_that_is_missing_or_absent_from_a_row_is_refused(
    httpx_mock: HTTPXMock,
) -> None:
    """A null score reads as "no coexpression evidence"; the rows had one.

    Every non-Arabidopsis release came back with z_score null on every
    neighbour because the score sat under LSmr. A result set that names no
    score type, or a row without the key it names, must fail naming what
    is missing rather than answer with a null score.
    """
    url = "https://atted.jp/api5/?gene=Os01g0100100&topN=25&db=Osa-u.c1-0"
    row = {"gene": 4326732, "other_id": ["Os01g0100200"], "LSmr": 8.57}
    # Positive control: the declared key is read.
    httpx_mock.add_response(url=url, json={"result_set": [{"type": "LSmr", "results": [row]}]})
    async with httpx.AsyncClient() as client:
        ok = await atted.lookup_coexpression(client, "Os01g0100100", organism="oryza_sativa")
    assert ok["neighbors"][0]["score"] == 8.57

    atted._CACHE.clear()
    httpx_mock.add_response(url=url, json={"result_set": [{"results": [row]}]})
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlantGenomicsError, match=r"declares no score type \(type=None\)"):
            await atted.lookup_coexpression(client, "Os01g0100100", organism="oryza_sativa")

    atted._CACHE.clear()
    httpx_mock.add_response(url=url, json={"result_set": [{"type": "z", "results": [row]}]})
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlantGenomicsError, match=r"has no 'z' score.*'LSmr'"):
            await atted.lookup_coexpression(client, "Os01g0100100", organism="oryza_sativa")


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


@pytest.mark.skipif(
    not os.environ.get("PLANT_GENOMICS_MCP_LIVE"),
    reason="set PLANT_GENOMICS_MCP_LIVE=1 to hit atted.jp",
)
@pytest.mark.asyncio
async def test_live_atted_rice_neighbours_carry_the_declared_score():
    """The dossier's null-score row: all 25 rice neighbours had z_score null."""
    async with httpx.AsyncClient() as client:
        rice = await atted.lookup_coexpression(
            client, "Os08g0520550", organism="oryza_sativa", top_n=5
        )
        ath = await atted.lookup_coexpression(
            client, "AT1G19850", organism="arabidopsis_thaliana", top_n=5
        )
    assert rice["score_type"] == "LSmr"
    assert all(isinstance(n["score"], float) for n in rice["neighbors"])
    assert all(n["z_score"] is None for n in rice["neighbors"])
    # Positive control: Arabidopsis still scores by z, in both fields.
    assert ath["score_type"] == "z"
    assert all(n["score"] == n["z_score"] is not None for n in ath["neighbors"])


# ---------- issue #137: one locus spelling across tools ----------


def test_an_agi_neighbour_is_spelled_as_every_other_tool_spells_it() -> None:
    """396 of 400 neighbours came back 'At2g44830' beside 'AT1G19850' everywhere else."""
    row = atted._normalize({"gene": 818087, "other_id": ["At2g44830"], "z": 11.8})
    assert row["locus"] == "AT2G44830"
    # Positive controls: ids that are not AGIs keep their own canonical case —
    # a rice RAP id is mixed-case by convention, and the empty id stays empty.
    for sent in ("Os08g0520550", "OSNPB_080175600", ""):
        assert atted._normalize({"gene": 1, "other_id": [sent], "z": 1.0})["locus"] == sent
