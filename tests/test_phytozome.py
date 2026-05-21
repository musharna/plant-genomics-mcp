"""Tests for the Phytozome BioMart client.

Two tiers (mirrors the ensembl_plants sibling pattern):
  1. Unit tests with mocked HTTP via pytest-httpx (always run).
  2. Live integration test gated by PLANT_GENOMICS_MCP_LIVE=1, hitting
     the real phytozome-next.jgi.doe.gov. Satisfies the real-execution-
     check doctrine — BioMart's TSV-with-200-on-error wire format drifts
     quietly and only a real call catches it.
"""

from __future__ import annotations

import os

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import phytozome

LIVE = os.environ.get("PLANT_GENOMICS_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set PLANT_GENOMICS_MCP_LIVE=1 to run")

# Controller-verified verbatim Phytozome response for AT1G01010 (2026-05-21).
_AT1G01010_TSV = (
    "Organism Name\tGene Name\tChromosome Name\tGene Start (bp)\t"
    "Gene End (bp)\tStrand\tDescription\n"
    "Athaliana_TAIR10\tAT1G01010\tChr1\t3631\t5899\t1\t"
    "(1 of 1) PTHR31989:SF215 - NAC DOMAIN-CONTAINING PROTEIN 1\n"
)

_BIOMART_URL = "https://phytozome-next.jgi.doe.gov/biomart/martservice"


# ---------- mocked unit tests ----------


@pytest.mark.asyncio
async def test_lookup_locus_at1g01010_returns_nac001(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=_BIOMART_URL,
        method="POST",
        text=_AT1G01010_TSV,
    )
    async with httpx.AsyncClient() as client:
        result = await phytozome.lookup_locus(client, "AT1G01010")
    assert result["organism_name"] == "Athaliana_TAIR10"
    assert result["gene_name"] == "AT1G01010"
    assert result["chromosome"] == "Chr1"
    # Numeric fields preserved as strings (BioMart TSV is untyped — see module docstring).
    assert result["gene_start"] == "3631"
    assert result["gene_end"] == "5899"
    assert result["strand"] == "1"
    assert "NAC" in result["description"]


@pytest.mark.asyncio
async def test_lookup_locus_default_organism_is_arabidopsis(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=_BIOMART_URL, method="POST", text=_AT1G01010_TSV)
    async with httpx.AsyncClient() as client:
        await phytozome.lookup_locus(client, "AT1G01010")
    # Inspect the request body to confirm organism_id=167 was sent.
    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    body = requests[0].content.decode()
    # Form-encoded: query=<urlencoded XML>. Decoding via httpx's helper is
    # heavier than a substring check; the XML escapes are deterministic.
    assert "organism_id" in body
    assert "value%3D%22167%22" in body or 'value="167"' in body


@pytest.mark.asyncio
async def test_lookup_locus_retries_on_429_then_succeeds(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=_BIOMART_URL,
        method="POST",
        status_code=429,
        headers={"Retry-After": "0"},
    )
    httpx_mock.add_response(url=_BIOMART_URL, method="POST", text=_AT1G01010_TSV)
    async with httpx.AsyncClient() as client:
        result = await phytozome.lookup_locus(client, "AT1G01010")
    assert result["gene_name"] == "AT1G01010"


@pytest.mark.asyncio
async def test_lookup_locus_raises_on_biomart_query_error(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=_BIOMART_URL,
        method="POST",
        text="Query ERROR: caught BioMart::Exception::Usage: Filter organism_id NOT FOUND",
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(phytozome.PlantGenomicsError, match="Query ERROR"):
            await phytozome.lookup_locus(client, "AT1G01010")


@pytest.mark.asyncio
async def test_lookup_locus_raises_on_empty_results(httpx_mock: HTTPXMock) -> None:
    header_only = (
        "Organism Name\tGene Name\tChromosome Name\tGene Start (bp)\t"
        "Gene End (bp)\tStrand\tDescription\n"
    )
    httpx_mock.add_response(url=_BIOMART_URL, method="POST", text=header_only)
    async with httpx.AsyncClient() as client:
        with pytest.raises(phytozome.PlantGenomicsError, match="not found"):
            await phytozome.lookup_locus(client, "AT9G99999")


@pytest.mark.asyncio
async def test_lookup_locus_rejects_xml_injection() -> None:
    # Must fail BEFORE any HTTP call — no httpx_mock interactions allowed.
    async with httpx.AsyncClient() as client:
        with pytest.raises(phytozome.PlantGenomicsError, match="invalid locus"):
            await phytozome.lookup_locus(client, "AT1G01010<x>")


# ---------- live integration (real-execution check) ----------


@live_only
@pytest.mark.asyncio
async def test_live_lookup_at1g01010_phytozome() -> None:
    """Real call to phytozome-next.jgi.doe.gov — verifies wire format hasn't drifted."""
    async with httpx.AsyncClient() as client:
        result = await phytozome.lookup_locus(client, "AT1G01010")
    assert result["gene_name"] == "AT1G01010"
    assert "NAC" in result["description"]
