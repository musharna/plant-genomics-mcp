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

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    organism: str = Field(description="Plant organism canonical slug, e.g. arabidopsis_thaliana")

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
    organism: str
    count: int = Field(description="Number of xref records returned")
    xrefs: list[GeneXrefEntry] = Field(description="Raw Ensembl xref records")
    by_db: dict[str, list[str]] = Field(
        description="dbname → primary_ids[]; e.g. {'Uniprot_gn': ['Q0WV96']}",
    )


class EnsemblSequence(BaseModel):
    """Ensembl ``/sequence/id/{locus}`` response — a fetched sequence.

    The fetch half of the lookup → fetch → BLAST loop: ``sequence`` feeds
    straight into ``blast_sequence``.
    """

    model_config = ConfigDict(extra="forbid")

    locus: str
    organism: str = Field(description="Resolved canonical organism slug")
    type: Literal["genomic", "cds", "cdna", "protein"] = Field(
        description="Sequence type requested"
    )
    length: int = Field(description="Sequence length (residues for protein, bases otherwise)")
    sequence: str = Field(description="The sequence string; feed to blast_sequence")
    molecule: str | None = Field(default=None, description='"dna" or "protein"')
    ensembl_id: str | None = Field(default=None, description="Resolved Ensembl stable id")
    description: str | None = Field(default=None)
    version: int | None = Field(default=None, description="Ensembl sequence version")


class RegionFeature(BaseModel):
    """One feature from Ensembl ``/overlap/region``.

    ``extra="allow"`` keeps assembly-specific fields (e.g. ``gene_id``,
    ``canonical_transcript``) from raising on output validation.
    """

    model_config = ConfigDict(extra="allow")

    id: str | None = Field(default=None, description="Feature stable id, e.g. AT1G01020")
    feature_type: str | None = Field(default=None, description="gene, transcript, cds, exon")
    biotype: str | None = Field(default=None)
    external_name: str | None = Field(default=None, description="Gene symbol, e.g. ARV1")
    description: str | None = Field(default=None)
    seq_region_name: str | None = Field(default=None)
    start: int | None = Field(default=None)
    end: int | None = Field(default=None)
    strand: int | None = Field(default=None, description="1 forward, -1 reverse")
    source: str | None = Field(default=None)
    assembly_name: str | None = Field(default=None)


class EnsemblRegionFeatures(BaseModel):
    """Ensembl ``/overlap/region/{species}/{region}`` wrapper.

    Ensembl returns a top-level array; we wrap it with query metadata + a
    count so the MCP outputSchema stays ``type=object``.
    """

    model_config = ConfigDict(extra="forbid")

    organism: str
    region: str = Field(description="seq_region:start-end, e.g. 1:3000-10000")
    feature: str = Field(description="Feature type queried")
    count: int = Field(description="Number of overlapping features returned")
    features: list[RegionFeature] = Field(description="Raw Ensembl overlap records")


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
    organism: str
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


class PlantOntologyAnnotation(BaseModel):
    """One Planteome GOlr annotation row, projected to a fixed field set.

    ``extra="allow"`` keeps any additional GOlr field from raising on
    validation (the upstream doc carries ~50 fields; we keep the core).
    """

    model_config = ConfigDict(extra="allow")

    term_id: str | None = Field(default=None, description="Annotation class id, e.g. PO:0009005")
    term_name: str | None = Field(default=None, description="Human-readable term name")
    ontology: str | None = Field(default=None, description="Namespace: PO | TO | PECO | GO")
    aspect: str | None = Field(default=None, description="Ontology aspect code, e.g. A / G")
    evidence: str | None = Field(default=None, description="Evidence code, e.g. IEP, IDA")
    taxon: str | None = Field(default=None, description="e.g. NCBITaxon:3702")
    taxon_label: str | None = Field(default=None, description="Scientific name")
    reference: list[str] | str | None = Field(default=None, description="Supporting reference(s)")
    assigned_by: str | None = Field(default=None, description="Curating source, e.g. TAIR, Gramene")
    bioentity_label: str | None = Field(default=None, description="Gene symbol / label")


