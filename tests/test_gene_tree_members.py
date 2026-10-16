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
from plant_genomics_mcp.errors import NotFoundError, UpstreamUnavailableError

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


@pytest.mark.asyncio
async def test_a_leaf_without_its_gene_or_taxon_is_refused_before_the_store(
    httpx_mock: HTTPXMock,
) -> None:
    """Read after the store, one bad leaf failed every call for the TTL
    without asking again. Now it is asked once more and never stored;
    positive control, same cache: the whole tree that follows is served."""
    bad = _tree()
    del bad["tree"]["children"][-1]["taxonomy"]
    httpx_mock.add_response(url=TREE_URL, json=bad)
    httpx_mock.add_response(url=TREE_URL, json=bad)
    httpx_mock.add_response(url=TREE_URL, json=_tree())
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamUnavailableError, match="leaf without gene id or taxonomy"):
            await ensembl_plants.gene_tree_members(client, TREE_ID)
        good = await ensembl_plants.gene_tree_members(client, TREE_ID)
    assert good["total"] == 5
    assert len(httpx_mock.get_requests()) == 3


# By member (#234 review of the ortholog demo, 2026-10-16): Gramene v69 is
# built on an older Compara than rest.ensembl.org serves, so AT1G01010's
# Gramene tree EPlGT01130000406172 is 400 there; Ensembl holds the gene in
# EPlGT01160000455820 now. /genetree/member/id finds that tree itself.
CURRENT_ID = "EPlGT01160000455820"
STALE_ID = "EPlGT01130000406172"


def _member_url(slug: str, gene: str) -> re.Pattern[str]:
    return re.compile(
        rf"^https://rest\.ensembl\.org/genetree/member/id/{re.escape(slug)}/{re.escape(gene)}\?.*"
    )


@pytest.mark.asyncio
async def test_a_locus_names_the_tree_ensembl_holds_it_in_now(httpx_mock: HTTPXMock) -> None:
    """The answer's gene_tree_id is the tree served, not one asked for.
    Tomato rides the same route with its wire prefix (``gene-``)."""
    current = {**_tree(), "id": CURRENT_ID}
    httpx_mock.add_response(url=_member_url("arabidopsis_thaliana", "AT1G01010"), json=current)
    httpx_mock.add_response(
        url=_member_url("solanum_lycopersicum_gca000188115v5cm", "gene-Solyc04g081240.2"),
        json=_tree(),
    )
    async with httpx.AsyncClient() as client:
        result = await ensembl_plants.gene_tree_members(client, locus="AT1G01010")
        tomato = await ensembl_plants.gene_tree_members(
            client, locus="Solyc04g081240.2", organism="tomato"
        )
    request = httpx_mock.get_requests()[0]
    assert request.url.params["compara"] == "plants"
    assert result["gene_tree_id"] == CURRENT_ID
    assert (result["locus"], result["organism"]) == ("AT1G01010", "arabidopsis_thaliana")
    assert result["total"] == 5
    assert tomato["gene_tree_id"] == TREE_ID and tomato["organism"] == "solanum_lycopersicum"


@pytest.mark.asyncio
async def test_a_stale_tree_id_says_why_and_the_locus_route_answers(
    httpx_mock: HTTPXMock,
) -> None:
    """A not-found that only says "No GeneTree found" reads as "this gene has
    no tree", the wrong conclusion for an id from an older release."""
    httpx_mock.add_response(
        url=re.compile(rf"^https://rest\.ensembl\.org/genetree/id/{STALE_ID}\?.*"),
        status_code=400,
        json={"error": f"No GeneTree found for ID {STALE_ID}"},
    )
    httpx_mock.add_response(
        url=_member_url("arabidopsis_thaliana", "AT1G01010"), json={**_tree(), "id": CURRENT_ID}
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError) as caught:
            await ensembl_plants.gene_tree_members(client, STALE_ID)
        ok = await ensembl_plants.gene_tree_members(client, locus="AT1G01010")
    message = str(caught.value)
    assert message.startswith("[NotFoundError] ") and message.count("[NotFoundError]") == 1
    assert f"No GeneTree found for ID {STALE_ID}" in message
    assert "older Ensembl Compara release" in message and "locus=" in message
    assert ok["gene_tree_id"] == CURRENT_ID


