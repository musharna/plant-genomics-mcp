"""BAR (Bio-Analytic Resource) backend unit tests."""

from __future__ import annotations

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import bar
from plant_genomics_mcp.errors import NotFoundError


@pytest.fixture(autouse=True)
def _clear_cache():
    bar._CACHE.clear()
    yield
    bar._CACHE.clear()


# Live shape captured 2026-05-23 against bar.utoronto.ca/api.
# /thalemine/gene_information/ returns an InterMine envelope with the
# positional row under results[0]; /gaia/aliases/ returns wasSuccessful +
# a list of entries (case-variant rows for the same locus).
_THALEMINE_OK = {
    "wasSuccessful": True,
    "modelName": "genomic",
    "results": [
        [
            "AT1G01010",
            "NAC domain containing protein 1",
            "locus:2200935",
            "NAC domain containing protein 1",
            "NAC001",
            "ANAC001, NAC001, NTL10",
            "NAC domain containing protein 1;(source:Araport11)",
            "Member of the NAC domain containing family of plant specific transcriptional regulators.",
            "NAC domain containing protein 1",
        ]
    ],
}

_GAIA_OK = {
    "wasSuccessful": True,
    "data": [
        {
            "species": "Arabidopsis_thaliana",
            "locus": "At1g01010",
            "geneid": None,
            "aliases": ["NAC domain containing protein 1"],
        },
        {
            "species": "Arabidopsis_thaliana",
            "locus": "AT1G01010",
            "geneid": "839580",
            "aliases": [
                "NAC001",
                "ANAC001",
                "NAC domain containing protein 1",
                "T25K16.1",
                "NM_099983",
                "Q0WV96",
                "locus:2200935",
            ],
        },
    ],
}


@pytest.mark.asyncio
async def test_gene_summary_happy(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://bar.utoronto.ca/api/thalemine/gene_information/AT1G01010",
        json=_THALEMINE_OK,
    )
    httpx_mock.add_response(
        url="https://bar.utoronto.ca/api/gaia/aliases/AT1G01010",
        json=_GAIA_OK,
    )
    async with httpx.AsyncClient() as client:
        result = await bar.gene_summary(client, "AT1G01010")
    assert result["locus"] == "AT1G01010"
    assert result["agi"] == "AT1G01010"
    assert result["symbol"] == "NAC001"
    assert result["full_name"] == "NAC domain containing protein 1"
    assert result["tair_locus_id"] == "locus:2200935"
    assert result["synonyms"] == ["ANAC001", "NAC001", "NTL10"]
    assert "Araport11" in result["computational_description"]
    assert "transcriptional regulators" in result["curator_summary"]
    assert result["ncbi_gene_id"] == "839580"
    assert "Q0WV96" in result["aliases"]
    assert "T25K16.1" in result["aliases"]
    assert result["species"] == "arabidopsis_thaliana"
    assert (
        result["source_url"] == "https://bar.utoronto.ca/api/thalemine/gene_information/AT1G01010"
    )


@pytest.mark.asyncio
async def test_gene_summary_invalid_locus() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="invalid locus"):
            await bar.gene_summary(client, "AT1G01010<script>")


@pytest.mark.asyncio
async def test_gene_summary_http_400_propagates(httpx_mock: HTTPXMock) -> None:
    # Non-Arabidopsis loci 400 with wasSuccessful=false (live shape 2026-05-23
    # for LOC_Os01g01080). _get raises PlantGenomicsError on 400 before we
    # even see the envelope; asyncio.gather surfaces the thalemine error.
    httpx_mock.add_response(
        url="https://bar.utoronto.ca/api/thalemine/gene_information/LOC_Os01g01080",
        status_code=400,
        json={"wasSuccessful": False, "error": "Invalid gene id"},
    )
    httpx_mock.add_response(
        url="https://bar.utoronto.ca/api/gaia/aliases/LOC_Os01g01080",
        json={
            "wasSuccessful": True,
            "data": [
                {
                    "species": "Oryza_sativa",
                    "locus": "LOC_Os01g01080",
                    "geneid": None,
                    "aliases": ["Os01g0100900"],
                }
            ],
        },
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(Exception, match="HTTP 400"):
            await bar.gene_summary(client, "LOC_Os01g01080")


@pytest.mark.asyncio
async def test_gene_summary_empty_results(httpx_mock: HTTPXMock) -> None:
    # Unknown Arabidopsis locus: thalemine 200s with wasSuccessful=true but
    # results=[] (live shape 2026-05-23 for AT1G99999).
    httpx_mock.add_response(
        url="https://bar.utoronto.ca/api/thalemine/gene_information/AT1G99999",
        json={"wasSuccessful": True, "modelName": "genomic", "results": []},
    )
    httpx_mock.add_response(
        url="https://bar.utoronto.ca/api/gaia/aliases/AT1G99999",
        json={"wasSuccessful": False, "error": "Nothing found"},
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="no record"):
            await bar.gene_summary(client, "AT1G99999")


@pytest.mark.asyncio
async def test_gene_summary_malformed_row(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://bar.utoronto.ca/api/thalemine/gene_information/AT1G01010",
        json={"wasSuccessful": True, "results": [["AT1G01010", "short"]]},
    )
    httpx_mock.add_response(
        url="https://bar.utoronto.ca/api/gaia/aliases/AT1G01010",
        json=_GAIA_OK,
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="malformed row"):
            await bar.gene_summary(client, "AT1G01010")


@pytest.mark.asyncio
async def test_gene_summary_aliases_degrade_on_failure(httpx_mock: HTTPXMock) -> None:
    # /gaia/aliases/ failures must not block the canonical TAIR fields:
    # ncbi_gene_id is None and aliases is [] when gaia returns wasSuccessful=false.
    httpx_mock.add_response(
        url="https://bar.utoronto.ca/api/thalemine/gene_information/AT1G01010",
        json=_THALEMINE_OK,
    )
    httpx_mock.add_response(
        url="https://bar.utoronto.ca/api/gaia/aliases/AT1G01010",
        json={"wasSuccessful": False, "error": "Nothing found"},
    )
    async with httpx.AsyncClient() as client:
        result = await bar.gene_summary(client, "AT1G01010")
    assert result["symbol"] == "NAC001"
    assert result["ncbi_gene_id"] is None
    assert result["aliases"] == []
