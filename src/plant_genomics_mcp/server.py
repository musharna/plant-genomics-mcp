"""MCP server entry point — exposes plant genomics tools over stdio.

This dispatch ships fifteen tools — eight single-locus, one BLAST
sequence-similarity search, plus six batch variants that fan out
per-locus calls in parallel:

  - ``ensembl_plants_lookup_locus``       — Ensembl Plants REST (live)
  - ``get_gene_xrefs``                    — Ensembl Plants xrefs (live)
  - ``phytozome_lookup_locus``            — Phytozome BioMart (live)
  - ``resolve_locus_to_uniprot``          — UniProt KB search (live)
  - ``locus_literature``                  — Europe PMC search (live)
  - ``locus_go_annotations``              — QuickGO GO annotations (live, locus→UniProt→QuickGO)
  - ``blast_sequence``                    — NCBI BLAST URLAPI (live, async Put/Get polling)
  - ``tair_locus_info``                   — informational stub (subscription-gated)
  - ``plantcyc_locus_info``               — informational stub (subscription-gated)
  - ``batch_ensembl_plants_lookup_locus`` — Ensembl Plants POST /lookup/id (one round-trip)
  - ``batch_get_gene_xrefs``              — gather over get_gene_xrefs
  - ``batch_phytozome_lookup_locus``      — gather over phytozome_lookup_locus
  - ``batch_resolve_locus_to_uniprot``    — gather over resolve_locus_to_uniprot
  - ``batch_locus_literature``            — gather over locus_literature
  - ``batch_locus_go_annotations``        — gather over locus_go_annotations

The TAIR and PlantCyc stubs are pure-data — both backends gate their free
per-locus REST APIs behind paid subscriptions (Phoenix Bioinformatics for
TAIR; SRI/Phoenix for the BioCyc PLANT orgid; probed 2026-05-21). Those
tools return structured redirects to the free Ensembl / Phytozome / UniProt
backends, which cover the same Arabidopsis annotation.

Batch tools share an envelope shape ``{tool, count, results, errors}`` so a
chain consumer can route by typed prefix the same way it does for the
single-locus tools. Capped at ``batch.MAX_BATCH = 50`` loci per call.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from pydantic import AnyUrl

from plant_genomics_mcp import (
    batch,
    blast,
    ensembl_plants,
    europe_pmc,
    phytozome,
    plantcyc,
    progress,
    quickgo,
    resources,
    tair,
    uniprot,
)
from plant_genomics_mcp.models import (
    BatchEnvelope,
    BlastResult,
    EnsemblPlantsLocus,
    GeneXrefs,
    LocusGoAnnotations,
    LocusLiterature,
    PhytozomeLocus,
    PlantCycLocusInfo,
    TairLocusInfo,
    UniProtLocus,
)

server: Server = Server("plant-genomics-mcp")


def _build_reporter() -> progress.Reporter | None:
    """Construct a progress reporter from the current MCP request context.

    Returns ``None`` if (a) we're called outside a request, (b) the client
    did not pass a ``progressToken``, or (c) the session isn't reachable.
    The HTTP helpers fall back to no-op behavior when no reporter is
    installed, so this never has to raise.
    """
    try:
        ctx = server.request_context
    except LookupError:
        return None
    meta = getattr(ctx, "meta", None)
    token = getattr(meta, "progressToken", None) if meta is not None else None
    if token is None:
        return None
    session = ctx.session

    async def _send(p: float, t: float | None, m: str | None) -> None:
        await session.send_progress_notification(
            progress_token=token,
            progress=p,
            total=t,
            message=m,
        )

    return progress.Reporter(_send)


# ---- EDAM ontology tags -----------------------------------------------------
# Attached via _meta on each Tool so registry indexers (Smithery, Glama,
# bio.tools) can categorize. Default operation is 2422 (Data retrieval)
# with the topic pair (Plant biology, Gene structure).
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

# GO annotations tool — operation_0306 (Text mining) + topic_0085 (Functional, regulatory
# and non-coding RNA).
_EDAM_GO = {
    "edam": {
        "operation": ["operation_2422"],  # Data retrieval
        "topic": ["topic_0780", "topic_0085"],  # Plant biology, Functional genomics
    },
}

# BLAST tool — operation_0292 (Sequence alignment) + topic_0182 (Sequence analysis).
_EDAM_BLAST = {
    "edam": {
        "operation": ["operation_0292"],  # Sequence alignment
        "topic": ["topic_0182", "topic_0080"],  # Sequence analysis, Sequence sites
    },
}


# ---- shared schema fragments ------------------------------------------------

_LOCI_SCHEMA = {
    "type": "array",
    "items": {"type": "string"},
    "minItems": 1,
    "maxItems": batch.MAX_BATCH,
    "description": (
        f"List of locus identifiers (1–{batch.MAX_BATCH}). "
        "Successes land in results[locus]; PlantGenomicsError failures in errors[locus]."
    ),
}

_BATCH_OUTPUT = BatchEnvelope.model_json_schema()


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
        name="locus_go_annotations",
        description=(
            "Fetch Gene Ontology annotations for a plant locus from QuickGO "
            "(EBI). Free, no API key. The locus is first resolved to a "
            "UniProt accession via the same logic as resolve_locus_to_uniprot, "
            "then QuickGO is queried by geneProductId. Returns annotations[] "
            "with goId/goName/goAspect/qualifier/evidence + a by_aspect rollup "
            "({molecular_function: [{goId, goName}, ...], biological_process: "
            "[...], cellular_component: [...]}) deduped on goId so the "
            "high-level term set is one read away."
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
                "limit": {
                    "type": "integer",
                    "description": "Max annotations from QuickGO (1–100, default 50)",
                    "default": quickgo.DEFAULT_LIMIT,
                    "minimum": 1,
                    "maximum": quickgo.MAX_LIMIT,
                },
            },
            "required": ["locus"],
        },
        outputSchema=LocusGoAnnotations.model_json_schema(),
        _meta=_EDAM_GO,
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
    types.Tool(
        name="batch_ensembl_plants_lookup_locus",
        description=(
            "Batch variant of ensembl_plants_lookup_locus. Uses Ensembl's "
            "native POST /lookup/id endpoint — one HTTP round-trip for "
            f"up to {batch.MAX_BATCH} loci, materially cheaper than N "
            "parallel GETs. Misses (loci with no record) land in errors[] "
            "with the [NotFoundError] prefix; successes in results[] with "
            "the same shape as the single-locus tool."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "loci": _LOCI_SCHEMA,
                "species": {
                    "type": "string",
                    "description": "Ensembl species slug, e.g. arabidopsis_thaliana",
                    "default": "arabidopsis_thaliana",
                },
            },
            "required": ["loci"],
        },
        outputSchema=_BATCH_OUTPUT,
        _meta=_EDAM,
    ),
    types.Tool(
        name="batch_get_gene_xrefs",
        description=(
            "Batch variant of get_gene_xrefs. Fans out per-locus xref "
            f"lookups over Ensembl Plants in parallel (up to {batch.MAX_BATCH} "
            "loci). Each results[locus] is the full single-locus shape "
            "(count + xrefs[] + by_db rollup)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "loci": _LOCI_SCHEMA,
                "species": {
                    "type": "string",
                    "description": "Ensembl species slug, e.g. arabidopsis_thaliana",
                    "default": "arabidopsis_thaliana",
                },
            },
            "required": ["loci"],
        },
        outputSchema=_BATCH_OUTPUT,
        _meta=_EDAM,
    ),
    types.Tool(
        name="batch_phytozome_lookup_locus",
        description=(
            "Batch variant of phytozome_lookup_locus. Fans out per-locus "
            f"BioMart queries in parallel (up to {batch.MAX_BATCH} loci). "
            "Each results[locus] is the full single-locus row "
            "(organism_name, gene_name, chromosome, start/end/strand, description)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "loci": _LOCI_SCHEMA,
                "organism_id": {
                    "type": "integer",
                    "description": (
                        "Phytozome proteome integer ID (default 167 = Arabidopsis thaliana TAIR10)"
                    ),
                    "default": 167,
                },
            },
            "required": ["loci"],
        },
        outputSchema=_BATCH_OUTPUT,
        _meta=_EDAM,
    ),
    types.Tool(
        name="batch_resolve_locus_to_uniprot",
        description=(
            "Batch variant of resolve_locus_to_uniprot. Fans out per-locus "
            "UniProtKB searches in parallel (up to "
            f"{batch.MAX_BATCH} loci). Each results[locus] is the full "
            "single-locus record (primaryAccession + uniProtkbId + entryType "
            "+ geneNames + organism + sequenceLength + web_url + …)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "loci": _LOCI_SCHEMA,
                "organism_id": {
                    "type": "integer",
                    "description": "NCBI taxonomy ID (default 3702 = Arabidopsis thaliana)",
                    "default": 3702,
                },
            },
            "required": ["loci"],
        },
        outputSchema=_BATCH_OUTPUT,
        _meta=_EDAM,
    ),
    types.Tool(
        name="batch_locus_literature",
        description=(
            "Batch variant of locus_literature. Fans out per-locus Europe PMC "
            f"searches in parallel (up to {batch.MAX_BATCH} loci). Each "
            "results[locus] is the full single-locus payload (query + "
            "hitCount + returned + hits[])."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "loci": _LOCI_SCHEMA,
                "species": {
                    "type": "string",
                    "description": "Ensembl species slug, used to qualify the query",
                    "default": "arabidopsis_thaliana",
                },
                "size": {
                    "type": "integer",
                    "description": "Max results per locus (1–25, default 10)",
                    "default": europe_pmc.DEFAULT_PAGE_SIZE,
                    "minimum": 1,
                    "maximum": europe_pmc.MAX_PAGE_SIZE,
                },
            },
            "required": ["loci"],
        },
        outputSchema=_BATCH_OUTPUT,
        _meta=_EDAM_LITERATURE,
    ),
    types.Tool(
        name="blast_sequence",
        description=(
            "Run a BLAST sequence-similarity search against NCBI BLAST URLAPI. "
            "Async Put/Get under the hood — submits the query, polls the RID "
            "(honoring NCBI's per-RID 60s floor), and returns the parsed top "
            "hits + raw text report excerpt. Programs: blastn / blastp / "
            "blastx / tblastn / tblastx. Database defaults to swissprot for "
            "protein programs, core_nt for nucleotide. Emits "
            "notifications/progress on each poll. Long searches (>10 min) "
            "raise [NotFoundError] with the RID preserved so the client can "
            "re-poll. Set PLANT_GENOMICS_MCP_NCBI_EMAIL to identify the "
            "request per NCBI etiquette."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "sequence": {
                    "type": "string",
                    "description": "Raw or FASTA-formatted query sequence.",
                },
                "program": {
                    "type": "string",
                    "enum": ["blastn", "blastp", "blastx", "tblastn", "tblastx"],
                    "description": "BLAST program — default blastp.",
                    "default": "blastp",
                },
                "database": {
                    "type": "string",
                    "description": (
                        "NCBI BLAST database slug (e.g. swissprot, core_nt, "
                        "refseq_protein). Defaults to swissprot for protein "
                        "programs and core_nt for nucleotide programs."
                    ),
                },
                "hitlist_size": {
                    "type": "integer",
                    "description": "Max hits to return (default 10).",
                    "default": 10,
                    "minimum": 1,
                    "maximum": 100,
                },
                "expect": {
                    "type": "number",
                    "description": "E-value threshold (default 10).",
                    "default": 10.0,
                    "exclusiveMinimum": 0,
                },
                "megablast": {
                    "type": "boolean",
                    "description": "Enable megablast (blastn only). Default false.",
                    "default": False,
                },
                "poll_interval": {
                    "type": "number",
                    "description": (
                        "Seconds between polls. Clamped up to NCBI's per-RID 60s floor."
                    ),
                    "default": blast.DEFAULT_POLL_INTERVAL,
                    "minimum": blast.MIN_POLL_INTERVAL,
                },
                "max_wait": {
                    "type": "number",
                    "description": (
                        "Max seconds to wait for the search to finish before "
                        "raising NotFoundError with the RID preserved "
                        "(default 600)."
                    ),
                    "default": blast.DEFAULT_MAX_WAIT,
                    "exclusiveMinimum": 0,
                },
            },
            "required": ["sequence"],
        },
        outputSchema=BlastResult.model_json_schema(),
        _meta=_EDAM_BLAST,
    ),
    types.Tool(
        name="batch_locus_go_annotations",
        description=(
            "Batch variant of locus_go_annotations. Two-stage fanout — each "
            "locus is resolved to UniProt and then queried in QuickGO. Per-locus "
            "NotFoundError from either stage lands in errors[] with the typed "
            "prefix preserved. Capped at "
            f"{batch.MAX_BATCH} loci."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "loci": _LOCI_SCHEMA,
                "organism_id": {
                    "type": "integer",
                    "description": "NCBI taxonomy ID (default 3702 = Arabidopsis thaliana)",
                    "default": 3702,
                },
                "limit": {
                    "type": "integer",
                    "description": "Max annotations per locus from QuickGO (1–100, default 50)",
                    "default": quickgo.DEFAULT_LIMIT,
                    "minimum": 1,
                    "maximum": quickgo.MAX_LIMIT,
                },
            },
            "required": ["loci"],
        },
        outputSchema=_BATCH_OUTPUT,
        _meta=_EDAM_GO,
    ),
]


@server.list_tools()
async def _list_tools() -> list[types.Tool]:
    return TOOLS


# ---- resources --------------------------------------------------------------
# Read-only metadata surface (cache stats, supported organisms, backend status).
# See plant_genomics_mcp.resources for the URI catalog and payload builders.


@server.list_resources()
async def _list_resources() -> list[types.Resource]:
    return resources.RESOURCES


@server.read_resource()
async def _read_resource(uri: AnyUrl):  # type: ignore[no-untyped-def]
    return await resources.read_resource(uri)


# ---- dispatch ---------------------------------------------------------------


async def _resolve_then_go_annotations(
    client: httpx.AsyncClient,
    locus: str,
    organism_id: int,
    limit: int,
) -> dict[str, Any]:
    """Locus → UniProt accession → QuickGO annotations.

    Propagates NotFoundError from either step — a locus with no UniProt
    entry can't be queried in QuickGO, so the caller gets a typed error
    rather than an empty result that hides the resolution failure.
    """
    up = await uniprot.lookup_locus(client, locus, organism_id=organism_id)
    accession = up["primaryAccession"]
    go = await quickgo.lookup_by_uniprot(client, accession, limit=limit)
    return {
        "locus": locus,
        "uniprot_accession": accession,
        "numberOfHits": go["numberOfHits"],
        "returned": go["returned"],
        "annotations": go["annotations"],
        "by_aspect": go["by_aspect"],
    }


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
            case "locus_go_annotations":
                return await _resolve_then_go_annotations(
                    client,
                    args["locus"],
                    organism_id=args.get("organism_id", uniprot.DEFAULT_TAXON_ID),
                    limit=args.get("limit", quickgo.DEFAULT_LIMIT),
                )
            case "tair_locus_info":
                # Pure-data sync call — no client, no await. Returns a
                # structured redirect; see plant_genomics_mcp.tair docstring.
                return tair.lookup_locus(args["locus"])
            case "plantcyc_locus_info":
                # Pure-data sync call — no client, no await. Returns a
                # structured redirect; see plant_genomics_mcp.plantcyc docstring.
                return plantcyc.lookup_locus(args["locus"])
            case "batch_ensembl_plants_lookup_locus":
                return await batch.batch_ensembl_plants_lookup_locus(
                    client,
                    args["loci"],
                    species=args.get("species", "arabidopsis_thaliana"),
                )
            case "batch_get_gene_xrefs":
                return await batch.batch_get_gene_xrefs(
                    client,
                    args["loci"],
                    species=args.get("species", "arabidopsis_thaliana"),
                )
            case "batch_phytozome_lookup_locus":
                return await batch.batch_phytozome_lookup_locus(
                    client,
                    args["loci"],
                    organism_id=args.get("organism_id", 167),
                )
            case "batch_resolve_locus_to_uniprot":
                return await batch.batch_resolve_locus_to_uniprot(
                    client,
                    args["loci"],
                    organism_id=args.get("organism_id", uniprot.DEFAULT_TAXON_ID),
                )
            case "batch_locus_literature":
                return await batch.batch_locus_literature(
                    client,
                    args["loci"],
                    species=args.get("species", "arabidopsis_thaliana"),
                    size=args.get("size", europe_pmc.DEFAULT_PAGE_SIZE),
                )
            case "blast_sequence":
                return await blast.blast_sequence(
                    client,
                    args["sequence"],
                    program=args.get("program", "blastp"),
                    database=args.get("database"),
                    hitlist_size=args.get("hitlist_size", 10),
                    expect=args.get("expect", 10.0),
                    megablast=args.get("megablast", False),
                    poll_interval=args.get("poll_interval", blast.DEFAULT_POLL_INTERVAL),
                    max_wait=args.get("max_wait", blast.DEFAULT_MAX_WAIT),
                )
            case "batch_locus_go_annotations":
                return await batch.batch_locus_go_annotations(
                    client,
                    args["loci"],
                    organism_id=args.get("organism_id", uniprot.DEFAULT_TAXON_ID),
                    limit=args.get("limit", quickgo.DEFAULT_LIMIT),
                )
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

    If the client passed a ``progressToken`` in the request meta, install a
    Reporter on the contextvar so the HTTP helpers (retry loops + BioMart
    POST) emit ``notifications/progress`` messages over the active session.
    """
    reporter = _build_reporter()
    if reporter is None:
        return await _dispatch(name, arguments)
    token = progress.set_reporter(reporter)
    try:
        return await _dispatch(name, arguments)
    finally:
        progress.reset_reporter(token)


# ---- entrypoint -------------------------------------------------------------


async def _serve() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
