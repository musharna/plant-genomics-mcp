"""gene_tree_members: a Gramene/Ensembl gene_tree_id -> its member genes (#130).

`gramene_homologs` returns a `gene_tree_id` on every homolog and nothing on
the server took one back. Ensembl REST serves the tree at
`/genetree/id/<id>?compara=plants`; its leaves carry the gene id, the protein
id and the species taxon (live, 2026-09-25: EPlGT00940000167082 -> 187
leaves). The fixture below is that response's shape, cut to five leaves.
"""

from __future__ import annotations

import os
import re

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import ensembl_plants
from plant_genomics_mcp.errors import NotFoundError

TREE_ID = "EPlGT00940000167082"
TREE_URL = re.compile(r"^https://rest\.ensembl\.org/genetree/id/EPlGT00940000167082\?.*")


def _leaf(gene: str, protein: str, taxid: int, name: str) -> dict:
    return {
        "branch_length": 0.1,
        "id": {"accession": gene, "source": "EnsEMBL"},
        "sequence": {"id": [{"accession": protein, "source": "EnsEMBL"}]},
        "taxonomy": {"id": taxid, "scientific_name": name},
    }


def _tree() -> dict:
    """Leaves at three depths, as a real tree nests them."""
    return {
        "type": "gene tree",
        "id": TREE_ID,
        "rooted": 1,
        "tree": {
            "children": [
                _leaf("AT1G19850", "AT1G19850.1", 3702, "Arabidopsis thaliana strain Columbia"),
                {
                    "children": [
                        _leaf(
                            "gene-Solyc04g081240.2",
                            "CDS-Solyc04g081240.2.1",
                            4081,
                            "Solanum lycopersicum cultivar Heinz 1706",
                        ),
                        {
                            "children": [
                                _leaf(
                                    "Os04g0664400",
                                    "Os04t0664400-02",
                                    39947,
                                    "Oryza sativa Japonica Group reference (Nipponbare) cultivar",
                                ),
                                _leaf(
                                    "HORVU.MOREX.r3.4HG0400000",
                                    "HORVU.MOREX.r3.4HG0400000.1",
                                    112509,
                                    "Hordeum vulgare subsp. vulgare cultivar Morex",
                                ),
                            ]
                        },
                    ]
                },
                _leaf("LOC116249272", "XP_049932378.1", 210225, "Nymphaea colorata"),
            ]
        },
    }


@pytest.fixture(autouse=True)
def _clear_cache():
    ensembl_plants._CACHE.clear()
    yield
    ensembl_plants._CACHE.clear()


@pytest.mark.asyncio
async def test_every_leaf_at_every_depth_is_a_member(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=TREE_URL, json=_tree())
    async with httpx.AsyncClient() as client:
        result = await ensembl_plants.gene_tree_members(client, TREE_ID)
    request = httpx_mock.get_requests()[0]
    assert request.url.params["compara"] == "plants"
    assert request.url.params["sequence"] == "none"
    by_locus = {m["locus"]: m for m in result["members"]}
    assert set(by_locus) == {
        "AT1G19850",
        "Solyc04g081240.2",  # the tomato wire prefix "gene-" is not part of the locus
        "Os04g0664400",
        "HORVU.MOREX.r3.4HG0400000",
        "LOC116249272",
    }
    assert by_locus["AT1G19850"]["organism"] == "arabidopsis_thaliana"
    assert by_locus["AT1G19850"]["protein_id"] == "AT1G19850.1"
    assert by_locus["HORVU.MOREX.r3.4HG0400000"]["organism"] == "hordeum_vulgare"
    # A species outside the server's organism table is still a member, unnamed.
    assert by_locus["LOC116249272"]["organism"] is None
    assert by_locus["LOC116249272"]["species"] == "Nymphaea colorata"
    assert (result["total"], result["returned"], result["truncated"]) == (5, 5, False)
    assert result["gene_tree_id"] == TREE_ID and result["target_organism"] is None


@pytest.mark.asyncio
async def test_organism_filter_matches_the_taxon_compara_uses(httpx_mock: HTTPXMock) -> None:
    """Compara tags barley with the subspecies taxid 112509, not the species'
    4513 (live tree, 2026-09-25); a filter on 4513 would find no barley gene.
    Positive control in the same test: rice matches on its own taxid."""
    httpx_mock.add_response(url=TREE_URL, json=_tree(), is_reusable=True)
    async with httpx.AsyncClient() as client:
        barley = await ensembl_plants.gene_tree_members(client, TREE_ID, target_organism="barley")
        rice = await ensembl_plants.gene_tree_members(
            client, TREE_ID, target_organism="oryza_sativa"
        )
    assert [m["locus"] for m in barley["members"]] == ["HORVU.MOREX.r3.4HG0400000"]
    assert barley["target_organism"] == "hordeum_vulgare" and barley["total"] == 1
    assert [m["locus"] for m in rice["members"]] == ["Os04g0664400"]


@pytest.mark.asyncio
async def test_limit_truncates_and_says_so(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=TREE_URL, json=_tree())
    async with httpx.AsyncClient() as client:
        result = await ensembl_plants.gene_tree_members(client, TREE_ID, limit=2)
    assert (result["total"], result["returned"], result["truncated"]) == (5, 2, True)
    assert len(result["members"]) == 2


@pytest.mark.asyncio
async def test_unknown_tree_is_not_found_and_a_known_one_answers(httpx_mock: HTTPXMock) -> None:
    """Ensembl answers an unknown tree with 400 "No GeneTree found for ID ..."
    (live, 2026-09-25) — no "not found" in it, so the shared 400 pattern would
    leave it an untyped error."""
    httpx_mock.add_response(
        url=re.compile(r"^https://rest\.ensembl\.org/genetree/id/EPlGT00000000000000\?.*"),
        status_code=400,
        json={"error": "No GeneTree found for ID EPlGT00000000000000"},
    )
    httpx_mock.add_response(url=TREE_URL, json=_tree())
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="No GeneTree found"):
            await ensembl_plants.gene_tree_members(client, "EPlGT00000000000000")
        ok = await ensembl_plants.gene_tree_members(client, TREE_ID)
    assert ok["total"] == 5


@pytest.mark.asyncio
async def test_a_malformed_id_never_reaches_the_wire() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="invalid gene_tree_id"):
            await ensembl_plants.gene_tree_members(client, "EPlGT0094/../x")


@pytest.mark.skipif(
    os.environ.get("PLANT_GENOMICS_MCP_LIVE") != "1",
    reason="set PLANT_GENOMICS_MCP_LIVE=1 to hit rest.ensembl.org",
)
@pytest.mark.asyncio
async def test_live_arf_gene_tree_holds_its_arabidopsis_member():
    """Real execution: the tree gramene_homologs names for AT1G19850."""
    async with httpx.AsyncClient() as client:
        result = await ensembl_plants.gene_tree_members(
            client, TREE_ID, target_organism="arabidopsis_thaliana"
        )
    assert "AT1G19850" in [m["locus"] for m in result["members"]], result["members"]
