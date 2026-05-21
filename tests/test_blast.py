"""Tests for the NCBI BLAST URLAPI client.

Two tiers (mirrors the other backend test layout):
  1. Unit tests with mocked HTTP via pytest-httpx (always run).
  2. Live integration test gated by PLANT_GENOMICS_MCP_LIVE=1.

The orchestrator's polling sleep is replaced with a fast no-op so the
mocked tests don't actually sleep 60s/poll — the per-RID floor is a
production-etiquette concern, not a test concern.
"""

from __future__ import annotations

import asyncio
import os

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import blast

LIVE = os.environ.get("PLANT_GENOMICS_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set PLANT_GENOMICS_MCP_LIVE=1 to run")


PUT_RESPONSE_TEMPLATE = """<!DOCTYPE html>
<html>
<head><title>NCBI BLAST</title></head>
<body>
<form>
<!--QBlastInfoBegin
    RID = {rid}
    RTOE = {rtoe}
QBlastInfoEnd
-->
</form>
</body>
</html>
"""


def _put_response(rid: str = "ABC123XYZ", rtoe: int = 0) -> str:
    return PUT_RESPONSE_TEMPLATE.format(rid=rid, rtoe=rtoe)


def _searchinfo_response(status: str) -> str:
    return f"""<!DOCTYPE html>
<html><body>
<!--QBlastInfoBegin
    Status={status}
QBlastInfoEnd
-->
</body></html>
"""


RESULT_REPORT = """BLASTP 2.15.0+

Query= test sequence
Length=120

                                                                      Score        E
Sequences producing significant alignments:                          (Bits)     Value

NAC domain-containing protein 1 [Arabidopsis thaliana]                250        2e-80    NP_001185207.1
NAC domain-containing protein 2 [Arabidopsis thaliana]                200        3e-60    NP_001185208.1
hypothetical protein OsI_01234 [Oryza sativa Indica]                  150        5e-40    EAY78901.1

ALIGNMENTS
>NAC domain-containing protein 1 [Arabidopsis thaliana]
... full alignment text follows ...
"""


# ---------- pure-parser unit tests ----------


def test_parse_put_response_extracts_rid_and_rtoe() -> None:
    rid, rtoe = blast._parse_put_response(_put_response("RID12345", 27))
    assert rid == "RID12345"
    assert rtoe == 27


def test_parse_put_response_missing_rtoe_defaults_to_zero() -> None:
    text = "garbage<!--QBlastInfoBegin\n    RID = ABCDEF\nQBlastInfoEnd-->garbage"
    rid, rtoe = blast._parse_put_response(text)
    assert rid == "ABCDEF"
    assert rtoe == 0


def test_parse_put_response_missing_rid_raises() -> None:
    with pytest.raises(blast.UpstreamUnavailableError, match="missing RID"):
        blast._parse_put_response("no QBlastInfo block here")


@pytest.mark.parametrize(
    "raw,expected",
    [
        (_searchinfo_response("WAITING"), "WAITING"),
        (_searchinfo_response("READY"), "READY"),
        (_searchinfo_response("FAILED"), "FAILED"),
        (_searchinfo_response("UNKNOWN"), "UNKNOWN"),
        ("body with no Status=", "UNKNOWN"),
    ],
)
def test_parse_status_recognizes_all_four_states(raw: str, expected: str) -> None:
    assert blast._parse_status(raw) == expected


def test_parse_hit_table_extracts_accession_evalue_bitscore_and_description() -> None:
    hits = blast._parse_hit_table(RESULT_REPORT)
    assert len(hits) == 3
    first = hits[0]
    assert first["accession"] == "NP_001185207.1"
    assert first["bit_score"] == 250.0
    assert first["evalue"] == 2e-80
    assert "NAC domain-containing protein 1" in first["description"]


def test_parse_hit_table_handles_missing_block() -> None:
    assert blast._parse_hit_table("BLASTP report with no hits at all") == []


def test_supported_program_rejects_unknown() -> None:
    with pytest.raises(blast.PlantGenomicsError, match="must be one of"):
        blast._supported_program("blastz")


