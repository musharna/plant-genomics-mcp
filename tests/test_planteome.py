"""Tests for the Planteome (PO/TO) Solr client.

Two tiers (mirrors the quickgo pattern):
  1. Unit tests with mocked HTTP via pytest-httpx.
  2. Live integration test gated by PLANT_GENOMICS_MCP_LIVE=1.

The Solr query carries list-valued ``fq`` params whose exact URL encoding is
fragile, so mocks match any GET and assert on the recorded request params
rather than pinning the full URL.
"""

from __future__ import annotations

import os

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import planteome
from plant_genomics_mcp.errors import PlantGenomicsError

LIVE = os.environ.get("PLANT_GENOMICS_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set PLANT_GENOMICS_MCP_LIVE=1 to run")


def _doc(term_id: str, label: str, **overrides) -> dict:
    """Synthetic Planteome GOlr annotation doc shaped like the wire format."""
    base = {
        "annotation_class": term_id,
        "annotation_class_label": label,
        "aspect": "A",
        "evidence_type": "IEP",
        "taxon": "NCBITaxon:3702",
        "taxon_label": "Arabidopsis thaliana",
        "reference": ["TAIR:Publication:501714637"],
        "assigned_by": "TAIR",
        "bioentity_label": "NAC001",
        "document_category": "annotation",
    }
    base.update(overrides)
    return base


def _payload(docs: list[dict], num_found: int | None = None) -> dict:
    return {
        "response": {"numFound": num_found if num_found is not None else len(docs), "docs": docs}
    }


# ---------- mocked unit tests ----------


@pytest.mark.asyncio
async def test_lookup_locus_basic(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        json=_payload(
            [
                _doc("PO:0000293", "guard cell"),
                _doc("TO:0000207", "leaf development trait", aspect="T"),
            ]
        )
    )
    async with httpx.AsyncClient() as client:
        result = await planteome.lookup_locus(client, "AT1G01010", "arabidopsis")
    assert result["locus"] == "AT1G01010"
    assert result["organism"] == "arabidopsis_thaliana"
    assert result["taxon"] == "NCBITaxon:3702"
    assert result["numberOfHits"] == 2
    assert result["returned"] == 2
    first = result["annotations"][0]
    # annotation_class → term_id, ontology derived from the prefix
    assert first["term_id"] == "PO:0000293"
    assert first["term_name"] == "guard cell"
    assert first["ontology"] == "PO"
    assert first["evidence"] == "IEP"
    # by_ontology rollup groups on namespace
    assert result["by_ontology"]["PO"] == [{"term_id": "PO:0000293", "term_name": "guard cell"}]
    assert result["by_ontology"]["TO"] == [
        {"term_id": "TO:0000207", "term_name": "leaf development trait"}
    ]
    # Request carried the q + taxon fq for the organism.
    req = httpx_mock.get_requests()[0]
    assert req.url.params["q"] == "AT1G01010"
    assert 'taxon:"NCBITaxon:3702"' in req.url.params.get_list("fq")


@pytest.mark.asyncio
async def test_lookup_locus_dedupes_repeated_term_in_rollup(httpx_mock: HTTPXMock) -> None:
    """Same term_id with different evidence → one rollup entry per term_id."""
    httpx_mock.add_response(
        json=_payload(
            [
                _doc("PO:0009005", "root"),
                _doc("PO:0009005", "root", evidence_type="IDA", reference=["PMID:99999999"]),
                _doc("PO:0009009", "plant embryo"),
            ]
        )
    )
    async with httpx.AsyncClient() as client:
        result = await planteome.lookup_locus(client, "AT1G01010")
    assert result["returned"] == 3
    po = result["by_ontology"]["PO"]
    assert len(po) == 2, f"expected dedup on term_id, got {po}"
    assert {t["term_id"] for t in po} == {"PO:0009005", "PO:0009009"}


