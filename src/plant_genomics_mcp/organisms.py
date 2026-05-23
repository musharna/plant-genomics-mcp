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

from .errors import OrganismNotFound, OrganismNotSupported


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


def _normalize(query: str) -> str:
    """Lower, strip, collapse whitespace/hyphens to underscores."""
    s = query.strip().lower()
    s = s.replace("-", "_").replace(" ", "_")
    # Collapse runs of underscores from double-spaces, etc.
    while "__" in s:
        s = s.replace("__", "_")
    return s


def _build_alias_index() -> dict[str, str]:
    """Build a lookup from every accepted string form to canonical key.

    Called once at module import. Includes:
      - canonical slug
      - scientific name (lower, underscores)
      - scientific abbreviation ("a. thaliana" -> "a_thaliana")
      - common names (lower, underscores)
      - explicit aliases from the record
    """
    index: dict[str, str] = {}
    for canonical, record in ORGANISMS.items():
        forms = {
            canonical,
            _normalize(record.scientific),
        }
        # Scientific abbrev: "Arabidopsis thaliana" -> "a. thaliana" -> "a_thaliana"
        sci_parts = record.scientific.split()
        if len(sci_parts) >= 2:
            abbrev = f"{sci_parts[0][0]}_{sci_parts[1]}".lower()
            forms.add(abbrev)
        for name in record.common:
            forms.add(_normalize(name))
        for alias in record.aliases:
            forms.add(_normalize(alias))
        for form in forms:
            index[form] = canonical
    return index


def _build_taxid_index() -> dict[int, str]:
    return {record.ncbi_taxid: record.canonical for record in ORGANISMS.values()}


_ALIAS_INDEX: dict[str, str] = _build_alias_index()
_TAXID_INDEX: dict[int, str] = _build_taxid_index()


def resolve(query: str | int) -> OrganismRecord:
    """Map any accepted input form to the canonical OrganismRecord.

    Accepts: canonical slug, scientific name, scientific abbreviation,
    common name, NCBI taxid (int), case/whitespace/hyphen variants.

    Raises OrganismNotFound (with the full supported list) if no match.
    """
    if isinstance(query, int):
        canonical = _TAXID_INDEX.get(query)
        if canonical is None:
            raise OrganismNotFound(query, supported=list(ORGANISMS.keys()))
        return ORGANISMS[canonical]

    normalized = _normalize(query)
    canonical = _ALIAS_INDEX.get(normalized)
    if canonical is None:
        raise OrganismNotFound(query, supported=list(ORGANISMS.keys()))
    return ORGANISMS[canonical]


def _supported_for(backend_field: str) -> list[str]:
    """Return the canonical names of organisms with a non-None value for this backend."""
    return [
        canonical
        for canonical, record in ORGANISMS.items()
        if getattr(record, backend_field) is not None
    ]


def ensembl_slug_for(query: str | int) -> str:
    record = resolve(query)
    if record.ensembl_slug is None:
        raise OrganismNotSupported(
            backend="ensembl",
            organism=record.canonical,
            supported=_supported_for("ensembl_slug"),
        )
    return record.ensembl_slug


def phytozome_int_for(query: str | int) -> int:
    record = resolve(query)
    if record.phytozome_int is None:
        raise OrganismNotSupported(
            backend="phytozome",
            organism=record.canonical,
            supported=_supported_for("phytozome_int"),
        )
    return record.phytozome_int


def ncbi_taxid_for(query: str | int) -> int:
    # NCBI taxid is always populated on every record — no support gap.
    return resolve(query).ncbi_taxid


def string_taxid_for(query: str | int) -> int:
    record = resolve(query)
    if record.string_taxid is None:
        raise OrganismNotSupported(
            backend="string",
            organism=record.canonical,
            supported=_supported_for("string_taxid"),
        )
    return record.string_taxid


def europe_pmc_slug_for(query: str | int) -> str | None:
    """Return the slug prefix to strip from locus IDs for Europe PMC, or None.

    None means the locus IDs for this organism are already unambiguous and
    need no slug-stripping (matches the existing ``europe_pmc.py`` contract).
    This helper does NOT raise OrganismNotSupported — None is a contract value.
    """
    return resolve(query).europe_pmc_slug
