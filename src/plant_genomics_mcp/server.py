"""MCP server entry point — exposes plant genomics tools over stdio.

This dispatch ships four tools (``ensembl_plants_lookup_locus``,
``phytozome_lookup_locus``, ``tair_locus_info``, ``plantcyc_locus_info``).
``tair_locus_info`` and ``plantcyc_locus_info`` are pure-data
informational stubs — both TAIR and PlantCyc gate their free per-locus
REST APIs behind paid subscriptions (Phoenix Bioinformatics for TAIR;
SRI/Phoenix for the BioCyc PLANT orgid; both probed 2026-05-21). Those
tools return structured redirects to the two free Ensembl/Phytozome
backends, which cover the same Arabidopsis annotation.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from plant_genomics_mcp import ensembl_plants, phytozome, plantcyc, tair

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
    types.Tool(
        name="phytozome_lookup_locus",
        description=(
            "Fetch a gene record from Phytozome BioMart "
            "(phytozome-next.jgi.doe.gov). Defaults to organism_id=167 "
            "(Arabidopsis thaliana TAIR10); pass organism_id= for other "
            "Phytozome proteomes (e.g. 275 Glycine max, 313 Sorghum bicolor "
            "— hints, unverified). Locus is the source-genome gene name "
            "(e.g. AT1G01010, Glyma.01G000100). Returns organism_name, "
            "gene_name, chromosome, gene_start, gene_end, strand, description."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "locus": {
                    "type": "string",
                    "description": "e.g. AT1G01010 (Arabidopsis), Glyma.01G000100 (soybean)",
                },
                "organism_id": {
                    "type": "integer",
                    "description": (
                        "Phytozome proteome integer ID (default 167 = Arabidopsis thaliana TAIR10)"
                    ),
                    "default": 167,
                },
            },
            "required": ["locus"],
        },
    ),
    types.Tool(
        name="tair_locus_info",
        description=(
            "Returns subscription-access info and alternatives for a TAIR "
            "locus. Does NOT fetch annotation data — TAIR's free per-locus "
            "REST API was retired (Phoenix Bioinformatics subscription "
            "gate, probed 2026-05-21); use ensembl_plants_lookup_locus or "
            "phytozome_lookup_locus for the same Arabidopsis annotation. "
            "Returns a structured redirect with rationale and probed_at date."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "locus": {
                    "type": "string",
                    "description": "TAIR-canonical locus, e.g. AT1G01010",
                },
            },
            "required": ["locus"],
        },
    ),
    types.Tool(
        name="plantcyc_locus_info",
        description=(
            "Returns subscription-access info and alternatives for a "
            "PlantCyc locus. Does NOT fetch annotation or pathway data — "
            "PlantCyc requires paid SRI/Phoenix subscription. Use "
            "ensembl_plants_lookup_locus or phytozome_lookup_locus for "
            "canonical gene annotation; PlantCyc's pathway-membership "
            "value-add is not currently substituted. Returns structured "
            "redirect with rationale and probed_at date."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "locus": {
                    "type": "string",
                    "description": "TAIR-canonical locus, e.g. AT1G01010",
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
            case "phytozome_lookup_locus":
                return await phytozome.lookup_locus(
                    client,
                    args["locus"],
                    organism_id=args.get("organism_id", 167),
                )
            case "tair_locus_info":
                # Pure-data sync call — no client, no await. Returns a
                # structured redirect; see plant_genomics_mcp.tair docstring.
                return tair.lookup_locus(args["locus"])
            case "plantcyc_locus_info":
                # Pure-data sync call — no client, no await. Returns a
                # structured redirect; see plant_genomics_mcp.plantcyc docstring.
                return plantcyc.lookup_locus(args["locus"])
            case _:
                raise ValueError(f"unknown tool: {name}")


@server.call_tool()
async def _call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    try:
        result = await _dispatch(name, arguments)
    except ensembl_plants.PlantGenomicsError as exc:
        # phytozome reuses ensembl_plants.PlantGenomicsError, so one except catches both.
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
