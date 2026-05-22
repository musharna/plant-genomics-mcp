"""KEGG pathway-membership backend unit tests.

KEGG returns plain text (TSV-like), not JSON. Each test mocks both calls
(link/pathway and get/path) so the two-step sequence is exercised end-to-end.
"""

from __future__ import annotations

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import kegg
from plant_genomics_mcp.errors import NotFoundError


@pytest.fixture(autouse=True)
def _clear_cache():
    kegg._CACHE.clear()
    yield
    kegg._CACHE.clear()


@pytest.mark.asyncio
async def test_lookup_pathways_happy_path(httpx_mock: HTTPXMock):
    # Step 1: locus → pathway list
    httpx_mock.add_response(
        url="https://rest.kegg.jp/link/pathway/ath:at1g01010",
        text="ath:at1g01010\tpath:ath04075\nath:at1g01010\tpath:ath04141\n",
    )
    # Step 2: per-pathway metadata
    httpx_mock.add_response(
        url="https://rest.kegg.jp/get/path:ath04075",
        text="ENTRY       ath04075                    Pathway\nNAME        Plant hormone signal transduction - Arabidopsis thaliana\nCLASS       Environmental Information Processing; Signal transduction\n",
    )
    httpx_mock.add_response(
        url="https://rest.kegg.jp/get/path:ath04141",
        text="ENTRY       ath04141                    Pathway\nNAME        Protein processing in endoplasmic reticulum - Arabidopsis thaliana\nCLASS       Genetic Information Processing; Folding, sorting and degradation\n",
    )
    async with httpx.AsyncClient() as client:
        result = await kegg.lookup_pathways(client, "AT1G01010")
    assert result["locus"] == "AT1G01010"
    assert result["kegg_gene_id"] == "ath:at1g01010"
    assert len(result["pathways"]) == 2
    p0 = result["pathways"][0]
    assert p0["id"] == "ath04075"
    assert "Plant hormone" in p0["name"]
    assert "Signal transduction" in p0["pathway_class"]
    assert result["errors"] == []


@pytest.mark.asyncio
async def test_lookup_pathways_empty_link_raises_not_found(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="https://rest.kegg.jp/link/pathway/ath:atnope",
        text="",
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError) as exc:
            await kegg.lookup_pathways(client, "ATNOPE")
    assert "[NotFoundError]" in str(exc.value)


@pytest.mark.asyncio
async def test_lookup_pathways_step2_failure_lands_in_errors(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="https://rest.kegg.jp/link/pathway/ath:at1g01010",
        text="ath:at1g01010\tpath:ath99999\n",
    )
    httpx_mock.add_response(
        url="https://rest.kegg.jp/get/path:ath99999",
        text="",
    )
    async with httpx.AsyncClient() as client:
        result = await kegg.lookup_pathways(client, "AT1G01010")
    assert len(result["pathways"]) == 1
    assert result["pathways"][0]["id"] == "ath99999"
    assert result["pathways"][0]["name"] == ""
    assert len(result["errors"]) == 1
    assert "ath99999" in result["errors"][0]


@pytest.mark.asyncio
async def test_lookup_pathways_404_treated_as_empty(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="https://rest.kegg.jp/link/pathway/ath:atnope",
        status_code=404,
        text="",
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError):
            await kegg.lookup_pathways(client, "ATNOPE")
