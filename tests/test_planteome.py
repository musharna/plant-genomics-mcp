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

from plant_genomics_mcp import organisms, planteome
from plant_genomics_mcp.errors import (
    NotFoundError,
    OrganismNotSupported,
    UpstreamUnavailableError,
)

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
    assert req.url.params["q"] == '"AT1G01010" OR bioentity_label:AT1G01010.*'
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


# Planteome names genes by our locus ids for these organisms only (live
# 2026-09-30, bioentities labelled in our namespace: arabidopsis 70,943, rice
# 53,172, wheat 91,061, tomato 22,982; every other organism 0, held under maize
# v4 ids, barley v2, grape VIT_, sorghum Sobic or protein accessions).
_PLANTEOME_COVERED = {
    "arabidopsis_thaliana",
    "oryza_sativa",
    "solanum_lycopersicum",
    "triticum_aestivum",
}


def test_planteome_coverage_is_the_organisms_it_matches_by_our_ids() -> None:
    covered = {o for o, r in organisms.ORGANISMS.items() if r.planteome_id_form is not None}
    assert covered == _PLANTEOME_COVERED


@pytest.mark.asyncio
@pytest.mark.parametrize("organism", sorted(set(organisms.ORGANISMS) - _PLANTEOME_COVERED))
async def test_an_organism_planteome_indexes_under_other_ids_is_not_supported(
    httpx_mock: HTTPXMock, organism: str
) -> None:
    """Asked by our ids, Planteome answered every such organism with zero
    annotations, read as "no annotations" (barley's was called a thin
    organism; it holds 121,094 bioentities under IBSC v2 ids)."""
    async with httpx.AsyncClient() as client:
        with pytest.raises(OrganismNotSupported, match="'planteome'"):
            await planteome.lookup_locus(client, "ANY1G00010", organism)
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_a_gene_planteome_does_not_know_is_not_found(httpx_mock: HTTPXMock) -> None:
    """No annotation and no bioentity for the gene (live: AT1G99990, 0 and 0;
    AT1G01010, 11 and 1) is "no such gene", not an empty answer."""
    httpx_mock.add_response(json=_payload([], num_found=0))
    httpx_mock.add_response(json=_payload([], num_found=0))
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError) as err:
            await planteome.lookup_locus(client, "AT1G99990")
    assert str(err.value) == (
        "[NotFoundError] Planteome has no gene 'AT1G99990' for arabidopsis_thaliana"
    )
    second = httpx_mock.get_requests()[1].url.params.get_list("fq")
    assert 'document_category:"bioentity"' in second
    assert 'taxon:"NCBITaxon:3702"' in second


@pytest.mark.asyncio
async def test_a_known_gene_without_annotations_is_an_empty_answer(httpx_mock: HTTPXMock) -> None:
    """Positive control of the test above: the gene is a bioentity."""
    httpx_mock.add_response(json=_payload([], num_found=0))
    httpx_mock.add_response(json=_payload([{"bioentity": "TAIR:locus:1"}], num_found=1))
    async with httpx.AsyncClient() as client:
        result = await planteome.lookup_locus(client, "AT1G01010")
    assert (result["returned"], result["annotations"], result["by_ontology"]) == (0, [], {})
    assert len(httpx_mock.get_requests()) == 2


def test_a_locus_cannot_widen_the_query() -> None:
    """The locus goes into Lucene syntax: a quote, colon or wildcard in it is
    escaped, not read as an operator. Positive control: a plain id as is."""
    assert planteome._query('AT1G01010" OR *:*') == (
        '"AT1G01010\\"\\ OR\\ \\*\\:\\*" OR bioentity_label:AT1G01010\\"\\ OR\\ \\*\\:\\*.*'
    )
    assert planteome._query("AT1G01010") == '"AT1G01010" OR bioentity_label:AT1G01010.*'


