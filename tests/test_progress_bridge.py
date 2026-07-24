"""Coverage for the progressToken → Reporter bridge (audit I3).

``server._build_reporter`` is the sole wiring between MCP's progress protocol
and the HTTP-layer ``progress.notify()`` calls the retry loops and BLAST poller
emit. No prior test installed a request context carrying a ``progressToken``, so
token extraction (server.py:106-132), the ``_send`` closure that calls
``session.send_progress_notification``, and the install/reset wrapper in
``_call_tool`` (server.py:1473-1480) were entirely uncovered — an SDK signature
change would have silently no-op'd every progress notification with zero signal.

These tests fake the SDK's ``request_ctx`` contextvar with a recording session
stub and assert a backend-emitted ``progress.notify`` actually reaches it.
"""

from __future__ import annotations

from typing import Any, cast

import mcp.server.lowlevel.server as _low
import pytest

from plant_genomics_mcp import ensembl_plants, progress, server


class _RecordingSession:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def send_progress_notification(
        self,
        *,
        progress_token: Any,
        progress: float,
        total: float | None,
        message: str | None,
    ) -> None:
        self.calls.append(
            {
                "progress_token": progress_token,
                "progress": progress,
                "total": total,
                "message": message,
            }
        )


class _Meta:
    def __init__(self, token: Any) -> None:
        self.progressToken = token


class _Ctx:
    def __init__(self, session: Any, meta: Any) -> None:
        self.session = session
        self.meta = meta


def test_build_reporter_none_outside_request_context() -> None:
    """No request context installed → request_ctx.get() raises → None."""
    assert server._build_reporter() is None


def test_build_reporter_none_when_no_progress_token() -> None:
    """Client opted out (no progressToken) → no reporter."""
    ctx = _Ctx(session=_RecordingSession(), meta=_Meta(token=None))
    # ``_Ctx`` is a structural test double for RequestContext; cast past the
    # invariant ContextVar type rather than construct the real generic.
    token = _low.request_ctx.set(cast(Any, ctx))
    try:
        assert server._build_reporter() is None
    finally:
        _low.request_ctx.reset(token)


@pytest.mark.asyncio
async def test_progress_notification_reaches_session_through_call_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A backend-emitted progress.notify is delivered to the session with the
    client's progressToken, exercising the bridge + install/reset end-to-end."""
    session = _RecordingSession()
    ctx = _Ctx(session=session, meta=_Meta(token="tok-xyz"))

    async def fake_backend(
        client: Any, locus: str, organism: str | int = "arabidopsis_thaliana"
    ) -> dict[str, Any]:
        await progress.notify("retrying upstream")
        return {"locus": locus, "organism": organism}

    monkeypatch.setattr(ensembl_plants, "lookup_locus", fake_backend)

    # ``_Ctx`` is a structural test double for RequestContext; cast past the
    # invariant ContextVar type rather than construct the real generic.
    token = _low.request_ctx.set(cast(Any, ctx))
    try:
        result = await server._call_tool("ensembl_plants_lookup_locus", {"locus": "AT1G01010"})
    finally:
        _low.request_ctx.reset(token)

    assert result == {"locus": "AT1G01010", "organism": "arabidopsis_thaliana"}
    assert session.calls, "no progress notification reached the session"
    first = session.calls[0]
    assert first["progress_token"] == "tok-xyz"
    assert first["message"] == "retrying upstream"
    assert first["progress"] == 1.0  # Reporter's monotonic step counter
    assert first["total"] is None


@pytest.mark.asyncio
async def test_reporter_not_installed_when_no_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a progressToken, _call_tool takes the reporter-is-None branch and
    progress.notify in the backend is a silent no-op (nothing recorded)."""
    session = _RecordingSession()
    ctx = _Ctx(session=session, meta=_Meta(token=None))

    async def fake_backend(
        client: Any, locus: str, organism: str | int = "arabidopsis_thaliana"
    ) -> dict[str, Any]:
        await progress.notify("should be dropped")
        return {"locus": locus}

    monkeypatch.setattr(ensembl_plants, "lookup_locus", fake_backend)

    # ``_Ctx`` is a structural test double for RequestContext; cast past the
    # invariant ContextVar type rather than construct the real generic.
    token = _low.request_ctx.set(cast(Any, ctx))
    try:
        result = await server._call_tool("ensembl_plants_lookup_locus", {"locus": "AT1G01010"})
    finally:
        _low.request_ctx.reset(token)

    assert result == {"locus": "AT1G01010"}
    assert session.calls == [], "notification leaked despite no progressToken"
