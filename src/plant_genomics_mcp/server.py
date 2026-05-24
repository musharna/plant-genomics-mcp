"""MCP server entry point — exposes plant genomics tools over stdio.

This dispatch ships thirty-two tools — fifteen single-locus, one BLAST
sequence-similarity search, twelve batch variants that fan out per-locus
calls in parallel, and four cross-source synthesis tools that compose
the live backends:

  - ``ensembl_plants_lookup_locus``       — Ensembl Plants REST (live)
  - ``get_gene_xrefs``                    — Ensembl Plants xrefs (live)
  - ``phytozome_lookup_locus``            — Phytozome BioMart (live)
  - ``resolve_locus_to_uniprot``          — UniProt KB search (live)
  - ``locus_literature``                  — Europe PMC search (live)
  - ``locus_go_annotations``              — QuickGO GO annotations (live, locus→UniProt→QuickGO)
  - ``blast_sequence``                    — NCBI BLAST URLAPI (live, async Put/Get polling)
  - ``gramene_homologs``                  — Gramene v69 homology (live, ortholog/paralog + gene_tree_id)
  - ``kegg_pathways``                     — KEGG pathway memberships (live, multi-organism via ``organism=``)
  - ``string_interactions``               — STRING-DB first-neighbor partners (live, per-channel scores)
  - ``atted_coexpression``                — ATTED-II Ath-u.c4-0 coexpression (live, z-scores)
  - ``bar_gene_summary``                  — BAR ThaleMine + GAIA aliases (live, Arabidopsis curator summary)
  - ``bar_efp_expression``                — BAR world-eFP natural-variation expression (live, ~36 Arabidopsis ecotypes)
  - ``bar_aiv_interactions``              — BAR AIV interactions (live, Arabidopsis GRN paper refs / Rice predicted PPI pairs)
  - ``tair_locus_info``                   — alias of ``bar_gene_summary`` (TAIR REST is subscription-gated; BAR mirrors the curator data)
  - ``plantcyc_locus_info``               — informational stub (subscription-gated)
  - ``batch_ensembl_plants_lookup_locus`` — Ensembl Plants POST /lookup/id (one round-trip)
  - ``batch_get_gene_xrefs``              — gather over get_gene_xrefs
  - ``batch_phytozome_lookup_locus``      — gather over phytozome_lookup_locus
  - ``batch_resolve_locus_to_uniprot``    — gather over resolve_locus_to_uniprot
  - ``batch_locus_literature``            — gather over locus_literature
  - ``batch_locus_go_annotations``        — gather over locus_go_annotations
  - ``batch_gramene_homologs``            — gather over gramene_homologs
  - ``batch_kegg_pathways``               — gather over kegg_pathways
  - ``batch_string_interactions``         — gather over string_interactions
  - ``batch_atted_coexpression``          — gather over atted_coexpression
  - ``batch_bar_gene_summary``            — gather over bar_gene_summary
  - ``batch_bar_aiv_interactions``        — gather over bar_aiv_interactions
  - ``analyze_locus_synth``               — v0.8 synthesis: Ensembl + Phytozome + UniProt + xrefs in one envelope
  - ``find_homologs_synth``               — v0.8 synthesis: BLAST + per-hit UniProt resolution
  - ``biological_context_synth``          — v0.8 synthesis: GO + literature + KEGG + STRING + ATTED + consensus_partners
  - ``consensus_homologs``                — v0.8 synthesis: cross-source ranking (Gramene + BLAST agreement)

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
    atted,
    bar,
    batch,
    blast,
    ensembl_plants,
    europe_pmc,
    gramene,
    kegg,
    phytozome,
    plantcyc,
    progress,
    prompts,
    quickgo,
    resources,
    string_db,
    synthesis,
    tair,
    uniprot,
)
from plant_genomics_mcp.models import (
    AttedCoexpression,
    BarAIVInteractions,
    BarEfpExpression,
    BarGeneSummary,
    BatchEnvelope,
    BlastResult,
    EnsemblPlantsLocus,
    GeneXrefs,
    GrameneHomologs,
    KeggPathways,
    LocusGoAnnotations,
    LocusLiterature,
    PhytozomeLocus,
    PlantCycLocusInfo,
    StringInteractions,
    SynthesisEnvelope,
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

# Synthesis tools compose multiple backends and don't fit a single EDAM
# operation, so we list all three operations the orchestrator covers.
# Topic stays on Plant biology + Functional genomics — the synthesis
# layer is most often used for functional context.
_EDAM_SYNTHESIS = {
    "edam": {
        "operation": [
            "operation_0224",  # Query and retrieval
            "operation_2424",  # Comparison
            "operation_2422",  # Data retrieval
        ],
        "topic": ["topic_0780", "topic_0085"],  # Plant biology, Functional genomics
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
            "Defaults to arabidopsis_thaliana; pass organism= for other plant "
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
                "organism": {
                    "type": ["string", "integer"],
                    "description": "Plant organism — accepts canonical slug (arabidopsis_thaliana), scientific or common name, or NCBI taxid",
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
            "Defaults to arabidopsis_thaliana; pass organism= for other "
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
                "organism": {
                    "type": ["string", "integer"],
                    "description": "Plant organism — accepts canonical slug (arabidopsis_thaliana), scientific or common name, or NCBI taxid",
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
            "(phytozome-next.jgi.doe.gov). Defaults to arabidopsis_thaliana; "
            "pass organism= for other Phytozome proteomes (slug, scientific/common "
            "name, or NCBI taxid — e.g. glycine_max, sorghum_bicolor). Locus is "
            "the source-genome gene name (e.g. AT1G01010, Glyma.01G000100). "
            "Returns organism_name, gene_name, chromosome, gene_start, gene_end, "
            "strand, description."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "locus": {
                    "type": "string",
                    "description": "e.g. AT1G01010 (Arabidopsis), Glyma.01G000100 (soybean)",
                },
                "organism": {
                    "type": ["string", "integer"],
                    "description": "Plant organism — accepts canonical slug (arabidopsis_thaliana), scientific or common name, or NCBI taxid",
                    "default": "arabidopsis_thaliana",
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
            "organism accepts a canonical slug, scientific/common name, or "
            "NCBI taxid (default arabidopsis_thaliana; e.g. oryza_sativa, "
            "zea_mays). "
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
                "organism": {
                    "type": ["string", "integer"],
                    "description": "Plant organism — accepts canonical slug (arabidopsis_thaliana), scientific or common name, or NCBI taxid",
                    "default": "arabidopsis_thaliana",
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
                "organism": {
                    "type": ["string", "integer"],
                    "description": "Plant organism — accepts canonical slug (arabidopsis_thaliana), scientific or common name, or NCBI taxid",
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
                "organism": {
                    "type": ["string", "integer"],
                    "description": "Plant organism — accepts canonical slug (arabidopsis_thaliana), scientific or common name, or NCBI taxid",
                    "default": "arabidopsis_thaliana",
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
        name="gramene_homologs",
        description=(
            "Fetch orthologs and paralogs for a plant locus from Gramene compara "
            "(data.gramene.org v69). Default homology_type='ortholog'; pass "
            "'paralog' for in-species duplicates or 'all' for everything. "
            "Returns target_locus + homology category (type) + shared gene_tree_id "
            "per hit. The fl=homology projection does not carry per-row taxon, "
            "identity, or protein ID; pair with resolve_locus_to_uniprot for "
            "protein-level enrichment and with blast_sequence for sequence "
            "similarity discovery."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "locus": {
                    "type": "string",
                    "description": "e.g. AT1G01010 (Arabidopsis), Os01g0100100 (rice)",
                },
                "homology_type": {
                    "type": "string",
                    "enum": ["ortholog", "paralog", "all"],
                    "description": "Filter on homology kind",
                    "default": "ortholog",
                },
            },
            "required": ["locus"],
        },
        outputSchema=GrameneHomologs.model_json_schema(),
        _meta=_EDAM,
    ),
    types.Tool(
        name="kegg_pathways",
        description=(
            "Fetch KEGG pathway memberships for an Arabidopsis locus from "
            "rest.kegg.jp. Returns a list of pathway IDs + names + KEGG "
            "category classes the locus participates in. Pairs with "
            "locus_go_annotations for the GO-level functional view. "
            "Multi-organism caveat (v1.1.0): the organism= field accepts "
            "any plant in the matrix for symmetry with the other backends, "
            "but only arabidopsis_thaliana resolves — KEGG uses NCBI "
            "Entrez Gene IDs for rice/maize/etc. and our cross-backend "
            "locus contract can't produce those yet, so any other organism "
            "raises OrganismNotSupported before any HTTP call. KEGG v118+ "
            "is case-sensitive on the locus: pass AGI loci as uppercase."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "locus": {
                    "type": "string",
                    "description": "Arabidopsis AGI locus, e.g. AT1G01010 (case preserved verbatim — KEGG v118+ is case-sensitive)",
                },
                "organism": {
                    "type": ["string", "integer"],
                    "description": "Plant organism — only arabidopsis_thaliana is supported in v1.1.0; other plants raise OrganismNotSupported until an Entrez bridge lands",
                    "default": "arabidopsis_thaliana",
                },
            },
            "required": ["locus"],
        },
        outputSchema=KeggPathways.model_json_schema(),
        _meta=_EDAM,
    ),
    types.Tool(
        name="bar_gene_summary",
        description=(
            "Fetch the BAR (Bio-Analytic Resource, U Toronto) merged "
            "ThaleMine + GAIA-aliases summary for an Arabidopsis locus. "
            "Returns the TAIR curator summary + Araport11 computational "
            "description from /thalemine/gene_information/ together with "
            "the NCBI Gene ID and cross-DB aliases (RefSeq, UniProt, "
            "TIGR locus-model IDs) from /gaia/aliases/. Arabidopsis only — "
            "ThaleMine carries taxon 3702 plus yeast/human for ortholog "
            "cross-reference. BAR is keyless and a Global Core Biodata "
            "Resource (2023); replaces the v0.9 subscription-gated "
            "tair_locus_info stub for the curator-summary use case."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "locus": {
                    "type": "string",
                    "description": "Arabidopsis AGI locus, e.g. AT1G01010",
                },
            },
            "required": ["locus"],
        },
        outputSchema=BarGeneSummary.model_json_schema(),
        _meta=_EDAM,
    ),
    types.Tool(
        name="bar_efp_expression",
        description=(
            "Fetch BAR/eFP world-map natural-variation expression for an "
            "Arabidopsis locus. Wraps the world-eFP view at "
            "/microarray_gene_expression/world_efp/arabidopsis/{locus} — "
            "returns expression across ~36 ecotypes (Bay-0, Col-0, Cvi-1, "
            "Ler-2, ...) with per-replicate values, control samples, "
            "collection lat/lng, and a per-ecotype mean computed client-"
            "side. Arabidopsis only. BAR is keyless and a Global Core "
            "Biodata Resource (2023)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "locus": {
                    "type": "string",
                    "description": "Arabidopsis AGI locus, e.g. AT1G01010",
                },
            },
            "required": ["locus"],
        },
        outputSchema=BarEfpExpression.model_json_schema(),
        _meta=_EDAM,
    ),
    types.Tool(
        name="bar_aiv_interactions",
        description=(
            "Fetch BAR AIV (Arabidopsis Interactions Viewer) interactions "
            "for an Arabidopsis or rice locus. Dispatches by organism: "
            "Arabidopsis returns curated GRN paper refs from "
            "/interactions/get_paper_by_agi/{locus} (PubMed ID, title, "
            "image, comments, pipe-split tags); rice returns predicted "
            "PPI partners from /interactions/rice/{locus} with Pearson "
            "co-expression r (pcc), evidence hits, and quality score. "
            "The `kind` field discriminates the response shape "
            "(grn_papers vs ppi_predictions). Rice requires the MSU "
            "LOC_Os* locus format — RAP-DB Os*g* is rejected upstream. "
            "Only Arabidopsis and rice are supported by AIV; other "
            "organisms raise OrganismNotSupported."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "locus": {
                    "type": "string",
                    "description": "AGI locus (AT1G01010) for Arabidopsis or MSU locus (LOC_Os01g01080) for rice",
                },
                "organism": {
                    "type": ["string", "integer"],
                    "description": "arabidopsis_thaliana or oryza_sativa — slug, scientific/common name, or NCBI taxid",
                    "default": "arabidopsis_thaliana",
                },
            },
            "required": ["locus"],
        },
        outputSchema=BarAIVInteractions.model_json_schema(),
        _meta=_EDAM,
    ),
    types.Tool(
        name="string_interactions",
        description=(
            "Fetch protein-protein interaction partners from STRING-DB "
            "(string-db.org). Accepts either a UniProt accession or a "
            "locus identifier — the latter is resolved via UniProt first. "
            "Defaults to arabidopsis_thaliana; pass organism= for other "
            "plant species (slug, scientific/common name, or NCBI taxid). "
            "Returns first-neighbor partners with the combined STRING score "
            "plus per-channel sub-scores (experimental, database, "
            "textmining, predicted)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "locus_or_accession": {
                    "type": "string",
                    "description": "UniProt accession (Q0WV96) or locus (AT1G01010)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of partners to return",
                    "default": 20,
                    "minimum": 1,
                    "maximum": 500,
                },
                "organism": {
                    "type": ["string", "integer"],
                    "description": "Plant organism — accepts canonical slug (arabidopsis_thaliana), scientific or common name, or NCBI taxid",
                    "default": "arabidopsis_thaliana",
                },
            },
            "required": ["locus_or_accession"],
        },
        outputSchema=StringInteractions.model_json_schema(),
        _meta=_EDAM,
    ),
    types.Tool(
        name="tair_locus_info",
        description=(
            "Fetch the TAIR curator-vetted Arabidopsis locus summary. Served "
            "via BAR/ThaleMine (U Toronto, Global Core Biodata Resource 2023) "
            "since TAIR's free per-locus REST API is gated behind a paid "
            "Phoenix Bioinformatics subscription. Returns TAIR curator summary "
            "+ Araport11 computational description + NCBI Gene ID + cross-DB "
            "aliases (RefSeq, UniProt, TIGR locus-model IDs). Arabidopsis "
            "only. Alias of bar_gene_summary."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "locus": {
                    "type": "string",
                    "description": "Arabidopsis AGI locus, e.g. AT1G01010",
                },
            },
            "required": ["locus"],
        },
        outputSchema=BarGeneSummary.model_json_schema(),
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
            "parallel GETs. Successes in results[] with the same shape as "
            "the single-locus tool. "
            "Retries 429/5xx via the shared `_http` helper (Retry-After "
            "capped at 60 s). Misses (loci with no record) still land in "
            "`errors[]` with the `[NotFoundError]` prefix; the whole batch "
            "only fails when the retry budget is exhausted."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "loci": _LOCI_SCHEMA,
                "organism": {
                    "type": ["string", "integer"],
                    "description": "Plant organism — accepts canonical slug (arabidopsis_thaliana), scientific or common name, or NCBI taxid",
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
                "organism": {
                    "type": ["string", "integer"],
                    "description": "Plant organism — accepts canonical slug (arabidopsis_thaliana), scientific or common name, or NCBI taxid",
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
                "organism": {
                    "type": ["string", "integer"],
                    "description": "Plant organism — accepts canonical slug (arabidopsis_thaliana), scientific or common name, or NCBI taxid",
                    "default": "arabidopsis_thaliana",
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
                "organism": {
                    "type": ["string", "integer"],
                    "description": "Plant organism — accepts canonical slug (arabidopsis_thaliana), scientific or common name, or NCBI taxid",
                    "default": "arabidopsis_thaliana",
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
                "organism": {
                    "type": ["string", "integer"],
                    "description": "Plant organism — accepts canonical slug (arabidopsis_thaliana), scientific or common name, or NCBI taxid",
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
                    "maxLength": 1_000_000,
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
                "organism": {
                    "type": ["string", "integer"],
                    "description": "Plant organism — accepts canonical slug (arabidopsis_thaliana), scientific or common name, or NCBI taxid",
                    "default": "arabidopsis_thaliana",
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
    types.Tool(
        name="batch_gramene_homologs",
        description=(
            "Batch version of gramene_homologs. Up to 50 loci per call; "
            "shares the homology_type filter across all loci. Returns the "
            "standard batch envelope (count + results dict + errors dict)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "loci": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 50,
                    "description": "List of locus identifiers (max 50)",
                },
                "homology_type": {
                    "type": "string",
                    "enum": ["ortholog", "paralog", "all"],
                    "default": "ortholog",
                },
            },
            "required": ["loci"],
        },
        outputSchema=BatchEnvelope.model_json_schema(),
        _meta=_EDAM,
    ),
    types.Tool(
        name="batch_kegg_pathways",
        description=(
            "Batch version of kegg_pathways. Up to 50 loci per call. "
            "v1.1.0: only arabidopsis_thaliana resolves — KEGG uses NCBI "
            "Entrez Gene IDs for other plants and our cross-backend locus "
            "contract can't produce those yet, so a non-ath organism= "
            "raises OrganismNotSupported before any HTTP fan-out."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "loci": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 50,
                },
                "organism": {
                    "type": ["string", "integer"],
                    "description": "Plant organism — only arabidopsis_thaliana is supported in v1.1.0; other plants raise OrganismNotSupported until an Entrez bridge lands",
                    "default": "arabidopsis_thaliana",
                },
            },
            "required": ["loci"],
        },
        outputSchema=BatchEnvelope.model_json_schema(),
        _meta=_EDAM,
    ),
    types.Tool(
        name="batch_bar_gene_summary",
        description=(
            "Batch variant of bar_gene_summary. Fans out per-locus BAR "
            "ThaleMine + GAIA-aliases calls in parallel (up to "
            f"{batch.MAX_BATCH} loci). Each results[locus] is the full "
            "single-locus payload (curator summary, computational "
            "description, NCBI Gene ID, cross-DB aliases). Arabidopsis only."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "loci": _LOCI_SCHEMA,
            },
            "required": ["loci"],
        },
        outputSchema=_BATCH_OUTPUT,
        _meta=_EDAM,
    ),
    types.Tool(
        name="batch_bar_aiv_interactions",
        description=(
            "Batch variant of bar_aiv_interactions. Fans out per-locus "
            "BAR AIV calls in parallel (up to "
            f"{batch.MAX_BATCH} loci); all loci in a single call share "
            "the same organism. Each results[locus] is the full single-"
            "locus payload (kind=grn_papers for Arabidopsis with `papers` "
            "list, kind=ppi_predictions for rice with `partners` list)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "loci": _LOCI_SCHEMA,
                "organism": {
                    "type": ["string", "integer"],
                    "description": "arabidopsis_thaliana or oryza_sativa — slug, scientific/common name, or NCBI taxid",
                    "default": "arabidopsis_thaliana",
                },
            },
            "required": ["loci"],
        },
        outputSchema=_BATCH_OUTPUT,
        _meta=_EDAM,
    ),
    types.Tool(
        name="batch_string_interactions",
        description="Batch version of string_interactions. Up to 50 inputs per call.",
        inputSchema={
            "type": "object",
            "properties": {
                "loci_or_accessions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 50,
                },
                "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 500},
                "organism": {
                    "type": ["string", "integer"],
                    "description": "Plant organism — accepts canonical slug (arabidopsis_thaliana), scientific or common name, or NCBI taxid",
                    "default": "arabidopsis_thaliana",
                },
            },
            "required": ["loci_or_accessions"],
        },
        outputSchema=BatchEnvelope.model_json_schema(),
        _meta=_EDAM,
    ),
    types.Tool(
        name="atted_coexpression",
        description=(
            "Fetch co-expressed gene neighbors from ATTED-II (atted.jp, "
            "API v5, Ath-u.c4-0 release) for an Arabidopsis locus. Returns "
            "top_n neighbors with target locus + NCBI Entrez gene ID + "
            "z-score (higher = stronger coexpression). Pairs with "
            "string_interactions to surface high-confidence functional "
            "partners (interactors that are also coexpressed)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "locus": {
                    "type": "string",
                    "description": "Arabidopsis AGI locus, e.g. AT1G01010",
                },
                "top_n": {
                    "type": "integer",
                    "default": 25,
                    "minimum": 1,
                    "maximum": 300,
                },
            },
            "required": ["locus"],
        },
        outputSchema=AttedCoexpression.model_json_schema(),
        _meta=_EDAM,
    ),
    types.Tool(
        name="batch_atted_coexpression",
        description="Batch version of atted_coexpression. Up to 50 loci per call.",
        inputSchema={
            "type": "object",
            "properties": {
                "loci": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 50,
                },
                "top_n": {"type": "integer", "default": 25, "minimum": 1, "maximum": 300},
            },
            "required": ["loci"],
        },
        outputSchema=BatchEnvelope.model_json_schema(),
        _meta=_EDAM,
    ),
    types.Tool(
        name="analyze_locus_synth",
        description=(
            "Synthesis: one-call equivalent of the analyze_locus prompt. "
            "Resolves a locus through Ensembl Plants, then fans out to xrefs, "
            "UniProt, Europe PMC, and QuickGO in parallel. Returns a "
            "SynthesisEnvelope with per-step status and a reconciled summary "
            "flagging cross-source name/accession disagreements."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "locus": {"type": "string", "description": "Locus name, e.g. AT1G01010"},
                "organism": {
                    "type": ["string", "integer"],
                    "default": "arabidopsis_thaliana",
                    "description": "Plant organism — accepts canonical slug (arabidopsis_thaliana), scientific or common name, or NCBI taxid",
                },
            },
            "required": ["locus"],
            "additionalProperties": False,
        },
        outputSchema=SynthesisEnvelope.model_json_schema(),
        _meta=_EDAM_SYNTHESIS,
    ),
    types.Tool(
        name="find_homologs_synth",
        description=(
            "Synthesis: one-call equivalent of the find_homologs prompt. "
            "Runs BLAST then resolves UniProt-shaped subject accessions via "
            "the batch UniProt helper. Returns ranked hits each annotated with "
            "their UniProt record (or null if subject_id is not a UniProt "
            "accession)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "sequence": {
                    "type": "string",
                    "description": "Query sequence (protein or nucleotide)",
                    "maxLength": 1_000_000,
                },
                "program": {
                    "type": "string",
                    "enum": ["blastn", "blastp", "blastx", "tblastn", "tblastx"],
                    "default": "blastp",
                },
                "top_n": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            },
            "required": ["sequence"],
            "additionalProperties": False,
        },
        outputSchema=SynthesisEnvelope.model_json_schema(),
        _meta=_EDAM_SYNTHESIS,
    ),
    types.Tool(
        name="biological_context_synth",
        description=(
            "Synthesis: one-call equivalent of the biological_context prompt. "
            "Resolves UniProt accession, then fans out to Gramene homologs, "
            "KEGG pathways, STRING-DB partners, and ATTED-II coexpression in "
            "parallel. Adds a consensus_partners ranking that merges STRING + "
            "ATTED scores."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "locus": {"type": "string"},
                "organism": {
                    "type": ["string", "integer"],
                    "description": "Plant organism — accepts canonical slug (arabidopsis_thaliana), scientific or common name, or NCBI taxid",
                    "default": "arabidopsis_thaliana",
                },
                "top_n": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            },
            "required": ["locus"],
            "additionalProperties": False,
        },
        outputSchema=SynthesisEnvelope.model_json_schema(),
        _meta=_EDAM_SYNTHESIS,
    ),
    types.Tool(
        name="consensus_homologs",
        description=(
            "Synthesis: cross-source homology consensus. Resolves UniProt + "
            "FASTA sequence, then runs Gramene homology calls and NCBI BLAST "
            "in parallel. Dedupes hits by normalized locus token and scores "
            "by n_sources * mean_identity — Gramene contributes identity=1.0, "
            "BLAST contributes pident/100."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "locus": {"type": "string"},
                "organism": {
                    "type": ["string", "integer"],
                    "description": "Plant organism — accepts canonical slug (arabidopsis_thaliana), scientific or common name, or NCBI taxid",
                    "default": "arabidopsis_thaliana",
                },
                "top_n": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            },
            "required": ["locus"],
            "additionalProperties": False,
        },
        outputSchema=SynthesisEnvelope.model_json_schema(),
        _meta=_EDAM_SYNTHESIS,
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


# ---- prompts ----------------------------------------------------------------
# Parameterized natural-language workflows (analyze_locus, find_homologs).
# See plant_genomics_mcp.prompts for the catalog and renderers.


@server.list_prompts()
async def _list_prompts() -> list[types.Prompt]:
    return prompts.PROMPTS


@server.get_prompt()
async def _get_prompt(name: str, arguments: dict[str, str] | None) -> types.GetPromptResult:
    return await prompts.get_prompt(name, arguments)


# ---- dispatch ---------------------------------------------------------------


async def _resolve_then_go_annotations(
    client: httpx.AsyncClient,
    locus: str,
    organism: str | int,
    limit: int,
) -> dict[str, Any]:
    """Locus → UniProt accession → QuickGO annotations.

    Propagates NotFoundError from either step — a locus with no UniProt
    entry can't be queried in QuickGO, so the caller gets a typed error
    rather than an empty result that hides the resolution failure.
    """
    up = await uniprot.lookup_locus(client, locus, organism=organism)
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
                    organism=args.get("organism", "arabidopsis_thaliana"),
                )
            case "get_gene_xrefs":
                return await ensembl_plants.lookup_xrefs(
                    client,
                    args["locus"],
                    organism=args.get("organism", "arabidopsis_thaliana"),
                )
            case "phytozome_lookup_locus":
                return await phytozome.lookup_locus(
                    client,
                    args["locus"],
                    organism=args.get("organism", "arabidopsis_thaliana"),
                )
            case "resolve_locus_to_uniprot":
                return await uniprot.lookup_locus(
                    client,
                    args["locus"],
                    organism=args.get("organism", "arabidopsis_thaliana"),
                )
            case "locus_literature":
                return await europe_pmc.lookup_locus(
                    client,
                    args["locus"],
                    organism=args.get("organism", "arabidopsis_thaliana"),
                    size=args.get("size", europe_pmc.DEFAULT_PAGE_SIZE),
                )
            case "locus_go_annotations":
                return await _resolve_then_go_annotations(
                    client,
                    args["locus"],
                    organism=args.get("organism", "arabidopsis_thaliana"),
                    limit=args.get("limit", quickgo.DEFAULT_LIMIT),
                )
            case "tair_locus_info":
                # Silent upgrade (Wave A.6.8): delegates to bar.gene_summary.
                # MCP tool name preserved; body now returns real BAR/ThaleMine
                # curator summary instead of a subscription_required stub.
                return await tair.lookup_locus(client, args["locus"])
            case "plantcyc_locus_info":
                # Pure-data sync call — no client, no await. Returns a
                # structured redirect; see plant_genomics_mcp.plantcyc docstring.
                return plantcyc.lookup_locus(args["locus"])
            case "batch_ensembl_plants_lookup_locus":
                return await batch.batch_ensembl_plants_lookup_locus(
                    client,
                    args["loci"],
                    organism=args.get("organism", "arabidopsis_thaliana"),
                )
            case "batch_get_gene_xrefs":
                return await batch.batch_get_gene_xrefs(
                    client,
                    args["loci"],
                    organism=args.get("organism", "arabidopsis_thaliana"),
                )
            case "batch_phytozome_lookup_locus":
                return await batch.batch_phytozome_lookup_locus(
                    client,
                    args["loci"],
                    organism=args.get("organism", "arabidopsis_thaliana"),
                )
            case "batch_resolve_locus_to_uniprot":
                return await batch.batch_resolve_locus_to_uniprot(
                    client,
                    args["loci"],
                    organism=args.get("organism", "arabidopsis_thaliana"),
                )
            case "batch_locus_literature":
                return await batch.batch_locus_literature(
                    client,
                    args["loci"],
                    organism=args.get("organism", "arabidopsis_thaliana"),
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
                    organism=args.get("organism", "arabidopsis_thaliana"),
                    limit=args.get("limit", quickgo.DEFAULT_LIMIT),
                )
            case "batch_kegg_pathways":
                return await batch.batch_kegg_pathways(
                    client,
                    args["loci"],
                    organism=args.get("organism", "arabidopsis_thaliana"),
                )
            case "bar_gene_summary":
                return await bar.gene_summary(client, args["locus"])
            case "batch_bar_gene_summary":
                return await batch.batch_bar_gene_summary(client, args["loci"])
            case "bar_efp_expression":
                return await bar.efp_expression(client, args["locus"])
            case "bar_aiv_interactions":
                return await bar.aiv_interactions(
                    client,
                    args["locus"],
                    organism=args.get("organism", "arabidopsis_thaliana"),
                )
            case "batch_bar_aiv_interactions":
                return await batch.batch_bar_aiv_interactions(
                    client,
                    args["loci"],
                    organism=args.get("organism", "arabidopsis_thaliana"),
                )
            case "batch_string_interactions":
                return await batch.batch_string_interactions(
                    client,
                    args["loci_or_accessions"],
                    limit=args.get("limit", string_db.DEFAULT_LIMIT),
                    organism=args.get("organism", "arabidopsis_thaliana"),
                )
            case "batch_gramene_homologs":
                return await batch.batch_gramene_homologs(
                    client,
                    args["loci"],
                    homology_type=args.get("homology_type", "ortholog"),
                )
            case "kegg_pathways":
                return await kegg.lookup_pathways(
                    client,
                    args["locus"],
                    organism=args.get("organism", "arabidopsis_thaliana"),
                )
            case "string_interactions":
                return await string_db.lookup_partners(
                    client,
                    args["locus_or_accession"],
                    limit=args.get("limit", string_db.DEFAULT_LIMIT),
                    organism=args.get("organism", "arabidopsis_thaliana"),
                )
            case "atted_coexpression":
                return await atted.lookup_coexpression(
                    client,
                    args["locus"],
                    top_n=args.get("top_n", atted.DEFAULT_TOP_N),
                )
            case "batch_atted_coexpression":
                return await batch.batch_atted_coexpression(
                    client,
                    args["loci"],
                    top_n=args.get("top_n", atted.DEFAULT_TOP_N),
                )
            case "gramene_homologs":
                return await gramene.lookup_homologs(
                    client,
                    args["locus"],
                    homology_type=args.get("homology_type", "ortholog"),
                )
            case "analyze_locus_synth":
                env = await synthesis.analyze_locus_synth(
                    client,
                    args["locus"],
                    organism=args.get("organism", "arabidopsis_thaliana"),
                )
                return env.model_dump()
            case "find_homologs_synth":
                env = await synthesis.find_homologs_synth(
                    client,
                    args["sequence"],
                    program=args.get("program", "blastp"),
                    top_n=args.get("top_n", 10),
                )
                return env.model_dump()
            case "biological_context_synth":
                env = await synthesis.biological_context_synth(
                    client,
                    args["locus"],
                    organism=args.get("organism", "arabidopsis_thaliana"),
                    top_n=args.get("top_n", 10),
                )
                return env.model_dump()
            case "consensus_homologs":
                env = await synthesis.consensus_homologs(
                    client,
                    args["locus"],
                    organism=args.get("organism", "arabidopsis_thaliana"),
                    top_n=args.get("top_n", 10),
                )
                return env.model_dump()
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
