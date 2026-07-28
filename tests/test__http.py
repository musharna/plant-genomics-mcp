"""Tests for the shared _http.request_with_retry helper.

This helper centralizes the 429/5xx retry + Retry-After-capped-at-60s +
progress-notify + status → typed-exception mapping that 9 backends were
duplicating before Wave D. See tests/test_<backend>.py for the integration
tests that exercise it via each backend's wrapper.
"""

from __future__ import annotations

import re

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import _http
from plant_genomics_mcp.errors import (
    NotFoundError,
    PlantGenomicsError,
    RateLimitError,
    UpstreamUnavailableError,
)


async def _no_sleep(_seconds: float) -> None:
    """Skip real backoff delays in retry tests."""


@pytest.mark.asyncio
async def test_returns_httpx_response_on_200(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url="https://example.test/ok", json={"hello": "world"})
    async with httpx.AsyncClient() as client:
        resp = await _http.request_with_retry(
            client, "GET", "https://example.test/ok", service="example"
        )
    assert resp.status_code == 200
    assert resp.json() == {"hello": "world"}


@pytest.mark.asyncio
async def test_rejects_oversized_response(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 200 body larger than the size cap is refused, not parsed (audit M4)."""
    monkeypatch.setattr(_http, "_MAX_RESPONSE_BYTES", 5)
    # text= sets Content-Length: 20, so this exercises the up-front header reject.
    httpx_mock.add_response(url="https://example.test/big", text="x" * 20)
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlantGenomicsError, match="Content-Length"):
            await _http.request_with_retry(
                client, "GET", "https://example.test/big", service="example"
            )


@pytest.mark.asyncio
async def test_rejects_oversized_chunked_response(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chunked body with NO Content-Length is capped mid-stream, before the
    whole body is buffered (audit L4 — the streaming incremental cap path)."""
    from pytest_httpx import IteratorStream

    monkeypatch.setattr(_http, "_MAX_RESPONSE_BYTES", 5)
    httpx_mock.add_response(
        url="https://example.test/chunked",
        stream=IteratorStream([b"xxx", b"yyy", b"zzz"]),  # 9 bytes, no Content-Length
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlantGenomicsError, match="streamed"):
            await _http.request_with_retry(
                client, "GET", "https://example.test/chunked", service="example"
            )


@pytest.mark.asyncio
async def test_raises_not_found_on_404_by_default(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url="https://example.test/missing", status_code=404, text="gone")
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="HTTP 404"):
            await _http.request_with_retry(
                client, "GET", "https://example.test/missing", service="example"
            )


@pytest.mark.asyncio
async def test_returns_sentinel_value_on_404_when_opted_in(httpx_mock: HTTPXMock) -> None:
    """KEGG treats 404 as 'no record' rather than an error."""
    httpx_mock.add_response(url="https://example.test/maybe", status_code=404, text="")
    async with httpx.AsyncClient() as client:
        result = await _http.request_with_retry(
            client,
            "GET",
            "https://example.test/maybe",
            service="example",
            not_found_returns="",
        )
    assert result == ""


@pytest.mark.asyncio
async def test_retries_on_429_then_succeeds(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://example.test/r", status_code=429, headers={"Retry-After": "0"}
    )
    httpx_mock.add_response(url="https://example.test/r", json={"ok": True})
    async with httpx.AsyncClient() as client:
        resp = await _http.request_with_retry(
            client, "GET", "https://example.test/r", service="example"
        )
    assert resp.json() == {"ok": True}


@pytest.mark.asyncio
async def test_retries_on_503_then_succeeds(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url="https://example.test/u", status_code=503)
    httpx_mock.add_response(url="https://example.test/u", json={"ok": True})
    async with httpx.AsyncClient() as client:
        resp = await _http.request_with_retry(
            client, "GET", "https://example.test/u", service="example"
        )
    assert resp.json() == {"ok": True}


