"""Tests for the Ensembl variation backend (locus_variants + vep_annotate).

Two tiers:
  1. Unit tests with mocked HTTP via pytest-httpx. ``locus_variants`` resolves
     coordinates through ``ensembl_plants.lookup_locus`` — monkeypatched to a
     fixed gene span so each case exercises only this module's overlap logic.
     ``vep_annotate`` hits only the ``/vep`` endpoint, mocked directly.
  2. Live integration tests gated by PLANT_GENOMICS_MCP_LIVE=1.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import ensembl_plants, ensembl_variation
from plant_genomics_mcp.errors import NotFoundError, PlantGenomicsError

LIVE = os.environ.get("PLANT_GENOMICS_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set PLANT_GENOMICS_MCP_LIVE=1 to run")

_SLUG = "arabidopsis_thaliana"

# One real-shaped /overlap variation feature (key names verified live 2026-07-20).
_VARIANT = {
    "id": "vcZ240GYV",
    "source": "EVA",
    "consequence_type": "missense_variant",
    "alleles": ["A", "G"],
    "clinical_significance": [],
    "seq_region_name": "1",
    "start": 300,
    "end": 300,
    "strand": 1,
    "feature_type": "variation",
    "assembly_name": "TAIR10",
}

# One real-shaped VEP result entry.
_VEP = [
    {
        "most_severe_consequence": "missense_variant",
        "assembly_name": "TAIR10",
        "seq_region_name": "1",
        "input": "1 300 300 T/C 1",
        "start": 300,
        "end": 300,
        "allele_string": "T/C",
        "transcript_consequences": [
            {
                "variant_allele": "C",
                "biotype": "protein_coding",
                "impact": "MODERATE",
                "gene_id": "AT1G01010",
                "strand": 1,
                "transcript_id": "AT1G01010.1",
                "consequence_terms": ["missense_variant"],
                "sift_prediction": "tolerated",
                "sift_score": 0.2,
            }
        ],
    }
]

_OVERLAP_URL = f"{ensembl_variation.BASE_URL}/overlap/region/{_SLUG}/1:100-500?feature=variation"
_VEP_URL = f"{ensembl_variation.BASE_URL}/vep/{_SLUG}/region/1:300-300:1/C"


def _fake_lookup(gene: dict | None, calls: list[tuple[object, ...]] | None = None):
    """Monkeypatch stand-in for ensembl_plants.lookup_locus.

    ``calls`` collects each call's (client, locus, organism).
    """

    async def _lookup(client, locus, organism=_SLUG):  # noqa: ANN001
        if calls is not None:
            calls.append((client, locus, organism))
        if gene is None:
            raise NotFoundError(f"no Ensembl entry for {locus!r}")
        return gene

    return _lookup


_GENE = {"seq_region_name": "1", "start": 100, "end": 500, "strand": 1}


# ---------- locus_variants ----------


@pytest.mark.asyncio
async def test_locus_variants_full(httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ensembl_plants, "lookup_locus", _fake_lookup(_GENE))
    httpx_mock.add_response(url=_OVERLAP_URL, json=[_VARIANT])
    async with httpx.AsyncClient() as client:
        r = await ensembl_variation.locus_variants(client, "AT1G01010", "arabidopsis")
    assert r["locus"] == "AT1G01010"
    assert r["organism"] == _SLUG
    assert r["region"] == "1:100-500"
    assert r["variant_count"] == 1
    assert r["truncated"] is False
    v = r["variants"][0]
    assert v["id"] == "vcZ240GYV"
    assert v["source"] == "EVA"
    assert v["consequence_type"] == "missense_variant"
    assert v["alleles"] == ["A", "G"]
    # second call serves the overlap query from cache (one mocked response only)
    async with httpx.AsyncClient() as client:
        r2 = await ensembl_variation.locus_variants(client, "AT1G01010", "arabidopsis")
    assert r2["variants"][0]["id"] == "vcZ240GYV"


@pytest.mark.asyncio
async def test_locus_variants_truncates(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ensembl_plants, "lookup_locus", _fake_lookup(_GENE))
    monkeypatch.setattr(ensembl_variation, "MAX_VARIANTS", 1)
    httpx_mock.add_response(url=_OVERLAP_URL, json=[_VARIANT, {**_VARIANT, "id": "v2"}])
    async with httpx.AsyncClient() as client:
        r = await ensembl_variation.locus_variants(client, "AT1G01010", "arabidopsis")
    assert r["variant_count"] == 2
    assert r["truncated"] is True
    assert len(r["variants"]) == 1


@pytest.mark.asyncio
async def test_locus_variants_no_coords_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A gene lookup with no genomic coordinates → typed PlantGenomicsError."""
    monkeypatch.setattr(ensembl_plants, "lookup_locus", _fake_lookup({"biotype": "protein_coding"}))
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlantGenomicsError, match="no genomic coordinates"):
            await ensembl_variation.locus_variants(client, "AT1G01010", "arabidopsis")


