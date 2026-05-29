"""Unit tests for the monitoring re-run's failing-loci classifier (v1.7 seed 4).

Exercises ``failing_loci`` against synthetic sidecars — no live calls. "Failing"
must match ``benchmark_annotations.EXIT_TRIGGERING_VERDICTS`` exactly (FAIL,
EXCEPTION_BAD, EXCEPTION_DIFFERENT, TIMEOUT) and ignore PASS/DRIFT/EXCEPTION_OK/
SKIPPED, regardless of where in the locus the verdict is nested.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from benchmark_failing_loci import failing_loci  # noqa: E402


def _locus(locus_id: str, **tools_verdicts: str) -> dict:
    """A locus whose each tool has a single assertion with the given verdict."""
    return {
        "locus_id": locus_id,
        "tools": {
            name: {"assertions": {"k": {"verdict": verdict}}}
            for name, verdict in tools_verdicts.items()
        },
    }


def test_all_clean_returns_empty() -> None:
    sidecar = {"loci": [_locus("A", t1="PASS"), _locus("B", t1="DRIFT", t2="EXCEPTION_OK")]}
    assert failing_loci(sidecar) == []


def test_single_tool_fail_is_flagged() -> None:
    sidecar = {"loci": [_locus("A", t1="PASS"), _locus("B", t1="FAIL")]}
    assert failing_loci(sidecar) == ["B"]


def test_exception_bad_in_probe_exceptions_is_flagged() -> None:
    sidecar = {
        "loci": [
            {
                "locus_id": "C",
                "tools": {"t1": {"assertions": {"k": {"verdict": "PASS"}}}},
                "probe_exceptions": {"some_tool": {"verdict": "EXCEPTION_BAD", "note": "boom"}},
            }
        ]
    }
    assert failing_loci(sidecar) == ["C"]


def test_invariant_fail_is_flagged() -> None:
    sidecar = {
        "loci": [
            {
                "locus_id": "D",
                "tools": {"t1": {"assertions": {"k": {"verdict": "PASS"}}}},
                "invariants": {"some_inv": {"verdict": "FAIL", "detail": "x != y"}},
            }
        ]
    }
    assert failing_loci(sidecar) == ["D"]


def test_exception_ok_is_not_failing() -> None:
    # A correctly-anticipated exception (expects_exception happy case) must NOT page.
    sidecar = {"loci": [_locus("E", phyto="EXCEPTION_OK")]}
    assert failing_loci(sidecar) == []


def test_timeout_and_exception_different_are_failing() -> None:
    sidecar = {"loci": [_locus("F", t1="TIMEOUT"), _locus("G", t1="EXCEPTION_DIFFERENT")]}
    assert failing_loci(sidecar) == ["F", "G"]


def test_ordering_is_preserved() -> None:
    sidecar = {"loci": [_locus("A", t1="FAIL"), _locus("B", t1="PASS"), _locus("C", t1="FAIL")]}
    assert failing_loci(sidecar) == ["A", "C"]


def test_missing_loci_key_and_missing_locus_id_tolerated() -> None:
    assert failing_loci({}) == []
    assert (
        failing_loci({"loci": [{"tools": {"t1": {"assertions": {"k": {"verdict": "FAIL"}}}}}]})
        == []
    )
