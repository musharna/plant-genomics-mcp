"""Curated multi-organism registry for plant-genomics-mcp.

A single source of truth for the organism IDs used by every backend.
Each record carries the canonical slug, scientific + common names,
NCBI taxid, and per-backend ID slots. ``None`` in a backend slot means
the backend does not cover that organism — accessor helpers raise
``OrganismNotSupported`` in that case.

The registry is hardcoded (no live discovery). ``scripts/verify_organisms.py``
re-probes the per-backend IDs pre-release to catch upstream drift.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class OrganismRecord:
    canonical: str
    scientific: str
    common: tuple[str, ...]
    ncbi_taxid: int
    ensembl_slug: str | None
    phytozome_int: int | None
    string_taxid: int | None
    europe_pmc_slug: str | None
    aliases: tuple[str, ...] = field(default_factory=tuple)


ORGANISMS: dict[str, OrganismRecord] = {
    "arabidopsis_thaliana": OrganismRecord(
        canonical="arabidopsis_thaliana",
        scientific="Arabidopsis thaliana",
        common=("thale cress", "mouse-ear cress"),
        ncbi_taxid=3702,
        ensembl_slug="arabidopsis_thaliana",
        phytozome_int=167,
        string_taxid=3702,
        europe_pmc_slug=None,
        aliases=("a. thaliana", "at", "arabidopsis"),
    ),
}


DEFAULT_ORGANISM: str = "arabidopsis_thaliana"