class LocusPlantOntology(BaseModel):
    """Planteome PO/TO annotation wrapper for a plant locus.

    The locus is matched across Planteome's searchable bioentity fields and
    filtered to the organism's NCBI taxon. ``by_ontology`` groups annotations
    by namespace (PO / TO / PECO / GO) with term_id-level dedup so a client
    can see the term set per ontology without per-evidence repetition. GO
    terms may appear here too, but ``locus_go_annotations`` is the dedicated
    GO tool; this one's value is the plant-specific PO / TO / PECO namespaces.
    """

    model_config = ConfigDict(extra="forbid")

    locus: str
    organism: str = Field(description="Canonical organism slug")
    taxon: str = Field(description="NCBI taxon filter applied, e.g. NCBITaxon:3702")
    numberOfHits: int = Field(description="Total annotations available upstream")
    returned: int = Field(description="Number of annotations in annotations[]")
    annotations: list[PlantOntologyAnnotation]
    by_ontology: dict[str, list[dict[str, str]]] = Field(
        description="namespace → [{term_id, term_name}, ...], deduped on term_id",
    )


class GoEnrichmentTerm(BaseModel):
    """One enriched term from g:Profiler g:GOSt over a gene set.

    ``extra="allow"`` keeps any additional g:Profiler statistic (e.g.
    ``goshv``, ``effective_domain_size``) from raising on output validation.
    """

    model_config = ConfigDict(extra="allow")

    source: str | None = Field(default=None, description="GO:BP | GO:MF | GO:CC | KEGG")
    term_id: str | None = Field(default=None, description="Term accession, e.g. GO:0007623")
    name: str | None = Field(default=None, description="Human-readable term name")
    description: str | None = Field(default=None)
    p_value: float | None = Field(default=None, description="g:SCS-corrected significance")
    significant: bool | None = Field(default=None)
    term_size: int | None = Field(default=None, description="Genes annotated to the term")
    query_size: int | None = Field(default=None, description="Mapped genes in the query domain")
    intersection_size: int | None = Field(
        default=None, description="Query genes annotated to the term"
    )
    precision: float | None = Field(default=None)
    recall: float | None = Field(default=None)


class GoEnrichmentResult(BaseModel):
    """g:Profiler g:GOSt over-representation wrapper for a gene list.

    ``enriched`` is capped at ``top_n`` (sorted by p-value); ``total_terms``
    is the pre-cap count. ``unmapped`` lists query loci g:Profiler could not
    recognize — surfaced rather than silently dropped so a locus-namespace
    mismatch is visible.
    """

    model_config = ConfigDict(extra="forbid")

    organism: str = Field(description="Canonical organism slug")
    gprofiler_id: str = Field(description="g:Profiler organism ID used, e.g. athaliana")
    sources: list[str] = Field(description="Annotation sources queried")
    query_size: int = Field(description="Number of loci submitted")
    mapped: int = Field(description="Loci g:Profiler recognized")
    unmapped: list[str] = Field(description="Loci g:Profiler could not map")
    total_terms: int = Field(description="Significant terms before the top_n cap")
    returned: int = Field(description="Terms in enriched[] after the top_n cap")
    enriched: list[GoEnrichmentTerm]


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


class PlantCycReaction(BaseModel):
    """One reaction catalyzed by a locus's gene product (PlantCyc/PMN)."""

    model_config = ConfigDict(extra="allow")

    id: str | None = Field(default=None, description="Reaction frame id, e.g. RXN-7775")
    name: str | None = Field(default=None, description="Reaction common name, if the frame has one")


class PlantCycPathway(BaseModel):
    """One PlantCyc/PMN pathway the locus participates in."""

    model_config = ConfigDict(extra="allow")

    id: str | None = Field(default=None, description="Pathway frame id, e.g. PWY-6787")
    name: str | None = Field(
        default=None, description="Pathway common name, e.g. flavonoid biosynthesis"
    )


