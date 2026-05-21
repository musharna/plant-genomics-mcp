"""End-to-end stdio smoke test — spawns the MCP server, drives it as a client.

This is a real-execution check, not a unit test. It verifies that the
``plant-genomics-mcp`` console script:

  1. Accepts the MCP ``initialize`` handshake over stdio.
  2. Advertises 8 tools via ``list_tools``, all with non-empty descriptions
     and a non-empty ``outputSchema``.
  3. Returns BOTH ``content`` (text) and ``structuredContent`` (dict) for
     a real call to ``tair_locus_info`` (the offline stub — doesn't need
     network, keeps CI deterministic).
  4. Surfaces our differentiated exception type info — invalid input
     produces an error result whose text contains the ``[NotFoundError]``
     prefix from ``PlantGenomicsError.__str__``.

We gate on ``PLANT_GENOMICS_MCP_STDIO_SMOKE=1`` for opt-in. The default
test run skips it (the unit-test layer covers the dispatch logic).

To run:
    PLANT_GENOMICS_MCP_STDIO_SMOKE=1 pytest tests/test_server_stdio.py -v
"""

from __future__ import annotations

import json
import os
import sys

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


# Opt-in: the smoke test spawns a subprocess, costs ~500ms, and we don't
# want it in the default `pytest -q` run.
pytestmark = pytest.mark.skipif(
    not os.environ.get("PLANT_GENOMICS_MCP_STDIO_SMOKE"),
    reason="set PLANT_GENOMICS_MCP_STDIO_SMOKE=1 to run the stdio smoke test",
)


@pytest.fixture
def server_params() -> StdioServerParameters:
    # Use the current interpreter to spawn the server module. This
    # avoids depending on the console script being on PATH (e.g. when
    # running the test suite from a fresh checkout before `pip install`).
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "plant_genomics_mcp.server"],
    )


@pytest.mark.asyncio
async def test_initialize_and_list_tools(server_params: StdioServerParameters) -> None:
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            names = {t.name for t in result.tools}
            assert names == {
                "ensembl_plants_lookup_locus",
                "get_gene_xrefs",
                "phytozome_lookup_locus",
                "resolve_locus_to_uniprot",
                "locus_literature",
                "locus_go_annotations",
                "tair_locus_info",
                "plantcyc_locus_info",
            }, f"got {names}"

            # Every tool publishes an outputSchema (P0.1).
            for tool in result.tools:
                assert tool.description, f"{tool.name} has empty description"
                assert tool.outputSchema is not None, f"{tool.name} missing outputSchema"
                assert tool.outputSchema.get("type") == "object", (
                    f"{tool.name} outputSchema not an object: {tool.outputSchema}"
                )


@pytest.mark.asyncio
async def test_tair_call_returns_structured_content(
    server_params: StdioServerParameters,
) -> None:
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("tair_locus_info", arguments={"locus": "AT1G01010"})

            # SDK builds BOTH unstructured (TextContent of JSON) and structured.
            assert result.structuredContent is not None
            assert result.structuredContent["locus"] == "AT1G01010"
            assert result.structuredContent["status"] == "subscription_required"
            assert "ensembl_plants_lookup_locus" in result.structuredContent["alternatives"]

            # Content TextContent should be the same data, JSON-stringified.
            assert result.content
            text_block = result.content[0]
            assert text_block.type == "text"
            parsed = json.loads(text_block.text)
            assert parsed == result.structuredContent


@pytest.mark.asyncio
async def test_invalid_locus_surfaces_typed_error(
    server_params: StdioServerParameters,
) -> None:
    """Anti-rot guard for the [ClassName] prefix on PlantGenomicsError.__str__."""
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("tair_locus_info", arguments={"locus": "AT1G01010<x>"})
            assert result.isError, "expected error result for invalid locus"
            assert result.content
            text = result.content[0].text
            # The [ClassName] prefix from errors.PlantGenomicsError.__str__
            # is what lets an LLM client route on failure type.
            assert "[NotFoundError]" in text, f"missing typed prefix in: {text!r}"
            assert "invalid locus" in text
