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
import re
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import blast, progress
from plant_genomics_mcp.errors import PlantGenomicsError

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
async def test_a_search_still_waiting_at_max_wait_is_an_outage_naming_its_rid(
    httpx_mock: HTTPXMock,
) -> None:
    """NCBI still has the search queued, so nothing is missing: it is not
    ``NotFoundError``, which a caller treats as final, but an outage naming the
    RID to re-poll (live 2026-09-30: RID BV98SFXM016 was still WAITING after
    720 s and again a minute later). An expired RID stays not found, below."""
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
        with pytest.raises(
            blast.UpstreamUnavailableError,
            match=r"^\[UpstreamUnavailableError\] BLAST RID=RIDLATE still WAITING after "
            r"max_wait=120s",
        ):
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


# The live search may wait this long for NCBI's queue. The nightly caps every
# test at --timeout=240, which killed this one first whenever the queue took
# longer, so its hitCount check never ran there (2026-09-30); its own timeout
# marker (read by pytest-timeout, installed in the nightly) covers the wait.
LIVE_BLAST_MAX_WAIT = 720.0


@live_only
@pytest.mark.timeout(LIVE_BLAST_MAX_WAIT + 120)
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
            max_wait=LIVE_BLAST_MAX_WAIT,
        )
    assert result["status"] == "READY"
    # HITLIST_SIZE reached NCBI: it asked for 5 hits and sent no more.
    assert 1 <= result["hitCount"] <= 5
    # Top hit should be a NAC-family protein.
    top = result["hits"][0]
    assert top["accession"]
    assert top["description"]


# ---------- the Put form, read back (#96 mutation survivors) ----------
# NCBI's URL API takes the search as form fields on a POST (CMD=Put, PROGRAM,
# DATABASE, QUERY, HITLIST_SIZE, EXPECT, FORMAT_TYPE, MEGABLAST=on for
# megablast). No test read the form, so a renamed or re-cased field survived.

_EMAIL = "tests@example.org"


def _form(request: httpx.Request) -> dict[str, str]:
    parsed = parse_qs(request.content.decode(), keep_blank_values=True)
    assert all(len(v) == 1 for v in parsed.values()), parsed
    return {k: v[0] for k, v in parsed.items()}


def _put_form(**search: str) -> dict[str, str]:
    return {
        "CMD": "Put",
        "FORMAT_TYPE": "Text",
        **search,
        "tool": blast.TOOL_ID,
        "email": _EMAIL,
    }


@pytest.mark.asyncio
async def test_the_put_form_carries_the_search_as_asked(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLANT_GENOMICS_MCP_NCBI_EMAIL", _EMAIL)
    httpx_mock.add_response(
        method="POST", url=blast.BASE_URL, text=_put_response("RID1", 7), is_reusable=True
    )
    searches = [
        ("blastn", True, {"MEGABLAST": "on"}),
        # MEGABLAST only for blastn, and only when asked.
        ("blastn", False, {}),
        ("blastp", True, {}),
    ]
    async with httpx.AsyncClient() as client:
        for program, megablast, extra in searches:
            rid_rtoe = await blast.submit(
                client,
                "ACGTACGT",
                program,
                "core_nt",
                hitlist_size=5,
                expect=0.001,
                megablast=megablast,
            )
            assert rid_rtoe == ("RID1", 7)
            request = httpx_mock.get_requests()[-1]
            assert _form(request) == _put_form(
                PROGRAM=program,
                DATABASE="core_nt",
                QUERY="ACGTACGT",
                HITLIST_SIZE="5",
                EXPECT="0.001",
                **extra,
            ), (program, megablast)
            assert request.method == "POST"
            assert request.extensions["timeout"]["read"] == blast.DEFAULT_TIMEOUT


@pytest.mark.asyncio
async def test_blast_sequence_defaults_reach_the_put_form(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLANT_GENOMICS_MCP_NCBI_EMAIL", _EMAIL)
    httpx_mock.add_response(method="POST", url=blast.BASE_URL, text=_put_response("RID2", 0))
    httpx_mock.add_response(method="GET", text=_searchinfo_response("READY"))
    httpx_mock.add_response(method="GET", text=RESULT_REPORT)
    async with httpx.AsyncClient() as client:
        result = await blast.blast_sequence(client, "MNSAKQ", max_wait=300.0)
    assert result["rid"] == "RID2"
    (put,) = httpx_mock.get_requests(method="POST")
    assert _form(put) == _put_form(
        PROGRAM="blastp",
        DATABASE="swissprot",
        QUERY="MNSAKQ",
        HITLIST_SIZE="10",
        EXPECT="10.0",
    )


@pytest.mark.asyncio
async def test_a_refused_put_is_named_and_the_submission_is_reported(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    httpx_mock.add_response(method="POST", url=blast.BASE_URL, status_code=400, text="bad")
    httpx_mock.add_response(method="POST", url=blast.BASE_URL, text=_put_response("RID3", 12))
    sent: list[str | None] = []

    async def _send(_p: float, _t: float | None, message: str | None) -> None:
        sent.append(message)

    token = progress.set_reporter(progress.Reporter(_send))
    try:
        async with httpx.AsyncClient() as client:
            with pytest.raises(PlantGenomicsError, match=r"^BLAST Put → HTTP 400"):
                await blast.submit(
                    client,
                    "MNSAKQ",
                    "blastp",
                    "swissprot",
                    hitlist_size=10,
                    expect=10.0,
                    megablast=False,
                )
            # Positive control: the next Put is read and reported.
            assert await blast.submit(
                client,
                "MNSAKQ",
                "blastp",
                "swissprot",
                hitlist_size=10,
                expect=10.0,
                megablast=False,
            ) == ("RID3", 12)
    finally:
        progress.reset_reporter(token)
    assert sent[-1] == "BLAST submitted — RID=RID3, RTOE=12s (program=blastp, db=swissprot)"


def test_the_live_search_has_a_budget_beyond_the_nightly_cap() -> None:
    """The nightly's per-test cap is shorter than the search's own wait, so the
    live test must carry a timeout of its own that covers the wait."""
    workflow = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "live-nightly.yml"
    cap = re.search(r"--timeout=(\d+)", workflow.read_text(encoding="utf-8"))
    assert cap is not None, "the nightly no longer sets --timeout"
    marks = [
        m
        for m in getattr(test_live_blastp_small_query_returns_hits, "pytestmark", [])
        if m.name == "timeout"
    ]
    assert marks, "the live BLAST test has no timeout of its own"
    assert marks[0].args[0] > LIVE_BLAST_MAX_WAIT > int(cap.group(1))


def test_every_text_a_client_reads_names_the_outage_for_a_slow_search() -> None:
    """The tool's description, each of its parameters and the BLAST prompt are
    what a client reads before calling: none may still promise NotFoundError
    for a search past max_wait (#224 review: the max_wait parameter did)."""
    from plant_genomics_mcp import prompts, server

    tool = next(t for t in server.TOOLS if t.name == "blast_sequence")
    params = {k: v.get("description", "") for k, v in tool.input_schema["properties"].items()}
    prompt = prompts._render_find_homologs("MEDQ", "blastp")
    for where, text in [("tool", tool.description or ""), ("prompt", prompt), *params.items()]:
        assert "NotFoundError" not in text, where
    for text in (tool.description or "", params["max_wait"], prompt):
        assert "UpstreamUnavailableError" in text, text