class PlantCycLocusInfo(BaseModel):
    """PlantCyc / PMN metabolic annotation for a locus.

    Walks gene → enzyme → reactions → pathways in the organism's PGDB via the
    free BioCyc web-services API. ``found=False`` with empty lists when the
    locus has no metabolic annotation (e.g. a non-enzymatic gene like a
    transcription factor) — this is a normal result, not an error.
    ``reaction_count`` / ``pathway_count`` are the true totals even when the
    returned lists are capped (see ``plantcyc.MAX_REACTIONS`` / ``MAX_PATHWAYS``).
    """

    model_config = ConfigDict(extra="forbid")

    locus: str
    organism: str = Field(description="Canonical organism slug")
    orgid: str = Field(description="PlantCyc PGDB org id, e.g. ARA (AraCyc)")
    found: bool = Field(description="True if the locus resolved to a metabolic gene")
    gene_frame: str | None = Field(default=None, description="Resolved PGDB gene frame id")
    gene_common_name: str | None = Field(default=None, description="Gene common name in the PGDB")
    enzymes: list[str] = Field(description="Product monomer (enzyme) frame ids")
    reactions: list[PlantCycReaction]
    pathways: list[PlantCycPathway]
    reaction_count: int = Field(description="Total distinct reactions (pre-cap)")
    pathway_count: int = Field(description="Total distinct pathways (pre-cap)")


class AlphaFoldStructure(BaseModel):
    """AlphaFold DB predicted-structure summary for a locus (via UniProt).

    The locus is resolved to a UniProt accession, then AlphaFold's prediction
    API is queried. ``found=False`` (with null fields) means the accession has
    no deposited model — a normal outcome, not an error. The full model
    coordinates are not inlined; ``cif_url`` / ``pdb_url`` link to them.
    """

    model_config = ConfigDict(extra="forbid")

    locus: str
    accession: str = Field(description="Resolved UniProt accession")
    found: bool = Field(description="True if a predicted model exists")
    model_entity_id: str | None = Field(default=None, description="e.g. AF-Q9SZ92-F1")
    mean_plddt: float | None = Field(
        default=None, description="Global mean pLDDT confidence (0–100)"
    )
    plddt_bands: dict[str, float | None] | None = Field(
        default=None, description="Fraction of residues per confidence band"
    )
    latest_version: int | None = Field(default=None, description="Latest AlphaFold model version")
    model_created: str | None = Field(default=None, description="Model creation date (ISO 8601)")
    residue_range: dict[str, int | None] | None = Field(
        default=None, description="Modelled residue span {start, end}"
    )
    organism: str | None = Field(default=None, description="Organism scientific name")
    gene: str | None = Field(default=None, description="Gene name from UniProt")
    description: str | None = Field(default=None, description="UniProt protein description")
    cif_url: str | None = Field(default=None, description="mmCIF model download URL")
    pdb_url: str | None = Field(default=None, description="PDB model download URL")
    pae_image_url: str | None = Field(default=None, description="Predicted-aligned-error image URL")


class InterProDomain(BaseModel):
    """One InterPro entry (domain / family / signature) on a protein."""

    model_config = ConfigDict(extra="allow")

    accession: str | None = Field(default=None, description="Member/InterPro accession")
    name: str | None = Field(default=None, description="Entry name")
    type: str | None = Field(
        default=None, description="domain / family / homologous_superfamily / …"
    )
    source_database: str | None = Field(default=None, description="pfam / cdd / panther / …")
    interpro: str | None = Field(
        default=None, description="Integrated InterPro accession, if any (else null)"
    )
    go_terms: list[dict[str, Any]] | None = Field(
        default=None, description="Associated GO terms, if the entry carries any"
    )
    locations: list[dict[str, int]] = Field(
        default_factory=list, description="Residue spans [{start, end}]"
    )


