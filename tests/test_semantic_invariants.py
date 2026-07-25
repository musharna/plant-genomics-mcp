"""Semantic invariants over the live tool catalog.

The SDK already validates every tool's ``structuredContent`` against its
``outputSchema``, so a payload of the wrong *shape* cannot escape. These tests
cover the layer above that: a payload of the right shape carrying the wrong
*content*. That is the failure mode that has actually shipped here — the SDK's
version echoed as ours, PDBe counting junk rows, a batch ``count`` that
contradicted its own documentation.

The self-tests come first deliberately. An invariant that cannot fail is worse
than no invariant, because it reports green forever; each one is therefore
proved to reject known-bad input before it is trusted against real data.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from semantic_invariants import (  # noqa: E402
    COUNT_SPECS,
    Verdict,
    check_count_semantics,
    check_organism_echo,
    normalize_organism,
    run_self_tests,
    unrecognised_echo_is_skipped_not_failed,
)

from plant_genomics_mcp import organisms, server  # noqa: E402


@pytest.mark.parametrize(
    "name,ok,note", run_self_tests(), ids=lambda v: v if isinstance(v, str) else ""
)
def test_invariant_self_tests(name: str, ok: bool, note: str) -> None:
    """Every invariant must accept a correct answer AND reject a wrong one."""
    assert ok, f"{name}: {note}"


def test_unrecognised_organism_skips_rather_than_failing() -> None:
    """Unknown vocabulary must be silence, not a false finding.

    This is the guard that keeps the organism-echo check from becoming the
    false-positive generator it was originally deferred for.
    """
    assert unrecognised_echo_is_skipped_not_failed().verdict is Verdict.SKIPPED


def test_every_declared_count_field_exists_in_its_tool_schema() -> None:
    """COUNT_SPECS must not rot as the catalog changes."""
    by_name = {t.name: t for t in server.TOOLS}
    missing = []
    for spec in COUNT_SPECS:
        tool = by_name.get(spec.tool)
        if tool is None:
            missing.append(f"{spec.tool} (tool absent)")
            continue
        props = (tool.outputSchema or {}).get("properties", {})
        if spec.field not in props:
            missing.append(f"{spec.tool}.{spec.field}")
        if spec.list_field is not None and spec.list_field not in props:
            missing.append(f"{spec.tool}.{spec.list_field} (list)")
    assert not missing, f"COUNT_SPECS references fields that no longer exist: {missing}"


def test_count_field_docs_agree_with_declared_semantics() -> None:
    """Three-way check, doc half: what the code declares vs what the schema says.

    ``count`` fields come in two kinds that are indistinguishable in the schema
    (both are ``int``): a pre-cap upstream total, or the size of what was
    actually returned. A caller who reads the wrong one builds a confidently
    wrong summary, so every count must state which it is.
    """
    by_name = {t.name: t for t in server.TOOLS}
    failures = []
    for spec in COUNT_SPECS:
        tool = by_name[spec.tool]
        desc = ((tool.outputSchema or {}).get("properties", {}).get(spec.field) or {}).get(
            "description", ""
        )
        result = check_count_semantics(spec, {spec.field: 0}, desc)
        if result.verdict is Verdict.FAIL:
            failures.append(result.detail)
    assert not failures, "count semantics drift:\n" + "\n".join(failures)


@pytest.mark.parametrize("slug", sorted(organisms.ORGANISMS))
def test_every_supported_organism_normalizes_to_itself(slug: str) -> None:
    """The registry's own slugs must round-trip, or the echo check is useless."""
    assert normalize_organism(slug) == slug


@pytest.mark.parametrize(
    "requested,echoed",
    [
        ("oryza_sativa", "Oryza sativa"),
        ("oryza_sativa", "Oryza sativa subsp. japonica"),
        ("oryza_sativa", "Osativa_v7.0"),
        ("glycine_max", "Gmax_Wm82.a2.v1"),
        ("solanum_lycopersicum", "solanum_lycopersicum_gca000188115v5cm"),
        ("zea_mays", "Zea mays"),
    ],
)
def test_organism_echo_accepts_real_backend_spellings(requested: str, echoed: str) -> None:
    """The four spellings backends actually emit must all compare equal."""
    assert check_organism_echo(requested, echoed, "test").verdict is Verdict.PASS


@pytest.mark.parametrize(
    "requested,echoed",
    [
        ("oryza_sativa", "Zea mays"),
        ("glycine_max", "Osativa_v7.0"),
        ("arabidopsis_thaliana", "Oryza sativa"),
    ],
)
def test_organism_echo_rejects_the_wrong_plant(requested: str, echoed: str) -> None:
    """Silently returning another species' data is the worst failure here."""
    assert check_organism_echo(requested, echoed, "test").verdict is Verdict.FAIL
