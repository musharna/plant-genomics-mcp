"""Pydantic output models for MCP tool ``outputSchema`` generation.

These models exist to publish a JSON schema that MCP clients can validate
against and that authoring tools (Smithery, registry indexers) can read.
They are NOT used to validate or transform the wire payload at runtime —
backend functions still return plain dicts so the existing tests stay
unchanged and so an unexpected upstream field never raises on the user.

To extend: add a new model + register it in ``server.TOOLS`` via
``outputSchema=Model.model_json_schema()``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EnsemblPlantsLocus(BaseModel):
    """Ensembl Plants ``/lookup/id/{locus}`` response.

    Ensembl returns a documented core set plus engine-internal fields that
    vary by assembly. We model the stable ones and let the rest through so
    a future Ensembl release that adds a field doesn't break clients.
    """

    model_config = ConfigDict(extra="allow")

    # Required: only the two fields the endpoint guarantees on success.
    # Everything else is Optional so a sparse / future Ensembl payload
    # never produces an "Output validation error" for the user.
    id: str = Field(description="Locus identifier, e.g. AT1G01010")
    species: str = Field(description="Ensembl species slug, e.g. arabidopsis_thaliana")

    object_type: str | None = Field(default=None, description='Usually "Gene"')
    biotype: str | None = Field(default=None, description="protein_coding, lncRNA, miRNA, ...")
    display_name: str | None = Field(default=None, description="Human-readable gene symbol")
    description: str | None = Field(default=None)
    seq_region_name: str | None = Field(default=None, description="Chromosome / contig name")
    start: int | None = Field(default=None)
    end: int | None = Field(default=None)
    strand: int | None = Field(default=None, description="1 forward, -1 reverse")
    assembly_name: str | None = Field(default=None, description="e.g. TAIR10")
    db_type: str | None = Field(default=None, description='Usually "core"')
    logic_name: str | None = Field(default=None, description="Source annotation pipeline")
    source: str | None = Field(default=None)
    canonical_transcript: str | None = Field(default=None)


class GeneXrefEntry(BaseModel):
    """One row from Ensembl ``/xrefs/id`` — a single external-DB link.

    ``extra="allow"`` keeps any new Ensembl field (e.g. ``ensembl_object_type``
    on newer assemblies) from raising on output validation.
    """

    model_config = ConfigDict(extra="allow")

    dbname: str | None = Field(default=None, description="e.g. Uniprot_gn, EntrezGene")
    db_display_name: str | None = Field(default=None, description="Human-readable DB name")
    primary_id: str | None = Field(default=None, description="The identifier in the foreign DB")
    display_id: str | None = Field(default=None)
    description: str | None = Field(default=None)
    info_type: str | None = Field(default=None, description="DIRECT, DEPENDENT, etc.")
    info_text: str | None = Field(default=None)
    version: str | None = Field(default=None)
    synonyms: list[str] | None = Field(default=None)


class GeneXrefs(BaseModel):
    """Ensembl ``/xrefs/id/{locus}`` wrapper.

    The MCP outputSchema must be ``type=object`` at the root, but Ensembl
    returns a top-level array. We wrap with metadata + the raw array + a
    ``by_db`` rollup so chain consumers don't need to walk the list to
    find a specific cross-reference (e.g. UniProt accession).
    """

    model_config = ConfigDict(extra="forbid")

    locus: str
    species: str
    count: int = Field(description="Number of xref records returned")
    xrefs: list[GeneXrefEntry] = Field(description="Raw Ensembl xref records")
    by_db: dict[str, list[str]] = Field(
        description="dbname → primary_ids[]; e.g. {'Uniprot_gn': ['Q0WV96']}",
    )


class UniProtLocus(BaseModel):
    """Normalized UniProtKB record for a single locus.

    Mirrors the dict shape returned by ``uniprot.lookup_locus`` /
    ``uniprot._normalize``. We model only the fields we surface — the
    full upstream record is not echoed, since clients can re-fetch
    ``https://rest.uniprot.org/uniprotkb/{accession}.json`` if needed.
    """

    model_config = ConfigDict(extra="forbid")

    locus_query: str = Field(description="The locus identifier the user asked about")
    primaryAccession: str = Field(description="UniProt accession, e.g. Q0WV96")
    uniProtkbId: str = Field(description="UniProtKB ID, e.g. NAC1_ARATH")
    entryType: str = Field(description="e.g. 'UniProtKB reviewed (Swiss-Prot)' or '... (TrEMBL)'")
    reviewed: bool = Field(description="True if Swiss-Prot (curated)")
    recommendedName: str | None = Field(default=None, description="Recommended protein name")
    geneNames: list[str] = Field(default_factory=list, description="Gene symbols, e.g. ['NAC001']")
    organism: str | None = Field(default=None, description="Scientific name")
    taxonId: int | None = Field(default=None, description="NCBI taxonomy ID")
    sequenceLength: int | None = Field(default=None, description="Protein length in residues")
    web_url: str | None = Field(default=None, description="Browser URL for the UniProt entry")


class PhytozomeLocus(BaseModel):
    """Phytozome BioMart gene row.

    BioMart's TSV is untyped — numeric fields land as strings here and we
    preserve the wire representation rather than guess casts.
    """

    model_config = ConfigDict(extra="forbid")

    organism_name: str
    gene_name: str
    chromosome: str
    gene_start: str = Field(description="String — BioMart TSV is untyped")
    gene_end: str = Field(description="String — BioMart TSV is untyped")
    strand: str = Field(description='String — typically "1" or "-1"')
    description: str


class LiteratureHit(BaseModel):
    """One Europe PMC ``/search`` result row, projected to a fixed field set.

    Europe PMC's ``resultType=core`` rows carry ~50 fields; we surface the
    subset useful for LLM clients (identifiers, citation, OA status, abstract).
    ``extra="allow"`` keeps any new upstream field from raising on validation.
    """

    model_config = ConfigDict(extra="allow")

    id: str | None = Field(default=None, description="Europe PMC record ID")
    source: str | None = Field(default=None, description="MED, PMC, PPR, AGR, ...")
    pmid: str | None = Field(default=None)
    pmcid: str | None = Field(default=None)
    doi: str | None = Field(default=None)
    title: str | None = Field(default=None)
    authorString: str | None = Field(default=None, description="Comma-separated author list")
    journalTitle: str | None = Field(default=None)
    pubYear: str | None = Field(default=None, description="String — wire format is untyped")
    firstPublicationDate: str | None = Field(default=None, description="ISO date")
    citedByCount: int | None = Field(default=None)
    isOpenAccess: str | None = Field(default=None, description='"Y" or "N"')
    hasPDF: str | None = Field(default=None, description='"Y" or "N"')
    abstractText: str | None = Field(default=None)
    web_url: str | None = Field(default=None, description="europepmc.org article URL")


class LocusLiterature(BaseModel):
    """Europe PMC ``/search`` wrapper for a locus query."""

    model_config = ConfigDict(extra="forbid")

    locus: str
    species: str
    query: str = Field(description="Final query string sent to Europe PMC")
    hitCount: int = Field(description="Total hits available upstream (may exceed returned)")
    returned: int = Field(description="Number of hits actually in hits[]")
    hits: list[LiteratureHit]


class GoAnnotation(BaseModel):
    """One QuickGO ``/annotation/search`` row, projected to a fixed field set.

    ``extra="allow"`` keeps any new QuickGO field from raising on validation
    (the upstream record has ~18 fields; we drop verbose ones).
    """

    model_config = ConfigDict(extra="allow")

    geneProductId: str | None = Field(default=None, description="e.g. UniProtKB:Q0WV96")
    symbol: str | None = Field(default=None, description="Gene symbol, e.g. NAC001")
    qualifier: str | None = Field(default=None, description="enables, involved_in, located_in, ...")
    goId: str | None = Field(default=None, description="GO term ID, e.g. GO:0000976")
    goName: str | None = Field(default=None, description="Human-readable GO term name")
    goAspect: str | None = Field(
        default=None,
        description="molecular_function | biological_process | cellular_component",
    )
    goEvidence: str | None = Field(default=None, description="3-letter code, e.g. IPI, IDA")
    evidenceCode: str | None = Field(default=None, description="ECO ontology ID")
    reference: str | None = Field(default=None, description="e.g. PMID:30356219")
    assignedBy: str | None = Field(default=None, description="Source DB, e.g. TAIR, UniProt")
    taxonId: int | None = Field(default=None, description="NCBI taxonomy ID")
    taxonName: str | None = Field(default=None, description="Scientific name")
    date: str | None = Field(default=None, description="YYYYMMDD string")
    withFrom: list[dict[str, Any]] | None = Field(
        default=None,
        description="Connected cross-refs from QuickGO",
    )


class LocusGoAnnotations(BaseModel):
    """QuickGO annotation search wrapper for a plant locus.

    The locus is first resolved to a UniProt accession via the same logic
    as ``resolve_locus_to_uniprot``, then handed to QuickGO. ``by_aspect``
    groups annotations by GO aspect with goId-level dedup so a chain
    consumer can see the high-level term set without the per-evidence
    repetition.
    """

    model_config = ConfigDict(extra="forbid")

    locus: str
    uniprot_accession: str = Field(description="UniProt accession used to query QuickGO")
    numberOfHits: int = Field(description="Total annotations available upstream")
    returned: int = Field(description="Number of annotations in annotations[]")
    annotations: list[GoAnnotation]
    by_aspect: dict[str, list[dict[str, str]]] = Field(
        description="aspect → [{goId, goName}, ...], deduped on goId",
    )


class BlastHit(BaseModel):
    """One row from the BLAST text-report "significant alignments" table.

    The Text-format report gives us accession + description + bit score +
    e-value + identity per hit; richer per-alignment data lives in the
    trailing ALIGNMENTS section and is preserved in ``raw_report_excerpt``.
    """

    model_config = ConfigDict(extra="forbid")

    accession: str = Field(description="NCBI accession, e.g. Q9FLJ2.1 / NP_001185207.1")
    description: str = Field(description="Subject description from the BLAST report")
    bit_score: float | str = Field(
        description="Bit score — float when parseable, raw string otherwise"
    )
    evalue: float | str = Field(
        description="E-value — float when parseable, raw string otherwise (e.g. '0.0')"
    )
    identity: str | None = Field(
        default=None,
        description=(
            'Percent identity from the summary table (e.g. "66%"). Kept as '
            "string because NCBI ships the literal % suffix; None on legacy "
            "reports that omit the Ident column."
        ),
    )


class BlastResult(BaseModel):
    """NCBI BLAST URLAPI search result wrapper.

    Async-submit + poll + fetch is hidden behind a single tool call. The
    server emits ``notifications/progress`` during polling. ``rid`` is
    returned so the client can re-poll independently if needed.
    ``raw_report_excerpt`` is capped at 50 KB to keep the wire payload
    bounded; ``raw_report_truncated`` flags whether more remains upstream.
    """

    model_config = ConfigDict(extra="forbid")

    rid: str = Field(description="NCBI BLAST request ID — re-usable via fetch_result()")
    program: str = Field(description="blastn | blastp | blastx | tblastn | tblastx")
    database: str = Field(description="NCBI BLAST database, e.g. swissprot, core_nt")
    status: str = Field(description='Always "READY" when this object is returned')
    hitCount: int = Field(description="Number of rows parsed from the alignment summary")
    hits: list[BlastHit] = Field(description="Top alignments, sorted by BLAST default order")
    raw_report_excerpt: str = Field(description="First 50 KB of the FORMAT_TYPE=Text report")
    raw_report_truncated: bool = Field(description="True if the upstream report exceeded the cap")
    elapsed_seconds: float = Field(description="Wall-clock from submit to READY")


class BatchEnvelope(BaseModel):
    """Shared response shape for every ``batch_*`` tool.

    All batch tools fan out per-locus calls and produce the same envelope:
    successes land in ``results`` keyed on the input locus; PlantGenomicsError
    failures land in ``errors`` with the ``[ClassName] message`` prefix the
    single-locus tools already use. ``count`` is the input cardinality
    (``len(loci)``), not ``len(results) + len(errors)`` — those two add up
    to ``count`` by construction.
    """

    model_config = ConfigDict(extra="forbid")

    tool: str = Field(description="The batch tool name, e.g. batch_resolve_locus_to_uniprot")
    count: int = Field(description="Number of loci in the input list")
    results: dict[str, dict[str, Any]] = Field(
        description="locus → per-locus result dict (same shape as the single-locus tool)",
    )
    errors: dict[str, str] = Field(
        description="locus → '[ClassName] message' for PlantGenomicsError failures",
    )


class SubscriptionGatedRedirect(BaseModel):
    """Shared shape for the TAIR and PlantCyc informational stubs.

    Neither tool calls upstream by default — both return a structured
    redirect to free alternatives. The shape is intentionally identical
    so a single client-side handler can render either tool's response.

    When the corresponding ``*_TOKEN`` env var is set (P2.20 config
    slot), ``status`` flips from ``subscription_required`` to
    ``configured_live_not_implemented``, ``auth_configured`` becomes
    True, and ``note_for_subscribers`` is populated with a pointer to
    the deferred live-wiring entry point.
    """

    model_config = ConfigDict(extra="forbid")

    locus: str
    status: str = Field(
        description=(
            'Either "subscription_required" (no token) or '
            '"configured_live_not_implemented" (token present, live HTTP '
            "wiring deferred to a subscriber PR)."
        ),
    )
    probed_at: str = Field(description="ISO date of the last live access probe (YYYY-MM-DD)")
    auth_configured: bool = Field(
        description="True when the subscription-token env var is set; False otherwise.",
    )
    rationale: str = Field(description="Why this backend is gated or deferred")
    note_for_subscribers: str | None = Field(
        default=None,
        description=(
            "Subscriber-PR pointer to the deferred live-wiring entry "
            "point. Only populated when auth_configured is True."
        ),
    )
    alternatives: list[str] = Field(description="Tool names users should call instead")
    alternatives_note: str = Field(description="What the alternatives do and do NOT cover")


class TairLocusInfo(SubscriptionGatedRedirect):
    """TAIR stub response — adds ``tair_web_url`` to the shared shape."""

    tair_web_url: str = Field(description="Browser URL for the TAIR locus page")


class PlantCycLocusInfo(SubscriptionGatedRedirect):
    """PlantCyc stub response — adds ``plantcyc_web_url`` to the shared shape."""

    plantcyc_web_url: str = Field(description="Browser URL for the PlantCyc gene page")


class GrameneHomolog(BaseModel):
    """One ortholog/paralog entry from Gramene compara.

    Gramene's ``fl=homology`` projection groups homologs by category and
    only emits target loci (as strings), plus a single gene_tree_id at the
    record level. Per-row taxon, identity, protein ID, dn/ds, and
    goc_score are NOT exposed by this endpoint, so we deliberately omit
    those fields rather than carry always-None placeholders.
    """

    model_config = ConfigDict(extra="allow")

    target_locus: str | None = Field(default=None)
    type: str | None = Field(
        default=None,
        description=(
            "Homology category: ortholog_one2one | ortholog_one2many | "
            "ortholog_many2many | within_species_paralog | between_species_paralog"
        ),
    )
    gene_tree_id: str | None = Field(
        default=None,
        description="Gramene gene-tree ID (shared across all homologs in the record)",
    )


class GrameneHomologs(BaseModel):
    """Gramene compara homology response wrapper."""

    model_config = ConfigDict(extra="forbid")

    locus: str
    release: str = Field(description="Gramene release identifier, e.g. v69")
    total: int = Field(description="Number of homologs after filtering")
    homologs: list[GrameneHomolog]