class InterProDomains(BaseModel):
    """InterPro domain / family architecture for a locus (via UniProt).

    The locus is resolved to a UniProt accession, then InterPro's per-protein
    entries are fetched (Pfam appears as ``source_database == "pfam"`` among the
    rows). ``found=True`` with an empty ``domains`` list means the protein has
    no annotated entries — distinct from an unresolvable locus, which errors.
    ``domain_count`` is the true total even when the row list is page-capped.
    """

    model_config = ConfigDict(extra="forbid")

    locus: str
    accession: str = Field(description="Resolved UniProt accession")
    found: bool = Field(description="True once the locus resolved to a UniProt entry")
    domain_count: int = Field(description="Total InterPro entries (pre-cap)")
    domains: list[InterProDomain]
    count_by_type: dict[str, int] = Field(description="Rollup of entry count by type")


class LocusVariants(BaseModel):
    """Natural variants overlapping a locus's genomic span (Ensembl /overlap).

    The locus is resolved to its coordinates, then EVA/dbSNP-sourced variants
    overlapping the gene are listed. ``variant_count`` is the true overlap total;
    ``variants`` is capped for payload size, with ``truncated`` set when capped.
    """

    model_config = ConfigDict(extra="forbid")

    locus: str
    organism: str = Field(description="Resolved Ensembl species slug")
    region: str = Field(description="Queried gene span, e.g. '1:33666-37840'")
    gene_start: int | None = Field(default=None, description="Gene span start (1-based)")
    gene_end: int | None = Field(default=None, description="Gene span end (1-based)")
    variant_count: int = Field(description="Total overlapping variants (pre-cap)")
    truncated: bool = Field(description="True if the variant list was capped")
    variants: list[dict[str, Any]] = Field(
        default_factory=list, description="Per-variant {id, source, consequence_type, alleles, …}"
    )


class VepAnnotation(BaseModel):
    """Ensembl VEP consequence prediction for a variant (region + allele).

    Variant-first, not locus-first: the caller supplies the region and allele.
    ``found=False`` when Ensembl reports no overlapping feature.
    """

    model_config = ConfigDict(extra="forbid")

    organism: str = Field(description="Resolved Ensembl species slug")
    region: str = Field(description="Ensembl region, e.g. '1:10000-10000:1'")
    allele: str = Field(description="Alternate allele, e.g. 'C'")
    found: bool = Field(description="True if VEP returned an overlapping feature")
    input: str | None = Field(default=None, description="VEP echo of the parsed input")
    most_severe_consequence: str | None = Field(default=None, description="Most severe SO term")
    assembly_name: str | None = Field(default=None, description="Assembly the call is against")
    seq_region_name: str | None = Field(default=None)
    start: int | None = Field(default=None)
    end: int | None = Field(default=None)
    allele_string: str | None = Field(default=None)
    transcript_consequences: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Per-transcript {gene_id, transcript_id, consequence_terms, impact, sift_*, …}",
    )


class PantherFamily(BaseModel):
    """PANTHER protein-family classification for a locus.

    ``found=False`` (with null/empty fields) means PANTHER could not classify the
    locus into a family — a normal outcome, not an error.
    """

    model_config = ConfigDict(extra="forbid")

    locus: str
    found: bool = Field(description="True if PANTHER classified the locus")
    accession: str | None = Field(default=None, description="PANTHER mapped accession")
    family_id: str | None = Field(default=None, description="PANTHER family id, e.g. PTHR12802")
    family_name: str | None = Field(default=None)
    subfamily_id: str | None = Field(default=None, description="e.g. PTHR12802:SF176")
    subfamily_name: str | None = Field(default=None)
    go_molecular_function: list[dict[str, Any]] = Field(default_factory=list)
    go_biological_process: list[dict[str, Any]] = Field(default_factory=list)
    go_cellular_component: list[dict[str, Any]] = Field(default_factory=list)
    protein_class: list[dict[str, Any]] = Field(default_factory=list)
    pathways: list[dict[str, Any]] = Field(default_factory=list)