@pytest.mark.parametrize(
    ("gene", "error"),
    [
        ("AT9G99999", "Unable to find given object: AT9G99999"),
        ("ATMG00010", "No GeneTree found for Gene ID ATMG00010"),
        ("AT1G01046", "Could not fetch a Gene object for ID AT1G01046"),
    ],
)
@pytest.mark.asyncio
async def test_a_locus_in_no_tree_is_not_found_and_one_in_a_tree_answers(
    gene: str, error: str, httpx_mock: HTTPXMock
) -> None:
    """The three 400 bodies /genetree/member/id answers (live, 2026-10-16):
    unknown id, a gene outside every tree, a non-coding gene. None says
    "not found", so the module pattern alone leaves them untyped."""
    httpx_mock.add_response(
        url=_member_url("arabidopsis_thaliana", gene), status_code=400, json={"error": error}
    )
    httpx_mock.add_response(url=_member_url("arabidopsis_thaliana", "AT1G19850"), json=_tree())
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match=re.escape(error)):
            await ensembl_plants.gene_tree_members(client, locus=gene)
        ok = await ensembl_plants.gene_tree_members(client, locus="AT1G19850")
    assert ok["gene_tree_id"] == TREE_ID


@pytest.mark.asyncio
async def test_a_tree_without_its_own_id_is_refused_before_the_store(
    httpx_mock: HTTPXMock,
) -> None:
    """By locus the tree id is the answer; a body without one would hand back
    no tree to name. Asked again, never stored; then the whole tree is served."""
    bad = _tree()
    del bad["id"]
    httpx_mock.add_response(url=_member_url("arabidopsis_thaliana", "AT1G19850"), json=bad)
    httpx_mock.add_response(url=_member_url("arabidopsis_thaliana", "AT1G19850"), json=bad)
    httpx_mock.add_response(url=_member_url("arabidopsis_thaliana", "AT1G19850"), json=_tree())
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamUnavailableError, match="tree id None is not EPlGT"):
            await ensembl_plants.gene_tree_members(client, locus="AT1G19850")
        good = await ensembl_plants.gene_tree_members(client, locus="AT1G19850")
    assert good["gene_tree_id"] == TREE_ID


@pytest.mark.skipif(
    os.environ.get("PLANT_GENOMICS_MCP_LIVE") != "1",
    reason="set PLANT_GENOMICS_MCP_LIVE=1 to hit rest.ensembl.org",
)
@pytest.mark.asyncio
async def test_live_nac001_by_locus_finds_its_current_tree_and_the_stale_id_says_so():
    """Real execution: the tree gramene_homologs names for AT1G01010 is gone
    from rest.ensembl.org; by locus the current tree holds the gene and its
    rice ortholog Os07g0683200 (Gramene v69, ortholog_one2many)."""
    async with httpx.AsyncClient() as client:
        result = await ensembl_plants.gene_tree_members(client, locus="AT1G01010")
        with pytest.raises(NotFoundError, match="locus="):
            await ensembl_plants.gene_tree_members(client, STALE_ID)
    loci = {m["locus"] for m in result["members"]} if not result["truncated"] else None
    assert result["gene_tree_id"].startswith("EPlGT") and result["gene_tree_id"] != STALE_ID
    assert result["total"] > 0
    rice = await _members_of(result["gene_tree_id"], "oryza_sativa")
    assert "Os07g0683200" in rice, rice
    assert loci is None or "AT1G01010" in loci


async def _members_of(tree_id: str, organism: str) -> list[str]:
    async with httpx.AsyncClient() as client:
        result = await ensembl_plants.gene_tree_members(
            client, tree_id, target_organism=organism, limit=1000
        )
    return [m["locus"] for m in result["members"]]
