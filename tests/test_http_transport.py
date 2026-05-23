"""Tests for the streamable-HTTP transport.

Two layers:
  1. Unit — ``build_app`` returns a Starlette app with a ``/mcp`` mount,
     and the env-flag parser handles the truthy/falsey forms.
  2. Real-execution — spin uvicorn on a free port, POST a JSON-RPC
     ``initialize`` + ``tools/list`` against ``/mcp``, assert the tool
     catalog ships back. This is the boundary check — if the manager
     weren't wired correctly we'd get 404 / 500 / a hung connection
     instead of a JSON-RPC envelope.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

from plant_genomics_mcp import __version__, server_http


# ---------- unit ----------


def test_build_app_returns_starlette_with_mcp_mount() -> None:
    app = server_http.build_app()
    assert isinstance(app, Starlette)
    mounts = [r for r in app.routes if isinstance(r, Mount)]
    assert any(r.path == "/mcp" for r in mounts), [r.path for r in mounts]


@pytest.mark.parametrize(
    "raw,default,expected",
    [
        (None, True, True),
        (None, False, False),
        ("1", False, True),
        ("true", False, True),
        ("yes", False, True),
        ("on", False, True),
        ("0", True, False),
        ("false", True, False),
        ("no", True, False),
        ("off", True, False),
        ("", True, False),  # empty string → falsey
    ],
)
def test_env_flag_parses_truthy_and_falsey(
    raw: str | None, default: bool, expected: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    var = "PLANT_GENOMICS_MCP_HTTP_TEST_FLAG_X"
    if raw is None:
        monkeypatch.delenv(var, raising=False)
    else:
        monkeypatch.setenv(var, raw)
    assert server_http._env_flag(var, default) is expected


# ---------- real-execution ----------


def _free_port() -> int:
    """Bind a fresh socket to OS-allocated port, release, return port number."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_healthz_returns_status_ok_with_version() -> None:
    """`GET /healthz` returns 200 with the package version.

    Lets external watchers (Uptime Kuma, Diun, curl-in-cron) verify
    liveness without sending a JSON-RPC POST. The version field doubles
    as a cheap deploy-confirmation probe.
    """
    app = server_http.build_app()
    with TestClient(app) as client:
        resp = client.get("/healthz")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"status": "ok", "version": __version__}, body


@pytest.mark.asyncio
async def test_http_tools_list_via_real_uvicorn() -> None:
    """End-to-end: real uvicorn → real Starlette → real session manager.

    Drives a stateless MCP handshake (``initialize`` then ``tools/list``)
    over a live HTTP socket. If the mount path, lifespan wiring, or
    JSON-response toggle were wrong we'd never see the tool catalog.
    """
    app = server_http.build_app()
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    uv_server = uvicorn.Server(config)
    serve_task = asyncio.create_task(uv_server.serve())
    try:
        # Wait up to ~5s for uvicorn to flip its started flag.
        for _ in range(100):
            if uv_server.started:
                break
            await asyncio.sleep(0.05)
        assert uv_server.started, "uvicorn never reported started"

        headers = {"Accept": "application/json, text/event-stream"}
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=10.0) as client:
            health_resp = await client.get("/healthz")
            assert health_resp.status_code == 200, health_resp.text
            assert health_resp.json()["status"] == "ok"
            assert health_resp.json()["version"] == __version__

            init_payload: dict[str, Any] = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "pgmcp-test", "version": "0.0.1"},
                },
            }
            init_resp = await client.post("/mcp/", json=init_payload, headers=headers)
            assert init_resp.status_code == 200, init_resp.text
            init_body = init_resp.json()
            assert init_body.get("jsonrpc") == "2.0"
            assert "result" in init_body, init_body
            assert init_body["result"]["serverInfo"]["name"] == "plant-genomics-mcp"

            list_payload: dict[str, Any] = {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
            }
            list_resp = await client.post("/mcp/", json=list_payload, headers=headers)
            assert list_resp.status_code == 200, list_resp.text
            list_body = list_resp.json()
            tools = list_body["result"]["tools"]
            names = {t["name"] for t in tools}
            # Spot-check a representative slice of the 14-tool catalog so
            # we'd notice if dispatch got severed from the HTTP path.
            for expected in (
                "ensembl_plants_lookup_locus",
                "phytozome_lookup_locus",
                "resolve_locus_to_uniprot",
                "batch_locus_go_annotations",
            ):
                assert expected in names, names
    finally:
        uv_server.should_exit = True
        try:
            await asyncio.wait_for(serve_task, timeout=5.0)
        except asyncio.TimeoutError:
            serve_task.cancel()