# ---------- mocked end-to-end orchestrator tests ----------


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the orchestrator's poll-interval sleep so tests don't block 60s+."""

    async def _instant(_: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant)


@pytest.mark.asyncio
async def test_blast_sequence_submits_polls_once_then_fetches(
    httpx_mock: HTTPXMock,
) -> None:
    """Happy path — Put returns RID, one WAITING poll, then READY, then result."""
    # Put — POST to BASE_URL.
    httpx_mock.add_response(
        method="POST",
        url=blast.BASE_URL,
        text=_put_response("RID789", rtoe=0),
    )
    # First poll — WAITING.
    httpx_mock.add_response(
        method="GET",
        text=_searchinfo_response("WAITING"),
    )
    # Second poll — READY.
    httpx_mock.add_response(
        method="GET",
        text=_searchinfo_response("READY"),
    )
    # FORMAT_TYPE=Text fetch.
    httpx_mock.add_response(
        method="GET",
        text=RESULT_REPORT,
    )
    async with httpx.AsyncClient() as client:
        result = await blast.blast_sequence(
            client,
            "MNSAKQ",
            program="blastp",
            max_wait=300.0,
        )
    assert result["rid"] == "RID789"
    assert result["status"] == "READY"
    assert result["program"] == "blastp"
    assert result["database"] == "swissprot"
    assert result["hitCount"] == 3
    assert result["hits"][0]["accession"] == "NP_001185207.1"
    assert result["raw_report_truncated"] is False
    assert "BLASTP 2.15.0+" in result["raw_report_excerpt"]


@pytest.mark.asyncio
async def test_blast_sequence_status_failed_raises_upstream_unavailable(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        method="POST",
        url=blast.BASE_URL,
        text=_put_response("RIDFAIL", rtoe=0),
    )
    httpx_mock.add_response(
        method="GET",
        text=_searchinfo_response("FAILED"),
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(blast.UpstreamUnavailableError, match="Status=FAILED"):
            await blast.blast_sequence(client, "MNSAKQ", program="blastp", max_wait=300.0)


@pytest.mark.asyncio
async def test_blast_sequence_status_unknown_raises_not_found(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        method="POST",
        url=blast.BASE_URL,
        text=_put_response("RIDGONE", rtoe=0),
    )
    httpx_mock.add_response(
        method="GET",
        text=_searchinfo_response("UNKNOWN"),
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(blast.NotFoundError, match="Status=UNKNOWN"):
            await blast.blast_sequence(client, "MNSAKQ", program="blastp", max_wait=300.0)


@pytest.mark.asyncio
async def test_blast_sequence_timeout_raises_not_found_with_rid_preserved(
    httpx_mock: HTTPXMock,
) -> None:
    """If max_wait is exceeded while WAITING, raise NotFoundError + include RID."""
    httpx_mock.add_response(
        method="POST",
        url=blast.BASE_URL,
        text=_put_response("RIDLATE", rtoe=0),
    )
    # Reusable WAITING poll — the orchestrator polls until max_wait elapses.
    httpx_mock.add_response(
        method="GET",
        text=_searchinfo_response("WAITING"),
        is_reusable=True,
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(blast.NotFoundError, match="RIDLATE"):
            await blast.blast_sequence(
                client,
                "MNSAKQ",
                program="blastp",
                poll_interval=60.0,
                max_wait=120.0,
            )


@pytest.mark.asyncio
async def test_blast_sequence_unknown_program_raises_before_submit(
    httpx_mock: HTTPXMock,
) -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(blast.PlantGenomicsError, match="must be one of"):
            await blast.blast_sequence(client, "MNSAKQ", program="blastz")
    # No HTTP call should have been made.
    assert not httpx_mock.get_requests()


@pytest.mark.asyncio
async def test_blast_sequence_database_defaults_per_program(
    httpx_mock: HTTPXMock,
) -> None:
    """blastn defaults to core_nt; the dispatched POST body carries it."""
    httpx_mock.add_response(
        method="POST",
        url=blast.BASE_URL,
        text=_put_response("RIDNT", rtoe=0),
    )
    httpx_mock.add_response(
        method="GET",
        text=_searchinfo_response("READY"),
    )
    httpx_mock.add_response(
        method="GET",
        text=RESULT_REPORT,
    )
    async with httpx.AsyncClient() as client:
        result = await blast.blast_sequence(
            client,
            "ACGTACGTACGT",
            program="blastn",
            max_wait=120.0,
        )
    assert result["database"] == "core_nt"
    put_request = httpx_mock.get_requests(method="POST")[0]
    # form-encoded body — assert the database param made the trip.
    assert b"DATABASE=core_nt" in put_request.content
    assert b"PROGRAM=blastn" in put_request.content


@pytest.mark.asyncio
async def test_blast_sequence_raw_report_truncated_when_huge(
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """raw_report_excerpt is capped; raw_report_truncated flips True."""
    monkeypatch.setattr(blast, "RAW_REPORT_CAP_BYTES", 100)
    big_report = RESULT_REPORT + ("X" * 5_000)
    httpx_mock.add_response(
        method="POST",
        url=blast.BASE_URL,
        text=_put_response("RIDBIG", rtoe=0),
    )
    httpx_mock.add_response(
        method="GET",
        text=_searchinfo_response("READY"),
    )
    httpx_mock.add_response(
        method="GET",
        text=big_report,
    )
    async with httpx.AsyncClient() as client:
        result = await blast.blast_sequence(client, "MNSAKQ", program="blastp", max_wait=300.0)
    assert result["raw_report_truncated"] is True
    assert len(result["raw_report_excerpt"].encode("utf-8")) <= 100


# ---------- live integration (real-execution check) ----------


@live_only
@pytest.mark.asyncio
async def test_live_blastp_small_query_returns_hits() -> None:
    """Real call to NCBI BLAST — short Arabidopsis NAC1 peptide vs Swiss-Prot.

    BLAST searches typically take 30–120s; we allow up to 8 minutes and a
    60s poll cadence (NCBI etiquette floor).
    """
    # First 40 residues of NAC001 / NP_001185207.1 — should hit itself + paralogs.
    query = "MEDQVGFGFRPNDEELVGHYLRNKIESQTSRSAIEVDLNK"
    async with httpx.AsyncClient() as client:
        result = await blast.blast_sequence(
            client,
            query,
            program="blastp",
            database="swissprot",
            hitlist_size=5,
            poll_interval=60.0,
            max_wait=480.0,
        )
    assert result["status"] == "READY"
    assert result["hitCount"] >= 1
    # Top hit should be a NAC-family protein.
    top = result["hits"][0]
    assert top["accession"]
    assert top["description"]
