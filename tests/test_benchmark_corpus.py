"""Offline integrity checks for the committed benchmark corpus (expected.json).

The corpus drives the live scientific-validation benchmark, but a malformed
edit (a typo'd tool name, an unresolvable organism, a bad fact shape, an unknown
exception name) would otherwise only surface at live-run time — weekly, or at
release. These tests validate the artifact's SCHEMA against the live `_TOOLS`
registry + organism resolver, fully offline, in normal PR CI. No network.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from benchmark_annotations import (  # noqa: E402
    _BENCHMARK_TYPED_EXCEPTIONS,
    _TOOLS,
    DEFAULT_EXPECTED_JSON,
)

from plant_genomics_mcp import organisms  # noqa: E402

_VALID_EXCEPTION_NAMES = {exc.__name__ for exc in _BENCHMARK_TYPED_EXCEPTIONS}


@pytest.fixture(scope="module")
def corpus() -> dict:
    return json.loads(Path(DEFAULT_EXPECTED_JSON).read_text())


def _loci(corpus: dict) -> list[dict]:
    return corpus["loci"]


def test_corpus_top_level_shape(corpus: dict) -> None:
    assert isinstance(corpus.get("loci"), list) and corpus["loci"], (
        "corpus has a non-empty loci list"
    )


def test_locus_ids_are_unique_nonempty_strings(corpus: dict) -> None:
    ids = [loc.get("locus_id") for loc in _loci(corpus)]
    assert all(isinstance(i, str) and i for i in ids), "every locus_id is a non-empty string"
    assert len(ids) == len(set(ids)), (
        f"duplicate locus_id(s): {[i for i in ids if ids.count(i) > 1]}"
    )


def test_every_organism_resolves(corpus: dict) -> None:
    for loc in _loci(corpus):
        org = loc.get("organism")
        assert isinstance(org, str) and org, f"{loc.get('locus_id')}: organism must be a string"
        organisms.resolve(org)  # raises OrganismNotFound if the corpus names a bogus organism


def test_every_tool_name_is_in_the_dispatch_registry(corpus: dict) -> None:
    for loc in _loci(corpus):
        for tool_name in loc.get("tools", {}):
            assert tool_name in _TOOLS, (
                f"{loc['locus_id']}: tool {tool_name!r} not in _TOOLS registry "
                f"(typo? renamed backend?)"
            )


def test_every_tool_entry_has_a_valid_assertion_shape(corpus: dict) -> None:
    for loc in _loci(corpus):
        for tool_name, entry in loc.get("tools", {}).items():
            where = f"{loc['locus_id']}/{tool_name}"
            assert isinstance(entry, dict), f"{where}: tool entry must be a dict"
            if "expects_exception" in entry:
                exc = entry["expects_exception"]
                assert exc in _VALID_EXCEPTION_NAMES, (
                    f"{where}: expects_exception {exc!r} is not a benchmark-typed exception "
                    f"{sorted(_VALID_EXCEPTION_NAMES)}"
                )
                # An exception case must not ALSO carry fact assertions.
                assert "stable_facts" not in entry and "variable_facts" not in entry, (
                    f"{where}: expects_exception entries must not also have stable/variable facts"
                )
            else:
                assert "stable_facts" in entry and "variable_facts" in entry, (
                    f"{where}: non-exception entry needs both stable_facts and variable_facts"
                )
                assert isinstance(entry["stable_facts"], dict)
                assert isinstance(entry["variable_facts"], dict)


def test_stable_fact_values_are_scalars(corpus: dict) -> None:
    for loc in _loci(corpus):
        for tool_name, entry in loc.get("tools", {}).items():
            for key, value in entry.get("stable_facts", {}).items():
                assert isinstance(key, str) and key, (
                    f"{loc['locus_id']}/{tool_name}: empty stable key"
                )
                assert isinstance(value, (str, int, float, bool)), (
                    f"{loc['locus_id']}/{tool_name}/{key}: stable_facts value must be a scalar "
                    f"(exact-match), got {type(value).__name__}"
                )


def test_variable_fact_entries_have_numeric_baseline_and_bands(corpus: dict) -> None:
    for loc in _loci(corpus):
        for tool_name, entry in loc.get("tools", {}).items():
            for key, spec in entry.get("variable_facts", {}).items():
                where = f"{loc['locus_id']}/{tool_name}/{key}"
                assert isinstance(key, str) and key, f"{where}: empty variable key"
                assert isinstance(spec, dict), f"{where}: variable_facts value must be a dict"
                assert isinstance(spec.get("baseline"), (int, float)) and not isinstance(
                    spec.get("baseline"), bool
                ), f"{where}: baseline must be numeric"
                for band in ("tolerance_pct", "floor", "ceiling"):
                    if band in spec:
                        assert isinstance(spec[band], (int, float)) and not isinstance(
                            spec[band], bool
                        ), f"{where}: {band} must be numeric when present"