class OrthoDbOrthologs(BaseModel):
    """OrthoDB ortholog group + cross-species member genes for a locus.

    ``found=False`` means the locus maps to no Viridiplantae ortholog group.
    ``organism_count`` is the true cluster total even when members are capped.
    """

    model_config = ConfigDict(extra="forbid")

    locus: str
    organism: str = Field(description="Resolved canonical organism")
    found: bool = Field(description="True if the locus maps to an ortholog group")
    group: dict[str, Any] | None = Field(
        default=None, description="Group metadata {id, name, evolutionary_rate, level_name, …}"
    )
    organism_count: int = Field(description="Number of member organisms (clusters)")
    member_count: int = Field(description="Member genes returned (post-cap)")
    truncated: bool = Field(description="True if the member list was capped")
    members: list[dict[str, Any]] = Field(
        default_factory=list, description="Per-gene {organism, gene_id, xref, description}"
    )


class AraGwasAssociations(BaseModel):
    """AraGWAS GWAS associations for an Arabidopsis locus.

    Arabidopsis-only. ``association_count`` is the true total even when the row
    list is page-capped.
    """

    model_config = ConfigDict(extra="forbid")

    locus: str
    organism: str = Field(description="Always arabidopsis_thaliana")
    found: bool = Field(description="True once the associations endpoint returned 200")
    association_count: int = Field(description="Total associations (pre-cap)")
    returned: int = Field(description="Associations returned (post page-cap)")
    truncated: bool = Field(description="True if pagination was capped")
    associations: list[dict[str, Any]] = Field(
        default_factory=list, description="Per-hit {score, maf, mac, snp{…}, study{…}}"
    )


class ArabidopsisNaturalVariation(BaseModel):
    """1001 Genomes natural-variation SNP effects for an Arabidopsis locus.

    Arabidopsis-only. ``variant_count`` is the true row total even when capped.
    """

    model_config = ConfigDict(extra="forbid")

    locus: str
    organism: str = Field(description="Always arabidopsis_thaliana")
    found: bool = Field(description="True once the effects endpoint returned 200")
    transcript: str = Field(description="Transcript-scoped gene id used (e.g. AT1G01060.1)")
    region: str | None = Field(default=None, description="Genomic span, e.g. 'Chr1:33666..37840'")
    variant_count: int = Field(description="Total effect rows (pre-cap)")
    returned: int = Field(description="Effect rows returned (post-cap)")
    truncated: bool = Field(description="True if the effect list was capped")
    variants: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Per-effect {chr, position, accession_id, effect, impact, amino_acid_change, …}",
    )


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


