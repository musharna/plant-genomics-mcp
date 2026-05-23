"""End-to-end stdio smoke test — spawns the MCP server, drives it as a client.

This is a real-execution check, not a unit test. It verifies that the
``plant-genomics-mcp`` console script:

  1. Accepts the MCP ``initialize`` handshake over stdio.
  2. Advertises 23 tools via ``list_tools``, all with non-empty descriptions
     and a non-empty ``outputSchema``.
  3. Advertises 3 prompts via ``list_prompts`` with required-arg flags
     preserved on the wire.
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
                "analyze_locus_synth",
                "atted_coexpression",
                "batch_atted_coexpression",
                "batch_ensembl_plants_lookup_locus",
                "batch_get_gene_xrefs",
                "batch_gramene_homologs",
                "batch_kegg_pathways",
                "batch_locus_go_annotations",
                "batch_locus_literature",
                "batch_phytozome_lookup_locus",
                "batch_resolve_locus_to_uniprot",
                "batch_string_interactions",
                "biological_context_synth",
                "blast_sequence",
                "consensus_homologs",
                "ensembl_plants_lookup_locus",
                "find_homologs_synth",
                "get_gene_xrefs",
                "gramene_homologs",
                "kegg_pathways",
                "locus_go_annotations",
                "locus_literature",
                "phytozome_lookup_locus",
                "plantcyc_locus_info",
                "resolve_locus_to_uniprot",
                "string_interactions",
                "tair_locus_info",
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


@pytest.mark.asyncio
async def test_list_prompts_advertises_all_prompts(
    server_params: StdioServerParameters,
) -> None:
    """prompts/list over the wire — discoverable surface for slash-command menus."""
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_prompts()
            names = {p.name for p in result.prompts}
            assert names == {"analyze_locus", "find_homologs", "biological_context"}, f"got {names}"
            by_name = {p.name: p for p in result.prompts}
            # Required-arg flags must round-trip (clients rely on these for UX).
            analyze_args = {a.name: a for a in by_name["analyze_locus"].arguments or []}
            assert analyze_args["locus"].required is True
            assert not analyze_args["organism"].required
            homolog_args = {a.name: a for a in by_name["find_homologs"].arguments or []}
            assert homolog_args["sequence"].required is True
            bio_args = {a.name: a for a in by_name["biological_context"].arguments or []}
            assert bio_args["locus"].required is True
            assert not bio_args["top_n"].required


@pytest.mark.asyncio
async def test_get_prompt_renders_chain(
    server_params: StdioServerParameters,
) -> None:
    """prompts/get — the rendered chain must mention every chained tool."""
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.get_prompt("analyze_locus", {"locus": "AT1G01010"})
            assert result.description and "AT1G01010" in result.description
            assert len(result.messages) == 1
            msg = result.messages[0]
            assert msg.role == "user"
            text = msg.content.text
            for tool in (
                "ensembl_plants_lookup_locus",
                "get_gene_xrefs",
                "resolve_locus_to_uniprot",
                "locus_literature",
                "locus_go_annotations",
            ):
                assert tool in text, f"chain missing {tool}"


@pytest.mark.asyncio
async def test_get_biological_context_prompt_renders_chain(
    server_params: StdioServerParameters,
) -> None:
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.get_prompt("biological_context", {"locus": "AT1G01010"})
            assert result.description and "AT1G01010" in result.description
            text = result.messages[0].content.text
            for tool in (
                "gramene_homologs",
                "kegg_pathways",
                "resolve_locus_to_uniprot",
                "string_interactions",
                "atted_coexpression",
            ):
                assert tool in text, f"chain missing {tool}"


@pytest.mark.asyncio
async def test_get_prompt_unknown_name_surfaces_typed_error(
    server_params: StdioServerParameters,
) -> None:
    """Anti-rot guard for [NotFoundError] prefix on the prompts/get path."""
    from mcp.shared.exceptions import McpError

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            with pytest.raises(McpError) as exc_info:
                await session.get_prompt("nonexistent_prompt", {})
            assert "[NotFoundError]" in str(exc_info.value), (
                f"missing typed prefix in: {exc_info.value!r}"
            )


@pytest.mark.asyncio
async def test_tool_schemas_use_organism_param(server_params: StdioServerParameters) -> None:
    """v0.9/1.0 contract: every multi-organism tool exposes a single
    'organism' input field accepting string | integer, defaulting to
    'arabidopsis_thaliana'. Old 'species' and 'organism_id' fields must
    not appear anywhere. Tools whose backend is intrinsically single-
    organism (KEGG, ATTED-II Arabidopsis-only), sequence-driven (BLAST,
    find_homologs_synth), Gramene's own cross-species index, or stub
    redirects (TAIR, PlantCyc) are explicitly out of scope.
    """
    organism_aware = {
        "ensembl_plants_lookup_locus",
        "get_gene_xrefs",
        "phytozome_lookup_locus",
        "resolve_locus_to_uniprot",
        "locus_literature",
        "locus_go_annotations",
        "string_interactions",
        "batch_ensembl_plants_lookup_locus",
        "batch_get_gene_xrefs",
        "batch_phytozome_lookup_locus",
        "batch_resolve_locus_to_uniprot",
        "batch_locus_literature",
        "batch_locus_go_annotations",
        "batch_string_interactions",
        "analyze_locus_synth",
        "biological_context_synth",
        "consensus_homologs",
    }
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            actual_organism = set()
            for tool in result.tools:
                props = (tool.inputSchema or {}).get("properties", {})
                assert "species" not in props, f"{tool.name} still has 'species' in inputSchema"
                assert "organism_id" not in props, (
                    f"{tool.name} still has 'organism_id' in inputSchema"
                )
                if "organism" in props:
                    actual_organism.add(tool.name)
                    field = props["organism"]
                    assert field.get("type") == ["string", "integer"], (
                        f"{tool.name}.organism type is {field.get('type')!r}, "
                        "expected ['string', 'integer']"
                    )
                    assert field.get("default") == "arabidopsis_thaliana", (
                        f"{tool.name}.organism default is {field.get('default')!r}, "
                        "expected 'arabidopsis_thaliana'"
                    )
            missing = organism_aware - actual_organism
            extra = actual_organism - organism_aware
            assert not missing, f"multi-organism tools missing organism field: {sorted(missing)}"
            assert not extra, (
                f"tools declare organism but aren't in the canonical "
                f"multi-organism set (update this test if intentional): {sorted(extra)}"
            )
