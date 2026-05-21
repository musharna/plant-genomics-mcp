"""Tests for the QuickGO REST client.

Two tiers (mirrors the europe_pmc / uniprot pattern):
  1. Unit tests with mocked HTTP via pytest-httpx.
  2. Live integration test gated by PLANT_GENOMICS_MCP_LIVE=1.
"""

from __future__ import annotations

import os

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import quickgo

LIVE = os.environ.get("PLANT_GENOMICS_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set PLANT_GENOMICS_MCP_LIVE=1 to run")


def _ann(go_id: str, aspect: str, *, go_name: str | None = None, **overrides):
    """Synthetic QuickGO annotation row shaped like the wire format."""
    base = {
        "geneProductId": "UniProtKB:Q0WV96",
        "symbol": "NAC001",
        "qualifier": "enables",
        "goId": go_id,
        "goName": go_name or f"term {go_id}",
        "goAspect": aspect,
        "goEvidence": "IPI",
        "evidenceCode": "ECO:0000353",
        "reference": "PMID:30356219",
        "assignedBy": "TAIR",
        "taxonId": 3702,
        "taxonName": "Arabidopsis thaliana",
        "date": "20201218",
        "withFrom": None,
    }
    base.update(overrides)
    return base


# ---------- mocked unit tests ----------


@pytest.mark.asyncio
async def test_lookup_by_uniprot_basic(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=(
            "https://www.ebi.ac.uk/QuickGO/services/annotation/search"
            "?geneProductId=Q0WV96&limit=50&includeFields=goName%2CtaxonName"
        ),
        json={
            "numberOfHits": 9,
            "results": [
                _ann("GO:0000976", "molecular_function", go_name="DNA binding"),
                _ann("GO:0006355", "biological_process", go_name="regulation of transcription"),
            ],
        },
    )
    async with httpx.AsyncClient() as client:
        result = await quickgo.lookup_by_uniprot(client, "Q0WV96")
    assert result["uniprot_accession"] == "Q0WV96"
    assert result["numberOfHits"] == 9
    assert result["returned"] == 2
    assert result["annotations"][0]["goId"] == "GO:0000976"
    assert result["annotations"][0]["goName"] == "DNA binding"
    # by_aspect rollup groups by goAspect.
    assert result["by_aspect"]["molecular_function"] == [
        {"goId": "GO:0000976", "goName": "DNA binding"},
    ]
    assert result["by_aspect"]["biological_process"] == [
        {"goId": "GO:0006355", "goName": "regulation of transcription"},
    ]


@pytest.mark.asyncio
async def test_lookup_by_uniprot_dedupes_repeated_go_id_in_rollup(httpx_mock: HTTPXMock) -> None:
    """Same goId with different evidence codes → one rollup entry per goId."""
    httpx_mock.add_response(
        url=(
            "https://www.ebi.ac.uk/QuickGO/services/annotation/search"
            "?geneProductId=Q0WV96&limit=50&includeFields=goName%2CtaxonName"
        ),
        json={
            "numberOfHits": 3,
            "results": [
                _ann("GO:0006355", "biological_process", go_name="reg of transcription"),
                _ann(
                    "GO:0006355",
                    "biological_process",
                    go_name="reg of transcription",
                    goEvidence="IDA",
                    reference="PMID:99999999",
                ),
                _ann("GO:0005634", "cellular_component", go_name="nucleus"),
            ],
        },
    )
    async with httpx.AsyncClient() as client:
        result = await quickgo.lookup_by_uniprot(client, "Q0WV96")
    # 3 raw annotations, 2 distinct goIds.
    assert result["returned"] == 3
    bp = result["by_aspect"]["biological_process"]
    assert len(bp) == 1, f"expected dedup on goId, got {bp}"
    assert bp[0]["goId"] == "GO:0006355"
    assert result["by_aspect"]["cellular_component"][0]["goId"] == "GO:0005634"


@pytest.mark.asyncio
async def test_lookup_by_uniprot_limit_clamps_to_max(httpx_mock: HTTPXMock) -> None:
    """limit=999 is clamped to MAX_LIMIT."""
    httpx_mock.add_response(
        url=(
            "https://www.ebi.ac.uk/QuickGO/services/annotation/search"
            f"?geneProductId=Q0WV96&limit={quickgo.MAX_LIMIT}"
            "&includeFields=goName%2CtaxonName"
        ),
        json={"numberOfHits": 0, "results": []},
    )
    async with httpx.AsyncClient() as client:
        result = await quickgo.lookup_by_uniprot(client, "Q0WV96", limit=999)
    assert result["returned"] == 0


@pytest.mark.asyncio
async def test_lookup_by_uniprot_skips_rows_missing_aspect_or_id(httpx_mock: HTTPXMock) -> None:
    """Rows with missing goAspect or goId don't contribute to by_aspect."""
    httpx_mock.add_response(
        url=(
            "https://www.ebi.ac.uk/QuickGO/services/annotation/search"
            "?geneProductId=Q0WV96&limit=50&includeFields=goName%2CtaxonName"
        ),
        json={
            "numberOfHits": 3,
            "results": [
                _ann("GO:0000976", "molecular_function"),
                {**_ann("GO:0000000", "molecular_function"), "goAspect": None},  # bad aspect
                {**_ann("GO:0000000", "molecular_function"), "goId": None},  # bad goId
            ],
        },
    )
    async with httpx.AsyncClient() as client:
        result = await quickgo.lookup_by_uniprot(client, "Q0WV96")
    # All 3 rows still surface in annotations[]; only the well-formed one rolls up.
    assert result["returned"] == 3
    assert result["by_aspect"]["molecular_function"] == [
        {"goId": "GO:0000976", "goName": "term GO:0000976"},
    ]


# ---------- live integration (real-execution check) ----------


@live_only
@pytest.mark.asyncio
async def test_live_lookup_q0wv96_returns_annotations() -> None:
    """Real call to QuickGO — Q0WV96 (NAC001) should have GO annotations."""
    async with httpx.AsyncClient() as client:
        result = await quickgo.lookup_by_uniprot(client, "Q0WV96", limit=20)
    assert result["numberOfHits"] > 0
    assert result["returned"] > 0
    aspects = set(result["by_aspect"].keys())
    # NAC001 is a transcription factor — molecular_function and
    # biological_process are both expected.
    assert "molecular_function" in aspects or "biological_process" in aspects