@pytest.mark.asyncio
async def test_locus_variants_malformed_raises(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ensembl_plants, "lookup_locus", _fake_lookup(_GENE))
    httpx_mock.add_response(url=_OVERLAP_URL, json={"unexpected": "object"})
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlantGenomicsError, match="non-list payload"):
            await ensembl_variation.locus_variants(client, "AT1G01010", "arabidopsis")


@pytest.mark.asyncio
async def test_locus_variants_unresolvable_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ensembl_plants, "lookup_locus", _fake_lookup(None))
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError):
            await ensembl_variation.locus_variants(client, "NOSUCH", "arabidopsis")


# ---------- vep_annotate ----------


@pytest.mark.asyncio
async def test_vep_annotate_full(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=_VEP_URL, json=_VEP)
    async with httpx.AsyncClient() as client:
        r = await ensembl_variation.vep_annotate(client, "1:300-300:1", "C", "arabidopsis")
    assert r["found"] is True
    assert r["most_severe_consequence"] == "missense_variant"
    assert r["assembly_name"] == "TAIR10"
    c = r["transcript_consequences"][0]
    assert c["gene_id"] == "AT1G01010"
    assert c["transcript_id"] == "AT1G01010.1"
    assert c["consequence_terms"] == ["missense_variant"]
    assert c["sift_prediction"] == "tolerated"
    assert c["sift_score"] == 0.2


@pytest.mark.asyncio
async def test_vep_annotate_empty_is_not_found(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=_VEP_URL, json=[])
    async with httpx.AsyncClient() as client:
        r = await ensembl_variation.vep_annotate(client, "1:300-300:1", "C", "arabidopsis")
    assert r["found"] is False
    assert r["most_severe_consequence"] is None
    assert r["transcript_consequences"] == []


@pytest.mark.asyncio
async def test_vep_annotate_malformed_raises(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=_VEP_URL, json={"unexpected": "object"})
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlantGenomicsError, match="non-list payload"):
            await ensembl_variation.vep_annotate(client, "1:300-300:1", "C", "arabidopsis")


@pytest.mark.asyncio
async def test_vep_annotate_empty_args_raises() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="non-empty region and allele"):
            await ensembl_variation.vep_annotate(client, "", "C", "arabidopsis")


@pytest.mark.asyncio
async def test_vep_annotate_rejects_path_metachars() -> None:
    """A region/allele carrying a URL path/query metachar raises before any HTTP call."""
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="region"):
            await ensembl_variation.vep_annotate(client, "1/2:100-100:1", "C", "arabidopsis")
        with pytest.raises(NotFoundError, match="allele"):
            await ensembl_variation.vep_annotate(client, "1:100-100:1", "A/C", "arabidopsis")


# ---------- live integration (real-execution check) ----------


@live_only
@pytest.mark.asyncio
async def test_live_rice_locus_variants() -> None:
    """Real Ensembl call — rice Os01g0100100 overlaps EVA variants."""
    async with httpx.AsyncClient() as client:
        r = await ensembl_variation.locus_variants(client, "Os01g0100100", "rice")
    assert r["variant_count"] >= 0
    if r["variants"]:
        assert "id" in r["variants"][0]


@live_only
@pytest.mark.asyncio
async def test_live_vep_rice() -> None:
    async with httpx.AsyncClient() as client:
        r = await ensembl_variation.vep_annotate(client, "1:10000-10000:1", "C", "rice")
    assert r["found"] is True
    assert r["most_severe_consequence"]


def test_variant_limit_is_clamped() -> None:
    assert ensembl_variation._resolve_limit(None) == ensembl_variation.MAX_VARIANTS
    assert ensembl_variation._resolve_limit(0) == 1
    assert ensembl_variation._resolve_limit(-3) == 1
    assert ensembl_variation._resolve_limit(10_000) == ensembl_variation.MAX_VARIANTS
    assert ensembl_variation._resolve_limit(25) == 25


# ---------- whole answers from verbatim live records (#96 mutation triage) ----
# The variant and consequence rows have no per-key schema, so conftest's
# output-contract check cannot see inside them, and the tests above read four
# or five keys of each: 45 mutants renaming a row key or the Ensembl key it is
# read from survived, and 15 misreading the VEP entry's own fields. Everything
# below is Ensembl's answer at AT1G01010 (TAIR10), live 2026-09-27.

