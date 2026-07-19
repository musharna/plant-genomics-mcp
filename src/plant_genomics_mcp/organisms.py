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
    kegg_org_code: str | None = None
    # KEGG 3-letter org code (e.g. "ath"). Arabidopsis ``ath`` accepts AGI
    # loci natively; all other plant scopes index NCBI Entrez Gene IDs.
    # v1.4.0 added an Ensembl Plants ``/xrefs/id`` bridge in ``kegg.py``
    # that resolves community loci to Entrez IDs for the populated set
    # (rice / maize / soybean). v1.5 extended the populated set to any
    # additional organism whose chr1 first-gene probe round-tripped through
    # Ensembl /xrefs → EntrezGene successfully (see
    # ``scripts/probe_kegg_bridge_candidates.json``). Matrix entries that
    # still leave this ``None`` did not pass the probe; line comments per
    # entry record the probe-dated falsification reason.
    atted_release: str | None = None  # e.g. "Ath-u.c4-0" — ATTED-II release id
    ensembl_id_prefix: str | None = None
    # Wire-only stable-id prefix prepended to the locus when querying Ensembl
    # ``/lookup/id`` and ``/xrefs/id`` — NOT part of the user-facing locus.
    # Needed for assemblies imported from an NCBI GFF, whose Ensembl stable
    # IDs carry a ``gene-`` prefix (e.g. tomato SL4.0 → ``gene-Solyc...``).
    # ``None``/"" means pass the locus through unchanged.
    gprofiler_id: str | None = None
    # g:Profiler organism ID (e.g. "athaliana") used by ``gprofiler.py`` for
    # GO/KEGG over-representation. NOT derivable from the NCBI taxid: g:Profiler
    # indexes specific assemblies/cultivars, so barley → ``hvulgare`` (its taxid
    # 112509) and wheat → ``talancer`` (Lancer cultivar), neither matching the
    # species-level taxid on this record. IDs verified against
    # /api/util/organisms_list/ (probed 2026-07-19). ``None`` = not covered.
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
        kegg_org_code="ath",
        atted_release="Ath-u.c4-0",
        gprofiler_id="athaliana",
        aliases=("a. thaliana", "at", "arabidopsis"),
    ),
    "oryza_sativa": OrganismRecord(
        canonical="oryza_sativa",
        scientific="Oryza sativa",
        common=("rice", "asian rice"),
        ncbi_taxid=39947,
        ensembl_slug="oryza_sativa",
        phytozome_int=323,
        string_taxid=39947,
        europe_pmc_slug="rice",
        kegg_org_code="osa",  # v1.4.0: bridge via Ensembl /xrefs EntrezGene resolves RAP-DB loci → Entrez Gene IDs
        atted_release="Osa-u.c1-0",
        gprofiler_id="osativa",
        aliases=("o. sativa",),
    ),
    "zea_mays": OrganismRecord(
        canonical="zea_mays",
        scientific="Zea mays",
        common=("maize", "corn"),
        ncbi_taxid=4577,
        ensembl_slug="zea_mays",
        phytozome_int=833,
        string_taxid=4577,
        europe_pmc_slug="maize",
        kegg_org_code="zma",  # v1.4.0: bridge via Ensembl /xrefs EntrezGene resolves MaizeGDB loci → Entrez Gene IDs
        atted_release="Zma-u.c1-0",
        gprofiler_id="zmays",
        aliases=("z. mays",),
    ),
    "triticum_aestivum": OrganismRecord(
        canonical="triticum_aestivum",
        scientific="Triticum aestivum",
        common=("bread wheat", "wheat"),
        ncbi_taxid=4565,
        ensembl_slug="triticum_aestivum",
        phytozome_int=725,
        string_taxid=4565,
        europe_pmc_slug="wheat",
        # v1.5 probe: Ensembl /xrefs returned no EntrezGene xref for chr1
        # locus TraesCS1A02G000300 (observed dbs: ArrayExpress,
        # KNETMINER_WHEAT, WHEATEXP_GENE); v1.4.0 bridge mechanism
        # falsified. KEGG taes indexes NCBI Entrez Gene IDs and would
        # need a UniProt → Entrez two-hop fallback (probed 2026-05-25,
        # deferred to v1.6+).
        kegg_org_code=None,
        atted_release=None,  # ATTED-II has no Tae-u release (probed 2026-05-24)
        gprofiler_id="talancer",  # g:Profiler indexes the Lancer cultivar (taxid 4565002); species taxid 4565 is not a g:Profiler org
        aliases=("t. aestivum",),
    ),
    "solanum_lycopersicum": OrganismRecord(
        canonical="solanum_lycopersicum",
        scientific="Solanum lycopersicum",
        common=("tomato",),
        ncbi_taxid=4081,
        # Ensembl Plants dropped the bare ``solanum_lycopersicum`` slug; tomato
        # is now the assembly-qualified SL4.0 genome (GCA_000188115.5) whose
        # stable IDs carry a ``gene-`` prefix (e.g. ``gene-Solyc01g005610.4``).
        # The bare slug + unprefixed id now 400 ("Genome not found" / "ID not
        # found") — re-pointed here so the shipped ensembl_plants tool works
        # for tomato again (upstream drift caught by the weekly benchmark,
        # 2026-06-22).
        ensembl_slug="solanum_lycopersicum_gca000188115v5cm",
        ensembl_id_prefix="gene-",
        phytozome_int=691,
        string_taxid=4081,
        europe_pmc_slug="tomato",
        # KEGG left disabled: the v1.5 probe falsified the bridge on the old
        # assembly (only ArrayExpress, no EntrezGene). The SL4.0 re-release DOES
        # now expose an EntrezGene xref (LOC101244801), so ``sly`` re-enablement
        # is a viable future follow-up — deferred to its own probe + PR to keep
        # this regression fix tight.
        kegg_org_code=None,
        atted_release="Sly-u.c1-0",
        gprofiler_id="slycopersicum",
        aliases=("s. lycopersicum",),
    ),
    "glycine_max": OrganismRecord(
        canonical="glycine_max",
        scientific="Glycine max",
        common=("soybean", "soya bean"),
        ncbi_taxid=3847,
        ensembl_slug="glycine_max",
        phytozome_int=275,
        string_taxid=3847,
        europe_pmc_slug="soybean",
        kegg_org_code="gmx",  # v1.4.0: bridge via Ensembl /xrefs EntrezGene; SoyBase ``Glyma.`` form normalized to Ensembl ``GLYMA_`` on the wire
        atted_release="Gma-u.c1-0",
        gprofiler_id="gmax",
        aliases=("g. max",),
    ),
    "sorghum_bicolor": OrganismRecord(
        canonical="sorghum_bicolor",
        scientific="Sorghum bicolor",
        common=("sorghum",),
        ncbi_taxid=4558,
        ensembl_slug="sorghum_bicolor",
        phytozome_int=454,
        string_taxid=4558,
        europe_pmc_slug="sorghum",
        # v1.5 probe: Ensembl /xrefs returned no EntrezGene xref for chr1
        # locus SORBI_3001G002100 (observed dbs: ArrayExpress); v1.4.0
        # bridge mechanism falsified. KEGG sbi indexes NCBI Entrez Gene
        # IDs and would need a UniProt → Entrez two-hop fallback
        # (probed 2026-05-25, deferred to v1.6+).
        kegg_org_code=None,
        atted_release=None,  # ATTED-II has no Sbi-u release (probed 2026-05-24)
        gprofiler_id="sbicolor",
        aliases=("s. bicolor",),
    ),
    "hordeum_vulgare": OrganismRecord(
        canonical="hordeum_vulgare",
        scientific="Hordeum vulgare",
        common=("barley",),
        ncbi_taxid=4513,
        ensembl_slug="hordeum_vulgare",
        phytozome_int=702,
        string_taxid=4513,
        europe_pmc_slug="barley",
        kegg_org_code="hvg",  # v1.5: bridge probed pass — Ensembl /xrefs returns EntrezGene for chr1 locus HORVU.MOREX.r3.1HG0000090 (probed 2026-05-25, scripts/probe_kegg_bridge_candidates.json)
        atted_release=None,  # ATTED-II has no Hvg-u release (probed 2026-05-24)
        gprofiler_id="hvulgare",  # g:Profiler taxid 112509; species taxid 4513 is not a g:Profiler org
        aliases=("h. vulgare",),
    ),
    "vitis_vinifera": OrganismRecord(
        canonical="vitis_vinifera",
        scientific="Vitis vinifera",
        common=("grape", "grapevine"),
        ncbi_taxid=29760,
        ensembl_slug="vitis_vinifera",
        phytozome_int=457,
        string_taxid=29760,
        europe_pmc_slug=None,
        # v1.5 probe: Ensembl /xrefs returned no EntrezGene xref for chr1
        # locus Vitis01g00017 (observed dbs: ArrayExpress); v1.4.0
        # bridge mechanism falsified. KEGG vvi indexes NCBI Entrez Gene
        # IDs and would need a UniProt → Entrez two-hop fallback
        # (probed 2026-05-25, deferred to v1.6+).
        kegg_org_code=None,
        atted_release="Vvi-u.c1-0",
        gprofiler_id="vvinifera",
        aliases=("v. vinifera",),
    ),
    "populus_trichocarpa": OrganismRecord(
        canonical="populus_trichocarpa",
        scientific="Populus trichocarpa",
        common=("poplar", "black cottonwood"),
        ncbi_taxid=3694,
        ensembl_slug="populus_trichocarpa",
        phytozome_int=210,
        string_taxid=3694,
        europe_pmc_slug=None,
        kegg_org_code="pop",  # v1.5: bridge probed pass — Ensembl /xrefs returns EntrezGene for chr1 locus Potri.001G006600.v4.1 (probed 2026-05-25, scripts/probe_kegg_bridge_candidates.json)
        atted_release=None,  # ATTED-II Pop-u/Ptr-u return invalid-db (probed 2026-05-24)
        gprofiler_id="ptrichocarpa",
        aliases=("p. trichocarpa",),
    ),
    "medicago_truncatula": OrganismRecord(
        canonical="medicago_truncatula",
        scientific="Medicago truncatula",
        common=("barrel medic", "barrel clover"),
        ncbi_taxid=3880,
        ensembl_slug="medicago_truncatula",
        phytozome_int=285,
        string_taxid=3880,
        europe_pmc_slug=None,
        # v1.5 probe: Ensembl /xrefs returned no EntrezGene xref for chr1
        # locus gene55 (observed dbs: ArrayExpress); v1.4.0 bridge
        # mechanism falsified. KEGG mtr indexes NCBI Entrez Gene IDs
        # and would need a UniProt → Entrez two-hop fallback (probed
        # 2026-05-25, deferred to v1.6+).
        kegg_org_code=None,
        atted_release="Mtr-u.c1-0",
        gprofiler_id="mtruncatula",
        aliases=("m. truncatula",),
    ),
    "brachypodium_distachyon": OrganismRecord(
        canonical="brachypodium_distachyon",
        scientific="Brachypodium distachyon",
        common=("purple false brome",),
        ncbi_taxid=15368,
        ensembl_slug="brachypodium_distachyon",
        phytozome_int=314,
        string_taxid=15368,
        europe_pmc_slug="Brachypodium",
        kegg_org_code="bdi",  # v1.5: bridge probed pass — Ensembl /xrefs returns EntrezGene for chr1 locus BRADI_1g00485v3 (probed 2026-05-25, scripts/probe_kegg_bridge_candidates.json)
        atted_release=None,  # ATTED-II has no Bdi-u release (probed 2026-05-24)
        gprofiler_id="bdistachyon",
        aliases=("b. distachyon",),
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


def ensembl_id_prefix_for(query: str | int) -> str:
    """Return the wire-only Ensembl stable-id prefix for this organism.

    Empty string when the organism needs no prefix (the common case). See
    ``OrganismRecord.ensembl_id_prefix`` for why a prefix is ever needed.
    """
    return resolve(query).ensembl_id_prefix or ""


def phytozome_int_for(query: str | int) -> int:
    record = resolve(query)
    if record.phytozome_int is None:
        raise OrganismNotSupported(
            backend="phytozome",
            organism=record.canonical,
            supported=_supported_for("phytozome_int"),
        )
    return record.phytozome_int


def kegg_org_code_for(query: str | int) -> str:
    record = resolve(query)
    if record.kegg_org_code is None:
        raise OrganismNotSupported(
            backend="kegg",
            organism=record.canonical,
            supported=_supported_for("kegg_org_code"),
        )
    return record.kegg_org_code


def atted_release_for(query: str | int) -> str:
    record = resolve(query)
    if record.atted_release is None:
        raise OrganismNotSupported(
            backend="atted",
            organism=record.canonical,
            supported=_supported_for("atted_release"),
        )
    return record.atted_release


def gprofiler_id_for(query: str | int) -> str:
    record = resolve(query)
    if record.gprofiler_id is None:
        raise OrganismNotSupported(
            backend="gprofiler",
            organism=record.canonical,
            supported=_supported_for("gprofiler_id"),
        )
    return record.gprofiler_id


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
