"""Coverage for the progress_token → Reporter bridge (audit I3).

``server._build_reporter`` is the sole wiring between MCP's progress protocol
and the HTTP-layer ``progress.notify()`` calls the retry loops and BLAST poller
emit. No prior test installed a request context carrying a progress token, so
token extraction, the ``_send`` closure that calls
``session.send_progress_notification``, and the install/reset wrapper in
``_call_tool`` were entirely uncovered — an SDK signature change would have
silently no-op'd every progress notification with zero signal.

That is not hypothetical: the mcp 1.x → 2.x migration changed all three moving
parts at once. The SDK dropped the ``request_ctx`` ContextVar these tests used
to monkeypatch (the context is a handler argument now), renamed the meta key
``progressToken`` → ``progress_token``, and made ``RequestParamsMeta`` a
TypedDict, so meta is a plain dict rather than an attribute-bearing object.
Each of those alone would have silently broken progress reporting.

Passing the context in directly is also what the 2.x signature makes possible:
these tests no longer reach into SDK internals to install a contextvar, so they
break when OUR contract breaks rather than when the SDK reshuffles its private
module layout.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from mcp import types

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


class _Ctx:
    """Structural stand-in for ServerRequestContext.

    ``meta`` is a plain dict because mcp 2.x models RequestParamsMeta as a
    TypedDict — passing an object with a ``.progress_token`` attribute here
    would make the test pass against a server that reads it the wrong way.
    """

    def __init__(self, session: Any, meta: dict[str, Any] | None) -> None:
        self.session = session
        self.meta = meta


def _params(name: str, arguments: dict[str, Any]) -> types.CallToolRequestParams:
    return types.CallToolRequestParams(name=name, arguments=arguments)


def test_build_reporter_none_when_no_meta() -> None:
    """No meta on the request at all → no reporter.

    Under 1.x this case was "no request context installed, so the contextvar
    lookup raises LookupError". 2.x always hands the handler a context, so the
    equivalent no-token path is a context whose meta is absent.
    """
    ctx = _Ctx(session=_RecordingSession(), meta=None)
    assert server._build_reporter(cast(Any, ctx)) is None


def test_build_reporter_none_when_no_progress_token() -> None:
    """Client opted out (no progress_token) → no reporter."""
    ctx = _Ctx(session=_RecordingSession(), meta={})
    assert server._build_reporter(cast(Any, ctx)) is None


@pytest.mark.asyncio
async def test_progress_notification_reaches_session_through_call_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A backend-emitted progress.notify is delivered to the session with the
    client's progress_token, exercising the bridge + install/reset end-to-end."""
    session = _RecordingSession()
    ctx = _Ctx(session=session, meta={"progress_token": "tok-xyz"})

    async def fake_backend(
        client: Any, locus: str, organism: str | int = "arabidopsis_thaliana"
    ) -> dict[str, Any]:
        await progress.notify("retrying upstream")
        return {"locus": locus, "organism": organism}

    monkeypatch.setattr(ensembl_plants, "lookup_locus", fake_backend)

    result = await server._call_tool(
        cast(Any, ctx), _params("ensembl_plants_lookup_locus", {"locus": "AT1G01010"})
    )

    assert not result.is_error, f"tool call failed: {result.content}"
    assert result.structured_content == {
        "locus": "AT1G01010",
        "organism": "arabidopsis_thaliana",
    }
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
    """Without a progress_token, _call_tool takes the reporter-is-None branch and
    progress.notify in the backend is a silent no-op (nothing recorded).

    The success assertion is the positive control: without it a _call_tool that
    failed outright would also record no notifications, and this test would pass
    on a completely broken dispatch path.
    """
    session = _RecordingSession()
    ctx = _Ctx(session=session, meta={})

    async def fake_backend(
        client: Any, locus: str, organism: str | int = "arabidopsis_thaliana"
    ) -> dict[str, Any]:
        await progress.notify("should be dropped")
        return {"locus": locus}

    monkeypatch.setattr(ensembl_plants, "lookup_locus", fake_backend)

    result = await server._call_tool(
        cast(Any, ctx), _params("ensembl_plants_lookup_locus", {"locus": "AT1G01010"})
    )

    assert not result.is_error, f"tool call failed: {result.content}"
    assert result.structured_content == {"locus": "AT1G01010"}
    assert session.calls == [], "notification leaked despite no progress_token"