_LIVE_GENE = {"seq_region_name": "1", "start": 3631, "end": 5899, "strand": 1}
_LIVE_OVERLAP_URL = (
    f"{ensembl_variation.BASE_URL}/overlap/region/{_SLUG}/1:3631-5899?feature=variation"
)
# One of the 105 rows /overlap sent, verbatim.
_LIVE_VARIANT = {
    "id": "ENSVATH04500126",
    "consequence_type": "missense_variant",
    "start": 3767,
    "clinical_significance": [],
    "assembly_name": "TAIR10",
    "end": 3767,
    "strand": 1,
    "seq_region_name": "1",
    "source": "The 1001 Genomes Project",
    "alleles": ["A", "G"],
    "feature_type": "variation",
}

# VEP on that variant (1:3767 A/G), verbatim except that the 7 transcript
# consequences are cut to the first two: the missense one (SIFT) and one
# downstream (distance). Ensembl sends no PolyPhen for any plant species.
_LIVE_VEP_URL = f"{ensembl_variation.BASE_URL}/vep/{_SLUG}/region/1:3767-3767:1/G"
_LIVE_VEP = [
    {
        "allele_string": "A/G",
        "assembly_name": "TAIR10",
        "end": 3767,
        "id": "1_3767_A/G",
        "input": "1 3767 3767 A/G 1",
        "most_severe_consequence": "missense_variant",
        "seq_region_name": "1",
        "start": 3767,
        "strand": 1,
        "transcript_consequences": [
            {
                "cdna_end": 137,
                "transcript_id": "AT1G01010.1",
                "protein_end": 3,
                "sift_prediction": "tolerated",
                "cdna_start": 137,
                "gene_symbol_source": "EntrezGene",
                "amino_acids": "D/G",
                "cds_start": 8,
                "cds_end": 8,
                "variant_allele": "G",
                "protein_start": 3,
                "impact": "MODERATE",
                "biotype": "protein_coding",
                "strand": 1,
                "codons": "gAt/gGt",
                "sift_score": 0.25,
                "gene_symbol": "NAC001",
                "gene_id": "AT1G01010",
                "consequence_terms": ["missense_variant"],
            },
            {
                "biotype": "protein_coding",
                "impact": "MODIFIER",
                "transcript_id": "AT1G01020.1",
                "strand": -1,
                "gene_symbol_source": "EntrezGene",
                "gene_symbol": "ARV1",
                "gene_id": "AT1G01020",
                "distance": 3021,
                "variant_allele": "G",
                "consequence_terms": ["downstream_gene_variant"],
            },
        ],
    }
]

# Written out by hand from the records above, not produced by the code.
_LIVE_VARIANTS_ANSWER: dict[str, Any] = {
    "locus": "AT1G01010",
    "organism": _SLUG,
    "region": "1:3631-5899",
    "gene_start": 3631,
    "gene_end": 5899,
    "variant_count": 1,
    "total": 1,
    "returned": 1,
    "truncated": False,
    "variants": [
        {
            "id": "ENSVATH04500126",
            "source": "The 1001 Genomes Project",
            "consequence_type": "missense_variant",
            "alleles": ["A", "G"],
            "clinical_significance": [],
            "seq_region_name": "1",
            "start": 3767,
            "end": 3767,
            "strand": 1,
        }
    ],
}

_LIVE_VEP_ANSWER: dict[str, Any] = {
    "organism": _SLUG,
    "region": "1:3767-3767:1",
    "allele": "G",
    "found": True,
    "input": "1 3767 3767 A/G 1",
    "most_severe_consequence": "missense_variant",
    "assembly_name": "TAIR10",
    "seq_region_name": "1",
    "start": 3767,
    "end": 3767,
    "allele_string": "A/G",
    "transcript_consequences": [
        {
            "gene_id": "AT1G01010",
            "transcript_id": "AT1G01010.1",
            "biotype": "protein_coding",
            "impact": "MODERATE",
            "consequence_terms": ["missense_variant"],
            "variant_allele": "G",
            "sift_prediction": "tolerated",
            "sift_score": 0.25,
            "polyphen_prediction": None,
            "polyphen_score": None,
            "distance": None,
        },
        {
            "gene_id": "AT1G01020",
            "transcript_id": "AT1G01020.1",
            "biotype": "protein_coding",
            "impact": "MODIFIER",
            "consequence_terms": ["downstream_gene_variant"],
            "variant_allele": "G",
            "sift_prediction": None,
            "sift_score": None,
            "polyphen_prediction": None,
            "polyphen_score": None,
            "distance": 3021,
        },
    ],
}

_NEVER_SENT_FOR_PLANTS = {"polyphen_prediction", "polyphen_score"}


