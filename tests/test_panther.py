"""Tests for the PANTHER protein-family backend.

Two tiers:
  1. Unit tests with mocked HTTP via pytest-httpx against the ``geneinfo``
     endpoint (organism taxid resolved from the real registry).
  2. Live integration tests gated by PLANT_GENOMICS_MCP_LIVE=1.
"""

from __future__ import annotations

import os

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import panther
from plant_genomics_mcp.errors import PlantGenomicsError

LIVE = os.environ.get("PLANT_GENOMICS_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set PLANT_GENOMICS_MCP_LIVE=1 to run")

# Real-shaped geneinfo response (key names + type codes verified live 2026-07-20,
# AT1G01060 / organism 3702). Includes a stray non-dict annotation block to
# exercise the defensive skip, PC as a single object, and GO:MF as a list.
_GENE = {
    "accession": "ARABIDOPSIS|TAIR=AT1G01060|UniProtKB=Q6R0H1",
    "family_id": "PTHR12802",
    "family_name": "MYB-RELATED TRANSCRIPTION FACTOR",
    "sf_id": "PTHR12802:SF176",
    "sf_name": "PROTEIN LHY",
    "annotation_type_list": {
        "annotation_data_type": [
            "unexpected-non-dict-block",
            {
                "content": "ANNOT_TYPE_ID_PANTHER_PC",
                "annotation_list": {
                    "annotation": {"id": "PC00077", "name": "chromatin-binding protein"}
                },
            },
            {
                "content": "GO:0003674",
                "annotation_list": {
                    "annotation": [
                        {"id": "GO:0000976", "name": "transcription cis-regulatory region binding"},
                        {"id": "GO:0003700", "name": "DNA-binding transcription factor activity"},
                    ]
                },
            },
            {
                "content": "GO:0008150",
                "annotation_list": {
                    "annotation": {"id": "GO:0006355", "name": "regulation of transcription"}
                },
            },
            {
                "content": "GO:0005575",
                "annotation_list": {"annotation": {"id": "GO:0005634", "name": "nucleus"}},
            },
            {
                "content": "ANNOT_TYPE_ID_PANTHER_PATHWAY",
                "annotation_list": {
                    "annotation": {"id": "P00028", "name": "circadian clock system"}
                },
            },
        ]
    },
}


def _resp(gene) -> dict:
    return {"search": {"product": {"source": "PANTHERDB", "version": 19}, "mapped_genes": gene}}


_URL = f"{panther.BASE_URL}/services/oai/pantherdb/geneinfo?geneInputList=AT1G01060&organism=3702"


@pytest.mark.asyncio
async def test_lookup_full(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=_URL, json=_resp({"gene": _GENE}))
    async with httpx.AsyncClient() as client:
        r = await panther.lookup_locus(client, "AT1G01060", "arabidopsis")
    assert r["locus"] == "AT1G01060"
    assert r["found"] is True
    assert r["family_id"] == "PTHR12802"
    assert r["family_name"] == "MYB-RELATED TRANSCRIPTION FACTOR"
    assert r["subfamily_id"] == "PTHR12802:SF176"
    assert r["subfamily_name"] == "PROTEIN LHY"
    assert [t["id"] for t in r["go_molecular_function"]] == ["GO:0000976", "GO:0003700"]
    assert r["go_biological_process"] == [
        {"id": "GO:0006355", "name": "regulation of transcription"}
    ]
    assert r["go_cellular_component"] == [{"id": "GO:0005634", "name": "nucleus"}]
    assert r["protein_class"] == [{"id": "PC00077", "name": "chromatin-binding protein"}]
    assert r["pathways"] == [{"id": "P00028", "name": "circadian clock system"}]


@pytest.mark.asyncio
async def test_lookup_minimal_gene_no_annotations(httpx_mock: HTTPXMock) -> None:
    """A mapped gene with a family but no annotation_type_list → empty buckets."""
    httpx_mock.add_response(url=_URL, json=_resp({"gene": {"family_id": "PTHR10000"}}))
    async with httpx.AsyncClient() as client:
        r = await panther.lookup_locus(client, "AT1G01060", "arabidopsis")
    assert r["found"] is True
    assert r["family_id"] == "PTHR10000"
    assert r["go_molecular_function"] == []
    assert r["protein_class"] == []


@pytest.mark.asyncio
async def test_lookup_unmapped_is_found_false(httpx_mock: HTTPXMock) -> None:
    """A gene PANTHER can't map (no mapped_genes.gene) → found=False."""
    httpx_mock.add_response(url=_URL, json={"search": {"product": {}}})
    async with httpx.AsyncClient() as client:
        r = await panther.lookup_locus(client, "AT1G01060", "arabidopsis")
    assert r["found"] is False
    assert r["family_id"] is None
    assert r["go_molecular_function"] == []