@pytest.mark.asyncio
async def test_a_tomato_locus_is_asked_without_its_version(httpx_mock: HTTPXMock) -> None:
    """Planteome holds tomato genes as ITAG2 transcripts (Solyc09g008170.1.1)
    and SGN genes with the unversioned id as a synonym; our SL4.0 version is
    not theirs (live: Solyc09g008170 5 annotations, Solyc09g008170.1 none).
    The answer names the locus as asked."""
    httpx_mock.add_response(json=_payload([_doc("PO:0009005", "root")]))
    async with httpx.AsyncClient() as client:
        result = await planteome.lookup_locus(client, "Solyc09g008170.1", "tomato")
    q = httpx_mock.get_requests()[0].url.params["q"]
    assert q == '"Solyc09g008170" OR bioentity_label:Solyc09g008170.*'
    assert result["locus"] == "Solyc09g008170.1"
    assert result["returned"] == 1


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
    httpx_mock.add_response(json=_payload([_doc("PO:0000293", "guard cell")]))
    async with httpx.AsyncClient() as client:
        await planteome.lookup_locus(client, "Os01g0100100", "rice")
    fq = httpx_mock.get_requests()[0].url.params.get_list("fq")
    assert 'taxon:"NCBITaxon:39947"' in fq
    assert 'document_category:"annotation"' in fq


@pytest.mark.asyncio
async def test_lookup_locus_limit_clamps_to_max(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=_payload([_doc("PO:0000293", "guard cell")]))
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
    """Positive control, same request and cache: the well-formed answer that
    follows the two refused ones is served, so neither was stored."""
    httpx_mock.add_response(json=["not", "a", "dict"])
    httpx_mock.add_response(json=["not", "a", "dict"])
    httpx_mock.add_response(json=_payload([_doc("PO:0000293", "guard cell")]))
    async with httpx.AsyncClient() as client:
        with pytest.raises(
            UpstreamUnavailableError,
            match=r"answered 200 twice without a readable result \(list, not an object\)",
        ):
            await planteome.lookup_locus(client, "AT1G01010")
        assert len(httpx_mock.get_requests()) == 2
        result = await planteome.lookup_locus(client, "AT1G01010")
    assert result["annotations"][0]["term_id"] == "PO:0000293"
    assert len(httpx_mock.get_requests()) == 3


@pytest.mark.asyncio
async def test_lookup_locus_missing_response_raises(httpx_mock: HTTPXMock) -> None:
    """Positive control, same request and cache: the well-formed answer that
    follows the two refused ones is served, so neither was stored."""
    httpx_mock.add_response(json={"responseHeader": {"status": 0}})
    httpx_mock.add_response(json={"responseHeader": {"status": 0}})
    httpx_mock.add_response(json=_payload([_doc("PO:0000293", "guard cell")]))
    async with httpx.AsyncClient() as client:
        with pytest.raises(
            UpstreamUnavailableError,
            match=r"answered 200 twice without a readable result \(no 'response' object: got NoneType\)",
        ):
            await planteome.lookup_locus(client, "AT1G01010")
        assert len(httpx_mock.get_requests()) == 2
        result = await planteome.lookup_locus(client, "AT1G01010")
    assert result["annotations"][0]["term_id"] == "PO:0000293"
    assert len(httpx_mock.get_requests()) == 3


@pytest.mark.asyncio
async def test_lookup_locus_docs_not_list_raises(httpx_mock: HTTPXMock) -> None:
    """Positive control, same request and cache: the well-formed answer that
    follows the two refused ones is served, so neither was stored."""
    httpx_mock.add_response(json={"response": {"numFound": 1, "docs": {"oops": 1}}})
    httpx_mock.add_response(json={"response": {"numFound": 1, "docs": {"oops": 1}}})
    httpx_mock.add_response(json=_payload([_doc("PO:0000293", "guard cell")]))
    async with httpx.AsyncClient() as client:
        with pytest.raises(
            UpstreamUnavailableError,
            match=r"answered 200 twice without a readable result \(response.docs is not a list: dict\)",
        ):
            await planteome.lookup_locus(client, "AT1G01010")
        assert len(httpx_mock.get_requests()) == 2
        result = await planteome.lookup_locus(client, "AT1G01010")
    assert result["annotations"][0]["term_id"] == "PO:0000293"
    assert len(httpx_mock.get_requests()) == 3


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
async def test_live_each_covered_organism_answers_and_an_unknown_gene_is_not_found() -> None:
    """One gene per covered organism has annotations when asked by our id;
    AT1G99990 does not exist. Barley, which this test used to call thin,
    holds 121,094 bioentities under other ids: it is refused, not emptied."""
    probes = {
        "arabidopsis_thaliana": "AT1G01010",
        "oryza_sativa": "Os04g0544100",
        "solanum_lycopersicum": "Solyc01g005000.3",
        "triticum_aestivum": "TraesCS6D02G130400",
    }
    assert set(probes) == _PLANTEOME_COVERED
    async with httpx.AsyncClient() as client:
        for organism, gene in probes.items():
            result = await planteome.lookup_locus(client, gene, organism)
            assert result["returned"] > 0, (organism, gene)
        with pytest.raises(NotFoundError):
            await planteome.lookup_locus(client, "AT1G99990")
        with pytest.raises(OrganismNotSupported):
            await planteome.lookup_locus(client, "HORVU.MOREX.r3.1HG0000020", "barley")


