"""Tests for the Ensembl Plants REST client.

Two tiers (mirrors the genomics-mcp sibling pattern):
  1. Unit tests with mocked HTTP via pytest-httpx (always run).
  2. Live integration tests gated by PLANT_GENOMICS_MCP_LIVE=1, hitting
     the real rest.ensembl.org. These satisfy the real-execution-check
     doctrine.
"""

from __future__ import annotations

import os

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import ensembl_plants

LIVE = os.environ.get("PLANT_GENOMICS_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set PLANT_GENOMICS_MCP_LIVE=1 to run")


# ---------- mocked unit tests ----------


@pytest.mark.asyncio
async def test_lookup_locus_at1g01010_returns_nac001(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://rest.ensembl.org/lookup/id/AT1G01010?species=arabidopsis_thaliana&expand=0",
        json={
            "id": "AT1G01010",
            "display_name": "NAC001",
            "biotype": "protein_coding",
            "species": "arabidopsis_thaliana",
            "description": "NAC domain containing protein 1 [Source:UniProtKB/Swiss-Prot;Acc:Q0WV96]",
        },
    )
    async with httpx.AsyncClient() as client:
        result = await ensembl_plants.lookup_locus(
            client, "AT1G01010", species="arabidopsis_thaliana"
        )
    assert result["id"] == "AT1G01010"
    assert result["display_name"] == "NAC001"
    assert "NAC" in result["description"]


@pytest.mark.asyncio
async def test_lookup_locus_default_species_is_arabidopsis(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://rest.ensembl.org/lookup/id/AT1G01010?species=arabidopsis_thaliana&expand=0",
        json={"id": "AT1G01010", "display_name": "NAC001"},
    )
    async with httpx.AsyncClient() as client:
        result = await ensembl_plants.lookup_locus(client, "AT1G01010")
    assert result["id"] == "AT1G01010"


@pytest.mark.asyncio
async def test_lookup_locus_retries_on_429_then_succeeds(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://rest.ensembl.org/lookup/id/AT1G01010?species=arabidopsis_thaliana&expand=0",
        status_code=429,
        headers={"Retry-After": "0"},
    )
    httpx_mock.add_response(
        url="https://rest.ensembl.org/lookup/id/AT1G01010?species=arabidopsis_thaliana&expand=0",
        json={"id": "AT1G01010", "display_name": "NAC001"},
    )
    async with httpx.AsyncClient() as client:
        result = await ensembl_plants.lookup_locus(client, "AT1G01010")
    assert result["display_name"] == "NAC001"


@pytest.mark.asyncio
async def test_lookup_locus_raises_on_404(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://rest.ensembl.org/lookup/id/NOTREAL?species=arabidopsis_thaliana&expand=0",
        status_code=404,
        text="not found",
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(ensembl_plants.PlantGenomicsError, match="HTTP 404"):
            await ensembl_plants.lookup_locus(client, "NOTREAL")


# ---------- live integration (real-execution check) ----------


@live_only
@pytest.mark.asyncio
async def test_live_lookup_at1g01010() -> None:
    """Real call to rest.ensembl.org — verifies wire format hasn't drifted."""
    async with httpx.AsyncClient() as client:
        result = await ensembl_plants.lookup_locus(client, "AT1G01010")
    assert result["id"] == "AT1G01010"
    # NAC001 is the canonical display name; description should mention NAC.
    assert "NAC" in (result.get("display_name", "") + result.get("description", ""))
