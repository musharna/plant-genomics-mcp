"""MCP ``notifications/progress`` shim for retry loops and BioMart.

The MCP protocol lets a server emit progress notifications during a
long-running tool call. The client opts in by passing a ``progressToken``
in the request meta; without one, every notification is dropped.

Threading a ``Context`` argument through every backend helper would
bloat every signature for a sidecar concern. Instead this module
exposes a context-local :class:`Reporter` that the HTTP helpers consult
via :func:`notify`. The server's ``call_tool`` handler installs a
reporter at dispatch time; the retry loops and BioMart POST emit
notifications without knowing whether anyone is listening.

The wire payload is the MCP ``notifications/progress`` shape:
``{progressToken, progress, total, message}``. ``progress`` is a
monotonically increasing float (we use it as a step counter, not a
percentage — there is no reliable total for retry storms). ``total``
is omitted unless the caller knows the bound.
"""

from __future__ import annotations

import contextvars
from typing import Awaitable, Callable, Optional

# Async callable: (progress, total, message) -> awaitable.
# Matches the signature of mcp.server.session.ServerSession.send_progress_notification
# after the progressToken is partial-applied.
SendFn = Callable[[float, Optional[float], Optional[str]], Awaitable[None]]


class Reporter:
    """Per-request progress emitter.

    Maintains a monotonically increasing step counter so the client
    sees real motion across multiple ``notify`` calls. Concurrent
    callers (e.g. parallel batch fanouts) share the counter — the wire
    progress value reflects total emitted notifications, not per-task
    progress. That's the right shape for an LLM client surfacing "X
    upstream requests in flight" status.
    """

    def __init__(self, send: SendFn, *, total: float | None = None) -> None:
        self._send = send
        self._total = total
        self._progress = 0.0

    async def notify(self, message: str, *, step: float = 1.0) -> None:
        self._progress += step
        await self._send(self._progress, self._total, message)


_current: contextvars.ContextVar[Optional[Reporter]] = contextvars.ContextVar(
    "plant_genomics_mcp_progress",
    default=None,
)


def set_reporter(reporter: Reporter | None) -> contextvars.Token:
    """Install ``reporter`` as the active emitter for this context."""
    return _current.set(reporter)


def reset_reporter(token: contextvars.Token) -> None:
    """Restore the prior reporter (use the token returned by ``set_reporter``)."""
    _current.reset(token)


def get_reporter() -> Reporter | None:
    """Read the active reporter, or ``None`` if no client opted in."""
    return _current.get()


async def notify(message: str, *, step: float = 1.0) -> None:
    """Emit ``message`` if a reporter is installed; otherwise no-op.

    Safe to call from any backend helper — the retry loops and BioMart
    POST use this without checking whether the client opted in.
    """
    reporter = _current.get()
    if reporter is None:
        return
    await reporter.notify(message, step=step)
