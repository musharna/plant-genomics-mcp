"""Tests for the TAIR backend — silent upgrade alias of ``bar.gene_summary``.

Wave A.6.8 swapped ``tair.lookup_locus`` from a v0.9 subscription-required
redirect stub to a direct delegate to BAR/ThaleMine. Tests use the same
respx-style ``pytest_httpx`` mocks as the BAR suite, then assert (a) the
shape matches BAR's contract, (b) the upgrade is a true alias (output ==
``bar.gene_summary`` output), and (c) the live ``server.TOOLS`` registry
still exposes the same tool name.
"""

from __future__ import annotations

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import bar, server, tair
from plant_genomics_mcp.errors import NotFoundError
from plant_genomics_mcp.models import BarGeneSummary

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
            "locus": "AT1G01010",
            "geneid": "839580",
            "aliases": ["NAC001", "ANAC001", "T25K16.1", "Q0WV96"],
        }
    ],
}


@pytest.fixture(autouse=True)
def _clear_cache():
    bar._CACHE.clear()
    yield
    bar._CACHE.clear()


@pytest.mark.asyncio
async def test_lookup_locus_returns_bar_gene_summary_shape(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://bar.utoronto.ca/api/thalemine/gene_information/AT1G01010",
        json=_THALEMINE_OK,
    )
    httpx_mock.add_response(
        url="https://bar.utoronto.ca/api/gaia/aliases/AT1G01010",
        json=_GAIA_OK,
    )
    async with httpx.AsyncClient() as client:
        result = await tair.lookup_locus(client, "AT1G01010")
    assert result["locus"] == "AT1G01010"
    assert result["symbol"] == "NAC001"
    assert result["ncbi_gene_id"] == "839580"
    assert "T25K16.1" in result["aliases"]
    assert result["species"] == "arabidopsis_thaliana"


@pytest.mark.asyncio
async def test_lookup_locus_is_exact_alias_of_bar_gene_summary(httpx_mock: HTTPXMock) -> None:
    """Silent upgrade contract: tair.lookup_locus output must equal bar.gene_summary output."""
    httpx_mock.add_response(
        url="https://bar.utoronto.ca/api/thalemine/gene_information/AT1G01010",
        json=_THALEMINE_OK,
    )
    httpx_mock.add_response(
        url="https://bar.utoronto.ca/api/gaia/aliases/AT1G01010",
        json=_GAIA_OK,
    )
    httpx_mock.add_response(
        url="https://bar.utoronto.ca/api/thalemine/gene_information/AT1G01010",
        json=_THALEMINE_OK,
    )
    httpx_mock.add_response(
        url="https://bar.utoronto.ca/api/gaia/aliases/AT1G01010",
        json=_GAIA_OK,
    )
    async with httpx.AsyncClient() as client:
        tair_out = await tair.lookup_locus(client, "AT1G01010")
        bar._CACHE.clear()
        bar_out = await bar.gene_summary(client, "AT1G01010")
    assert tair_out == bar_out


@pytest.mark.asyncio
async def test_lookup_locus_rejects_invalid_locus() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="invalid locus"):
            await tair.lookup_locus(client, "AT1G01010<x>")


@pytest.mark.asyncio
async def test_lookup_locus_rejects_empty_locus() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="invalid locus"):
            await tair.lookup_locus(client, "")


def test_tair_locus_info_tool_still_registered_in_server() -> None:
    """Tool name preserved — silent-upgrade contract for existing MCP clients."""
    names = {t.name for t in server.TOOLS}
    assert "tair_locus_info" in names


def test_tair_locus_info_output_schema_is_bar_gene_summary() -> None:
    """After the upgrade, outputSchema should match BarGeneSummary's schema."""
    tool = next(t for t in server.TOOLS if t.name == "tair_locus_info")
    assert tool.outputSchema == BarGeneSummary.model_json_schema()