@pytest.mark.asyncio
async def test_retry_after_capped_at_60s(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hostile upstream returning Retry-After: 3600 (one hour) must not
    pin the agent. The 60s ceiling is shared policy (Wave B2). This is the
    canonical test for the cap; per-backend tests can be removed once they
    delegate here."""
    sleeps: list[float] = []

    async def _record(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(_http.asyncio, "sleep", _record)

    httpx_mock.add_response(
        url="https://example.test/cap",
        status_code=429,
        headers={"Retry-After": "3600"},
    )
    httpx_mock.add_response(url="https://example.test/cap", json={"ok": True})
    async with httpx.AsyncClient() as client:
        await _http.request_with_retry(client, "GET", "https://example.test/cap", service="example")
    assert sleeps, "retry path never slept"
    assert max(sleeps) <= 60.0, f"sleep {max(sleeps)} exceeded 60s cap"


@pytest.mark.asyncio
async def test_raises_rate_limit_on_final_429(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _noop(_: float) -> None:
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _noop)

    for _ in range(3):
        httpx_mock.add_response(
            url="https://example.test/dead",
            status_code=429,
            headers={"Retry-After": "0"},
        )
    async with httpx.AsyncClient() as client:
        with pytest.raises(RateLimitError, match="exhausted"):
            await _http.request_with_retry(
                client, "GET", "https://example.test/dead", service="example"
            )


@pytest.mark.asyncio
async def test_raises_upstream_unavailable_on_exhausted_5xx(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _noop(_: float) -> None:
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _noop)

    for _ in range(3):
        httpx_mock.add_response(url="https://example.test/down", status_code=503)
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamUnavailableError, match="exhausted"):
            await _http.request_with_retry(
                client, "GET", "https://example.test/down", service="example"
            )


@pytest.mark.asyncio
async def test_raises_plant_genomics_error_on_non_retryable_4xx(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(url="https://example.test/bad", status_code=400, text="bad request")
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlantGenomicsError, match="HTTP 400"):
            await _http.request_with_retry(
                client, "GET", "https://example.test/bad", service="example"
            )


@pytest.mark.asyncio
async def test_retries_on_connect_timeout_then_succeeds(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient ConnectTimeout (the failure that reddened the 2026-06-29
    benchmark when bar.utoronto.ca was briefly unreachable from the CI
    runner) must be retried, not surfaced on the first attempt. Transport
    exceptions are raised before any HTTP status exists, so the original
    status-only retry loop let them propagate immediately with zero retries.
    """
    sleeps: list[float] = []

    async def _record(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(_http.asyncio, "sleep", _record)

    httpx_mock.add_exception(
        httpx.ConnectTimeout("connect timed out"), url="https://example.test/t"
    )
    httpx_mock.add_response(url="https://example.test/t", json={"ok": True})
    async with httpx.AsyncClient() as client:
        resp = await _http.request_with_retry(
            client, "GET", "https://example.test/t", service="example"
        )
    assert resp.json() == {"ok": True}
    assert sleeps, "transport-error retry path never slept"


@pytest.mark.asyncio
async def test_transport_retry_sleep_capped_at_60s(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The transport-error backoff reuses the same 60s ceiling as the
    status-code path, so a long exhausted retry chain can't pin the agent."""
    sleeps: list[float] = []

    async def _record(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(_http.asyncio, "sleep", _record)

    httpx_mock.add_exception(httpx.ConnectError("refused"), url="https://example.test/cap2")
    httpx_mock.add_response(url="https://example.test/cap2", json={"ok": True})
    async with httpx.AsyncClient() as client:
        await _http.request_with_retry(
            client, "GET", "https://example.test/cap2", service="example"
        )
    assert sleeps, "retry path never slept"
    assert max(sleeps) <= 60.0, f"sleep {max(sleeps)} exceeded 60s cap"


@pytest.mark.asyncio
async def test_raises_upstream_unavailable_on_exhausted_transport_errors(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When every attempt hits a transport exception, the helper raises the
    typed UpstreamUnavailableError naming the underlying exception class —
    not a bare httpx error and not a misleading 'last HTTP None'."""

    async def _noop(_: float) -> None:
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", _noop)

    for _ in range(3):
        httpx_mock.add_exception(
            httpx.ConnectTimeout("connect timed out"), url="https://example.test/dead2"
        )
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamUnavailableError, match="exhausted.*ConnectTimeout"):
            await _http.request_with_retry(
                client, "GET", "https://example.test/dead2", service="example"
            )


@pytest.mark.asyncio
async def test_supports_post_with_form_data(httpx_mock: HTTPXMock) -> None:
    """Phytozome BioMart POSTs form-encoded XML."""
    httpx_mock.add_response(url="https://example.test/biomart", method="POST", text="row1\trow2\n")
    async with httpx.AsyncClient() as client:
        resp = await _http.request_with_retry(
            client,
            "POST",
            "https://example.test/biomart",
            service="biomart",
            data={"query": "<xml/>"},
        )
    assert resp.text == "row1\trow2\n"


# --- content-coding: the gzip double-decode regression -----------------------
# PR #42's streaming size-cap copied upstream headers verbatim onto the
# reassembled Response. `aiter_bytes()` yields DECODED bytes, so carrying
# `Content-Encoding: gzip` across made httpx decode a SECOND time, and every
# gzipped upstream — UniProt, Phytozome, InterPro, and everything downstream of
# the locus->UniProt resolution — died with "incorrect header check".
#
# 760 tests passed against that broken build, and not because someone forgot a
# gzip case: `pytest_httpx` CANNOT serve gzip over a streaming read. Plain
# `client.stream()` + `aiter_bytes()` against a gzipped mock fails the same way
# with no project code involved. The fixture layer simply cannot reach this
# path, so the only honest regression test is a real server.


@pytest.mark.asyncio
async def test_gzipped_response_is_decoded_exactly_once() -> None:
    """Real uvicorn, real gzip, real socket — the only way to cover this path."""
    import asyncio
    import gzip
    import json as _json
    import socket

    import uvicorn
    from starlette.applications import Starlette
    from starlette.responses import Response
    from starlette.routing import Route

    payload = {"primaryAccession": "Q0WV96", "sequence": {"length": 429}}

    async def gzipped(_request: object) -> Response:
        body = gzip.compress(_json.dumps(payload).encode())
        return Response(
            content=body,
            media_type="application/json",
            headers={"Content-Encoding": "gzip", "Content-Length": str(len(body))},
        )

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    app = Starlette(routes=[Route("/gz", gzipped)])
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        assert server.started, "uvicorn never reported started"

        async with httpx.AsyncClient() as client:
            resp = await _http.request_with_retry(
                client, "GET", f"http://127.0.0.1:{port}/gz", service="probe"
            )
        # Decoded exactly once: readable JSON, not a DecodingError.
        assert resp.json() == payload
        # And the reassembled headers must not still advertise a coding this
        # body no longer carries, or the next consumer decodes it again.
        assert "content-encoding" not in {k.lower() for k in resp.headers}
    finally:
        server.should_exit = True
        await task


# --- 400-means-not-found: the Ensembl dialect --------------------------------
# Ensembl answers an unknown identifier with `400 {"error":"ID '...' not
# found"}` instead of 404, so the 404 -> NotFoundError mapping never fired and
# callers could not tell "no such gene" from "the backend is broken".
#
# The pairing below is the whole point: the SAME status code must produce
# DIFFERENT types depending on the body. Without the second test, a blanket
# 400 -> NotFoundError would pass the first and quietly mislabel every
# malformed request (an oversized region span is also a 400) as "not found".


@pytest.mark.asyncio
async def test_400_with_not_found_body_raises_notfound(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(status_code=400, json={"error": "ID 'AT1G01010' not found"})
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError):
            await _http.request_with_retry(
                client,
                "GET",
                "https://example.test/lookup",
                service="Ensembl Plants /lookup/id",
                not_found_400_pattern=re.compile(r"\bnot found\b", re.IGNORECASE),
            )


@pytest.mark.asyncio
async def test_400_without_not_found_body_stays_generic(httpx_mock: HTTPXMock) -> None:
    """A malformed request is NOT a missing gene — it must not be relabelled."""
    httpx_mock.add_response(status_code=400, json={"error": "requested region is too large"})
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlantGenomicsError) as excinfo:
            await _http.request_with_retry(
                client,
                "GET",
                "https://example.test/overlap",
                service="Ensembl Plants /overlap/region",
                not_found_400_pattern=re.compile(r"\bnot found\b", re.IGNORECASE),
            )
    assert not isinstance(excinfo.value, NotFoundError)


@pytest.mark.asyncio
async def test_400_without_the_pattern_is_unchanged(httpx_mock: HTTPXMock) -> None:
    """Opt-in: a backend that does not pass the pattern keeps today's behaviour."""
    httpx_mock.add_response(status_code=400, json={"error": "ID 'X' not found"})
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlantGenomicsError) as excinfo:
            await _http.request_with_retry(
                client, "GET", "https://example.test/x", service="other backend"
            )
    assert not isinstance(excinfo.value, NotFoundError)


# --- HTTP 200 that isn't the payload: interposed pages ------------------------
# PlantCyc/PMN sits behind Imperva. `GET /{orgid}/xmlquery` intermittently
# answers 200 text/html with an `_Incapsula_Resource` challenge instead of
# ptools-XML. Because it is a 200, the helper used to hand it straight to the
# caller, and `ET.fromstring` reported `mismatched tag: line 1, column 356` —
# blaming the upstream's DATA for what was really a blocked request. The whole
# 2026-07-28 triage went down that wrong path first.
#
# CHALLENGE_BODY below is a verbatim capture from pmn.plantcyc.org
# (2026-07-28), not a hand-written approximation.

CHALLENGE_BODY = (
    '<html>\r\n<head>\r\n<META NAME="robots" CONTENT="noindex,nofollow">\r\n'
    '<script src="/_Incapsula_Resource?SWJIYLWA=5074a744e2e3d891814e9a2dace20bd4,'
    '719d34d31c8e3a6e6fffd425f7e032f3">\r\n</script>\r\n<body>\r\n</body></html>\r\n'
)

PTOOLS_XML = (
    "<?xml version='1.0' encoding='iso-8859-1'?>\n"
    "<ptools-xml ptools-version='29.0'><metadata><num_results>0</num_results>"
    "</metadata></ptools-xml>"
)


def test_challenge_body_really_is_unparseable_as_xml() -> None:
    """Negative control for the fixture itself.

    If PMN ever changed this page to something XML parses, the tests below
    would still pass while testing nothing. Pin the premise.
    """
    from xml.etree import ElementTree as ET

    with pytest.raises(ET.ParseError):
        ET.fromstring(CHALLENGE_BODY)


@pytest.mark.asyncio
async def test_html_on_200_is_retried_then_raises_typed_error(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_http.asyncio, "sleep", _no_sleep)
    for _ in range(3):
        httpx_mock.add_response(text=CHALLENGE_BODY, headers={"Content-Type": "text/html"})
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamUnavailableError) as excinfo:
            await _http.request_with_retry(
                client,
                "GET",
                "https://example.test/ARA/xmlquery",
                service="PlantCyc xmlquery ARA",
                max_retries=3,
            )
    # The retry budget was actually spent, not short-circuited on attempt 1.
    assert len(httpx_mock.get_requests()) == 3
    # And the message names the real problem instead of an XML syntax error.
    assert "text/html" in str(excinfo.value)


@pytest.mark.asyncio
async def test_html_on_200_retried_then_succeeds(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The functional payoff: the challenge is per-request, so a retry wins.

    Pre-fix this could not happen — a 200 never reached the retry branch, so a
    transient block was a hard first-attempt failure.
    """
    monkeypatch.setattr(_http.asyncio, "sleep", _no_sleep)
    httpx_mock.add_response(text=CHALLENGE_BODY, headers={"Content-Type": "text/html"})
    httpx_mock.add_response(text=PTOOLS_XML, headers={"Content-Type": "text/xml"})
    async with httpx.AsyncClient() as client:
        resp = await _http.request_with_retry(
            client,
            "GET",
            "https://example.test/ARA/xmlquery",
            service="PlantCyc xmlquery ARA",
            max_retries=3,
        )
    assert resp.text == PTOOLS_XML
    assert len(httpx_mock.get_requests()) == 2


@pytest.mark.asyncio
async def test_allow_html_keeps_html_payloads_working(httpx_mock: HTTPXMock) -> None:
    """Positive control: NCBI QBlast serves the RID inside an HTML page.

    Without this opt-out the fix would break `blast_sequence` outright — the
    check must reject INTERPOSED html, not all html.
    """
    body = "<html><body><!--QBlastInfoBegin\n RID = ABC123\nQBlastInfoEnd--></body></html>"
    httpx_mock.add_response(text=body, headers={"Content-Type": "text/html"})
    async with httpx.AsyncClient() as client:
        resp = await _http.request_with_retry(
            client, "GET", "https://example.test/Blast.cgi", service="BLAST Put", allow_html=True
        )
    assert "RID = ABC123" in resp.text
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.asyncio
async def test_non_html_200s_are_untouched(httpx_mock: HTTPXMock) -> None:
    """Positive control for every other backend: XML/JSON/plain still pass."""
    for media in ("text/xml", "application/json", "text/plain", "application/octet-stream"):
        httpx_mock.add_response(text=PTOOLS_XML, headers={"Content-Type": media})
        async with httpx.AsyncClient() as client:
            resp = await _http.request_with_retry(
                client, "GET", "https://example.test/x", service="probe"
            )
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_interposed_html_rejected_against_a_real_server() -> None:
    """Real uvicorn, real socket — the boundary check the fixtures can't make.

    The v1.19.1 gzip regression shipped green because pytest_httpx could not
    reach the streaming path at all. Anything that touches the reassembly in
    request_with_retry gets one real-server pass on principle.
    """
    import asyncio
    import socket

    import uvicorn
    from starlette.applications import Starlette
    from starlette.responses import Response
    from starlette.routing import Route

    hits = {"n": 0}

    async def flaky(_request: object) -> Response:
        # First call challenged, second call real data — the live behaviour
        # observed at pmn.plantcyc.org, where the same URL alternated between
        # a challenge and ptools-XML seconds apart.
        hits["n"] += 1
        if hits["n"] == 1:
            return Response(content=CHALLENGE_BODY, media_type="text/html")
        return Response(content=PTOOLS_XML, media_type="text/xml")

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    app = Starlette(routes=[Route("/xmlquery", flaky)])
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        assert server.started, "uvicorn never reported started"

        async with httpx.AsyncClient() as client:
            resp = await _http.request_with_retry(
                client,
                "GET",
                f"http://127.0.0.1:{port}/xmlquery",
                service="PlantCyc xmlquery ARA",
                max_retries=3,
            )
        assert resp.text == PTOOLS_XML
        assert hits["n"] == 2, "the challenge response was not retried"
    finally:
        server.should_exit = True
        await task
