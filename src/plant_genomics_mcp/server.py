"""MCP server entry point — exposes Ensembl + KEGG tools over stdio."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from plant_genomics_mcp import ensembl, kegg

server: Server = Server("plant-genomics-mcp")


# ---- tool catalog -----------------------------------------------------------

TOOLS: list[types.Tool] = [
    types.Tool(
        name="ensembl_lookup_id",
        description="Fetch metadata for an Ensembl stable ID (gene, transcript, or protein).",
        inputSchema={
            "type": "object",
            "properties": {
                "ensembl_id": {"type": "string", "description": "e.g. ENSG00000139618"},
                "expand": {"type": "boolean", "default": False},
            },
            "required": ["ensembl_id"],
        },
    ),
    types.Tool(
        name="ensembl_lookup_symbol",
        description="Resolve a gene symbol to its Ensembl record for a given species.",
        inputSchema={
            "type": "object",
            "properties": {
                "species": {
                    "type": "string",
                    "description": "e.g. homo_sapiens, arabidopsis_thaliana",
                },
                "symbol": {"type": "string", "description": "e.g. BRCA2, ARF7"},
                "expand": {"type": "boolean", "default": False},
            },
            "required": ["species", "symbol"],
        },
    ),
    types.Tool(
        name="ensembl_sequence_by_id",
        description="Retrieve a sequence by Ensembl ID. seq_type ∈ {genomic, cds, cdna, protein}.",
        inputSchema={
            "type": "object",
            "properties": {
                "ensembl_id": {"type": "string"},
                "seq_type": {
                    "type": "string",
                    "enum": ["genomic", "cds", "cdna", "protein"],
                    "default": "genomic",
                },
            },
            "required": ["ensembl_id"],
        },
    ),
    types.Tool(
        name="ensembl_xrefs_by_id",
        description="List external database cross-references for an Ensembl ID.",
        inputSchema={
            "type": "object",
            "properties": {"ensembl_id": {"type": "string"}},
            "required": ["ensembl_id"],
        },
    ),
    types.Tool(
        name="ensembl_homology_by_id",
        description="Get orthologs/paralogs for an Ensembl gene ID. Optional target_species and homology_type ∈ {orthologues, paralogues, projections}.",
        inputSchema={
            "type": "object",
            "properties": {
                "ensembl_id": {"type": "string"},
                "target_species": {"type": "string"},
                "homology_type": {
                    "type": "string",
                    "enum": ["orthologues", "paralogues", "projections"],
                },
            },
            "required": ["ensembl_id"],
        },
    ),
    types.Tool(
        name="kegg_find",
        description="Search a KEGG database. database ∈ {pathway, module, ko, genome, compound, glycan, reaction, rclass, enzyme, drug, dgroup, brite, <organism-code>}.",
        inputSchema={
            "type": "object",
            "properties": {
                "database": {"type": "string"},
                "query": {"type": "string"},
            },
            "required": ["database", "query"],
        },
    ),
    types.Tool(
        name="kegg_get",
        description="Fetch a KEGG entry by ID. Returns raw flat-file text.",
        inputSchema={
            "type": "object",
            "properties": {"entry_id": {"type": "string"}},
            "required": ["entry_id"],
        },
    ),
    types.Tool(
        name="kegg_link",
        description="Find linked entries between two KEGG databases (e.g. link pathway to hsa:7157).",
        inputSchema={
            "type": "object",
            "properties": {
                "target_db": {"type": "string"},
                "source_db_or_entry": {"type": "string"},
            },
            "required": ["target_db", "source_db_or_entry"],
        },
    ),
    types.Tool(
        name="kegg_conv",
        description="Convert KEGG IDs to/from external databases (NCBI gene, UniProt, ChEBI, PubChem).",
        inputSchema={
            "type": "object",
            "properties": {
                "target_db": {"type": "string"},
                "source_db_or_entry": {"type": "string"},
            },
            "required": ["target_db", "source_db_or_entry"],
        },
    ),
]


@server.list_tools()
async def _list_tools() -> list[types.Tool]:
    return TOOLS


# ---- dispatch ---------------------------------------------------------------


async def _dispatch(name: str, args: dict[str, Any]) -> Any:
    async with httpx.AsyncClient() as client:
        match name:
            case "ensembl_lookup_id":
                return await ensembl.lookup_id(
                    client, args["ensembl_id"], expand=args.get("expand", False)
                )
            case "ensembl_lookup_symbol":
                return await ensembl.lookup_symbol(
                    client, args["species"], args["symbol"], expand=args.get("expand", False)
                )
            case "ensembl_sequence_by_id":
                return await ensembl.sequence_by_id(
                    client, args["ensembl_id"], seq_type=args.get("seq_type", "genomic")
                )
            case "ensembl_xrefs_by_id":
                return await ensembl.xrefs_by_id(client, args["ensembl_id"])
            case "ensembl_homology_by_id":
                return await ensembl.homology_by_id(
                    client,
                    args["ensembl_id"],
                    target_species=args.get("target_species"),
                    homology_type=args.get("homology_type"),
                )
            case "kegg_find":
                return await kegg.find(client, args["database"], args["query"])
            case "kegg_get":
                return await kegg.get(client, args["entry_id"])
            case "kegg_link":
                return await kegg.link(client, args["target_db"], args["source_db_or_entry"])
            case "kegg_conv":
                return await kegg.conv(client, args["target_db"], args["source_db_or_entry"])
            case _:
                raise ValueError(f"unknown tool: {name}")


@server.call_tool()
async def _call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    try:
        result = await _dispatch(name, arguments)
    except (ensembl.EnsemblError, kegg.KeggError) as exc:
        return [types.TextContent(type="text", text=f"error: {exc}")]
    text = result if isinstance(result, str) else json.dumps(result, indent=2)
    return [types.TextContent(type="text", text=text)]


# ---- entrypoint -------------------------------------------------------------


async def _serve() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
