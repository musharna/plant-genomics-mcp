"""``PLANT_GENOMICS_MCP_OMIT_OUTPUT_SCHEMAS``: a smaller ``tools/list``, on request.

Output schemas are about two thirds of the tool list. A client that loads
every tool definition before its first call pays for all of them, so the
switch leaves ``outputSchema`` out of what ``tools/list`` returns. It is off
unless set, and it changes nothing else: the same tools, descriptions, input
schemas and annotations are listed, and a call still returns
``structuredContent``.

Each test pairs the changed behaviour with the unchanged one, so a handler
that dropped every schema, or none, cannot pass.

The same check over real HTTP lives in tests/test_http_transport.py, the one
file allowed to make raw HTTP calls to this server.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, cast

import pytest
from mcp import ClientSession, types
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import TextContent

from plant_genomics_mcp import ensembl_plants, server, server_http
from tests.test_progress_bridge import _Ctx, _params, _RecordingSession

ENV = "PLANT_GENOMICS_MCP_OMIT_OUTPUT_SCHEMAS"
_VALID_TOKEN = "t" * 32


async def _listed() -> list[types.Tool]:
    result = await server._list_tools(cast(Any, None), None)
    return result.tools


def _wire(tool: types.Tool) -> dict[str, Any]:
    """The tool as it goes on the wire: aliases, and no key for an unset field."""
    return tool.model_dump(by_alias=True, exclude_none=True)


def _wire_bytes(tools: list[types.Tool]) -> int:
    return len(json.dumps([_wire(t) for t in tools], separators=(",", ":")).encode())


async def test_the_switch_drops_output_schemas_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ENV, raising=False)
    full = await _listed()
    assert len(full) == len(server.TOOLS) > 0
    assert all(_wire(t).get("outputSchema") for t in full)

    monkeypatch.setenv(ENV, "1")
    compact = await _listed()
    assert [t.name for t in compact] == [t.name for t in full]
    for small, big in zip(compact, full, strict=True):
        assert "outputSchema" not in _wire(small), small.name
        rest = {k: v for k, v in _wire(big).items() if k != "outputSchema"}
        assert _wire(small) == rest, small.name

    # The list a later request gets is whole again: nothing was dropped in place.
    monkeypatch.delenv(ENV)
    assert [_wire(t) for t in await _listed()] == [_wire(t) for t in full]
    assert all(t.output_schema for t in server.TOOLS)


async def test_the_documented_saving_is_what_the_list_measures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docs/reference.md says the switch cuts the list by about 65%."""
    monkeypatch.delenv(ENV, raising=False)
    full = _wire_bytes(await _listed())
    monkeypatch.setenv(ENV, "1")
    compact = _wire_bytes(await _listed())
    cut = 100 * (full - compact) / full
    assert 60 <= cut <= 70, f"{full} -> {compact} bytes is a {cut:.1f}% cut"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, False),
        ("", False),
        ("0", False),
        ("false", False),
        ("No", False),
        ("off", False),
        ("1", True),
        ("true", True),
        ("YES", True),
        (" on ", True),
    ],
)
def test_the_values_the_switch_reads(
    monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: bool
) -> None:
    if raw is None:
        monkeypatch.delenv(ENV, raising=False)
    else:
        monkeypatch.setenv(ENV, raw)
    assert server.omit_output_schemas() is expected


def test_an_unreadable_value_stops_both_transports_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo must not read as "on" or as "off": the server does not start."""
    served: list[str] = []

    async def fake_serve() -> None:
        served.append("stdio")

    monkeypatch.setattr(server, "_serve", fake_serve)
    monkeypatch.setenv("PLANT_GENOMICS_MCP_HTTP_TOKEN", _VALID_TOKEN)

    monkeypatch.setenv(ENV, "maybe")
    with pytest.raises(ValueError, match=f"{ENV}.*'maybe'"):
        server.omit_output_schemas()
    with pytest.raises(SystemExit, match=f"{ENV}.*'maybe'"):
        server.main()
    assert served == []
    with pytest.raises(SystemExit, match=f"{ENV}.*'maybe'"):
        server_http.build_app()

    # The same two entry points start with a value the switch can read.
    monkeypatch.setenv(ENV, "1")
    server.main()
    assert served == ["stdio"]
    assert server_http.build_app() is not None


async def test_a_call_still_returns_structured_content_with_the_switch_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {"locus": "AT1G01010", "symbol": "NAC001"}

    async def ok(client: Any, locus: str, **kwargs: Any) -> dict[str, Any]:
        return payload

    monkeypatch.setattr(ensembl_plants, "lookup_locus", ok)
    ctx = _Ctx(session=_RecordingSession(), meta={})
    request = _params("ensembl_plants_lookup_locus", {"locus": "AT1G01010"})

    monkeypatch.setenv(ENV, "1")
    listed = {t.name: t for t in await _listed()}
    assert listed["ensembl_plants_lookup_locus"].output_schema is None
    result = await server._call_tool(cast(Any, ctx), request)
    assert not result.is_error
    assert result.structured_content == payload


# ---- the real server process ------------------------------------------------
# Opt-in like tests/test_server_stdio.py; test.yml sets the variable, so CI
# runs it.


@pytest.mark.skipif(
    not os.environ.get("PLANT_GENOMICS_MCP_STDIO_SMOKE"),
    reason="set PLANT_GENOMICS_MCP_STDIO_SMOKE=1 to run the stdio smoke test",
)
async def test_a_real_server_process_lists_without_output_schemas_when_asked() -> None:
    async def listing(env: dict[str, str]) -> tuple[list[types.Tool], str]:
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "plant_genomics_mcp.server"], env=env
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                # Raised before any network call, so this stays offline.
                bad = await session.call_tool(
                    "plantcyc_locus_info", arguments={"locus": "AT1G01010<x>"}
                )
                assert bad.is_error
                first = bad.content[0]
                assert isinstance(first, TextContent)
                return tools, first.text

    full, full_error = await listing({})
    compact, compact_error = await listing({ENV: "1"})

    names = [t.name for t in server.TOOLS]
    assert [t.name for t in full] == names
    assert all(t.output_schema for t in full)
    assert [t.name for t in compact] == names
    assert all(t.output_schema is None for t in compact)
    assert [t.input_schema for t in compact] == [t.input_schema for t in full]
    assert compact_error == full_error
    assert compact_error.startswith("[NotFoundError]")
