"""Tests for the shared _http.request_with_retry helper.

This helper centralizes the 429/5xx retry + Retry-After-capped-at-60s +
progress-notify + status → typed-exception mapping that 9 backends were
duplicating before Wave D. See tests/test_<backend>.py for the integration
tests that exercise it via each backend's wrapper.
"""

from __future__ import annotations

import asyncio
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


# --- the label lies in BOTH directions ---------------------------------------
# v1.19.3 decided this on Content-Type alone and broke
# `arabidopsis_natural_variation`: tools.1001genomes.org serves valid JSON under
# `Content-Type: text/html; charset=UTF-8`. Rejecting it as a challenge page was
# the same mistake as the bug the check exists to fix — trusting a label over
# the content it describes. Captured live 2026-07-28:
#
#   HTTP/1.1 200 OK
#   Server: Apache/2.4.58 (Ubuntu)
#   Content-Type: text/html; charset=UTF-8
#   {"regions":[{"reg_str": "Chr3:19025192..19026872","dir": "+"}]}

ONEKG_MISLABELLED_JSON = '{"regions":[{"reg_str": "Chr3:19025192..19026872","dir": "+"}]}'


@pytest.mark.asyncio
async def test_json_body_mislabelled_as_html_is_still_payload(httpx_mock: HTTPXMock) -> None:
    """A real payload under a wrong Content-Type must NOT be rejected."""
    httpx_mock.add_response(
        text=ONEKG_MISLABELLED_JSON, headers={"Content-Type": "text/html; charset=UTF-8"}
    )
    async with httpx.AsyncClient() as client:
        resp = await _http.request_with_retry(
            client, "GET", "https://example.test/api/v2/gi2coords", service="1001 Genomes"
        )
    assert resp.json()["regions"][0]["dir"] == "+"
    assert len(httpx_mock.get_requests()) == 1, "a valid payload must not be retried"


