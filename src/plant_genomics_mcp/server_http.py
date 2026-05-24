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
  PLANT_GENOMICS_MCP_HTTP_TOKEN      REQUIRED, >= 32 chars — build_app
                                     aborts with SystemExit if absent or
                                     too short. ``Authorization: Bearer
                                     <token>`` is required on /mcp;
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

from plant_genomics_mcp.server import server

# Minimum length for PLANT_GENOMICS_MCP_HTTP_TOKEN — half a `openssl rand
# -hex 32` value, also the length of a stringified UUID without dashes.
# Anything shorter is considered toy/test input and refused at startup.
_MIN_TOKEN_LEN = 32


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
    expected_token = os.environ.get("PLANT_GENOMICS_MCP_HTTP_TOKEN", "")
    if len(expected_token) < _MIN_TOKEN_LEN:
        raise SystemExit(
            "PLANT_GENOMICS_MCP_HTTP_TOKEN is required and must be at least "
            f"{_MIN_TOKEN_LEN} characters (got {len(expected_token)}). "
            "Generate one with `openssl rand -hex 32` and pass it via the "
            "container env_file or docker compose `environment:` block."
        )
    max_body = int(os.environ.get("PLANT_GENOMICS_MCP_HTTP_MAX_BODY", "2097152"))

    session_manager = StreamableHTTPSessionManager(
        app=server,
        event_store=None,
        json_response=json_response,
        stateless=stateless,
    )

    async def healthz(_request: Request) -> JSONResponse:
        # Version intentionally omitted: /healthz is unauthenticated for
        # liveness probes, and leaking the exact version string to anonymous
        # callers hands them a CVE-targeting shortcut. Authenticated callers
        # can read __version__ from the initialize handshake.
        return JSONResponse({"status": "ok"})

    async def handle_mcp(scope: Scope, receive: Receive, send: Send) -> None:
        # /mcp is always gated on bearer token (build_app aborts before we
        # get here if the env var is absent or <32 chars). /healthz is
        # routed separately and never enters this handler. Content-Length
        # pre-check rejects oversize bodies before the session manager
        # streams them into memory; body-cap precedes auth so a 100 MB
        # POST gets cut off before we burn cycles checking the token.
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

    app = Starlette(
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
    # Starlette 1.0 removed redirect_slashes from the constructor; set it
    # on the router directly. Without this, ``GET /mcp`` triggers a
    # ``307 Location: http://…/mcp/`` redirect — the scheme reflects the
    # inner HTTP hop through uvicorn, not the outer HTTPS Tailscale Funnel
    # layer, breaking HTTPS-only clients that follow the redirect.
    app.router.redirect_slashes = False
    return app


def main() -> None:
    """Run the HTTP transport via uvicorn — installed as
    ``plant-genomics-mcp-http``."""
    host = os.environ.get("PLANT_GENOMICS_MCP_HTTP_HOST", "127.0.0.1")
    port = int(os.environ.get("PLANT_GENOMICS_MCP_HTTP_PORT", "8765"))
    uvicorn.run(build_app(), host=host, port=port)


if __name__ == "__main__":
    main()