# ---------- _normalize, field for field (#96 mutation survivors) ----------

# One doc of Planteome's AT1G01060 answer as sent on 2026-09-28: every field
# _normalize reads, plus some it does not (source, evidence, date, ...).
_LIVE_DOC = {
    "document_category": "annotation",
    "source": "TAIR",
    "bioentity": "TAIR:locus:2200970",
    "bioentity_label": "LHY",
    "annotation_class": "PO:0000013",
    "annotation_class_label": "cauline leaf",
    "aspect": "A",
    "bioentity_name": "AT1G01060",
    "type": "protein",
    "date": "20081209",
    "assigned_by": "TAIR",
    "taxon": "NCBITaxon:3702",
    "taxon_label": "Arabidopsis thaliana",
    "evidence_type": "IEP",
    "evidence": "ECO:0000270",
    "reference": ["TAIR:Publication:501715286", "PMID:15806101"],
}


@pytest.mark.asyncio
async def test_an_annotation_is_projected_whole(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=_payload([_LIVE_DOC]))
    async with httpx.AsyncClient() as client:
        result = await planteome.lookup_locus(client, "AT1G01060", "arabidopsis")
    assert result["annotations"] == [
        {
            "term_id": "PO:0000013",
            "term_name": "cauline leaf",
            "ontology": "PO",
            "aspect": "A",
            "evidence": "IEP",
            "taxon": "NCBITaxon:3702",
            "taxon_label": "Arabidopsis thaliana",
            "reference": ["TAIR:Publication:501715286", "PMID:15806101"],
            "assigned_by": "TAIR",
            "bioentity_label": "LHY",
        }
    ]
    # No expected value is null, so a field read under another name (null)
    # cannot match by accident.
    assert None not in result["annotations"][0].values()


@live_only
@pytest.mark.asyncio
async def test_live_every_field_normalize_reads_is_sent() -> None:
    """_LIVE_DOC is one day's answer; this notices a field Planteome renames.
    All 36 AT1G01060 annotations carried every field on 2026-09-28."""
    async with httpx.AsyncClient() as client:
        result = await planteome.lookup_locus(
            client, "AT1G01060", "arabidopsis", limit=planteome.MAX_LIMIT
        )
    assert result["returned"] > 0
    nulls = {k for a in result["annotations"] for k, v in a.items() if v is None}
    assert nulls == set(), nulls


@pytest.mark.asyncio
async def test_a_body_without_its_count_is_refused_before_the_store(
    httpx_mock: HTTPXMock,
) -> None:
    """``numFound`` was read after the store, so a body without it failed every
    call for the TTL without asking again. Every live answer states it, zero
    included (2026-09-28). Positive control, same cache: the answer after."""
    bad: dict[str, dict[str, list[dict]]] = {"response": {"docs": []}}
    httpx_mock.add_response(json=bad)
    httpx_mock.add_response(json=bad)
    httpx_mock.add_response(json=_payload([_doc("PO:0000293", "guard cell")]))
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamUnavailableError, match="no integer 'numFound'"):
            await planteome.lookup_locus(client, "AT1G01010")
        good = await planteome.lookup_locus(client, "AT1G01010")
    assert good["total"] == 1
    assert len(httpx_mock.get_requests()) == 3