@pytest.mark.asyncio
async def test_lookup_locus_empty_is_graceful(httpx_mock: HTTPXMock) -> None:
    """A thin-coverage organism returns zero annotations, not an error."""
    httpx_mock.add_response(json=_payload([], num_found=0))
    async with httpx.AsyncClient() as client:
        result = await planteome.lookup_locus(client, "HORVU.MOREX.r3.1HG0000020", "barley")
    assert result["returned"] == 0
    assert result["annotations"] == []
    assert result["by_ontology"] == {}
    assert result["taxon"] == "NCBITaxon:4513"


@pytest.mark.asyncio
async def test_lookup_locus_malformed_term_id_skipped_in_rollup(httpx_mock: HTTPXMock) -> None:
    """A term_id with no namespace prefix surfaces in annotations[] but is
    omitted from the by_ontology rollup (ontology can't be derived)."""
    httpx_mock.add_response(
        json=_payload([_doc("PO:0009005", "root"), _doc("NOCOLON", "malformed")])
    )
    async with httpx.AsyncClient() as client:
        result = await planteome.lookup_locus(client, "AT1G01010")
    assert result["returned"] == 2
    onts = {a["ontology"] for a in result["annotations"]}
    assert onts == {"PO", None}
    # Only the well-formed PO term rolls up.
    assert list(result["by_ontology"]) == ["PO"]
    assert result["by_ontology"]["PO"] == [{"term_id": "PO:0009005", "term_name": "root"}]


@pytest.mark.asyncio
async def test_lookup_locus_taxon_filter_tracks_organism(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=_payload([]))
    async with httpx.AsyncClient() as client:
        await planteome.lookup_locus(client, "Os01g0100100", "rice")
    fq = httpx_mock.get_requests()[0].url.params.get_list("fq")
    assert 'taxon:"NCBITaxon:39947"' in fq
    assert 'document_category:"annotation"' in fq


@pytest.mark.asyncio
async def test_lookup_locus_limit_clamps_to_max(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=_payload([]))
    async with httpx.AsyncClient() as client:
        await planteome.lookup_locus(client, "AT1G01010", limit=999)
    assert httpx_mock.get_requests()[0].url.params["rows"] == str(planteome.MAX_LIMIT)


@pytest.mark.asyncio
async def test_lookup_locus_rejects_empty_locus() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="non-empty"):
            await planteome.lookup_locus(client, "   ")


@pytest.mark.asyncio
async def test_lookup_locus_non_dict_payload_raises(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=["not", "a", "dict"])
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlantGenomicsError, match="non-dict payload"):
            await planteome.lookup_locus(client, "AT1G01010")


@pytest.mark.asyncio
async def test_lookup_locus_missing_response_raises(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json={"responseHeader": {"status": 0}})
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlantGenomicsError, match="missing 'response'"):
            await planteome.lookup_locus(client, "AT1G01010")


@pytest.mark.asyncio
async def test_lookup_locus_docs_not_list_raises(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json={"response": {"numFound": 1, "docs": {"oops": 1}}})
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlantGenomicsError, match="docs is not a list"):
            await planteome.lookup_locus(client, "AT1G01010")


# ---------- live integration (real-execution check) ----------


@live_only
@pytest.mark.asyncio
async def test_live_at1g01010_has_plant_ontology_terms() -> None:
    """Real Planteome call — NAC001 (AT1G01010) has PO annotations."""
    async with httpx.AsyncClient() as client:
        result = await planteome.lookup_locus(client, "AT1G01010", "arabidopsis")
    assert result["numberOfHits"] > 0
    assert result["returned"] > 0
    assert "PO" in result["by_ontology"], "Arabidopsis NAC001 expected to carry PO terms"
    labels = {t["term_name"] for t in result["by_ontology"]["PO"]}
    assert any(lab == "guard cell" for lab in labels), f"expected 'guard cell' in {labels}"


@live_only
@pytest.mark.asyncio
async def test_live_thin_organism_returns_empty_not_error() -> None:
    """An organism Planteome doesn't curate returns an empty list, not an error."""
    async with httpx.AsyncClient() as client:
        result = await planteome.lookup_locus(client, "HORVU.MOREX.r3.1HG0000020", "barley")
    assert result["returned"] == 0
    assert result["annotations"] == []