class KeggPathway(BaseModel):
    """One KEGG pathway entry."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="KEGG pathway ID, e.g. ath04075")
    name: str = Field(description="Pathway name from KEGG NAME line")
    pathway_class: str = Field(description="Hierarchical category from KEGG CLASS line")


class KeggPathways(BaseModel):
    """KEGG pathway-membership response wrapper."""

    model_config = ConfigDict(extra="forbid")

    locus: str
    organism: str = Field(description="Resolved canonical organism slug, e.g. arabidopsis_thaliana")
    kegg_gene_id: str = Field(description='e.g. "ath:at1g01010"')
    entrez_gene_id: str | None = Field(
        default=None,
        description="Entrez Gene ID from the non-Arabidopsis KEGG↔Entrez bridge; absent for ath.",
    )
    pathways: list[KeggPathway]
    errors: list[str] = Field(
        default_factory=list,
        description="Per-pathway step-2 failures (kept inline so the call doesn't abort)",
    )


class BarGeneSummary(BaseModel):
    """BAR ThaleMine + GAIA-aliases response for an Arabidopsis locus.

    Merges ``/thalemine/gene_information/{locus}`` (TAIR curator summary +
    Araport11 computational description, positional row under an
    InterMine envelope) with ``/gaia/aliases/{locus}`` (NCBI Gene ID +
    cross-DB synonyms: RefSeq, UniProt, locus-model IDs). Arabidopsis
    only — ThaleMine carries only taxon 3702.

    Replaces the v0.9 ``tair_locus_info`` subscription-gated stub for
    Arabidopsis Curator Summary lookup (BAR is keyless and a Global Core
    Biodata Resource as of 2023).
    """

    model_config = ConfigDict(extra="forbid")

    locus: str
    agi: str | None = Field(
        default=None,
        description='AGI primary identifier echoed by ThaleMine, e.g. "AT1G01010"',
    )
    symbol: str | None = Field(default=None, description='Gene symbol, e.g. "NAC001"')
    full_name: str | None = Field(default=None, description="Gene name from ThaleMine")
    tair_locus_id: str | None = Field(
        default=None,
        description='TAIR locus ID from Gene.secondaryIdentifier, e.g. "locus:2200935"',
    )
    synonyms: list[str] = Field(
        default_factory=list,
        description="TAIR aliases (CSV from Gene.tairAliases, split on commas + stripped)",
    )
    computational_description: str | None = Field(
        default=None,
        description="Gene.tairComputationalDescription — Araport11-sourced computed description",
    )
    curator_summary: str | None = Field(
        default=None,
        description="Gene.tairCuratorSummary — the TAIR-curated functional summary prose",
    )
    brief_description: str | None = Field(
        default=None,
        description="Gene.briefDescription — short blurb (often same as full_name)",
    )
    tair_short_description: str | None = Field(
        default=None,
        description="Gene.tairShortDescription — TAIR-specific short description",
    )
    ncbi_gene_id: str | None = Field(
        default=None,
        description="NCBI Gene ID from /gaia/aliases/ — None if BAR has no NCBI cross-ref",
    )
    aliases: list[str] = Field(
        default_factory=list,
        description=(
            "Cross-DB aliases from /gaia/aliases/ (RefSeq accessions, UniProt accessions, "
            "TIGR locus-model IDs, and TAIR aliases). Empty list if /gaia degraded."
        ),
    )
    species: Literal["arabidopsis_thaliana"]
    source_url: str = Field(description="ThaleMine endpoint URL for traceability")


class BarEfpEcotype(BaseModel):
    """One ecotype row from BAR's world-eFP natural-variation expression view."""

    model_config = ConfigDict(extra="allow")

    code: str = Field(description='Ecotype numeric code, e.g. "111"')
    name: str = Field(
        description=(
            'Ecotype name + provenance, e.g. "Bay-0 (CS6608) from Bayreuth, Germany". '
            "Stripped at first <br> so the leading label is uncluttered; climate "
            "fragments live in the upstream source URL."
        ),
    )
    samples: list[str] = Field(
        default_factory=list,
        description='Replicate sample IDs, e.g. ["ATGE_111_A", "ATGE_111_B"]',
    )
    ctrl_samples: list[str] = Field(
        default_factory=list,
        description="Control sample IDs used by BAR to compute relative expression",
    )
    values: dict[str, float] = Field(
        default_factory=dict,
        description="Per-replicate expression values; keys match `samples`",
    )
    mean: float | None = Field(
        default=None,
        description="Mean of `values`; None if no replicates",
    )
    position: dict[str, str] | None = Field(
        default=None,
        description='Collection lat/lng, e.g. {"lat": "49.95", "lng": "11.57"}',
    )
    source: str | None = Field(
        default=None,
        description="Upstream TairObject URL for the bio_sample_collection record",
    )


class BarEfpExpression(BaseModel):
    """BAR world-eFP natural-variation expression response wrapper.

    Wraps ``/microarray_gene_expression/world_efp/arabidopsis/{locus}``. The
    world-eFP view returns expression across ~36 Arabidopsis ecotypes (Bay-0,
    Col-0, Cvi-1, Ler-2, ...) with per-replicate values, control samples for
    each ecotype, and collection coordinates. Arabidopsis only.
    """

    model_config = ConfigDict(extra="forbid")

    locus: str
    probeset: str | None = Field(
        default=None,
        description="Microarray probeset ID, uniform across ecotypes for one gene",
    )
    ecotype_count: int = Field(description="Number of ecotype rows in `ecotypes`")
    ecotypes: list[BarEfpEcotype]
    species: Literal["arabidopsis_thaliana"]
    source_url: str = Field(description="BAR world-eFP endpoint URL for traceability")


