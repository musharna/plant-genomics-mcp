"""MCP server entry point — exposes Ensembl Plants tools over stdio.

This dispatch ships ONE tool (``ensembl_plants_lookup_locus``). Phytozome,
TAIR, and PlantCyc backends are roadmapped as separate follow-up tasks.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from plant_genomics_mcp import ensembl_plants

server: Server = Server("plant-genomics-mcp")


# ---- tool catalog -----------------------------------------------------------

TOOLS: list[types.Tool] = [
    types.Tool(
        name="ensembl_plants_lookup_locus",
        description=(
            "Fetch metadata for a plant locus identifier from Ensembl Plants. "
            "Defaults to arabidopsis_thaliana; pass species= for other plant "
            "species (oryza_sativa, zea_mays, ...). Locus is the TAIR-style "
            "identifier (e.g. AT1G01010 for Arabidopsis NAC001)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "locus": {
                    "type": "string",
                    "description": "e.g. AT1G01010 (Arabidopsis), Os01g0100100 (rice)",
                },
                "species": {
                    "type": "string",
                    "description": "Ensembl species slug, e.g. arabidopsis_thaliana",
                    "default": "arabidopsis_thaliana",
                },
            },
            "required": ["locus"],
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
            case "ensembl_plants_lookup_locus":
                return await ensembl_plants.lookup_locus(
                    client,
                    args["locus"],
                    species=args.get("species", "arabidopsis_thaliana"),
                )
            case _:
                raise ValueError(f"unknown tool: {name}")


@server.call_tool()
async def _call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    try:
        result = await _dispatch(name, arguments)
    except ensembl_plants.PlantGenomicsError as exc:
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
