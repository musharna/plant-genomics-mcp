"""Semantic invariants — catch values that are well-formed but WRONG.

The structural layer is already covered: the MCP SDK validates every tool's
``structuredContent`` against its declared ``outputSchema`` at runtime, so a
payload of the wrong *shape* cannot escape. What escapes is the payload of the
right shape carrying the wrong *content* — the dominant defect class in this
repo (SDK version echoed as ours, PDBe counting junk rows, a batch ``count``
disagreeing with its own documentation, JASPAR's silently-ignored filter param
returning another gene's motifs).

Those values are believable, so no reachability or health check catches them.
Each invariant here encodes one thing that must be true of a *correct* answer.

Two design rules learned the hard way while auditing:

1. **A checker can itself be wrong-but-plausible.** A first pass at the count
   audit flagged all 29 count fields as under-documented; the checker was
   wrong, not the code — it looked for the phrase "true total" and missed the
   actual ``(pre-cap)`` convention. So every invariant here ships with a
   NEGATIVE control (``self_test``) proving it FAILS on known-bad input, not
   just that it passes on good input. An invariant that cannot fail is noise.

2. **Do not trust one source of truth.** ``count`` fields are checked
   three ways — what the code DECLARES (``COUNT_SPECS`` below), what the schema
   DOCUMENTS (parsed from the field description), and what the payload actually
   DOES. Agreement of all three is the invariant; any two disagreeing is a
   finding. This is what makes documentation drift self-detecting instead of
   something a human has to notice.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class Verdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True)
class Result:
    verdict: Verdict
    detail: str


class CountKind(StrEnum):
    """What a ``*_count`` field means.

    The two kinds are indistinguishable in the schema — both are just
    ``int`` — so a caller cannot tell "here are 500 variants" from "there are
    500 upstream, here are the 50 I kept" without the description. Getting this
    wrong in a consumer (a synthesis tool rendering a dossier, say) produces a
    confidently wrong summary.
    """

    #: Total available upstream; MAY exceed the returned list when capped.
    PRE_CAP = "pre-cap"
    #: Size of what is actually returned; MUST equal the list length.
    RETURNED = "returned"


@dataclass(frozen=True)
class CountSpec:
    tool: str
    field: str
    kind: CountKind
    #: Companion list field this count refers to. None when the count has no
    #: single backing list (e.g. an aggregate across nested records).
    list_field: str | None


# Explicit declaration of intent, maintained by hand. Deliberately NOT derived
# from the schema descriptions — deriving it would make the three-way check
# circular and blind to exactly the drift it exists to catch.
COUNT_SPECS: tuple[CountSpec, ...] = (
    CountSpec("get_gene_xrefs", "count", CountKind.RETURNED, "xrefs"),
    CountSpec("ensembl_region_query", "count", CountKind.RETURNED, "features"),
    CountSpec("bar_efp_expression", "ecotype_count", CountKind.RETURNED, "ecotypes"),
    CountSpec("experimental_structures", "structure_count", CountKind.PRE_CAP, "structures"),
    CountSpec("tf_binding_motifs", "motif_count", CountKind.PRE_CAP, "motifs"),
    CountSpec("experimental_interactions", "partner_count", CountKind.PRE_CAP, "partners"),
    CountSpec("locus_gene_rifs", "rif_count", CountKind.PRE_CAP, "gene_rifs"),
    CountSpec("interpro_domains", "domain_count", CountKind.PRE_CAP, "domains"),
    CountSpec("locus_variants", "variant_count", CountKind.PRE_CAP, "variants"),
    CountSpec("arabidopsis_natural_variation", "variant_count", CountKind.PRE_CAP, "variants"),
    CountSpec("aragwas_associations", "association_count", CountKind.PRE_CAP, "associations"),
    CountSpec("orthodb_orthologs", "organism_count", CountKind.PRE_CAP, None),
    CountSpec("orthodb_orthologs", "member_count", CountKind.RETURNED, "members"),
    CountSpec("plantcyc_locus_info", "pathway_count", CountKind.PRE_CAP, "pathways"),
    CountSpec("plantcyc_locus_info", "reaction_count", CountKind.PRE_CAP, "reactions"),
    # Aggregate over nested partner records, not a top-level list length.
    CountSpec("experimental_interactions", "evidence_count", CountKind.PRE_CAP, None),
)

_PRE_CAP_MARKERS = ("pre-cap", "true total", "total available", "before cap")
_RETURNED_MARKERS = ("post-cap", "returned", "rows in", "len of")


def documented_kind(description: str) -> CountKind | None:
    """Parse the count kind out of a schema field description.

    Returns None when the description states neither, which is itself worth
    reporting — a caller reading only the schema then cannot tell whether the
    number describes what they received or what exists upstream.
    """
    d = description.lower()
    pre = any(m in d for m in _PRE_CAP_MARKERS)
    ret = any(m in d for m in _RETURNED_MARKERS)
    if pre and not ret:
        return CountKind.PRE_CAP
    if ret and not pre:
        return CountKind.RETURNED
    return None


def check_count_semantics(spec: CountSpec, payload: dict[str, Any], description: str) -> Result:
    """Three-way agreement: declared kind vs documented kind vs observed data."""
    if spec.field not in payload:
        return Result(Verdict.SKIPPED, f"{spec.field} absent from payload")

    count = payload[spec.field]
    if not isinstance(count, int):
        return Result(Verdict.FAIL, f"{spec.field} is {type(count).__name__}, not int")
    if count < 0:
        return Result(Verdict.FAIL, f"{spec.field} is negative ({count})")

    doc = documented_kind(description)
    if doc is None:
        return Result(
            Verdict.FAIL,
            f"{spec.tool}.{spec.field}: description states neither pre-cap nor "
            f"returned semantics, so a schema-only reader cannot interpret it "
            f"— {description!r}",
        )
    if doc is not spec.kind:
        return Result(
            Verdict.FAIL,
            f"{spec.tool}.{spec.field}: code declares {spec.kind.value} but the "
            f"schema documents {doc.value} — {description!r}",
        )

    if spec.list_field is None:
        return Result(Verdict.PASS, f"{spec.field}={count}, {doc.value} (no backing list)")

    items = payload.get(spec.list_field)
    if not isinstance(items, list):
        return Result(Verdict.SKIPPED, f"{spec.list_field} absent or not a list")
    n = len(items)

    if spec.kind is CountKind.RETURNED and count != n:
        return Result(
            Verdict.FAIL,
            f"{spec.tool}.{spec.field}={count} but len({spec.list_field})={n}; "
            f"declared '{CountKind.RETURNED.value}' means they must be equal",
        )
    if spec.kind is CountKind.PRE_CAP and count < n:
        return Result(
            Verdict.FAIL,
            f"{spec.tool}.{spec.field}={count} is LESS than "
            f"len({spec.list_field})={n}; a pre-cap total can never be smaller "
            f"than what was returned",
        )
    return Result(Verdict.PASS, f"{spec.field}={count} vs len({spec.list_field})={n} ({doc.value})")


def check_truncated_flag(spec: CountSpec, payload: dict[str, Any]) -> Result:
    """``truncated`` must be true exactly when rows were withheld.

    A stale-true wrongly tells the caller to go paginate for data that isn't
    there; a stale-false silently presents a partial list as complete. The
    second is the dangerous one — it looks like a finished answer.

    Only decidable for a PRE_CAP count. A RETURNED count is the length of the
    list by construction, so ``count > len(list)`` can never hold and this
    check would report every truncated payload as inconsistent — a
    wrong-but-plausible finding of the checker's own making. The registry
    already records which kind each count is; consult it rather than assuming.
    """
    if "truncated" not in payload or spec.list_field is None:
        return Result(Verdict.SKIPPED, "no truncated flag or no backing list")
    if spec.kind is not CountKind.PRE_CAP:
        return Result(
            Verdict.SKIPPED,
            f"{spec.field} is {spec.kind}; withheld count is not derivable from it",
        )
    if spec.field not in payload:
        return Result(Verdict.SKIPPED, f"{spec.field} absent")
    items = payload.get(spec.list_field)
    if not isinstance(items, list):
        return Result(Verdict.SKIPPED, f"{spec.list_field} absent or not a list")

    truncated = bool(payload["truncated"])
    withheld = payload[spec.field] > len(items)
    if truncated != withheld:
        return Result(
            Verdict.FAIL,
            f"{spec.tool}: truncated={truncated} but {spec.field}="
            f"{payload[spec.field]} vs len({spec.list_field})={len(items)} "
            f"implies withheld={withheld}",
        )
    return Result(Verdict.PASS, f"truncated={truncated} agrees with {spec.field} vs list length")


# --- organism echo (the deferred INV-3) --------------------------------------
# Deferred originally because naive comparison is a false-positive generator:
# the same species is spelled `oryza_sativa`, `Oryza sativa`, `osa`,
# `Osativa_v7.0`, `Gmax_Wm82.a2.v1`. Normalize to a genus+species token pair
# first, then compare only that.

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_INFRASPECIFIC = ("subsp", "var", "cv", "str", "ssp")


def _canonical_slugs() -> tuple[str, ...]:
    """Supported organisms, from the live registry — not a hand-copied list."""
    from plant_genomics_mcp import organisms

    return tuple(organisms.ORGANISMS)


def _abbreviate(slug: str) -> str:
    """``oryza_sativa`` -> ``osativa``; the Phytozome/JGI identifier style."""
    genus, _, species = slug.partition("_")
    return f"{genus[:1]}{species}" if genus and species else slug


def normalize_organism(value: str) -> str:
    """Resolve a species echo to a canonical registry slug, or "" if unknown.

    Backends spell the same organism at least four ways — slug
    (``oryza_sativa``), scientific name (``Oryza sativa subsp. japonica``),
    JGI-abbreviated assembly id (``Osativa_v7.0``, ``Gmax_Wm82.a2.v1``), and
    assembly-suffixed slug (Ensembl's tomato is literally
    ``solanum_lycopersicum_gca000188115v5cm``). Stripping version junk with a
    regex is what made this a false-positive generator when it was first
    attempted, so instead every candidate is resolved against the REGISTRY
    vocabulary and anything unrecognised returns "" (-> SKIPPED, never FAIL).
    """
    if not value:
        return ""
    tokens = [t for t in _NON_ALNUM.split(value.strip().lower()) if t]
    if not tokens:
        return ""
    for stop in _INFRASPECIFIC:
        if stop in tokens:
            tokens = tokens[: tokens.index(stop)]
    if not tokens:
        return ""

    slugs = _canonical_slugs()
    # 1. genus + species prefix — covers slugs, scientific names, and the
    #    assembly-suffixed slug (extra trailing tokens are simply ignored).
    if len(tokens) >= 2:
        head = f"{tokens[0]}_{tokens[1]}"
        if head in slugs:
            return head
    # 2. JGI abbreviation in the leading token (`Osativa_v7.0`, `Gmax_...`).
    for slug in slugs:
        if tokens[0] == _abbreviate(slug):
            return slug
    # 3. bare genus, accepted only when it is unambiguous in the registry.
    genus_hits = [s for s in slugs if s.split("_", 1)[0] == tokens[0]]
    if len(genus_hits) == 1:
        return genus_hits[0]
    return ""


def check_organism_echo(requested: str, echoed: str, source: str) -> Result:
    """The species a backend echoes must be the species that was asked for.

    This is the invariant that catches silently returning another organism's
    data — the worst failure mode for a scientific tool, because the result is
    entirely plausible and simply belongs to the wrong plant.
    """
    want = normalize_organism(requested)
    got = normalize_organism(echoed)
    if not want or not got:
        return Result(
            Verdict.SKIPPED, f"{source}: nothing comparable ({requested!r} vs {echoed!r})"
        )
    if want == got:
        return Result(Verdict.PASS, f"{source}: {echoed!r} matches {requested!r}")
    return Result(
        Verdict.FAIL,
        f"{source}: echoed organism {echoed!r} (normalized {got!r}) does NOT "
        f"match requested {requested!r} (normalized {want!r})",
    )


# --- self-tests: every invariant must be provably able to FAIL ---------------


@dataclass(frozen=True)
class SelfTest:
    name: str
    #: Must return PASS — proves the invariant accepts a correct answer.
    positive: Callable[[], Result]
    #: Must return FAIL — proves the invariant is not vacuous.
    negative: Callable[[], Result]


_RETURNED_SPEC = CountSpec("t", "count", CountKind.RETURNED, "items")
_PRE_CAP_SPEC = CountSpec("t", "count", CountKind.PRE_CAP, "items")
_DOC_RETURNED = "Number of records returned"
_DOC_PRE_CAP = "Total records (pre-cap)"

SELF_TESTS: tuple[SelfTest, ...] = (
    SelfTest(
        "count_returned_must_equal_list_length",
        lambda: check_count_semantics(_RETURNED_SPEC, {"count": 2, "items": [1, 2]}, _DOC_RETURNED),
        lambda: check_count_semantics(_RETURNED_SPEC, {"count": 3, "items": [1, 2]}, _DOC_RETURNED),
    ),
    SelfTest(
        "pre_cap_count_may_exceed_but_never_undercut",
        lambda: check_count_semantics(_PRE_CAP_SPEC, {"count": 99, "items": [1, 2]}, _DOC_PRE_CAP),
        lambda: check_count_semantics(_PRE_CAP_SPEC, {"count": 1, "items": [1, 2]}, _DOC_PRE_CAP),
    ),
    SelfTest(
        "declared_kind_must_match_documented_kind",
        lambda: check_count_semantics(_PRE_CAP_SPEC, {"count": 5, "items": []}, _DOC_PRE_CAP),
        # Code says pre-cap, schema says returned — the F2 drift class.
        lambda: check_count_semantics(_PRE_CAP_SPEC, {"count": 5, "items": []}, _DOC_RETURNED),
    ),
    SelfTest(
        "undocumented_count_semantics_is_a_finding",
        lambda: check_count_semantics(_PRE_CAP_SPEC, {"count": 5, "items": []}, _DOC_PRE_CAP),
        lambda: check_count_semantics(_PRE_CAP_SPEC, {"count": 5, "items": []}, "How many"),
    ),
    SelfTest(
        "truncated_must_track_withheld_rows",
        lambda: check_truncated_flag(_PRE_CAP_SPEC, {"count": 9, "items": [1], "truncated": True}),
        # The dangerous direction: a partial list presented as complete.
        lambda: check_truncated_flag(_PRE_CAP_SPEC, {"count": 9, "items": [1], "truncated": False}),
    ),
    SelfTest(
        "organism_echo_matches_jgi_abbreviation",
        lambda: check_organism_echo("oryza_sativa", "Osativa_v7.0", "phytozome"),
        # Wrong plant entirely — the failure this invariant exists for.
        lambda: check_organism_echo("oryza_sativa", "Zea mays", "phytozome"),
    ),
    SelfTest(
        "organism_echo_matches_assembly_suffixed_slug",
        # Ensembl's tomato slug really is this shape.
        lambda: check_organism_echo(
            "solanum_lycopersicum", "solanum_lycopersicum_gca000188115v5cm", "ensembl"
        ),
        lambda: check_organism_echo("solanum_lycopersicum", "Gmax_Wm82.a2.v1", "ensembl"),
    ),
    SelfTest(
        "organism_echo_matches_scientific_name_with_infraspecific_rank",
        lambda: check_organism_echo("oryza_sativa", "Oryza sativa subsp. japonica", "ncbi"),
        lambda: check_organism_echo("zea_mays", "Oryza sativa subsp. japonica", "ncbi"),
    ),
)


def unrecognised_echo_is_skipped_not_failed() -> Result:
    """An organism we cannot resolve must never be reported as a mismatch.

    This is the guard that keeps INV-3 from becoming the false-positive
    generator it was deferred for: silence on unknown vocabulary, not noise.
    """
    return check_organism_echo("oryza_sativa", "Unknown Species XYZ", "somewhere")


def run_self_tests() -> list[tuple[str, bool, str]]:
    """Prove each invariant both accepts correct input and rejects bad input."""
    out: list[tuple[str, bool, str]] = []
    for st in SELF_TESTS:
        pos = st.positive()
        neg = st.negative()
        ok = pos.verdict is Verdict.PASS and neg.verdict is Verdict.FAIL
        note = f"positive={pos.verdict.value} negative={neg.verdict.value}"
        if not ok:
            note += f" | pos: {pos.detail} | neg: {neg.detail}"
        out.append((st.name, ok, note))
    return out


if __name__ == "__main__":
    failures = 0
    for name, ok, note in run_self_tests():
        print(f"{'ok  ' if ok else 'FAIL'} {name}: {note}")
        failures += 0 if ok else 1
    skip = unrecognised_echo_is_skipped_not_failed()
    ok = skip.verdict is Verdict.SKIPPED
    print(
        f"{'ok  ' if ok else 'FAIL'} unrecognised_echo_is_skipped_not_failed: {skip.verdict.value}"
    )
    failures += 0 if ok else 1
    raise SystemExit(1 if failures else 0)