class BarAIVPaper(BaseModel):
    """One curated GRN paper row from BAR AIV Arabidopsis lane.

    From ``/interactions/get_paper_by_agi/{locus}``: each entry is a yeast
    one-hybrid / ChIP / curated TF-target study referencing the queried AGI
    as a node. `tags` is pipe-split into a list of ``"name:type"`` items
    (e.g. ``"PLT3:Gene"``, ``"auxin:Misc"``, ``"wound:Condition"``).
    """

    model_config = ConfigDict(extra="allow")

    source_id: int | None = Field(default=None, description="BAR internal study ID")
    pmid: str | None = Field(default=None, description='PubMed ID, e.g. "29462363"')
    title: str | None = Field(default=None, description="Study title with citation")
    image_url: str | None = Field(
        default=None,
        description="BAR-hosted thumbnail of the GRN network diagram",
    )
    comments: str | None = Field(default=None, description="BAR curator commentary on the study")
    cyjs_layout: str | None = Field(
        default=None,
        description="Cytoscape.js layout config (JSON string) for the GRN view",
    )
    tags: list[str] = Field(
        default_factory=list,
        description='Pipe-split "name:type" tags, e.g. ["PLT3:Gene", "auxin:Misc"]',
    )


class BarAIVPartner(BaseModel):
    """One predicted PPI partner row from BAR AIV Rice lane.

    From ``/interactions/rice/{locus}``: each entry is a predicted
    protein-protein interaction where ``protein_1`` is the queried locus
    and ``protein_2`` is the partner. ``partner_locus`` is derived as the
    non-queried side for ergonomic access.
    """

    model_config = ConfigDict(extra="allow")

    partner_locus: str | None = Field(
        default=None,
        description="MSU LOC_Os* locus of the predicted interactor",
    )
    protein_1: str | None = Field(default=None, description="First protein in the pair")
    protein_2: str | None = Field(default=None, description="Second protein in the pair")
    pcc: float | None = Field(
        default=None,
        description="Pearson correlation of co-expression evidence (range -1 to 1)",
    )
    total_hits: int | None = Field(default=None, description="Count of supporting evidence hits")
    num_species: int | None = Field(
        default=None,
        description="Species count supporting the prediction (BAR upstream field: Num_species)",
    )
    quality: int | None = Field(
        default=None,
        description="BAR-internal evidence quality score (upstream: Quality)",
    )


class BarAIVInteractions(BaseModel):
    """BAR AIV response wrapper — interactions for Arabidopsis or rice.

    The two BAR AIV lanes return completely different shapes, so this
    envelope uses ``kind`` as a discriminator:

      ``kind="grn_papers"``      → ``papers`` list populated, ``partners`` empty
                                   (Arabidopsis, curated GRN references)
      ``kind="ppi_predictions"`` → ``partners`` list populated, ``papers`` empty
                                   (Rice, predicted PPI pairs with PCC)

    Other organisms in the registry have no AIV lane — the underlying
    backend raises ``OrganismNotSupported`` before this model is built.
    """

    model_config = ConfigDict(extra="forbid")

    locus: str
    organism: Literal["arabidopsis_thaliana", "oryza_sativa"]
    kind: Literal["grn_papers", "ppi_predictions"] = Field(
        description="Discriminator: grn_papers (Arabidopsis) or ppi_predictions (rice)",
    )
    count: int = Field(description="Total rows returned (len of papers or partners)")
    papers: list[BarAIVPaper] = Field(
        default_factory=list,
        description="GRN paper refs (populated when kind=grn_papers)",
    )
    partners: list[BarAIVPartner] = Field(
        default_factory=list,
        description="PPI predictions (populated when kind=ppi_predictions)",
    )
    source_url: str = Field(description="BAR AIV endpoint URL for traceability")


class StringPartner(BaseModel):
    """One STRING interaction-partner row."""

    model_config = ConfigDict(extra="allow")

    string_id: str | None = Field(default=None, description='e.g. "3702.AT3G15500.1"')
    accession: str | None = Field(
        default=None,
        description="Partner's stringId; UniProt resolution is the caller's job",
    )
    preferred_name: str | None = Field(default=None, description="Human-readable gene symbol")
    score: float | None = Field(default=None, description="Combined STRING confidence [0,1]")
    escore: float | None = Field(default=None, description="Experimental sub-score")
    dscore: float | None = Field(default=None, description="Database sub-score")
    tscore: float | None = Field(default=None, description="Textmining sub-score")
    pscore: float | None = Field(default=None, description="Predicted (homology) sub-score")


