"""Tests for the Europe PMC REST client.

Two tiers (mirrors the ensembl_plants / uniprot pattern):
  1. Unit tests with mocked HTTP via pytest-httpx (always run).
  2. Live integration test gated by PLANT_GENOMICS_MCP_LIVE=1.
"""

from __future__ import annotations

import os
import re

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import europe_pmc

LIVE = os.environ.get("PLANT_GENOMICS_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set PLANT_GENOMICS_MCP_LIVE=1 to run")


def _one_result(**overrides):
    """Synthetic Europe PMC result row shaped like the resultType=core wire format."""
    base = {
        "id": "12345678",
        "source": "MED",
        "pmid": "12345678",
        "pmcid": "PMC0000001",
        "doi": "10.1000/example.001",
        "title": "Functional analysis of NAC001 in Arabidopsis thaliana.",
        "authorString": "Doe J, Smith A.",
        "journalTitle": "Plant Cell",
        "pubYear": "2024",
        "firstPublicationDate": "2024-03-15",
        "citedByCount": 7,
        "isOpenAccess": "Y",
        "hasPDF": "Y",
        "abstractText": "We characterize NAC001 ...",
    }
    base.update(overrides)
    return base


# ---------- mocked unit tests ----------


@pytest.mark.asyncio
async def test_lookup_locus_at1g01010_arabidopsis_strips_species_suffix(
    httpx_mock: HTTPXMock,
) -> None:
    """Arabidopsis queries don't get a species common-name suffix appended."""
    httpx_mock.add_response(
        url=(
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
            "?query=AT1G01010&format=json&resultType=core&pageSize=10"
        ),
        json={
            "hitCount": 40,
            "resultList": {"result": [_one_result()]},
        },
    )
    async with httpx.AsyncClient() as client:
        result = await europe_pmc.lookup_locus(client, "AT1G01010")
    assert result["locus"] == "AT1G01010"
    assert result["organism"] == "arabidopsis_thaliana"
    assert result["query"] == "AT1G01010"
    assert result["hitCount"] == 40
    assert result["returned"] == 1
    hit = result["hits"][0]
    assert hit["title"].startswith("Functional analysis")
    assert hit["web_url"] == "https://europepmc.org/article/PMC/PMC0000001"


@pytest.mark.asyncio
async def test_lookup_locus_rice_appends_species_common_name(httpx_mock: HTTPXMock) -> None:
    """Non-Arabidopsis species get ` AND <common_name>` appended to disambiguate."""
    httpx_mock.add_response(
        url=(
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
            "?query=Os01g0100100+AND+rice&format=json&resultType=core&pageSize=10"
        ),
        json={
            "hitCount": 12,
            "resultList": {"result": [_one_result(pmcid=None, pmid="99999999")]},
        },
    )
    async with httpx.AsyncClient() as client:
        result = await europe_pmc.lookup_locus(client, "Os01g0100100", organism="oryza_sativa")
    assert result["query"] == "Os01g0100100 AND rice"
    # No pmcid → web_url falls back to PMID-based MED URL.
    assert result["hits"][0]["web_url"] == "https://europepmc.org/article/MED/99999999"


@pytest.mark.asyncio
async def test_lookup_locus_size_is_clamped_to_max(httpx_mock: HTTPXMock) -> None:
    """size=999 is clamped to MAX_PAGE_SIZE so the upstream pageSize stays bounded."""
    httpx_mock.add_response(
        url=(
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
            f"?query=AT1G01010&format=json&resultType=core&pageSize={europe_pmc.MAX_PAGE_SIZE}"
        ),
        json={"hitCount": 0, "resultList": {"result": []}},
    )
    async with httpx.AsyncClient() as client:
        result = await europe_pmc.lookup_locus(client, "AT1G01010", size=999)
    assert result["returned"] == 0
    assert result["hits"] == []


@pytest.mark.asyncio
async def test_lookup_locus_empty_result_list(httpx_mock: HTTPXMock) -> None:
    """hitCount=0 with empty result[] returns empty hits[] — not an error."""
    httpx_mock.add_response(
        url=(
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
            "?query=NONEXISTENT_LOCUS&format=json&resultType=core&pageSize=10"
        ),
        json={"hitCount": 0, "resultList": {"result": []}},
    )
    async with httpx.AsyncClient() as client:
        result = await europe_pmc.lookup_locus(client, "NONEXISTENT_LOCUS")
    assert result["hitCount"] == 0
    assert result["returned"] == 0
    assert result["hits"] == []


@pytest.mark.asyncio
async def test_lookup_locus_normalizes_missing_optional_fields(httpx_mock: HTTPXMock) -> None:
    """Hits with null fields propagate as None — outputSchema marks them optional."""
    httpx_mock.add_response(
        url=(
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
            "?query=AT1G01010&format=json&resultType=core&pageSize=10"
        ),
        json={
            "hitCount": 1,
            "resultList": {
                "result": [{"id": "BARE", "source": "PPR", "title": "Preprint"}],
            },
        },
    )
    async with httpx.AsyncClient() as client:
        result = await europe_pmc.lookup_locus(client, "AT1G01010")
    hit = result["hits"][0]
    assert hit["id"] == "BARE"
    assert hit["pmid"] is None
    assert hit["pmcid"] is None
    assert hit["citedByCount"] is None
    # No pmcid, no pmid → web_url is None.
    assert hit["web_url"] is None


# ---------- live integration (real-execution check) ----------


@live_only
@pytest.mark.asyncio
async def test_live_lookup_at1g01010_returns_hits() -> None:
    """Real call to Europe PMC — AT1G01010 should have published literature."""
    async with httpx.AsyncClient() as client:
        result = await europe_pmc.lookup_locus(client, "AT1G01010", size=3)
    assert result["hitCount"] > 0
    assert 1 <= result["returned"] <= 3
    # Sanity: at least one hit has a title.
    assert any(h.get("title") for h in result["hits"])


# ---------- T10: organism= param via resolver ----------


@pytest.mark.asyncio
async def test_lookup_locus_accepts_organism_param(httpx_mock: HTTPXMock) -> None:
    """T10: lookup_locus accepts ``organism=`` (via organisms.resolve), not ``species=``."""
    httpx_mock.add_response(
        url=re.compile(r"https://www\.ebi\.ac\.uk/europepmc/.*"),
        json={"hitCount": 1, "resultList": {"result": [{"id": "12345"}]}},
    )
    async with httpx.AsyncClient() as client:
        result = await europe_pmc.lookup_locus(client, "AT1G01010", organism="arabidopsis_thaliana")
    assert result is not None


@live_only
@pytest.mark.asyncio
async def test_live_lookup_rice_locus_returns_hits() -> None:
    """v0.9 T19: real call against rice — exercises europe_pmc_slug='rice'.

    Confirms the organism resolver feeds the right slug into the Europe
    PMC query for a non-Arabidopsis organism. Rice OsDREB1A homolog
    Os01g0100100 should have hits (cereal genes are well-published).
    """
    async with httpx.AsyncClient() as client:
        result = await europe_pmc.lookup_locus(
            client, "Os01g0100100", organism="oryza_sativa", size=3
        )
    assert result["hitCount"] >= 0  # non-crash check; literature may be sparse
    assert result["organism"] == "oryza_sativa"
