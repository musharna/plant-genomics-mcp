"""Streamable-HTTP transport entry point.

Mounts the existing low-level ``Server`` on a Starlette ASGI app via
``StreamableHTTPSessionManager`` and serves with uvicorn. Public MCP
clients (Claude Code over HTTP, registry indexers, hosted endpoints)
connect at ``/mcp``. Stdio remains the primary distribution surface —
HTTP is the additive transport that registries and remote callers want.

Stateless by default. Each request creates its own transport, no session
bookkeeping, no idle-timeout reaping. The per-request ``progressToken``
meta still works because the reporter is installed per ``_call_tool``
invocation, not per-session. Flip to stateful with
``PLANT_GENOMICS_MCP_HTTP_STATELESS=0`` when you need durable session
state (resumable SSE, long-running tools holding sessions open).

JSON response by default — clients that don't speak SSE get a single
JSON-RPC response per POST. Set ``PLANT_GENOMICS_MCP_HTTP_JSON=0`` to
emit the streaming SSE event format instead.

Env knobs:
  PLANT_GENOMICS_MCP_HTTP_HOST       default 127.0.0.1
  PLANT_GENOMICS_MCP_HTTP_PORT       default 8765
  PLANT_GENOMICS_MCP_HTTP_STATELESS  default 1
  PLANT_GENOMICS_MCP_HTTP_JSON       default 1
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator

import uvicorn
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.types import Receive, Scope, Send

from plant_genomics_mcp.server import server


def _env_flag(name: str, default: bool) -> bool:
    """Parse a boolean env var; missing / empty → default."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def build_app() -> Starlette:
    """Build the Starlette ASGI app, mounting the MCP manager at ``/mcp``.

    Returns a fresh Starlette instance each call — the SDK requires one
    ``StreamableHTTPSessionManager`` per process, so call this once per
    server lifetime. The lifespan handler runs the manager's task group
    for the duration of the app.
    """
    stateless = _env_flag("PLANT_GENOMICS_MCP_HTTP_STATELESS", True)
    json_response = _env_flag("PLANT_GENOMICS_MCP_HTTP_JSON", True)

    session_manager = StreamableHTTPSessionManager(
        app=server,
        event_store=None,
        json_response=json_response,
        stateless=stateless,
    )

    async def handle_mcp(scope: Scope, receive: Receive, send: Send) -> None:
        await session_manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with session_manager.run():
            yield

    return Starlette(
        routes=[Mount("/mcp", app=handle_mcp)],
        lifespan=lifespan,
    )


def main() -> None:
    """Run the HTTP transport via uvicorn — installed as
    ``plant-genomics-mcp-http``."""
    host = os.environ.get("PLANT_GENOMICS_MCP_HTTP_HOST", "127.0.0.1")
    port = int(os.environ.get("PLANT_GENOMICS_MCP_HTTP_PORT", "8765"))
    uvicorn.run(build_app(), host=host, port=port)


if __name__ == "__main__":
    main()