@pytest.mark.asyncio
async def test_lookup_gene_as_list_takes_first(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=_URL, json=_resp({"gene": [_GENE, {"family_id": "PTHR99999"}]}))
    async with httpx.AsyncClient() as client:
        r = await panther.lookup_locus(client, "AT1G01060", "arabidopsis")
    assert r["family_id"] == "PTHR12802"


@pytest.mark.asyncio
async def test_lookup_malformed_raises(httpx_mock: HTTPXMock) -> None:
    """A 200 whose body is not a JSON object → typed PlantGenomicsError."""
    httpx_mock.add_response(url=_URL, json=["unexpected", "list"])
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlantGenomicsError, match="unexpected payload"):
            await panther.lookup_locus(client, "AT1G01060", "arabidopsis")


@pytest.mark.asyncio
async def test_upstream_version_is_the_release_the_answer_states(httpx_mock: HTTPXMock) -> None:
    """Mapped AND unmapped answers carry ``product.version`` (live 2026-09-26: 19).

    The value is read from each answer, never a constant: a mapped answer that
    states 18 reports "18", and an answer that states no product reports null.
    """
    unmapped = {"search": {"product": {"source": "PANTHERDB", "version": 18}, "unmapped_list": {}}}
    httpx_mock.add_response(url=_URL, json=unmapped)
    async with httpx.AsyncClient() as client:
        r = await panther.lookup_locus(client, "AT1G01060", "arabidopsis")
    assert (r["found"], r["upstream_version"]) == (False, "18")

    panther._CACHE.clear()
    httpx_mock.add_response(url=_URL, json=_resp({"gene": _GENE}))
    async with httpx.AsyncClient() as client:
        r = await panther.lookup_locus(client, "AT1G01060", "arabidopsis")
    assert (r["found"], r["upstream_version"]) == (True, "19")

    searches: list[dict] = [{"mapped_genes": {"gene": _GENE}}, {"product": {}, "mapped_genes": {}}]
    for search in searches:
        panther._CACHE.clear()
        httpx_mock.add_response(url=_URL, json={"search": search})
        async with httpx.AsyncClient() as client:
            r = await panther.lookup_locus(client, "AT1G01060", "arabidopsis")
        assert r["upstream_version"] is None, search


@pytest.mark.parametrize(
    "product",
    [["PANTHERDB", 19], {"version": True}, {"version": 19.0}, {"version": "  "}],
    ids=["product-not-object", "bool", "float", "blank"],
)
@pytest.mark.asyncio
async def test_an_unusable_stated_release_raises(httpx_mock: HTTPXMock, product) -> None:
    """A release read from a payload we no longer understand is a guess: refuse it."""
    httpx_mock.add_response(url=_URL, json={"search": {"product": product, "mapped_genes": {}}})
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlantGenomicsError, match=r"PANTHER geneinfo product"):
            await panther.lookup_locus(client, "AT1G01060", "arabidopsis")
    # Positive control: the same answer with a well-formed product is accepted.
    panther._CACHE.clear()
    good = {"search": {"product": {"version": "19"}, "mapped_genes": {}}}
    httpx_mock.add_response(url=_URL, json=good)
    async with httpx.AsyncClient() as client:
        r = await panther.lookup_locus(client, "AT1G01060", "arabidopsis")
    assert r["upstream_version"] == "19"


@live_only
@pytest.mark.asyncio
async def test_live_arabidopsis_family() -> None:
    """Real PANTHER call — AT1G01060 (LHY) classifies into a PTHR family."""
    async with httpx.AsyncClient() as client:
        r = await panther.lookup_locus(client, "AT1G01060", "arabidopsis")
    assert r["found"] is True
    assert r["family_id"].startswith("PTHR")
    # The release comes from the same answer as the family (live 2026-09-26: 19).
    assert r["upstream_version"] and r["upstream_version"].isdigit(), r["upstream_version"]


@live_only
@pytest.mark.asyncio
async def test_live_a_mapped_gene_with_no_family_is_found_with_null_family() -> None:
    """PANTHER maps ATMG00940 and annotates GO terms but assigns no family.

    The schema documents that as found=True with null family and subfamily
    (arf dossier, unclassified-member); LHY is the classified control.
    """
    async with httpx.AsyncClient() as client:
        unassigned = await panther.lookup_locus(client, "ATMG00940", "arabidopsis")
        classified = await panther.lookup_locus(client, "AT1G01060", "arabidopsis")
    assert unassigned["found"] is True
    assert unassigned["go_molecular_function"], unassigned
    assert (unassigned["family_id"], unassigned["subfamily_id"]) == (None, None), unassigned
    assert classified["subfamily_id"].startswith(classified["family_id"] + ":SF"), classified
