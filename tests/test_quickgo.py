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


# ---------- issue #132: the payload says what it cut and what it collapsed ----------

_SEARCH_URL = (
    "https://www.ebi.ac.uk/QuickGO/services/annotation/search"
    "?geneProductId=Q0WV96&limit=50&includeFields=goName%2CtaxonName"
)


@pytest.mark.asyncio
async def test_a_capped_answer_is_flagged_truncated_and_a_whole_one_is_not(
    httpx_mock: HTTPXMock,
) -> None:
    """numberOfHits 51 / returned 50 used to ship with no flag (issue #132)."""
    rows = [_ann("GO:0006355", "biological_process"), _ann("GO:0005634", "cellular_component")]
    httpx_mock.add_response(url=_SEARCH_URL, json={"numberOfHits": 3, "results": rows})
    httpx_mock.add_response(url=_SEARCH_URL, json={"numberOfHits": 2, "results": rows})
    async with httpx.AsyncClient() as client:
        capped = await quickgo.lookup_by_uniprot(client, "Q0WV96")
        quickgo._CACHE.clear()
        whole = await quickgo.lookup_by_uniprot(client, "Q0WV96")
    assert (capped["numberOfHits"], capped["returned"], capped["truncated"]) == (3, 2, True)
    # Positive control: everything upstream has was returned.
    assert (whole["numberOfHits"], whole["returned"], whole["truncated"]) == (2, 2, False)


@pytest.mark.asyncio
async def test_the_payload_names_the_key_by_aspect_is_deduplicated_on(
    httpx_mock: HTTPXMock,
) -> None:
    """16 rollup terms beside 50 annotations read as a cut, not a dedup (#132)."""
    httpx_mock.add_response(
        url=_SEARCH_URL,
        json={
            "numberOfHits": 2,
            "results": [
                _ann("GO:0006355", "biological_process"),
                _ann("GO:0006355", "biological_process", goEvidence="IDA"),
            ],
        },
    )
    async with httpx.AsyncClient() as client:
        result = await quickgo.lookup_by_uniprot(client, "Q0WV96")
    assert result["by_aspect_deduped_on"] == "goId"
    # The dedup it names is the one it did: two rows, one term.
    assert result["returned"] == 2 and len(result["by_aspect"]["biological_process"]) == 1


@pytest.mark.asyncio
async def test_both_tool_forms_ship_every_field_the_output_schema_declares(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The single and batch wrappers used to re-list QuickGO's keys by hand.

    Each copy dropped whatever was added to QuickGO's answer after it was
    written — ``upstream_version`` (issue #121) never reached either form's
    payload although the output schema declares it. Driven through the real
    projection, with only UniProt resolution stubbed and QuickGO's HTTP mocked.
    """
    from plant_genomics_mcp import batch, server, uniprot
    from plant_genomics_mcp.models import LocusGoAnnotations

    async def _resolved(client, locus, organism="arabidopsis_thaliana"):
        return {"primaryAccession": "Q0WV96"}

    monkeypatch.setattr(uniprot, "lookup_locus", _resolved)
    rows = [_ann("GO:0006355", "biological_process")]
    httpx_mock.add_response(
        url=_SEARCH_URL, json={"numberOfHits": 2, "results": rows}, is_reusable=True
    )
    declared = set(LocusGoAnnotations.model_fields)

    async with httpx.AsyncClient() as client:
        single = await server._resolve_then_go_annotations(
            client, "AT1G01010", "arabidopsis_thaliana", 50
        )
        env = await batch.batch_locus_go_annotations(client, ["AT1G01010"])

    for form, payload in (("single", single), ("batch", env["results"]["AT1G01010"])):
        assert set(payload) == declared, (form, declared ^ set(payload))
        LocusGoAnnotations.model_validate(payload)  # extra="forbid": nothing undeclared
        assert payload["truncated"] is True and payload["upstream_version"] is None, form
    assert env["errors"] == {}  # positive control: the batch call itself succeeded