# ---------- bearer auth (Wave B1) ----------


def test_mcp_open_when_token_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backward-compat: env var unset → /mcp accepts unauthenticated POSTs.

    Existing deployments that bind to 127.0.0.1 or sit behind a reverse
    proxy must keep working without operator intervention.
    """
    monkeypatch.delenv("PLANT_GENOMICS_MCP_HTTP_TOKEN", raising=False)
    app = server_http.build_app()
    with TestClient(app) as client:
        resp = client.post(
            "/mcp/",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={"Accept": "application/json, text/event-stream"},
        )
    # Status must not be 401 — the request reached the MCP manager (which
    # may legitimately return any non-auth error for an incomplete init).
    assert resp.status_code != 401, resp.text


def test_mcp_requires_bearer_when_token_set_no_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """Token configured + missing Authorization header → 401."""
    monkeypatch.setenv("PLANT_GENOMICS_MCP_HTTP_TOKEN", "s3cret-token")
    app = server_http.build_app()
    with TestClient(app) as client:
        resp = client.post(
            "/mcp/",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Accept": "application/json, text/event-stream"},
        )
    assert resp.status_code == 401, resp.text


def test_mcp_requires_bearer_when_token_set_wrong_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Token configured + wrong bearer → 401 (constant-time compare)."""
    monkeypatch.setenv("PLANT_GENOMICS_MCP_HTTP_TOKEN", "s3cret-token")
    app = server_http.build_app()
    with TestClient(app) as client:
        resp = client.post(
            "/mcp/",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={
                "Accept": "application/json, text/event-stream",
                "Authorization": "Bearer not-the-right-token",
            },
        )
    assert resp.status_code == 401, resp.text


def test_mcp_accepts_correct_bearer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Token configured + matching bearer → middleware passes request through.

    We don't drive a full JSON-RPC handshake here — the assertion is
    simply that auth doesn't reject (status != 401), proving the
    middleware accepted the credential and forwarded to the manager.
    """
    monkeypatch.setenv("PLANT_GENOMICS_MCP_HTTP_TOKEN", "s3cret-token")
    app = server_http.build_app()
    with TestClient(app) as client:
        resp = client.post(
            "/mcp/",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={
                "Accept": "application/json, text/event-stream",
                "Authorization": "Bearer s3cret-token",
            },
        )
    assert resp.status_code != 401, resp.text


def test_healthz_exempt_from_bearer_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """/healthz must remain open even with auth configured — external
    watchers (Uptime Kuma, Diun, k8s probes) can't carry a bearer token.
    """
    monkeypatch.setenv("PLANT_GENOMICS_MCP_HTTP_TOKEN", "s3cret-token")
    app = server_http.build_app()
    with TestClient(app) as client:
        resp = client.get("/healthz")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok"


def test_mcp_rejects_oversized_body_413(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST with Content-Length over the configured cap → 413.

    Defends against memory-exhaustion DoS — a 100 MB JSON payload would
    blow the process before the MCP manager even sees it. We cap at 2 MB
    by default; this test lowers the cap so the 413 fires on a small
    payload without allocating multi-MB strings in the test process.
    """
    monkeypatch.delenv("PLANT_GENOMICS_MCP_HTTP_TOKEN", raising=False)
    monkeypatch.setenv("PLANT_GENOMICS_MCP_HTTP_MAX_BODY", "1024")
    app = server_http.build_app()
    oversized = "x" * 4096  # 4 KB body > 1 KB cap
    with TestClient(app) as client:
        resp = client.post(
            "/mcp/",
            content=oversized,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
    assert resp.status_code == 413, resp.text


def test_mcp_passes_through_body_under_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default 2 MB cap doesn't reject a normal JSON-RPC payload."""
    monkeypatch.delenv("PLANT_GENOMICS_MCP_HTTP_TOKEN", raising=False)
    monkeypatch.delenv("PLANT_GENOMICS_MCP_HTTP_MAX_BODY", raising=False)
    app = server_http.build_app()
    with TestClient(app) as client:
        resp = client.post(
            "/mcp/",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Accept": "application/json, text/event-stream"},
        )
    assert resp.status_code != 413, resp.text


def test_blast_sequence_inputschema_has_maxlength_cap() -> None:
    """Schema-layer guard: `blast_sequence` and `find_homologs_synth`
    declare ``maxLength`` on the ``sequence`` field so MCP clients can
    reject oversize input before submitting, and the JSON-Schema
    validation layer rejects monster payloads even when the HTTP body
    cap isn't in play (stdio transport).
    """
    from plant_genomics_mcp import server as server_module

    by_name = {t.name: t for t in server_module.TOOLS}
    blast = by_name["blast_sequence"].inputSchema
    seq = blast["properties"]["sequence"]
    assert seq.get("maxLength") == 1_000_000, seq
    homologs = by_name["find_homologs_synth"].inputSchema
    seq2 = homologs["properties"]["sequence"]
    assert seq2.get("maxLength") == 1_000_000, seq2


# ---------- CORS deny-all (Wave B5) ----------


def test_cors_middleware_is_installed() -> None:
    """CORSMiddleware appears in the Starlette middleware stack.

    Structural assertion — the deny-all policy must be wired in
    declaratively. ASGI middleware doesn't show up in routes; the
    canonical surface is ``app.user_middleware``.
    """
    from starlette.middleware.cors import CORSMiddleware

    app = server_http.build_app()
    assert any(m.cls is CORSMiddleware for m in app.user_middleware), [
        m.cls.__name__ for m in app.user_middleware
    ]


def test_cors_preflight_is_intercepted_not_405() -> None:
    """OPTIONS preflight with ``Access-Control-Request-Method`` is
    intercepted by CORSMiddleware — it short-circuits and never reaches
    the route, so the response is NOT a plain 405 from the routing
    layer. With no middleware installed, OPTIONS /healthz returns 405
    ``Allow: HEAD, GET`` — this test fails in that state.
    """
    app = server_http.build_app()
    with TestClient(app) as client:
        resp = client.options(
            "/healthz",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "GET",
            },
        )
    assert resp.status_code != 405, (resp.status_code, dict(resp.headers))


