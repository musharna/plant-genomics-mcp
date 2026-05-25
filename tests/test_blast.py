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

from plant_genomics_mcp import blast, progress

# Capture the unpatched real sleep BEFORE the autouse _no_sleep fixture
# can replace ``asyncio.sleep`` — the semaphore concurrency test needs
# the event loop to actually yield, not a no-op coroutine.
_REAL_ASYNCIO_SLEEP = asyncio.sleep

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

                                                                  Score     E
Sequences producing significant alignments:                       (Bits)  Value  Ident

Q9FLJ2.1 RecName: Full=NAC domain-containing protein 100; Shor...  204     8e-65  66%
Q9FKA0.1 RecName: Full=NAC domain-containing protein 92; Short...  199     1e-63  66%
Q9FLR3.1 RecName: Full=NAC domain-containing protein 79; Short...  200     2e-63  64%

ALIGNMENTS
>Q9FLJ2.1 RecName: Full=NAC domain-containing protein 100; Short=ANAC100;
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
    assert first["accession"] == "Q9FLJ2.1"
    assert first["bit_score"] == 204.0
    assert first["evalue"] == 8e-65
    assert first["identity"] == "66%"
    assert "NAC domain-containing protein 100" in first["description"]


def test_parse_hit_table_handles_missing_block() -> None:
    assert blast._parse_hit_table("BLASTP report with no hits at all") == []


def test_supported_program_rejects_unknown() -> None:
    with pytest.raises(blast.PlantGenomicsError, match="must be one of"):
        blast._supported_program("blastz")


# ---------- mocked end-to-end orchestrator tests ----------


@pytest.fixture(autouse=True)
def _no_sleep(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the orchestrator's poll-interval sleep so mocked tests don't block 60s+.

    Bypassed for live tests (``test_live_*``) which hit real NCBI and must
    honor the per-RID 60s poll floor — without this gate the autouse mock
    collapses the live polling loop and raises NotFoundError in seconds.
    """
    if request.node.name.startswith("test_live_"):
        return

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
    assert result["hits"][0]["accession"] == "Q9FLJ2.1"
    assert result["hits"][0]["identity"] == "66%"
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


# ---------- Wave B4: semaphore + operator email ----------


def test_identity_params_uses_env_email(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLANT_GENOMICS_MCP_NCBI_EMAIL", "ops@example.com")
    params = blast._identity_params()
    assert params["email"] == "ops@example.com"
    assert params["tool"] == "plant-genomics-mcp"


def test_identity_params_fallback_is_unmistakable_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fallback email when env unset must be:
    - a reserved/invalid domain so it cannot route to a real inbox
    - clearly identifying this tool so NCBI ops can pattern-match the
      traffic source if they ever need to.
    """
    monkeypatch.delenv("PLANT_GENOMICS_MCP_NCBI_EMAIL", raising=False)
    params = blast._identity_params()
    assert params["email"].endswith(".invalid"), params["email"]
    assert "plant-genomics-mcp" in params["email"], params["email"]


@pytest.mark.asyncio
async def test_blast_emits_email_warning_when_env_unset(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the operator hasn't set NCBI_EMAIL, ``blast_sequence`` emits a
    progress notification flagging the placeholder — the LLM client surfaces
    it so the operator notices before NCBI throttles them.
    """
    monkeypatch.delenv("PLANT_GENOMICS_MCP_NCBI_EMAIL", raising=False)
    captured: list[str] = []

    async def _send(_progress: float, _total: float | None, message: str | None) -> None:
        if message:
            captured.append(message)

    token = progress.set_reporter(progress.Reporter(_send))
    try:
        httpx_mock.add_response(method="POST", url=blast.BASE_URL, text=_put_response("REM1", 0))
        httpx_mock.add_response(method="GET", text=_searchinfo_response("READY"))
        httpx_mock.add_response(method="GET", text=RESULT_REPORT)
        async with httpx.AsyncClient() as client:
            await blast.blast_sequence(client, "MNSAKQ", program="blastp", max_wait=300.0)
    finally:
        progress.reset_reporter(token)
    assert any("PLANT_GENOMICS_MCP_NCBI_EMAIL" in m for m in captured), captured


@pytest.mark.asyncio
async def test_blast_no_email_warning_when_env_set(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLANT_GENOMICS_MCP_NCBI_EMAIL", "ops@example.com")
    captured: list[str] = []

    async def _send(_progress: float, _total: float | None, message: str | None) -> None:
        if message:
            captured.append(message)

    token = progress.set_reporter(progress.Reporter(_send))
    try:
        httpx_mock.add_response(method="POST", url=blast.BASE_URL, text=_put_response("REM2", 0))
        httpx_mock.add_response(method="GET", text=_searchinfo_response("READY"))
        httpx_mock.add_response(method="GET", text=RESULT_REPORT)
        async with httpx.AsyncClient() as client:
            await blast.blast_sequence(client, "MNSAKQ", program="blastp", max_wait=300.0)
    finally:
        progress.reset_reporter(token)
    assert not any("PLANT_GENOMICS_MCP_NCBI_EMAIL" in m for m in captured), captured


@pytest.mark.asyncio
async def test_blast_semaphore_caps_concurrent_at_two(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Four parallel ``blast_sequence`` calls — the module-level
    semaphore must hold the in-flight count at <= MAX_CONCURRENT_BLAST.

    Stubs the three HTTP-bound helpers so the test doesn't need
    httpx_mock; the slow_submit stub yields the event loop via the
    captured real ``asyncio.sleep`` so other coroutines actually get
    a chance to enter the critical section.
    """
    assert blast.MAX_CONCURRENT_BLAST == 2
    in_flight = 0
    peak = 0

    async def slow_submit(*_args: object, **_kwargs: object) -> tuple[str, int]:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            await _REAL_ASYNCIO_SLEEP(0.05)
        finally:
            in_flight -= 1
        return ("RIDSEM", 0)

    async def fast_poll(*_args: object, **_kwargs: object) -> str:
        return "READY"

    async def fast_fetch(*_args: object, **_kwargs: object) -> str:
        return RESULT_REPORT

    monkeypatch.setattr(blast, "submit", slow_submit)
    monkeypatch.setattr(blast, "poll_status", fast_poll)
    monkeypatch.setattr(blast, "fetch_result", fast_fetch)

    async with httpx.AsyncClient() as client:
        await asyncio.gather(
            *[
                blast.blast_sequence(client, "MNSAKQ", program="blastp", max_wait=10.0)
                for _ in range(4)
            ]
        )
    assert peak <= blast.MAX_CONCURRENT_BLAST, f"peak in-flight={peak} > cap"
    assert peak >= 2, f"semaphore unused — got peak={peak}, expected at least 2"


# ---------- live integration (real-execution check) ----------


@live_only
@pytest.mark.asyncio
async def test_live_blastp_small_query_returns_hits() -> None:
    """Real call to NCBI BLAST — short Arabidopsis NAC1 peptide vs Swiss-Prot.

    BLAST searches typically take 30–120s; we allow up to 12 minutes and a
    60s poll cadence (NCBI etiquette floor). 12min absorbs the queue-depth
    variance observed in the v1.3.0 baseline (job 804: WAITING after 480s).
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
            max_wait=720.0,
        )
    assert result["status"] == "READY"
    assert result["hitCount"] >= 1
    # Top hit should be a NAC-family protein.
    top = result["hits"][0]
    assert top["accession"]
    assert top["description"]
