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


def _fake_lookup(gene: dict | None):
    """Monkeypatch stand-in for ensembl_plants.lookup_locus."""

    async def _lookup(client, locus, organism=_SLUG):  # noqa: ANN001, ARG001
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
