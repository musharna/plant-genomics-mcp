"""Gramene compara backend unit tests."""

from __future__ import annotations

import os

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import gramene
from plant_genomics_mcp.errors import (
    NotFoundError,
    RateLimitError,
    UpstreamUnavailableError,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    gramene._CACHE.clear()
    yield
    gramene._CACHE.clear()


@pytest.mark.asyncio
async def test_lookup_homologs_happy_path(httpx_mock: HTTPXMock):
    # Live shape (probed 2026-05-21, see /tmp/p3_probes_2026-05-21.txt):
    # homology is a DICT, with gene_tree metadata + homologous_genes whose
    # KEYS are the homology categories and whose VALUES are flat lists of
    # locus-ID strings. There is no per-row taxon/identity/protein_id field.
    httpx_mock.add_response(
        url="https://data.gramene.org/v69/genes?idList=AT1G01010&fl=homology",
        json=[
            {
                "_id": "AT1G01010",
                "homology": {
                    "gene_tree": {
                        "id": "EPlGT01130000406172",
                        "root_taxon_id": 3193,
                        "root_taxon_name": "Embryophyta",
                        "duplications": [3193],
                    },
                    "homologous_genes": {
                        "ortholog_one2many": ["Os01g0100100"],
                        "within_species_paralog": ["AT3G15500"],
                    },
                },
            }
        ],
    )
    async with httpx.AsyncClient() as client:
        result = await gramene.lookup_homologs(client, "AT1G01010", homology_type="all")
    assert result["locus"] == "AT1G01010"
    assert result["total"] == 2
    assert len(result["homologs"]) == 2
    by_locus = {h["target_locus"]: h for h in result["homologs"]}
    assert by_locus["Os01g0100100"]["type"] == "ortholog_one2many"
    assert by_locus["Os01g0100100"]["gene_tree_id"] == "EPlGT01130000406172"
    assert by_locus["AT3G15500"]["type"] == "within_species_paralog"


@pytest.mark.asyncio
async def test_lookup_homologs_ortholog_only(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="https://data.gramene.org/v69/genes?idList=AT1G01010&fl=homology",
        json=[
            {
                "_id": "AT1G01010",
                "homology": {
                    "gene_tree": {"id": "EPlGT01130000406172"},
                    "homologous_genes": {
                        "ortholog_one2many": ["Os01g0100100"],
                        "within_species_paralog": ["AT3G15500"],
                    },
                },
            }
        ],
    )
    async with httpx.AsyncClient() as client:
        result = await gramene.lookup_homologs(client, "AT1G01010", homology_type="ortholog")
    assert result["total"] == 1
    assert result["homologs"][0]["target_locus"] == "Os01g0100100"


@pytest.mark.asyncio
async def test_lookup_homologs_empty_record_raises_not_found(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="https://data.gramene.org/v69/genes?idList=NOPE&fl=homology",
        json=[],
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError) as exc:
            await gramene.lookup_homologs(client, "NOPE")
    assert "[NotFoundError]" in str(exc.value)


@pytest.mark.asyncio
async def test_lookup_homologs_503_then_200(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="https://data.gramene.org/v69/genes?idList=AT1G01010&fl=homology",
        status_code=503,
        text="upstream",
    )
    httpx_mock.add_response(
        url="https://data.gramene.org/v69/genes?idList=AT1G01010&fl=homology",
        json=[{"_id": "AT1G01010", "homology": {"gene_tree": {}, "homologous_genes": {}}}],
    )
    async with httpx.AsyncClient() as client:
        result = await gramene.lookup_homologs(client, "AT1G01010")
    assert result["total"] == 0


@pytest.mark.asyncio
async def test_lookup_homologs_503_exhausts_raises(httpx_mock: HTTPXMock):
    for _ in range(3):
        httpx_mock.add_response(
            url="https://data.gramene.org/v69/genes?idList=AT1G01010&fl=homology",
            status_code=503,
            text="upstream",
        )
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamUnavailableError):
            await gramene.lookup_homologs(client, "AT1G01010")


@pytest.mark.asyncio
async def test_lookup_homologs_429_exhausts_raises(httpx_mock: HTTPXMock):
    for _ in range(3):
        httpx_mock.add_response(
            url="https://data.gramene.org/v69/genes?idList=AT1G01010&fl=homology",
            status_code=429,
            text="rate limit",
        )
    async with httpx.AsyncClient() as client:
        with pytest.raises(RateLimitError):
            await gramene.lookup_homologs(client, "AT1G01010")


@pytest.mark.skipif(
    not os.environ.get("PLANT_GENOMICS_MCP_LIVE"),
    reason="set PLANT_GENOMICS_MCP_LIVE=1 to hit data.gramene.org",
)
@pytest.mark.asyncio
async def test_live_gramene_at1g01010_has_homologs():
    """Smoke against real Gramene v69 — regression for upstream schema drift."""
    async with httpx.AsyncClient() as client:
        result = await gramene.lookup_homologs(client, "AT1G01010", homology_type="all")
    assert result["locus"] == "AT1G01010"
    assert result["release"] == "v69"
    assert result["total"] > 0, "AT1G01010 should have at least one homolog in v69"
    sample = result["homologs"][0]
    assert sample["type"], "homology_type field should populate"
