"""Gramene compara backend unit tests."""

from __future__ import annotations

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import gramene


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
