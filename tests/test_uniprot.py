"""Tests for the UniProt REST client.

Two tiers (mirrors the ensembl_plants test layout):
  1. Unit tests with mocked HTTP via pytest-httpx (always run).
  2. Live integration test gated by PLANT_GENOMICS_MCP_LIVE=1, hitting
     the real rest.uniprot.org. Satisfies the real-execution-check doctrine.
"""

from __future__ import annotations

import os

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import uniprot
from plant_genomics_mcp.errors import NotFoundError

LIVE = os.environ.get("PLANT_GENOMICS_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set PLANT_GENOMICS_MCP_LIVE=1 to run")


# Helper: build a plausible UniProtKB search response with one hit.
def _one_hit(
    *,
    accession: str = "Q0WV96",
    uniprotkb_id: str = "NAC1_ARATH",
    entry_type: str = "UniProtKB reviewed (Swiss-Prot)",
    name: str = "NAC domain-containing protein 1",
    gene: str = "NAC001",
    organism: str = "Arabidopsis thaliana",
    taxon: int = 3702,
    length: int = 429,
) -> dict:
    return {
        "results": [
            {
                "primaryAccession": accession,
                "uniProtkbId": uniprotkb_id,
                "entryType": entry_type,
                "proteinDescription": {"recommendedName": {"fullName": {"value": name}}},
                "genes": [{"geneName": {"value": gene}}],
                "organism": {"scientificName": organism, "taxonId": taxon},
                "sequence": {"length": length},
            }
        ]
    }


# ---------- mocked unit tests ----------


@pytest.mark.asyncio
async def test_lookup_locus_at1g01010_returns_q0wv96(httpx_mock: HTTPXMock) -> None:
    """Default Arabidopsis path — single reviewed hit, normalized shape."""
    httpx_mock.add_response(
        url=(
            "https://rest.uniprot.org/uniprotkb/search"
            "?query=gene%3AAT1G01010+AND+organism_id%3A3702+AND+reviewed%3Atrue"
            "&format=json&size=1"
        ),
        json=_one_hit(),
    )
    async with httpx.AsyncClient() as client:
        result = await uniprot.lookup_locus(client, "AT1G01010")
    assert result["primaryAccession"] == "Q0WV96"
    assert result["uniProtkbId"] == "NAC1_ARATH"
    assert result["reviewed"] is True
    assert result["recommendedName"] == "NAC domain-containing protein 1"
    assert result["geneNames"] == ["NAC001"]
    assert result["organism"] == "Arabidopsis thaliana"
    assert result["taxonId"] == 3702
    assert result["sequenceLength"] == 429
    assert result["web_url"] == "https://www.uniprot.org/uniprotkb/Q0WV96"
    assert result["locus_query"] == "AT1G01010"


@pytest.mark.asyncio
async def test_lookup_locus_falls_back_to_unreviewed(httpx_mock: HTTPXMock) -> None:
    """Rice path — no Swiss-Prot hit, falls back to TrEMBL."""
    # Pass 1: reviewed=true → 0 hits
    httpx_mock.add_response(
        url=(
            "https://rest.uniprot.org/uniprotkb/search"
            "?query=gene%3AOs01g0100100+AND+organism_id%3A39947+AND+reviewed%3Atrue"
            "&format=json&size=1"
        ),
        json={"results": []},
    )
    # Pass 2: no reviewed filter → 1 TrEMBL hit
    httpx_mock.add_response(
        url=(
            "https://rest.uniprot.org/uniprotkb/search"
            "?query=gene%3AOs01g0100100+AND+organism_id%3A39947"
            "&format=json&size=1"
        ),
        json=_one_hit(
            accession="Q0JRI1",
            uniprotkb_id="Q0JRI1_ORYSJ",
            entry_type="UniProtKB unreviewed (TrEMBL)",
            name="Os01g0100100 protein",
            gene="Os01g0100100",
            organism="Oryza sativa subsp. japonica",
            taxon=39947,
            length=200,
        ),
    )
    async with httpx.AsyncClient() as client:
        result = await uniprot.lookup_locus(client, "Os01g0100100", organism_id=39947)
    assert result["primaryAccession"] == "Q0JRI1"
    assert result["reviewed"] is False
    assert "TrEMBL" in result["entryType"]


@pytest.mark.asyncio
async def test_lookup_locus_raises_not_found_when_both_passes_empty(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        url=(
            "https://rest.uniprot.org/uniprotkb/search"
            "?query=gene%3ANOTREAL+AND+organism_id%3A3702+AND+reviewed%3Atrue"
            "&format=json&size=1"
        ),
        json={"results": []},
    )
    httpx_mock.add_response(
        url=(
            "https://rest.uniprot.org/uniprotkb/search"
            "?query=gene%3ANOTREAL+AND+organism_id%3A3702"
            "&format=json&size=1"
        ),
        json={"results": []},
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="no entry for gene=NOTREAL"):
            await uniprot.lookup_locus(client, "NOTREAL")


@pytest.mark.asyncio
async def test_lookup_locus_retries_on_429_then_succeeds(httpx_mock: HTTPXMock) -> None:
    url = (
        "https://rest.uniprot.org/uniprotkb/search"
        "?query=gene%3AAT1G01010+AND+organism_id%3A3702+AND+reviewed%3Atrue"
        "&format=json&size=1"
    )
    httpx_mock.add_response(url=url, status_code=429, headers={"Retry-After": "0"})
    httpx_mock.add_response(url=url, json=_one_hit())
    async with httpx.AsyncClient() as client:
        result = await uniprot.lookup_locus(client, "AT1G01010")
    assert result["primaryAccession"] == "Q0WV96"


# ---------- live integration (real-execution check) ----------


@live_only
@pytest.mark.asyncio
async def test_live_lookup_at1g01010() -> None:
    """Real call to rest.uniprot.org — verifies wire format hasn't drifted."""
    async with httpx.AsyncClient() as client:
        result = await uniprot.lookup_locus(client, "AT1G01010")
    assert result["primaryAccession"] == "Q0WV96"
    assert result["uniProtkbId"] == "NAC1_ARATH"
    assert result["reviewed"] is True
    assert result["taxonId"] == 3702


@live_only
@pytest.mark.asyncio
async def test_live_lookup_rice_falls_back_to_trembl() -> None:
    """Real call for rice locus — should fall back to TrEMBL (no Swiss-Prot)."""
    async with httpx.AsyncClient() as client:
        result = await uniprot.lookup_locus(client, "Os01g0100100", organism_id=39947)
    assert result["primaryAccession"]  # any non-empty accession
    # If UniProt later curates this locus into Swiss-Prot, this test will need
    # updating — for now, asserting TrEMBL doubles as a wire-format guard.
    assert "TrEMBL" in result["entryType"]