class StringInteractions(BaseModel):
    """STRING interaction-partners response wrapper."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(description="The locus or accession the user passed")
    accession: str = Field(description="UniProt accession actually queried at STRING")
    organism: str = Field(description="Plant organism canonical slug, e.g. arabidopsis_thaliana")
    partners: list[StringPartner]


class CoexNeighbor(BaseModel):
    """One ATTED-II co-expression neighbor entry (API v5)."""

    model_config = ConfigDict(extra="allow")

    locus: str | None = Field(
        default=None,
        description="Target locus (first element of upstream 'other_id' list)",
    )
    entrez_gene_id: int | None = Field(
        default=None,
        description="NCBI Entrez gene ID (upstream 'gene' field)",
    )
    z_score: float | None = Field(
        default=None,
        description="ATTED-II z-score; higher = stronger coexpression",
    )


class AttedCoexpression(BaseModel):
    """ATTED-II coexpression response wrapper."""

    model_config = ConfigDict(extra="forbid")

    locus: str
    atted_release: str = Field(
        description="ATTED-II DB identifier, e.g. Ath-u.c4-0 (release version included)",
    )
    neighbors: list[CoexNeighbor]


class StepRow(BaseModel):
    """One backend call inside a synthesis envelope.

    ``status="ok"`` populates ``result``; ``status="error"`` populates ``error``
    with the existing ``[ExceptionClass] message`` wire format from
    ``errors.PlantGenomicsError.__str__``. ``status="skipped"`` populates
    ``error`` with a human-readable skip reason (e.g. phase 1 failed).
    """

    model_config = ConfigDict(extra="forbid")

    step: int = Field(description="1-indexed position in the orchestrator's execution order")
    tool: str = Field(description="Backend tool name, e.g. ensembl_plants_lookup_locus")
    status: Literal["ok", "error", "skipped"]
    elapsed_s: float | None = Field(
        default=None,
        description=(
            "Per-step wall time when separately measurable, else None. "
            "Phase-2 gather rows and phase-0 pre-call validation failures "
            "return None because their wall time can't be honestly attributed "
            "per-step; SynthesisEnvelope.elapsed_s carries the authoritative total."
        ),
    )
    result: dict | list | None = Field(
        default=None,
        description='Backend payload when status="ok"; None otherwise',
    )
    error: str | None = Field(
        default=None,
        description='"[ExceptionClass] message" when status="error"; skip reason when "skipped"',
    )

    @model_validator(mode="after")
    def _check_status_coherence(self) -> StepRow:
        if self.status == "ok":
            if self.result is None or self.error is not None:
                raise ValueError("status='ok' requires result is not None and error is None")
        elif self.status == "error":
            if self.error is None or self.result is not None:
                raise ValueError("status='error' requires error is not None and result is None")
        elif self.status == "skipped":  # noqa: SIM102 — parallel guard-clause branches
            if self.error is None or self.result is not None:
                raise ValueError("status='skipped' requires error is not None and result is None")
        return self


class SynthesisEnvelope(BaseModel):
    """Composed result of a synthesis orchestrator.

    ``result`` is ``None`` when the root (phase 1) step errored — the
    failure is recorded in ``steps[0]`` and phase-2 steps carry
    ``status="skipped"``.
    """

    model_config = ConfigDict(extra="forbid")

    tool: str = Field(description="Synthesis tool name, e.g. analyze_locus_synth")
    input: dict = Field(description="Echoed input arguments")
    started_at: str = Field(description="ISO 8601 UTC timestamp")
    elapsed_s: float = Field(description="Total orchestrator wall time")
    steps: list[StepRow] = Field(description="Per-backend execution rows")
    result: dict | None = Field(
        default=None,
        description="Composed cross-source result; None if root step failed",
    )
