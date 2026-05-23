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
  PLANT_GENOMICS_MCP_HTTP_TOKEN      default unset → /mcp open
                                     set → /mcp requires
                                     ``Authorization: Bearer <token>``;
                                     /healthz always exempt
  PLANT_GENOMICS_MCP_HTTP_MAX_BODY   default 2_097_152 (2 MiB) — POSTs
                                     advertising a larger Content-Length
                                     return 413 before the body is read

CORS is deny-all: no browser origin gets an
``Access-Control-Allow-Origin`` header, so cross-origin XHRs are
blocked client-side. MCP clients are stdio bridges or HTTP libraries,
not browser JS — non-browser callers (no ``Origin`` header) are
unaffected.
"""

from __future__ import annotations

import contextlib
import hmac
import os
from collections.abc import AsyncIterator

import uvicorn
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.types import Receive, Scope, Send

from plant_genomics_mcp import __version__
from plant_genomics_mcp.server import server


def _env_flag(name: str, default: bool) -> bool:
    """Parse a boolean env var; missing / empty → default."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def _extract_bearer(scope: Scope) -> str:
    """Pull the bearer credential out of an ASGI scope's headers.

    Returns the empty string when the header is absent or doesn't carry
    the ``Bearer `` scheme — the caller compares with ``compare_digest``,
    which treats the empty string as a non-match against any real token.
    """
    for name, value in scope.get("headers", []):
        if name == b"authorization":
            decoded = value.decode("latin-1", errors="replace")
            if decoded.startswith("Bearer "):
                return decoded[len("Bearer ") :]
            return ""
    return ""


def build_app() -> Starlette:
    """Build the Starlette ASGI app, mounting the MCP manager at ``/mcp``.

    Returns a fresh Starlette instance each call — the SDK requires one
    ``StreamableHTTPSessionManager`` per process, so call this once per
    server lifetime. The lifespan handler runs the manager's task group
    for the duration of the app.
    """
    stateless = _env_flag("PLANT_GENOMICS_MCP_HTTP_STATELESS", True)
    json_response = _env_flag("PLANT_GENOMICS_MCP_HTTP_JSON", True)
    expected_token = os.environ.get("PLANT_GENOMICS_MCP_HTTP_TOKEN")
    max_body = int(os.environ.get("PLANT_GENOMICS_MCP_HTTP_MAX_BODY", "2097152"))

    session_manager = StreamableHTTPSessionManager(
        app=server,
        event_store=None,
        json_response=json_response,
        stateless=stateless,
    )

    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "version": __version__})

    async def handle_mcp(scope: Scope, receive: Receive, send: Send) -> None:
        # /mcp gated on bearer token when PLANT_GENOMICS_MCP_HTTP_TOKEN
        # is set. /healthz routed separately, never enters this handler.
        # Content-Length pre-check rejects oversize bodies before the
        # session manager streams them into memory.
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    break
                if declared > max_body:
                    response = JSONResponse(
                        {"error": "payload too large", "max_body": max_body},
                        status_code=413,
                    )
                    await response(scope, receive, send)
                    return
                break
        if expected_token:
            provided = _extract_bearer(scope)
            if not hmac.compare_digest(provided, expected_token):
                response = JSONResponse(
                    {"error": "unauthorized"},
                    status_code=401,
                    headers={"WWW-Authenticate": 'Bearer realm="plant-genomics-mcp"'},
                )
                await response(scope, receive, send)
                return
        await session_manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with session_manager.run():
            yield

    return Starlette(
        routes=[
            Route("/healthz", healthz),
            Mount("/mcp", app=handle_mcp),
        ],
        middleware=[
            # Deny-all CORS: ``allow_origins=[]`` means no browser origin
            # ever gets an ``Access-Control-Allow-Origin`` header back, so
            # cross-origin XHRs/fetches are blocked client-side. Non-
            # browser callers (no ``Origin`` header) are unaffected. MCP
            # clients are stdio bridges or HTTP libraries, never browser
            # JS, so deny-all costs us nothing while closing the surface.
            Middleware(
                CORSMiddleware,
                allow_origins=[],
                allow_methods=["GET", "POST"],
            ),
        ],
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
