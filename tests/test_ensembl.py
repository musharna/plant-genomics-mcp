"""Tests for the Ensembl REST client.

Two tiers:
  1. Unit tests with mocked HTTP (always run).
  2. Live integration tests gated by GENOMICS_MCP_LIVE=1, hitting the real
     rest.ensembl.org. These satisfy the real-execution-check doctrine.
"""

from __future__ import annotations

import os

import httpx
import pytest
from pytest_httpx import HTTPXMock

from genomics_mcp import ensembl

LIVE = os.environ.get("GENOMICS_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set GENOMICS_MCP_LIVE=1 to run")


# ---------- mocked unit tests ----------


@pytest.mark.asyncio
async def test_lookup_id_returns_json(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://rest.ensembl.org/lookup/id/ENSG00000139618",
        json={"id": "ENSG00000139618", "display_name": "BRCA2", "biotype": "protein_coding"},
    )
    async with httpx.AsyncClient() as client:
        result = await ensembl.lookup_id(client, "ENSG00000139618")
    assert result["display_name"] == "BRCA2"


@pytest.mark.asyncio
async def test_lookup_symbol_with_expand(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://rest.ensembl.org/lookup/symbol/homo_sapiens/BRCA2?expand=1",
        json={"id": "ENSG00000139618", "Transcript": []},
    )
    async with httpx.AsyncClient() as client:
        result = await ensembl.lookup_symbol(client, "homo_sapiens", "BRCA2", expand=True)
    assert result["id"] == "ENSG00000139618"


@pytest.mark.asyncio
async def test_sequence_by_id_validates_seq_type() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(ensembl.EnsemblError, match="invalid seq_type"):
            await ensembl.sequence_by_id(client, "ENSG00000139618", seq_type="rubbish")


@pytest.mark.asyncio
async def test_sequence_by_id_passes_type_param(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://rest.ensembl.org/sequence/id/ENSG00000139618?type=cds",
        json={"id": "ENSG00000139618", "seq": "ATGCC..."},
    )
    async with httpx.AsyncClient() as client:
        result = await ensembl.sequence_by_id(client, "ENSG00000139618", seq_type="cds")
    assert result["seq"].startswith("ATG")


@pytest.mark.asyncio
async def test_retries_on_429_then_succeeds(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://rest.ensembl.org/xrefs/id/ENSG00000139618",
        status_code=429,
        headers={"Retry-After": "0"},
    )
    httpx_mock.add_response(
        url="https://rest.ensembl.org/xrefs/id/ENSG00000139618",
        json=[{"db_display_name": "HGNC Symbol", "primary_id": "HGNC:1101"}],
    )
    async with httpx.AsyncClient() as client:
        result = await ensembl.xrefs_by_id(client, "ENSG00000139618")
    assert result[0]["primary_id"] == "HGNC:1101"


@pytest.mark.asyncio
async def test_raises_on_404(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://rest.ensembl.org/lookup/id/NOTREAL",
        status_code=404,
        text="not found",
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(ensembl.EnsemblError, match="HTTP 404"):
            await ensembl.lookup_id(client, "NOTREAL")


@pytest.mark.asyncio
async def test_homology_filters(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://rest.ensembl.org/homology/id/ENSG00000139618?target_species=mus_musculus&type=orthologues",
        json={"data": []},
    )
    async with httpx.AsyncClient() as client:
        await ensembl.homology_by_id(
            client,
            "ENSG00000139618",
            target_species="mus_musculus",
            homology_type="orthologues",
        )


# ---------- live integration (real-execution check) ----------


@live_only
@pytest.mark.asyncio
async def test_live_lookup_brca2() -> None:
    """Real call to rest.ensembl.org — verifies wire format hasn't drifted."""
    async with httpx.AsyncClient() as client:
        result = await ensembl.lookup_id(client, "ENSG00000139618")
    assert result["display_name"] == "BRCA2"
    assert result["biotype"] == "protein_coding"