def test_cors_omits_acao_on_disallowed_origin() -> None:
    """Cross-origin GET with a disallowed Origin → no Access-Control-
    Allow-Origin header. Browsers refuse the response when ACAO is
    absent, enforcing the deny-all policy client-side.
    """
    app = server_http.build_app()
    with TestClient(app) as client:
        resp = client.get(
            "/healthz",
            headers={"Origin": "https://evil.example"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("access-control-allow-origin") is None, dict(resp.headers)


def test_cors_does_not_break_non_browser_clients() -> None:
    """No Origin header (CLI / stdio bridge / curl) → request flows
    through untouched and gets a normal response.
    """
    app = server_http.build_app()
    with TestClient(app) as client:
        resp = client.get("/healthz")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_bearer_auth_via_real_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real-execution check: middleware fires through actual uvicorn.

    Boundary test — TestClient invokes the ASGI app in-process; this
    confirms the middleware also rejects/accepts across a real socket.
    """
    monkeypatch.setenv("PLANT_GENOMICS_MCP_HTTP_TOKEN", "live-secret")
    app = server_http.build_app()
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    uv_server = uvicorn.Server(config)
    serve_task = asyncio.create_task(uv_server.serve())
    try:
        for _ in range(100):
            if uv_server.started:
                break
            await asyncio.sleep(0.05)
        assert uv_server.started, "uvicorn never reported started"

        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=10.0) as client:
            # /healthz still open
            health_resp = await client.get("/healthz")
            assert health_resp.status_code == 200, health_resp.text

            # /mcp with no auth → 401
            no_auth = await client.post(
                "/mcp/",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers={"Accept": "application/json, text/event-stream"},
            )
            assert no_auth.status_code == 401, no_auth.text

            # /mcp with correct token → passes middleware (not 401)
            good_auth = await client.post(
                "/mcp/",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "pgmcp-test", "version": "0.0.1"},
                    },
                },
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Authorization": "Bearer live-secret",
                },
            )
            assert good_auth.status_code == 200, good_auth.text
    finally:
        uv_server.should_exit = True
        try:
            await asyncio.wait_for(serve_task, timeout=5.0)
        except asyncio.TimeoutError:
            serve_task.cancel()
