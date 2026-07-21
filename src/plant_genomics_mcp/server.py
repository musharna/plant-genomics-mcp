"""MCP server entry point — exposes plant genomics tools over stdio.

This dispatch ships forty-six tools — twenty-five single-locus, one
genomic-region query, one variant-consequence (VEP) annotator, one
gene-set enrichment, one BLAST sequence-similarity search, twelve batch
variants that fan out per-locus calls in parallel, and five cross-source
synthesis tools that compose the live backends:

  - ``ensembl_plants_lookup_locus``       — Ensembl Plants REST (live)
  - ``get_gene_xrefs``                    — Ensembl Plants xrefs (live)
  - ``phytozome_lookup_locus``            — Phytozome BioMart (live)
  - ``resolve_locus_to_uniprot``          — UniProt KB search (live)
  - ``locus_literature``                  — Europe PMC search (live)
  - ``locus_go_annotations``              — QuickGO GO annotations (live, locus→UniProt→QuickGO)
  - ``locus_plant_ontology``              — Planteome PO/TO/PECO annotations (live, per-locus by taxon)
  - ``go_enrichment``                     — g:Profiler GO+KEGG over-representation over a gene LIST (live)
  - ``blast_sequence``                    — NCBI BLAST URLAPI (live, async Put/Get polling)
  - ``gramene_homologs``                  — Gramene v69 homology (live, ortholog/paralog + gene_tree_id)
  - ``kegg_pathways``                     — KEGG pathway memberships (live, multi-organism via ``organism=``)
  - ``string_interactions``               — STRING-DB first-neighbor partners (live, per-channel scores)
  - ``atted_coexpression``                — ATTED-II Ath-u.c4-0 coexpression (live, z-scores)
  - ``bar_gene_summary``                  — BAR ThaleMine + GAIA aliases (live, Arabidopsis curator summary)
  - ``bar_efp_expression``                — BAR world-eFP natural-variation expression (live, ~36 Arabidopsis ecotypes)
  - ``bar_aiv_interactions``              — BAR AIV interactions (live, Arabidopsis GRN paper refs / Rice predicted PPI pairs)
  - ``tair_locus_info``                   — alias of ``bar_gene_summary`` (TAIR REST is subscription-gated; BAR mirrors the curator data)
  - ``plantcyc_locus_info``               — PlantCyc/PMN metabolism (live, gene→enzyme→reactions→pathways)
  - ``alphafold_structure``               — AlphaFold DB predicted structure (live, locus→UniProt→model + pLDDT)
  - ``experimental_structures``           — PDBe experimentally-solved structures (live, locus→UniProt→best PDB entries)
  - ``interpro_domains``                  — InterPro domain/family architecture (live, locus→UniProt→domains; Pfam incl.)
  - ``tf_binding_motifs``                 — JASPAR TF binding motifs (live, locus→UniProt→UniProt-confirmed profiles + consensus)
  - ``jaspar_motif``                      — one JASPAR profile by matrix id incl. the raw PFM (live)
  - ``locus_variants``                    — Ensembl natural variants overlapping a locus (live, EVA/dbSNP; 12 organisms)
  - ``vep_annotate``                      — Ensembl VEP variant-consequence prediction (live, region+allele; SIFT/PolyPhen)
  - ``panther_family``                    — PANTHER protein family/subfamily + GO + protein class (live, 12 organisms)
  - ``orthodb_orthologs``                 — OrthoDB ortholog group + cross-species members (live, Viridiplantae; 12 organisms)
  - ``aragwas_associations``              — AraGWAS GWAS hits per locus (live, Arabidopsis-only)
  - ``arabidopsis_natural_variation``     — 1001 Genomes natural-variation SNP effects per locus (live, Arabidopsis-only)
  - ``get_sequence``                      — Ensembl /sequence/id genomic/cds/cdna/protein FASTA (live; feeds blast_sequence)
  - ``ensembl_region_query``              — genes/features overlapping a genomic interval via Ensembl /overlap/region (live)
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
  - ``gene_report``                       — v0.9 synthesis: one-shot Markdown gene dossier (annotation+xrefs+protein+domains+GO+KEGG+STRING+literature)

``tair_locus_info`` is a silent alias of ``bar_gene_summary`` — the TAIR REST
API is subscription-gated (Phoenix Bioinformatics), but BAR ThaleMine mirrors
the same curator data for free. ``plantcyc_locus_info`` is a live PlantCyc/PMN
metabolism client (v1.13): the earlier "subscription-gated" stub was a
misclassification — the BioCyc web-services API is free (re-probed 2026-07-19).

Batch tools share an envelope shape ``{tool, count, results, errors}`` so a
chain consumer can route by typed prefix the same way it does for the
single-locus tools. Capped at ``batch.MAX_BATCH = 50`` loci per call.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any

import httpx
from mcp import types
from mcp.server import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.stdio import stdio_server
from pydantic import AnyUrl

from plant_genomics_mcp import (
    alphafold,
    aragwas,
    atted,
    bar,
    batch,
    blast,
    ensembl_plants,
    ensembl_variation,
    europe_pmc,
    gprofiler,
    gramene,
    interpro,
    jaspar,
    kegg,
    onekg,
    orthodb,
    panther,
    pdbe,
    phytozome,
    plantcyc,
    planteome,
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
    AlphaFoldStructure,
    ArabidopsisNaturalVariation,
    AraGwasAssociations,
    AttedCoexpression,
    BarAIVInteractions,
    BarEfpExpression,
    BarGeneSummary,
    BatchEnvelope,
    BlastResult,
    EnsemblPlantsLocus,
    EnsemblRegionFeatures,
    EnsemblSequence,
    ExperimentalStructures,
    GeneXrefs,
    GoEnrichmentResult,
    GrameneHomologs,
    InterProDomains,
    JasparMotif,
    KeggPathways,
    LocusGoAnnotations,
    LocusLiterature,
    LocusPlantOntology,
    LocusVariants,
    OrthoDbOrthologs,
    PantherFamily,
    PhytozomeLocus,
    PlantCycLocusInfo,
    StringInteractions,
    SynthesisEnvelope,
    TfBindingMotifs,
    UniProtLocus,
    VepAnnotation,
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
            "additionalProperties": False,
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
            "additionalProperties": False,
        },
        outputSchema=GeneXrefs.model_json_schema(),
        _meta=_EDAM,
    ),
    types.Tool(
        name="get_sequence",
        description=(
            "Fetch a locus's sequence from Ensembl Plants. seq_type is one of "
            "genomic / cds / cdna / protein (default protein — the "
            "canonical-transcript product). Closes the lookup → fetch → BLAST "
            "loop: feed the returned `sequence` straight to blast_sequence "
            "(protein for blastp, cds/cdna for blastn). Defaults to "
            "arabidopsis_thaliana; pass organism= for other plant species."
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
                "seq_type": {
                    "type": "string",
                    "enum": ["genomic", "cds", "cdna", "protein"],
                    "default": "protein",
                    "description": "Sequence type to fetch",
                },
            },
            "required": ["locus"],
            "additionalProperties": False,
        },
        outputSchema=EnsemblSequence.model_json_schema(),
        _meta=_EDAM,
    ),
    types.Tool(
        name="ensembl_region_query",
        description=(
            "List features overlapping a genomic interval via Ensembl Plants "
            "/overlap/region. region is the seq-region name (chromosome / "
            "contig, e.g. '1'); start and end are 1-based inclusive. feature is "
            "one of gene / transcript / cds / exon (default gene). Answers "
            "'what genes are in this QTL interval / assembly window' without a "
            "per-locus lookup. Ensembl caps the span — oversized regions error. "
            "Defaults to arabidopsis_thaliana; pass organism= for other species."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "region": {
                    "type": "string",
                    "description": "seq-region name (chromosome / contig), e.g. '1' or 'Chr1'",
                },
                "start": {"type": "integer", "minimum": 1, "description": "1-based start"},
                "end": {"type": "integer", "minimum": 1, "description": "1-based inclusive end"},
                "organism": {
                    "type": ["string", "integer"],
                    "description": "Plant organism — accepts canonical slug (arabidopsis_thaliana), scientific or common name, or NCBI taxid",
                    "default": "arabidopsis_thaliana",
                },
                "feature": {
                    "type": "string",
                    "enum": ["gene", "transcript", "cds", "exon"],
                    "default": "gene",
                    "description": "Feature type to return",
                },
            },
            "required": ["region", "start", "end"],
            "additionalProperties": False,
        },
        outputSchema=EnsemblRegionFeatures.model_json_schema(),
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
            "additionalProperties": False,
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
            "additionalProperties": False,
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
            "additionalProperties": False,
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
            "additionalProperties": False,
        },
        outputSchema=LocusGoAnnotations.model_json_schema(),
        _meta=_EDAM_GO,
    ),
    types.Tool(
        name="locus_plant_ontology",
        description=(
            "Fetch Plant Ontology (PO) + Trait Ontology (TO) + experimental-"
            "condition (PECO) annotations for a plant locus from Planteome "
            "(browser.planteome.org, AmiGO2/GOlr; free, no API key). "
            "Complements locus_go_annotations: QuickGO serves GO (species-"
            "agnostic), Planteome serves the plant-specific ontologies — PO "
            "(anatomy + developmental stage), TO (traits). The locus is matched "
            "across Planteome's searchable bioentity fields and filtered by the "
            "organism's NCBI taxon. Returns annotations[] (term_id / term_name / "
            "ontology / aspect / evidence / reference) + a by_ontology rollup "
            "({PO: [{term_id, term_name}, ...], TO: [...], PECO: [...]}) deduped "
            "on term_id. Coverage is strong for arabidopsis, rice, maize, grape, "
            "soybean, tomato; other organisms return an empty list, not an error. "
            "Defaults to arabidopsis_thaliana; pass organism= for other species."
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
                    "description": "Max annotations from Planteome (1–200, default 100)",
                    "default": planteome.DEFAULT_LIMIT,
                    "minimum": 1,
                    "maximum": planteome.MAX_LIMIT,
                },
            },
            "required": ["locus"],
            "additionalProperties": False,
        },
        outputSchema=LocusPlantOntology.model_json_schema(),
        _meta=_EDAM_GO,
    ),
    types.Tool(
        name="go_enrichment",
        description=(
            "GO + KEGG over-representation analysis for a gene LIST via g:Profiler "
            "g:GOSt (biit.cs.ut.ee/gprofiler; free, no API key). Unlike "
            "locus_go_annotations (one locus → its terms), this answers 'what is my "
            "gene SET enriched for?' — the dominant question for a differential-"
            "expression or co-expression cluster. loci is the query gene list (e.g. "
            "AT-codes for Arabidopsis, RAP-DB IDs for rice). sources defaults to "
            "GO:BP/GO:MF/GO:CC + KEGG; user_threshold is the g:SCS-corrected "
            "significance cutoff (default 0.05). Optional background sets a custom "
            "statistical domain (default: all annotated genes). Returns enriched[] "
            "(term_id/name/p_value/intersection_size/…, capped at top_n by p-value) "
            "plus unmapped[] — query loci g:Profiler could not recognize, surfaced "
            "so a locus-namespace mismatch is visible. Defaults to "
            "arabidopsis_thaliana; pass organism= for any of the 12 species."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "loci": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Query gene set, e.g. ['AT2G46830', 'AT1G01060', ...]",
                },
                "organism": {
                    "type": ["string", "integer"],
                    "description": "Plant organism — accepts canonical slug (arabidopsis_thaliana), scientific or common name, or NCBI taxid",
                    "default": "arabidopsis_thaliana",
                },
                "sources": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["GO:BP", "GO:MF", "GO:CC", "KEGG"]},
                    "description": "Annotation sources to test (default: all four)",
                },
                "background": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional custom statistical background gene set",
                },
                "user_threshold": {
                    "type": "number",
                    "description": "Significance cutoff, g:SCS-corrected (default 0.05)",
                    "default": gprofiler.DEFAULT_THRESHOLD,
                    "exclusiveMinimum": 0,
                    "maximum": 1,
                },
                "top_n": {
                    "type": "integer",
                    "description": "Max terms returned, sorted by p-value (1–200, default 50)",
                    "default": gprofiler.DEFAULT_TOP_N,
                    "minimum": 1,
                    "maximum": gprofiler.MAX_TOP_N,
                },
            },
            "required": ["loci"],
            "additionalProperties": False,
        },
        outputSchema=GoEnrichmentResult.model_json_schema(),
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
            "additionalProperties": False,
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
            "additionalProperties": False,
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
            "additionalProperties": False,
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
            "additionalProperties": False,
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
            "additionalProperties": False,
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
            "additionalProperties": False,
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
            "additionalProperties": False,
        },
        outputSchema=BarGeneSummary.model_json_schema(),
        _meta=_EDAM,
    ),
    types.Tool(
        name="plantcyc_locus_info",
        description=(
            "Fetch metabolic annotation for a locus from PlantCyc / the Plant "
            "Metabolic Network (pmn.plantcyc.org; free BioCyc web-services API, "
            "no key). Walks gene → enzyme → catalyzed reactions → PlantCyc "
            "pathways in the organism's PGDB, returning enzymes[] + reactions[] "
            "(id/name) + pathways[] (id/name) — the metabolic-pathway view KEGG "
            "and GO don't provide. A non-enzymatic gene (e.g. a transcription "
            "factor) returns found=false with empty lists, not an error. "
            "reaction_count / pathway_count report true totals even when the "
            "lists are capped. 11 organisms have a PGDB (arabidopsis, rice, "
            "maize, soybean, grape, poplar, tomato, barley, sorghum, medicago, "
            "brachypodium); wheat is not yet mapped. Defaults to "
            "arabidopsis_thaliana (AraCyc, the best-curated); pass organism= "
            "for other species."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "locus": {
                    "type": "string",
                    "description": "e.g. AT3G51240 (Arabidopsis), Os11g0530600 (rice RAP-DB)",
                },
                "organism": {
                    "type": ["string", "integer"],
                    "description": "Plant organism — accepts canonical slug (arabidopsis_thaliana), scientific or common name, or NCBI taxid",
                    "default": "arabidopsis_thaliana",
                },
            },
            "required": ["locus"],
            "additionalProperties": False,
        },
        outputSchema=PlantCycLocusInfo.model_json_schema(),
        _meta=_EDAM,
    ),
    types.Tool(
        name="alphafold_structure",
        description=(
            "Fetch the AlphaFold DB predicted-structure summary for a locus "
            "(alphafold.ebi.ac.uk; free, no key). Resolves the locus → UniProt "
            "accession, then returns the predicted model's global mean pLDDT "
            "confidence, the per-band pLDDT distribution, modelled residue span, "
            "latest model version, and mmCIF / PDB / PAE download URLs. A valid "
            "protein with no deposited model returns found=false (a normal "
            "outcome, not an error); a locus with no UniProt entry raises a "
            "typed NotFoundError. Works for all 12 organisms (UniProt-keyed). "
            "Complements resolve_locus_to_uniprot (sequence-level) with the "
            "structure-level view. Defaults to arabidopsis_thaliana; pass "
            "organism= for other species."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "locus": {
                    "type": "string",
                    "description": "e.g. AT4G09760 (Arabidopsis), Os01g0100100 (rice RAP-DB)",
                },
                "organism": {
                    "type": ["string", "integer"],
                    "description": "Plant organism — accepts canonical slug (arabidopsis_thaliana), scientific or common name, or NCBI taxid",
                    "default": "arabidopsis_thaliana",
                },
            },
            "required": ["locus"],
            "additionalProperties": False,
        },
        outputSchema=AlphaFoldStructure.model_json_schema(),
        _meta=_EDAM,
    ),
    types.Tool(
        name="experimental_structures",
        description=(
            "Fetch experimentally-solved (X-ray / cryo-EM / NMR) protein "
            "structures for a locus from PDBe (www.ebi.ac.uk/pdbe; free, no key). "
            "Resolves the locus → UniProt accession, then returns PDBe's "
            "best_structures mapping ranked best-first: per entry the PDB id, "
            "chain, experimental method, resolution, coverage, and modelled "
            "residue span. Most plant proteins have NO deposited structure — that "
            "returns found=false (a normal outcome, not an error); a locus with no "
            "UniProt entry raises a typed NotFoundError. structure_count is the "
            "true total even when the list is capped. Complements "
            "alphafold_structure (the predicted view). Works for all 12 organisms "
            "(UniProt-keyed). Defaults to arabidopsis_thaliana; pass organism= for "
            "other species."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "locus": {
                    "type": "string",
                    "description": "e.g. AT4G09760 (Arabidopsis), Os01g0100100 (rice RAP-DB)",
                },
                "organism": {
                    "type": ["string", "integer"],
                    "description": "Plant organism — accepts canonical slug (arabidopsis_thaliana), scientific or common name, or NCBI taxid",
                    "default": "arabidopsis_thaliana",
                },
            },
            "required": ["locus"],
            "additionalProperties": False,
        },
        outputSchema=ExperimentalStructures.model_json_schema(),
        _meta=_EDAM,
    ),
    types.Tool(
        name="tf_binding_motifs",
        description=(
            "Fetch curated transcription-factor DNA binding motifs for a locus "
            "from JASPAR (jaspar.elixir.no; free, no key) — the cis-regulatory "
            "view. Resolves the locus → UniProt accession + gene symbol, "
            "searches JASPAR by symbol scoped to the organism's taxid, then "
            "CONFIRMS each candidate by matching the accession against the "
            "profile's uniprot_ids. Returns per motif the JASPAR matrix id, TF "
            "class/family, assay type (SELEX / ChIP-seq / PBM / DAP-seq), an "
            "IUPAC consensus derived from the position-frequency matrix (e.g. "
            "CACGTG, the G-box/ABRE core), motif length, PubMed refs, and an SVG "
            "sequence-logo URL. IMPORTANT: JASPAR's name search is fuzzy, so "
            "name-similarity hits belonging to a DIFFERENT gene are returned "
            "separately in name_only_matches and must NOT be attributed to this "
            "locus; only `motifs` is UniProt-confirmed. found=false means the "
            "gene has no curated profile (not a TF, or its family is unprofiled "
            "for that species) — a normal outcome, not an error. Use "
            "jaspar_motif to retrieve the raw matrix for any matrix_id. "
            "Coverage is Arabidopsis-heavy (1236 profiles) and thin elsewhere "
            "(maize 131, soybean 91, wheat 58, tomato 51, rice 10; Brachypodium "
            "and sorghum have none). Defaults to arabidopsis_thaliana; pass "
            "organism= for other species."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "locus": {
                    "type": "string",
                    "description": "e.g. AT2G46830 (Arabidopsis CCA1), Os01g0100100 (rice RAP-DB)",
                },
                "organism": {
                    "type": ["string", "integer"],
                    "description": "Plant organism — accepts canonical slug (arabidopsis_thaliana), scientific or common name, or NCBI taxid",
                    "default": "arabidopsis_thaliana",
                },
            },
            "required": ["locus"],
            "additionalProperties": False,
        },
        outputSchema=TfBindingMotifs.model_json_schema(),
        _meta=_EDAM,
    ),
    types.Tool(
        name="jaspar_motif",
        description=(
            "Fetch one JASPAR binding profile by matrix id, including its raw "
            "position-frequency matrix (PFM: per-base count vectors keyed "
            "A/C/G/T) plus TF class/family, assay type, source species, UniProt "
            "accessions, PubMed refs, IUPAC consensus, and the sequence-logo "
            "URL. The drill-down companion to tf_binding_motifs, which returns "
            "the derived consensus but not the matrix. Accepts a versioned id "
            "(MA0570.1) or a bare base id (MA0570, which resolves to the newest "
            "version). Unknown ids raise a typed NotFoundError."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "matrix_id": {
                    "type": "string",
                    "description": "JASPAR profile id, e.g. MA0570.1 or MA0570 (latest version)",
                },
            },
            "required": ["matrix_id"],
            "additionalProperties": False,
        },
        outputSchema=JasparMotif.model_json_schema(),
        _meta=_EDAM,
    ),
    types.Tool(
        name="interpro_domains",
        description=(
            "Fetch the InterPro domain / family architecture for a locus "
            "(www.ebi.ac.uk/interpro; free, no key). Resolves the locus → "
            "UniProt accession, then returns the protein's InterPro entries — "
            "each with accession, name, type (domain / family / "
            "homologous_superfamily / …), source_database (Pfam appears here as "
            "source_database='pfam', not a separate tool), the integrated "
            "InterPro accession, and residue spans — plus a count_by_type "
            "rollup. A protein with no annotated domains returns found=true with "
            "an empty list; a locus with no UniProt entry raises a typed "
            "NotFoundError. domain_count is the true total even when the row "
            "list is page-capped. Works for all 12 organisms (UniProt-keyed). "
            "Defaults to arabidopsis_thaliana; pass organism= for other species."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "locus": {
                    "type": "string",
                    "description": "e.g. AT4G09760 (Arabidopsis), Os01g0100100 (rice RAP-DB)",
                },
                "organism": {
                    "type": ["string", "integer"],
                    "description": "Plant organism — accepts canonical slug (arabidopsis_thaliana), scientific or common name, or NCBI taxid",
                    "default": "arabidopsis_thaliana",
                },
            },
            "required": ["locus"],
            "additionalProperties": False,
        },
        outputSchema=InterProDomains.model_json_schema(),
        _meta=_EDAM,
    ),
    types.Tool(
        name="locus_variants",
        description=(
            "List natural (germline) variants overlapping a locus's genomic span "
            "via Ensembl (rest.ensembl.org; free, no key). Resolves the locus → "
            "gene coordinates, then returns EVA/dbSNP-sourced SNPs and indels with "
            "id, source, consequence class, alleles, and clinical significance. "
            "variant_count is the true overlap total; the variant list is capped "
            "for payload size with truncated flagged. Opens the variation axis "
            "(distinct from get_sequence / ensembl_region_query). Works for all 12 "
            "organisms. Defaults to arabidopsis_thaliana; pass organism= for other "
            "species."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "locus": {
                    "type": "string",
                    "description": "e.g. AT1G01010 (Arabidopsis), Os01g0100100 (rice RAP-DB)",
                },
                "organism": {
                    "type": ["string", "integer"],
                    "description": "Plant organism — accepts canonical slug (arabidopsis_thaliana), scientific or common name, or NCBI taxid",
                    "default": "arabidopsis_thaliana",
                },
            },
            "required": ["locus"],
            "additionalProperties": False,
        },
        outputSchema=LocusVariants.model_json_schema(),
        _meta=_EDAM,
    ),
    types.Tool(
        name="vep_annotate",
        description=(
            "Predict a variant's molecular consequences with Ensembl VEP "
            "(rest.ensembl.org; free, no key). Variant-first (not locus-first): "
            "supply an Ensembl region (chr:start-end:strand, e.g. '1:10000-10000:1') "
            "and an alternate allele (e.g. 'C'); returns the most-severe "
            "consequence plus one row per overlapping transcript (consequence "
            "terms, IMPACT, and SIFT/PolyPhen when the variant is coding-missense). "
            "found=false when Ensembl reports no overlapping feature. Works for all "
            "12 organisms. Defaults to arabidopsis_thaliana; pass organism= for "
            "other species."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "region": {
                    "type": "string",
                    "description": "Ensembl region chr:start-end:strand, e.g. '1:10000-10000:1'",
                },
                "allele": {
                    "type": "string",
                    "description": "Alternate allele, e.g. 'C' (or 'A/C', an insertion, etc.)",
                },
                "organism": {
                    "type": ["string", "integer"],
                    "description": "Plant organism — accepts canonical slug (arabidopsis_thaliana), scientific or common name, or NCBI taxid",
                    "default": "arabidopsis_thaliana",
                },
            },
            "required": ["region", "allele"],
            "additionalProperties": False,
        },
        outputSchema=VepAnnotation.model_json_schema(),
        _meta=_EDAM,
    ),
    types.Tool(
        name="panther_family",
        description=(
            "Fetch the PANTHER protein-family classification for a locus "
            "(pantherdb.org; free, no key). Returns the PANTHER family and "
            "subfamily (id + name) plus curated GO terms grouped by aspect "
            "(molecular_function / biological_process / cellular_component), the "
            "PANTHER protein class, and pathways. found=false when PANTHER cannot "
            "classify the locus. Complements the sequence-homology tools "
            "(gramene_homologs / consensus_homologs) with an evolutionary-family "
            "view. Works for all 12 organisms. Defaults to arabidopsis_thaliana; "
            "pass organism= for other species."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "locus": {
                    "type": "string",
                    "description": "e.g. AT1G01060 (Arabidopsis), Os01g0100100 (rice RAP-DB)",
                },
                "organism": {
                    "type": ["string", "integer"],
                    "description": "Plant organism — accepts canonical slug (arabidopsis_thaliana), scientific or common name, or NCBI taxid",
                    "default": "arabidopsis_thaliana",
                },
            },
            "required": ["locus"],
            "additionalProperties": False,
        },
        outputSchema=PantherFamily.model_json_schema(),
        _meta=_EDAM,
    ),
    types.Tool(
        name="orthodb_orthologs",
        description=(
            "Resolve a locus to its OrthoDB ortholog group and cross-species "
            "member genes (data.orthodb.org; free, no key). Searches at the "
            "Viridiplantae level, then returns the group metadata (name, "
            "evolutionary rate) and member genes grouped by organism (organism, "
            "gene id, description). organism_count is the true cluster total; the "
            "member list is capped with truncated flagged. found=false when the "
            "locus maps to no ortholog group. Works for all 12 organisms. Defaults "
            "to arabidopsis_thaliana; pass organism= for other species."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "locus": {
                    "type": "string",
                    "description": "e.g. AT1G01060 (Arabidopsis), Os01g0100100 (rice RAP-DB)",
                },
                "organism": {
                    "type": ["string", "integer"],
                    "description": "Plant organism — accepts canonical slug (arabidopsis_thaliana), scientific or common name, or NCBI taxid",
                    "default": "arabidopsis_thaliana",
                },
            },
            "required": ["locus"],
            "additionalProperties": False,
        },
        outputSchema=OrthoDbOrthologs.model_json_schema(),
        _meta=_EDAM,
    ),
    types.Tool(
        name="aragwas_associations",
        description=(
            "Fetch AraGWAS genome-wide association study hits for an Arabidopsis "
            "locus (aragwas.1001genomes.org; free, no key). Returns each "
            "significant SNP association overlapping the gene with effect size "
            "(score), minor-allele frequency, the SNP's predicted molecular effect "
            "(impact, amino-acid change), and the phenotype/study it came from. "
            "association_count is the true total even when page-capped. "
            "ARABIDOPSIS-ONLY — any other organism raises OrganismNotSupported. "
            "Defaults to arabidopsis_thaliana."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "locus": {
                    "type": "string",
                    "description": "Arabidopsis AGI locus, e.g. AT1G01060",
                },
                "organism": {
                    "type": ["string", "integer"],
                    "description": "Arabidopsis only (the 1001 Genomes panel is A. thaliana)",
                    "default": "arabidopsis_thaliana",
                },
            },
            "required": ["locus"],
            "additionalProperties": False,
        },
        outputSchema=AraGwasAssociations.model_json_schema(),
        _meta=_EDAM,
    ),
    types.Tool(
        name="arabidopsis_natural_variation",
        description=(
            "Fetch 1001 Genomes natural-variation SNP effects for an Arabidopsis "
            "locus (tools.1001genomes.org; free, no key) — the variation observed "
            "across 1135 resequenced natural accessions. Returns per-SNP effect "
            "rows (chromosome, position, accession id, effect, impact, amino-acid "
            "change, transcript) plus the gene's genomic span. variant_count is the "
            "true row total even when capped. ARABIDOPSIS-ONLY — any other organism "
            "raises OrganismNotSupported. Defaults to arabidopsis_thaliana."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "locus": {
                    "type": "string",
                    "description": "Arabidopsis AGI locus, e.g. AT1G01060 (a bare AGI is transcript-scoped to .1)",
                },
                "organism": {
                    "type": ["string", "integer"],
                    "description": "Arabidopsis only (the 1001 Genomes panel is A. thaliana)",
                    "default": "arabidopsis_thaliana",
                },
            },
            "required": ["locus"],
            "additionalProperties": False,
        },
        outputSchema=ArabidopsisNaturalVariation.model_json_schema(),
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
            "additionalProperties": False,
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
            "additionalProperties": False,
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
            "additionalProperties": False,
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
            "additionalProperties": False,
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
            "additionalProperties": False,
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
            "additionalProperties": False,
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
            "additionalProperties": False,
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
            "additionalProperties": False,
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
            "additionalProperties": False,
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
            "additionalProperties": False,
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
            "additionalProperties": False,
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
            "additionalProperties": False,
        },
        outputSchema=BatchEnvelope.model_json_schema(),
        _meta=_EDAM,
    ),
    types.Tool(
        name="atted_coexpression",
        description=(
            "Fetch co-expressed gene neighbors from ATTED-II (atted.jp, "
            "API v5) for a plant locus. Returns top_n neighbors with "
            "target locus + NCBI Entrez gene ID + z-score (higher = "
            "stronger coexpression). The ATTED-II release "
            "(e.g. Ath-u.c4-0 for Arabidopsis, Osa-u.c1-0 for rice) is "
            "resolved per-organism; wheat, sorghum, barley, poplar, and "
            "brachypodium have no published release and raise "
            "OrganismNotSupported. Pairs with string_interactions to "
            "surface high-confidence functional partners (interactors "
            "that are also coexpressed)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "locus": {
                    "type": "string",
                    "description": "Plant locus, e.g. AT1G01010 (Arabidopsis) or Os01g0100100 (rice)",
                },
                "organism": {
                    "type": ["string", "integer"],
                    "default": "arabidopsis_thaliana",
                    "description": "Plant organism — accepts canonical slug (arabidopsis_thaliana), scientific or common name, or NCBI taxid",
                },
                "top_n": {
                    "type": "integer",
                    "default": 25,
                    "minimum": 1,
                    "maximum": 300,
                },
            },
            "required": ["locus"],
            "additionalProperties": False,
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
                "organism": {
                    "type": ["string", "integer"],
                    "default": "arabidopsis_thaliana",
                    "description": "Plant organism — accepts canonical slug (arabidopsis_thaliana), scientific or common name, or NCBI taxid",
                },
                "top_n": {"type": "integer", "default": 25, "minimum": 1, "maximum": 300},
            },
            "required": ["loci"],
            "additionalProperties": False,
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
    types.Tool(
        name="gene_report",
        description=(
            "Synthesis: one-shot 'tell me about this gene' dossier. Resolves a "
            "locus through Ensembl Plants + UniProt, then fans out to "
            "cross-references, KEGG pathways, STRING interactors, Europe PMC "
            "literature, and QuickGO GO terms. Returns a SynthesisEnvelope whose "
            "result.markdown is a rendered Markdown gene dossier (the headline "
            "output) alongside a structured result.sections mirror. Any single "
            "backend failure degrades that section to an 'Unavailable' note; the "
            "rest of the dossier still renders."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "locus": {"type": "string", "description": "Locus name, e.g. AT1G01010"},
                "organism": {
                    "type": ["string", "integer"],
                    "description": "Plant organism — accepts canonical slug (arabidopsis_thaliana), scientific or common name, or NCBI taxid",
                    "default": "arabidopsis_thaliana",
                },
                "top_n": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "default": 10,
                    "description": "Caps GO terms, pathways, interactors, xrefs, and papers per section",
                },
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
async def _read_resource(uri: AnyUrl) -> Iterable[ReadResourceContents]:
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
            case "get_sequence":
                return await ensembl_plants.get_sequence(
                    client,
                    args["locus"],
                    organism=args.get("organism", "arabidopsis_thaliana"),
                    seq_type=args.get("seq_type", "protein"),
                )
            case "ensembl_region_query":
                return await ensembl_plants.region_query(
                    client,
                    args["region"],
                    args["start"],
                    args["end"],
                    organism=args.get("organism", "arabidopsis_thaliana"),
                    feature=args.get("feature", "gene"),
                )
            case "go_enrichment":
                return await gprofiler.go_enrichment(
                    client,
                    args["loci"],
                    organism=args.get("organism", "arabidopsis_thaliana"),
                    sources=args.get("sources"),
                    background=args.get("background"),
                    user_threshold=args.get("user_threshold", gprofiler.DEFAULT_THRESHOLD),
                    top_n=args.get("top_n", gprofiler.DEFAULT_TOP_N),
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
            case "locus_plant_ontology":
                return await planteome.lookup_locus(
                    client,
                    args["locus"],
                    organism=args.get("organism", "arabidopsis_thaliana"),
                    limit=args.get("limit", planteome.DEFAULT_LIMIT),
                )
            case "tair_locus_info":
                # Silent upgrade (Wave A.6.8): delegates to bar.gene_summary.
                # MCP tool name preserved; body now returns real BAR/ThaleMine
                # curator summary instead of a subscription_required stub.
                return await tair.lookup_locus(client, args["locus"])
            case "plantcyc_locus_info":
                # v1.13: real PMN metabolic annotation (was a subscription-gated
                # stub; the API is free — see plant_genomics_mcp.plantcyc docstring).
                return await plantcyc.lookup_locus(
                    client,
                    args["locus"],
                    organism=args.get("organism", "arabidopsis_thaliana"),
                )
            case "alphafold_structure":
                return await alphafold.lookup_locus(
                    client,
                    args["locus"],
                    organism=args.get("organism", "arabidopsis_thaliana"),
                )
            case "experimental_structures":
                return await pdbe.lookup_locus(
                    client,
                    args["locus"],
                    organism=args.get("organism", "arabidopsis_thaliana"),
                )
            case "interpro_domains":
                return await interpro.lookup_locus(
                    client,
                    args["locus"],
                    organism=args.get("organism", "arabidopsis_thaliana"),
                )
            case "tf_binding_motifs":
                return await jaspar.lookup_locus(
                    client,
                    args["locus"],
                    organism=args.get("organism", "arabidopsis_thaliana"),
                )
            case "jaspar_motif":
                return await jaspar.lookup_matrix(client, args["matrix_id"])
            case "locus_variants":
                return await ensembl_variation.locus_variants(
                    client,
                    args["locus"],
                    organism=args.get("organism", "arabidopsis_thaliana"),
                )
            case "vep_annotate":
                return await ensembl_variation.vep_annotate(
                    client,
                    args["region"],
                    args["allele"],
                    organism=args.get("organism", "arabidopsis_thaliana"),
                )
            case "panther_family":
                return await panther.lookup_locus(
                    client,
                    args["locus"],
                    organism=args.get("organism", "arabidopsis_thaliana"),
                )
            case "orthodb_orthologs":
                return await orthodb.lookup_locus(
                    client,
                    args["locus"],
                    organism=args.get("organism", "arabidopsis_thaliana"),
                )
            case "aragwas_associations":
                return await aragwas.lookup_locus(
                    client,
                    args["locus"],
                    organism=args.get("organism", "arabidopsis_thaliana"),
                )
            case "arabidopsis_natural_variation":
                return await onekg.lookup_locus(
                    client,
                    args["locus"],
                    organism=args.get("organism", "arabidopsis_thaliana"),
                )
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
                    organism=args.get("organism", "arabidopsis_thaliana"),
                    top_n=args.get("top_n", atted.DEFAULT_TOP_N),
                )
            case "batch_atted_coexpression":
                return await batch.batch_atted_coexpression(
                    client,
                    args["loci"],
                    organism=args.get("organism", "arabidopsis_thaliana"),
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
            case "gene_report":
                env = await synthesis.gene_report(
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