@pytest.mark.asyncio
async def test_html_body_mislabelled_as_json_is_still_rejected(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The converse: sniffing the body catches a challenge page under any label.

    Header-based detection missed this case entirely — a WAF is under no
    obligation to label its challenge `text/html`.
    """
    monkeypatch.setattr(_http.asyncio, "sleep", _no_sleep)
    for _ in range(3):
        httpx_mock.add_response(text=CHALLENGE_BODY, headers={"Content-Type": "application/json"})
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamUnavailableError):
            await _http.request_with_retry(
                client, "GET", "https://example.test/x", service="probe", max_retries=3
            )
    assert len(httpx_mock.get_requests()) == 3


# --- upstream release capture ------------------------------------------------
# Only a header on the ANSWERING response can state the release that produced
# it. A separate /info call is a different request and may describe a different
# release, so it can be confidently wrong — which is the failure mode this
# codebase is most prone to. These pin that None means "not stated", never
# "no release exists".


def _resp(headers: dict[str, str]) -> httpx.Response:
    return httpx.Response(200, headers=headers, content=b"{}")


def test_upstream_version_reads_uniprot_header() -> None:
    assert _http.upstream_version(_resp({"X-UniProt-Release": "2026_02"})) == "2026_02"


def test_upstream_version_reads_interpro_header() -> None:
    assert _http.upstream_version(_resp({"InterPro-Version": "109.0"})) == "109.0"


def test_upstream_version_is_none_when_unstated() -> None:
    """Negative control: absence must be None, not a guess or a placeholder."""
    assert _http.upstream_version(_resp({"Content-Type": "application/json"})) is None


def test_upstream_version_ignores_unrelated_version_headers() -> None:
    """STRING sends an API version, which is NOT the data release.

    Reporting it as the release would be exactly the plausible-but-wrong value
    this field exists to avoid.
    """
    assert _http.upstream_version(_resp({"String-api-version": "2"})) is None


# --- Gaps named by the nightly mutation run (2026-09-16: 51 logic survivors in _http) ---
#
# Each test below was written from a surviving mutant, i.e. a behaviour change
# no test observed. The mutant it kills is named in the docstring.


def _record_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    sleeps: list[float] = []

    async def _record(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(_http.asyncio, "sleep", _record)
    return sleeps


@pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
@pytest.mark.parametrize("failure", ["503", "transport", "interposed-html"])
@pytest.mark.asyncio
async def test_backoff_doubles_from_one_second_and_stops_at_max_retries(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """The schedule is 1 s, 2 s, ... and exactly max_retries requests are made.

    Survivors: ``delay = 1.0`` -> 2.0; ``delay *= 2`` -> ``= 2`` / ``/= 2`` /
    ``*= 3``; ``attempt < max_retries - 1`` -> ``<=`` and ``max_retries + 1``,
    on all three retry paths (5xx, transport error, interposed HTML). The
    existing tests only capped the sleep at 60 s and only asserted "raises",
    so a fourth request or a wrong first delay passed unseen.
    """
    sleeps = _record_sleeps(monkeypatch)
    notices: list[str] = []

    async def _notify(text: str) -> None:
        notices.append(text)

    monkeypatch.setattr(_http.progress, "notify", _notify)
    url = f"https://example.test/backoff-{failure}"
    for _ in range(5):  # one MORE than the budget: a fifth request must never happen
        if failure == "503":
            httpx_mock.add_response(url=url, status_code=503)
        elif failure == "transport":
            httpx_mock.add_exception(httpx.ConnectError("boom"), url=url)
        else:
            httpx_mock.add_response(url=url, html="<html><body>challenge</body></html>")
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamUnavailableError):
            await _http.request_with_retry(client, "GET", url, service="example", max_retries=4)
    # Four attempts pin the DOUBLING: with three, [1, 2] cannot tell "*= 2" from "= 2".
    assert sleeps == [1.0, 2.0, 4.0], sleeps
    assert len(httpx_mock.get_requests(url=url)) == 4
    # The progress line numbers the attempt that is ABOUT to run, out of the budget.
    assert [n[n.index("(attempt") :] for n in notices] == [
        "(attempt 2/4)",
        "(attempt 3/4)",
        "(attempt 4/4)",
    ], notices


@pytest.mark.asyncio
async def test_retry_after_below_the_cap_is_honoured_exactly(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Survivor: ``retry_after_hdr = resp.headers.get("Retry-After")`` -> ``None``.

    The cap test only proves a huge header is clamped; a header the upstream
    means (5 s) must replace the 1 s backoff, not be ignored.
    """
    sleeps = _record_sleeps(monkeypatch)
    url = "https://example.test/retry-after"
    httpx_mock.add_response(url=url, status_code=429, headers={"Retry-After": "5"})
    httpx_mock.add_response(url=url, json={"ok": True})
    async with httpx.AsyncClient() as client:
        await _http.request_with_retry(client, "GET", url, service="example")
    assert sleeps == [5.0], sleeps


@pytest.mark.asyncio
async def test_unparseable_retry_after_falls_back_to_the_backoff(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Survivor: the ``except ValueError: retry_after = delay`` fallback -> ``None``,
    which then reaches ``min(None, 60.0)`` and raises TypeError. RFC 9110 allows
    an HTTP-date here; we do not parse it, so it must fall back to the schedule."""
    sleeps = _record_sleeps(monkeypatch)
    url = "https://example.test/retry-after-date"
    httpx_mock.add_response(
        url=url, status_code=503, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}
    )
    httpx_mock.add_response(url=url, json={"ok": True})
    async with httpx.AsyncClient() as client:
        resp = await _http.request_with_retry(client, "GET", url, service="example")
    assert resp.json() == {"ok": True}
    assert sleeps == [1.0], sleeps


@pytest.mark.asyncio
async def test_size_cap_is_inclusive_at_the_boundary(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A body of exactly the cap is accepted; one byte more is refused, and the
    refusal names the service. Both the Content-Length check and the streamed
    check.

    Survivors: ``> _MAX_RESPONSE_BYTES`` -> ``>=`` (twice) and
    ``_too_large(service, ...)`` -> ``_too_large(None, ...)`` (twice).
    """
    from pytest_httpx import IteratorStream

    monkeypatch.setattr(_http, "_MAX_RESPONSE_BYTES", 5)
    base = "https://example.test/cap"
    httpx_mock.add_response(url=f"{base}/len-5", content=b"x" * 5)  # Content-Length: 5
    httpx_mock.add_response(url=f"{base}/len-6", content=b"x" * 6)
    httpx_mock.add_response(url=f"{base}/stream-5", stream=IteratorStream([b"xx", b"xxx"]))
    httpx_mock.add_response(url=f"{base}/stream-6", stream=IteratorStream([b"xx", b"xxxx"]))
    async with httpx.AsyncClient() as client:
        ok = await _http.request_with_retry(client, "GET", f"{base}/len-5", service="example")
        assert ok.content == b"x" * 5
        with pytest.raises(PlantGenomicsError, match=r"example response too large.*Content-Length"):
            await _http.request_with_retry(client, "GET", f"{base}/len-6", service="example")
        ok = await _http.request_with_retry(client, "GET", f"{base}/stream-5", service="example")
        assert ok.content == b"xxxxx"
        with pytest.raises(PlantGenomicsError, match=r"example response too large.*streamed"):
            await _http.request_with_retry(client, "GET", f"{base}/stream-6", service="example")


@pytest.mark.asyncio
async def test_missing_content_type_is_handled_as_no_media_type(httpx_mock: HTTPXMock) -> None:
    """Survivor: ``headers.get("content-type", "")`` -> default ``None`` (then
    ``.split`` raises). A 200 with no Content-Type at all is still a payload."""
    url = "https://example.test/no-ct"
    httpx_mock.add_response(url=url, content=b'{"ok": true}', headers={})
    async with httpx.AsyncClient() as client:
        resp = await _http.request_with_retry(client, "GET", url, service="example")
    assert "content-type" not in resp.headers, "premise: the response carries no media type"
    assert _http._media_type(resp) == ""
    assert resp.json() == {"ok": True}
    # The reassembled response keeps its request (survivor: ``request=None``);
    # callers and error messages rely on ``resp.request.url``.
    assert resp.request is not None and str(resp.request.url) == url


@pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
@pytest.mark.asyncio
async def test_bom_and_whitespace_before_html_do_not_hide_an_interposed_page(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Survivors: the ``lstrip().lstrip(BOM).lstrip()`` chain mutated to
    ``rstrip`` at each link, and the BOM argument dropped. A challenge page
    that begins with a UTF-8 BOM and a newline is still a challenge page.
    Positive control: the same prefix before JSON is a payload.
    """
    _record_sleeps(monkeypatch)
    prefix = b"\xef\xbb\xbf \n\t"
    html_url = "https://example.test/bom-html"
    json_url = "https://example.test/bom-json"
    for _ in range(3):
        httpx_mock.add_response(
            url=html_url, content=prefix + b"<!DOCTYPE HTML><html>challenge</html>"
        )
    httpx_mock.add_response(url=json_url, content=prefix + b'{"ok": true}')
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamUnavailableError):
            await _http.request_with_retry(client, "GET", html_url, service="example")
        resp = await _http.request_with_retry(client, "GET", json_url, service="example")
    assert resp.content.endswith(b'{"ok": true}')


@pytest.mark.asyncio
async def test_configured_timeout_reaches_the_transport(httpx_mock: HTTPXMock) -> None:
    """Survivor: ``timeout=timeout`` -> ``timeout=None`` on the request (no
    timeout at all). The value the caller configured must be what httpx uses."""
    url = "https://example.test/timeout"
    httpx_mock.add_response(url=url, json={})
    async with httpx.AsyncClient() as client:
        await _http.request_with_retry(client, "GET", url, service="example", timeout=7.5)
    req = httpx_mock.get_request(url=url)
    assert req is not None
    assert req.extensions["timeout"] == {"connect": 7.5, "read": 7.5, "write": 7.5, "pool": 7.5}
    # And the documented default is 30 s (survivor: ``timeout: float = 30.0`` -> 31.0).
    httpx_mock.add_response(url=f"{url}-default", json={})
    async with httpx.AsyncClient() as client:
        await _http.request_with_retry(client, "GET", f"{url}-default", service="example")
    req = httpx_mock.get_request(url=f"{url}-default")
    assert req is not None and req.extensions["timeout"]["read"] == 30.0


# ---- a 403 that refuses on rate, and a per-upstream limit (issue #153) ------

_REFUSAL = re.compile(r"too high request rate")
# OrthoDB's refusal page, verbatim from a live 403 (2026-09-23).
_REFUSAL_PAGE = (
    "<html>\nYour query was rejected.\nThe reason can be any of the 3 below:\n<ul>\n"
    "<li> too high request rate</li>\n<li> your IP being blocked</li>\n"
    "<li> site being overloaded</li>\n</ul>\n</body>\n</html>"
)


@pytest.mark.asyncio
async def test_403_matching_the_refusal_pattern_is_retried(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_http.asyncio, "sleep", _no_sleep)
    httpx_mock.add_response(url="https://example.test/r403", status_code=403, text=_REFUSAL_PAGE)
    httpx_mock.add_response(url="https://example.test/r403", json={"ok": True})
    async with httpx.AsyncClient() as client:
        resp = await _http.request_with_retry(
            client,
            "GET",
            "https://example.test/r403",
            service="example",
            retry_403_pattern=_REFUSAL,
        )
    assert resp.json() == {"ok": True}


@pytest.mark.asyncio
async def test_403_is_not_retried_without_the_pattern_or_on_another_body(
    httpx_mock: HTTPXMock,
) -> None:
    """The opt-in is body-matched: a 403 that is not the refusal page stays terminal."""
    httpx_mock.add_response(url="https://example.test/a", status_code=403, text=_REFUSAL_PAGE)
    httpx_mock.add_response(url="https://example.test/b", status_code=403, text="Forbidden")
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlantGenomicsError, match="HTTP 403"):
            await _http.request_with_retry(
                client, "GET", "https://example.test/a", service="example"
            )
        with pytest.raises(PlantGenomicsError, match="HTTP 403: Forbidden"):
            await _http.request_with_retry(
                client,
                "GET",
                "https://example.test/b",
                service="example",
                retry_403_pattern=_REFUSAL,
            )
    # One request each: neither was retried (pytest-httpx fails on unused or
    # over-used responses, so a retry would have needed a second response).


@pytest.mark.asyncio
async def test_exhausted_403_refusals_say_what_the_upstream_said(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_http.asyncio, "sleep", _no_sleep)
    for _ in range(3):
        httpx_mock.add_response(
            url="https://example.test/r403x", status_code=403, text=_REFUSAL_PAGE
        )
    async with httpx.AsyncClient() as client:
        with pytest.raises(RateLimitError, match=r"exhausted 3 retries \(HTTP 403: .*too high"):
            await _http.request_with_retry(
                client,
                "GET",
                "https://example.test/r403x",
                service="example",
                retry_403_pattern=_REFUSAL,
            )


def _counting_transport(
    peak: list[int], status_when_crowded: int | None = None
) -> httpx.MockTransport:
    """A transport that records the most requests it ever held at once.

    With ``status_when_crowded`` it refuses any request that arrives while
    another is in flight, as OrthoDB does (live probe 2026-09-23).
    """
    in_flight = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight
        in_flight += 1
        peak[0] = max(peak[0], in_flight)
        try:
            crowded = in_flight > 1
            await asyncio.sleep(0.01)
            if crowded and status_when_crowded is not None:
                return httpx.Response(status_when_crowded, text=_REFUSAL_PAGE)
            return httpx.Response(200, json={"ok": True})
        finally:
            in_flight -= 1

    return httpx.MockTransport(handler)


async def _eight_at_once(limit: _http.UpstreamLimit | None) -> int:
    peak = [0]
    async with httpx.AsyncClient(transport=_counting_transport(peak)) as client:
        await asyncio.gather(
            *(
                _http.request_with_retry(
                    client, "GET", f"https://example.test/{i}", service="example", limit=limit
                )
                for i in range(8)
            )
        )
    return peak[0]


@pytest.mark.asyncio
async def test_upstream_limit_caps_requests_in_flight() -> None:
    # Positive control: unlimited, the transport does see all eight at once,
    # so a peak of 1 below is the limit working, not a harness that serialises.
    assert await _eight_at_once(None) == 8
    assert await _eight_at_once(_http.UpstreamLimit(1)) == 1
    assert await _eight_at_once(_http.UpstreamLimit(3)) == 3


@pytest.mark.asyncio
async def test_upstream_limit_is_released_during_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A request sleeping before its retry must not hold its slot."""
    order: list[str] = []
    real_sleep = asyncio.sleep

    async def handler(request: httpx.Request) -> httpx.Response:
        order.append(request.url.path)
        if request.url.path == "/slow" and order.count("/slow") == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    async def _backoff(seconds: float) -> None:
        await real_sleep(0.05)

    monkeypatch.setattr(_http.asyncio, "sleep", _backoff)
    limit = _http.UpstreamLimit(1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:

        async def fast() -> None:
            await real_sleep(0.01)  # arrive while /slow is in its backoff
            await _http.request_with_retry(
                client, "GET", "https://example.test/fast", service="example", limit=limit
            )

        await asyncio.gather(
            _http.request_with_retry(
                client, "GET", "https://example.test/slow", service="example", limit=limit
            ),
            fast(),
        )
    assert order == ["/slow", "/fast", "/slow"]


def test_upstream_limit_works_across_event_loops() -> None:
    """One module-level limit serves every loop the process runs.

    An ``asyncio.Semaphore`` binds to the first loop it makes a caller wait
    on and raises ``RuntimeError`` on any later loop, which a module-level
    limit meets under pytest's per-test loops and any host that restarts one.
    """
    limit = _http.UpstreamLimit(1)
    assert asyncio.run(_eight_at_once(limit)) == 1
    assert asyncio.run(_eight_at_once(limit)) == 1


@pytest.mark.asyncio
async def test_a_refusal_followed_by_interposed_pages_names_the_pages(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The error names the LAST failure: a refusal earlier in the budget must
    not be reported for a run that ended on challenge pages."""
    monkeypatch.setattr(_http.asyncio, "sleep", _no_sleep)
    url = "https://example.test/mixed"
    httpx_mock.add_response(url=url, status_code=403, text=_REFUSAL_PAGE)
    for _ in range(2):
        httpx_mock.add_response(url=url, text="<html>challenge</html>")
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamUnavailableError, match="not the requested data"):
            await _http.request_with_retry(
                client, "GET", url, service="example", retry_403_pattern=_REFUSAL
            )
