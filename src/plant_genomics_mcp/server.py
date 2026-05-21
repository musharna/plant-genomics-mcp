"""MCP server entry point — exposes plant genomics tools over stdio.

This dispatch ships seven tools:

  - ``ensembl_plants_lookup_locus``  — Ensembl Plants REST (live)
  - ``get_gene_xrefs``               — Ensembl Plants xrefs (live)
  - ``phytozome_lookup_locus``       — Phytozome BioMart (live)
  - ``resolve_locus_to_uniprot``     — UniProt KB search (live)
  - ``locus_literature``             — Europe PMC search (live)
  - ``tair_locus_info``              — informational stub (subscription-gated)
  - ``plantcyc_locus_info``          — informational stub (subscription-gated)

The TAIR and PlantCyc stubs are pure-data — both backends gate their free
per-locus REST APIs behind paid subscriptions (Phoenix Bioinformatics for
TAIR; SRI/Phoenix for the BioCyc PLANT orgid; probed 2026-05-21). Those
tools return structured redirects to the free Ensembl / Phytozome / UniProt
backends, which cover the same Arabidopsis annotation.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from plant_genomics_mcp import (
    ensembl_plants,
    europe_pmc,
    phytozome,
    plantcyc,
    tair,
    uniprot,
)
from plant_genomics_mcp.models import (
    EnsemblPlantsLocus,
    GeneXrefs,
    LocusLiterature,
    PhytozomeLocus,
    PlantCycLocusInfo,
    TairLocusInfo,
    UniProtLocus,
)

server: Server = Server("plant-genomics-mcp")


# ---- EDAM ontology tags -----------------------------------------------------
# Attached via _meta on each Tool so registry indexers (Smithery, Glama,
# bio.tools) can categorize. All tools share operation_2422 (Data
# retrieval) and the topic pair (Plant biology, Gene structure).
_EDAM = {
    "edam": {
        "operation": ["operation_2422"],  # Data retrieval
        "topic": ["topic_0780", "topic_0114"],  # Plant biology, Gene structure
    },
}

# Literature tool overrides the topic to Bibliography (topic_3068).
_EDAM_LITERATURE = {
    "edam": {
        "operation": ["operation_2422"],
        "topic": ["topic_0780", "topic_3068"],  # Plant biology, Literature and language
    },
}


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
        outputSchema=EnsemblPlantsLocus.model_json_schema(),
        _meta=_EDAM,
    ),
    types.Tool(
        name="get_gene_xrefs",
        description=(
            "Fetch cross-database references (UniProt, NCBI Gene, TAIR, "
            "ArrayExpress, …) for a plant locus from Ensembl Plants. "
            "Defaults to arabidopsis_thaliana; pass species= for other "
            "Ensembl Plants species. Returns count + raw xref list + a "
            "by_db rollup keyed on Ensembl's dbname (e.g. 'Uniprot_gn', "
            "'EntrezGene') for fast lookup of a single foreign identifier."
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
        outputSchema=GeneXrefs.model_json_schema(),
        _meta=_EDAM,
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
        outputSchema=PhytozomeLocus.model_json_schema(),
        _meta=_EDAM,
    ),
    types.Tool(
        name="resolve_locus_to_uniprot",
        description=(
            "Resolve a plant locus to its canonical UniProtKB record. Prefers "
            "reviewed (Swiss-Prot) entries; falls back to unreviewed (TrEMBL) "
            "when no curated record exists (common for non-Arabidopsis plants). "
            "organism_id is the NCBI taxonomy ID (default 3702 = Arabidopsis "
            "thaliana; 39947 = Oryza sativa japonica; 4577 = Zea mays). "
            "Returns primaryAccession, uniProtkbId, entryType, recommendedName, "
            "geneNames, organism, taxonId, sequenceLength, web_url. This is "
            "the protein-side entry point — pair with InterPro / AlphaFold / "
            "Reactome / structural-bio tools."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "locus": {
                    "type": "string",
                    "description": "e.g. AT1G01010 (Arabidopsis), Os01g0100100 (rice)",
                },
                "organism_id": {
                    "type": "integer",
                    "description": "NCBI taxonomy ID (default 3702 = Arabidopsis thaliana)",
                    "default": 3702,
                },
            },
            "required": ["locus"],
        },
        outputSchema=UniProtLocus.model_json_schema(),
        _meta=_EDAM,
    ),
    types.Tool(
        name="locus_literature",
        description=(
            "Search Europe PMC for literature mentioning a plant locus. "
            "Free, no API key. Returns up to `size` results (default 10, "
            "capped at 25) with title, authors, journal, year, DOI, PMID, "
            "open-access status, citation count, and abstract. For "
            "non-Arabidopsis species the species common name is appended "
            "to the query to disambiguate locus IDs (rice, maize, ...). "
            "Pair with resolve_locus_to_uniprot or ensembl_plants_lookup_locus "
            "to ground the locus before fanning out to the literature."
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
                    "description": "Ensembl species slug, used to qualify the query",
                    "default": "arabidopsis_thaliana",
                },
                "size": {
                    "type": "integer",
                    "description": "Max results (1–25, default 10)",
                    "default": europe_pmc.DEFAULT_PAGE_SIZE,
                    "minimum": 1,
                    "maximum": europe_pmc.MAX_PAGE_SIZE,
                },
            },
            "required": ["locus"],
        },
        outputSchema=LocusLiterature.model_json_schema(),
        _meta=_EDAM_LITERATURE,
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
        outputSchema=TairLocusInfo.model_json_schema(),
        _meta=_EDAM,
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
        outputSchema=PlantCycLocusInfo.model_json_schema(),
        _meta=_EDAM,
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
            case "get_gene_xrefs":
                return await ensembl_plants.lookup_xrefs(
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
            case "resolve_locus_to_uniprot":
                return await uniprot.lookup_locus(
                    client,
                    args["locus"],
                    organism_id=args.get("organism_id", uniprot.DEFAULT_TAXON_ID),
                )
            case "locus_literature":
                return await europe_pmc.lookup_locus(
                    client,
                    args["locus"],
                    species=args.get("species", "arabidopsis_thaliana"),
                    size=args.get("size", europe_pmc.DEFAULT_PAGE_SIZE),
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
async def _call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Return the dispatcher's dict directly.

    The SDK builds structuredContent (= this dict) AND a content[] of
    TextContent(JSON) for backwards compat. With outputSchema set on each
    tool, the SDK validates structuredContent against the model's schema.

    PlantGenomicsError (and subclasses) propagate to the SDK's outer
    ``except Exception`` handler, which calls ``_make_error_result(str(exc))``.
    Our PlantGenomicsError.__str__ prepends ``[ClassName]`` so the wire
    payload preserves the failure type.
    """
    return await _dispatch(name, arguments)


# ---- entrypoint -------------------------------------------------------------


async def _serve() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
