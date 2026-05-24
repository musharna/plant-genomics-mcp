"""Trailing-slash redirect must be disabled for the /mcp mount.

Background: uvicorn behind Tailscale Funnel runs HTTP-side, but Funnel
serves HTTPS. Starlette's auto-redirect on missing trailing slash uses
``request.url.scheme`` which reflects the inner HTTP hop, so it emits
``307 Location: http://.../mcp/``. That scheme-downgrade breaks
HTTPS-only clients (the tailnet host isn't listening on :80).

Fix: ``Starlette(redirect_slashes=False)``. Trade-off: ``GET /mcp``
(no slash) returns 404 instead of 307. Clients must register with the
exact ``/mcp/`` form. README documents this.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from plant_genomics_mcp import server_http


@pytest.fixture(autouse=True)
def _bearer(monkeypatch):
    # build_app() aborts startup without a >=32-char token (v1.0.1 contract).
    monkeypatch.setenv("PLANT_GENOMICS_MCP_HTTP_TOKEN", "x" * 32)


def test_get_mcp_no_slash_returns_404_not_307():
    app = server_http.build_app()
    with TestClient(app, base_url="https://test") as client:
        resp = client.get("/mcp", follow_redirects=False)
        assert resp.status_code == 404


def test_get_mcp_with_slash_still_requires_bearer():
    app = server_http.build_app()
    with TestClient(app, base_url="https://test") as client:
        # GET on /mcp/ falls through to the streamable-HTTP handler which
        # rejects with 401 because no Authorization header was sent.
        resp = client.get("/mcp/", follow_redirects=False)
        assert resp.status_code == 401
