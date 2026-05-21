"""Smoke tests for the MCP server: tool registration + dispatch routing.

Verifies the server exposes the expected tool surface and that dispatch
routes each tool name to the correct backend client. Backend HTTP is
mocked — this test isolates the MCP wiring layer.
"""

from __future__ import annotations

import json

import pytest
from pytest_httpx import HTTPXMock

from genomics_mcp import server


def test_tool_surface_is_stable() -> None:
    names = {t.name for t in server.TOOLS}
    expected = {
        "ensembl_lookup_id",
        "ensembl_lookup_symbol",
        "ensembl_sequence_by_id",
        "ensembl_xrefs_by_id",
        "ensembl_homology_by_id",
        "kegg_find",
        "kegg_get",
        "kegg_link",
        "kegg_conv",
    }
    assert names == expected


def test_every_tool_has_required_fields() -> None:
    for tool in server.TOOLS:
        assert tool.name and tool.description and tool.inputSchema
        schema = tool.inputSchema
        assert schema.get("type") == "object"
        assert "properties" in schema


@pytest.mark.asyncio
async def test_dispatch_routes_to_ensembl(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://rest.ensembl.org/lookup/id/ENSG00000139618",
        json={"id": "ENSG00000139618", "display_name": "BRCA2"},
    )
    result = await server._dispatch("ensembl_lookup_id", {"ensembl_id": "ENSG00000139618"})
    assert result["display_name"] == "BRCA2"


@pytest.mark.asyncio
async def test_dispatch_routes_to_kegg(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://rest.kegg.jp/find/genes/BRCA2",
        text="hsa:675\tBRCA2\n",
    )
    rows = await server._dispatch("kegg_find", {"database": "genes", "query": "BRCA2"})
    assert rows[0]["id"] == "hsa:675"


@pytest.mark.asyncio
async def test_dispatch_unknown_tool_raises() -> None:
    with pytest.raises(ValueError, match="unknown tool"):
        await server._dispatch("nonexistent_tool", {})


@pytest.mark.asyncio
async def test_call_tool_wraps_errors_as_text(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://rest.ensembl.org/lookup/id/NOTREAL",
        status_code=404,
        text="not found",
    )
    out = await server._call_tool("ensembl_lookup_id", {"ensembl_id": "NOTREAL"})
    assert len(out) == 1
    assert out[0].type == "text"
    assert "error:" in out[0].text


@pytest.mark.asyncio
async def test_call_tool_serializes_dicts_as_json(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://rest.ensembl.org/lookup/id/ENSG00000139618",
        json={"id": "ENSG00000139618", "biotype": "protein_coding"},
    )
    out = await server._call_tool("ensembl_lookup_id", {"ensembl_id": "ENSG00000139618"})
    parsed = json.loads(out[0].text)
    assert parsed["biotype"] == "protein_coding"
