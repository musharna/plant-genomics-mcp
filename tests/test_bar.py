"""BAR (Bio-Analytic Resource) backend unit tests."""

from __future__ import annotations

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import bar


@pytest.fixture(autouse=True)
def _clear_cache():
    bar._CACHE.clear()
    yield
    bar._CACHE.clear()


@pytest.mark.asyncio
async def test_gene_summary_happy(httpx_mock: HTTPXMock) -> None:
    # Live shape (probed 2026-05-23 against bar.utoronto.ca/api):
    #   GET /thalemine/gene_information/AT1G01010
    # Returns positional array, see bar._GI_* indices.
    httpx_mock.add_response(
        url="https://bar.utoronto.ca/api/thalemine/gene_information/AT1G01010",
        json=[
            "AT1G01010",
            "NAC domain containing protein 1",
            "locus:2200935",
            "NAC domain containing protein 1",
            "NAC001",
            "ANAC001, NAC001, NTL10",
            "NAC domain containing protein 1;(source:Araport11)",
            "Member of the NAC domain containing family of plant specific transcriptional regulators.",
            "NAC domain containing protein 1",
        ],
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
    assert result["species"] == "arabidopsis_thaliana"
    assert (
        result["source_url"] == "https://bar.utoronto.ca/api/thalemine/gene_information/AT1G01010"
    )
