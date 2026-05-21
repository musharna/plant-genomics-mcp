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


class SubscriptionGatedRedirect(BaseModel):
    """Shared shape for the TAIR and PlantCyc informational stubs.

    Neither tool calls upstream — both return a structured redirect to
    free alternatives. The shape is intentionally identical so a single
    client-side handler can render either tool's response.
    """

    model_config = ConfigDict(extra="forbid")

    locus: str
    status: str = Field(description='Always "subscription_required" for these stubs')
    probed_at: str = Field(description="ISO date of the last live access probe (YYYY-MM-DD)")
    rationale: str = Field(description="Why this backend is gated")
    alternatives: list[str] = Field(description="Tool names users should call instead")
    alternatives_note: str = Field(description="What the alternatives do and do NOT cover")


class TairLocusInfo(SubscriptionGatedRedirect):
    """TAIR stub response — adds ``tair_web_url`` to the shared shape."""

    tair_web_url: str = Field(description="Browser URL for the TAIR locus page")


class PlantCycLocusInfo(SubscriptionGatedRedirect):
    """PlantCyc stub response — adds ``plantcyc_web_url`` to the shared shape."""

    plantcyc_web_url: str = Field(description="Browser URL for the PlantCyc gene page")
