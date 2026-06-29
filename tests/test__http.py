"""Tests for the shared _http.request_with_retry helper.

This helper centralizes the 429/5xx retry + Retry-After-capped-at-60s +
progress-notify + status → typed-exception mapping that 9 backends were
duplicating before Wave D. See tests/test_<backend>.py for the integration
tests that exercise it via each backend's wrapper.
"""

from __future__ import annotations

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
