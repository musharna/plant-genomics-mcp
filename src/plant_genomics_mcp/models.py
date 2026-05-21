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