@pytest.mark.asyncio
async def test_a_live_variant_row_is_answered_whole(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(ensembl_plants, "lookup_locus", _fake_lookup(_LIVE_GENE, calls))
    httpx_mock.add_response(url=_LIVE_OVERLAP_URL, json=[_LIVE_VARIANT])
    async with httpx.AsyncClient() as client:
        r = await ensembl_variation.locus_variants(client, "AT1G01010", "arabidopsis")
        # The locus and the organism as the caller spelled it reach the lookup.
        assert calls == [(client, "AT1G01010", "arabidopsis")]
    assert r == _LIVE_VARIANTS_ANSWER
    # The fixture can fail: no expected row value is null.
    assert None not in _LIVE_VARIANTS_ANSWER["variants"][0].values()


@pytest.mark.asyncio
async def test_a_live_vep_answer_is_answered_whole(httpx_mock: HTTPXMock) -> None:
    # match_headers: a request without the JSON Accept header gets no response.
    httpx_mock.add_response(
        url=_LIVE_VEP_URL, match_headers={"Accept": "application/json"}, json=_LIVE_VEP
    )
    async with httpx.AsyncClient() as client:
        r = await ensembl_variation.vep_annotate(client, "1:3767-3767:1", "G")
    assert r == _LIVE_VEP_ANSWER
    (request,) = httpx_mock.get_requests()
    assert request.extensions["timeout"]["read"] == ensembl_variation.DEFAULT_TIMEOUT
    # The fixture can fail: every entry field is filled, and every consequence
    # field Ensembl sends for plants is filled in at least one row.
    assert None not in [v for k, v in _LIVE_VEP_ANSWER.items() if k != "transcript_consequences"]
    rows = _LIVE_VEP_ANSWER["transcript_consequences"]
    for key in set(rows[0]) - _NEVER_SENT_FOR_PLANTS:
        assert any(row[key] is not None for row in rows), key


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["seq_region_name", "start", "end"])
async def test_each_missing_coordinate_alone_stops_the_query(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    monkeypatch.setattr(ensembl_plants, "lookup_locus", _fake_lookup({**_LIVE_GENE, missing: None}))
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlantGenomicsError, match="no genomic coordinates"):
            await ensembl_variation.locus_variants(client, "AT1G01010")
    # Positive control: with all three present the overlap query is made.
    monkeypatch.setattr(ensembl_plants, "lookup_locus", _fake_lookup(_LIVE_GENE))
    httpx_mock.add_response(url=_LIVE_OVERLAP_URL, json=[])
    async with httpx.AsyncClient() as client:
        r = await ensembl_variation.locus_variants(client, "AT1G01010")
    assert r["variant_count"] == 0


@pytest.mark.asyncio
async def test_the_callers_limit_caps_the_rows(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ensembl_plants, "lookup_locus", _fake_lookup(_LIVE_GENE))
    second = {**_LIVE_VARIANT, "id": "ENSVATH00000002"}
    httpx_mock.add_response(url=_LIVE_OVERLAP_URL, json=[_LIVE_VARIANT, second])
    async with httpx.AsyncClient() as client:
        one = await ensembl_variation.locus_variants(client, "AT1G01010", limit=1)
        both = await ensembl_variation.locus_variants(client, "AT1G01010", limit=2)
    assert [v["id"] for v in one["variants"]] == ["ENSVATH04500126"]
    assert (one["variant_count"], one["returned"], one["truncated"]) == (2, 1, True)
    # Positive control: a limit that covers every row returns them all.
    assert (both["returned"], both["truncated"]) == (2, False)


@live_only
@pytest.mark.asyncio
async def test_live_variant_rows_fill_every_field() -> None:
    """The fixture is one row as Ensembl sent it on one day; this notices a
    renamed field later (projected as null)."""
    async with httpx.AsyncClient() as client:
        r = await ensembl_variation.locus_variants(client, "AT1G01010", "arabidopsis")
    assert r["returned"] > 0
    for row in r["variants"]:
        assert None not in row.values(), row


@live_only
@pytest.mark.asyncio
async def test_live_plant_missense_has_sift_and_no_polyphen() -> None:
    """The tool description says polyphen stays null for plants, because
    Ensembl runs PolyPhen for human only; this checks that against Ensembl."""
    async with httpx.AsyncClient() as client:
        r = await ensembl_variation.vep_annotate(client, "1:3767-3767:1", "G", "arabidopsis")
    assert None not in [v for k, v in r.items() if k != "transcript_consequences"], r
    (missense,) = [c for c in r["transcript_consequences"] if c["transcript_id"] == "AT1G01010.1"]
    assert missense["consequence_terms"] == ["missense_variant"]
    assert missense["sift_prediction"] is not None and missense["sift_score"] is not None
    assert (missense["polyphen_prediction"], missense["polyphen_score"]) == (None, None)
    assert any(c["distance"] is not None for c in r["transcript_consequences"])
