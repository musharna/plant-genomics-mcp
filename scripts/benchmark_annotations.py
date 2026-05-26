#!/usr/bin/env python3
"""v1.6 benchmark — scientific validation + drift detector for plant-genomics-mcp.

Drives ~12 curated canonical loci through all 9 backend modules + 5 synthesis
pipelines, compares results to scripts/benchmark_annotations.expected.json,
and emits per-locus-per-tool PASS / DRIFT / FAIL verdicts.

Twin-tier assertions:
  - stable_facts: exact match required (organism canonical, taxid, KEGG org
    code, gene_id prefix, scientific name)
  - variable_facts: tolerance band + optional floor/ceiling (pathway count,
    GO count, xref count, partner count, homolog count)

Exception assertions: ``expects_exception: "OrganismNotSupported"`` etc. for
matrix-falsified-organism cases (wheat/tomato/etc. on KEGG).

Verdict enumeration:
  PASS           — within tolerance / exact match
  DRIFT          — outside tolerance but within floor/ceiling
  FAIL           — below floor / above ceiling / regression
  EXCEPTION_OK   — anticipated exception raised
  EXCEPTION_BAD  — unanticipated exception
  EXCEPTION_DIFFERENT — exception class different from expected
  TIMEOUT        — signal.alarm tripped (per-organism walltime guard, 120s)
  SKIPPED        — gated by --include-blast or expected.json skip flag

Exit codes:
  0 — all PASS+DRIFT+SKIPPED+EXCEPTION_OK
  1 — any FAIL/EXCEPTION_BAD/EXCEPTION_DIFFERENT/TIMEOUT
  2 — script-level error (couldn't import, malformed expected.json)
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class Verdict(str, Enum):
    PASS = "PASS"
    DRIFT = "DRIFT"
    FAIL = "FAIL"
    EXCEPTION_OK = "EXCEPTION_OK"
    EXCEPTION_BAD = "EXCEPTION_BAD"
    EXCEPTION_DIFFERENT = "EXCEPTION_DIFFERENT"
    TIMEOUT = "TIMEOUT"
    SKIPPED = "SKIPPED"


@dataclass
class AssertionResult:
    """Outcome of one (locus, tool, key) assertion."""

    verdict: Verdict
    actual: Any
    expected: Any
    note: str = ""  # tolerance band, exception class mismatch, etc.


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_EXPECTED_JSON = SCRIPT_DIR / "benchmark_annotations.expected.json"
DEFAULT_LAST_RUN_JSON = SCRIPT_DIR / "benchmark_annotations.last_run.json"

PER_ORGANISM_WALLTIME_S = 120
INTER_ORGANISM_SLEEP_S = 2.0
DEFAULT_TOLERANCE_PCT = 25


def _resolve_path(actual: Any, dotted_path: str) -> Any:
    """Navigate a dict/list using dotted path notation.

    Supported forms (per spec §Dotted-path accessor):
      - ``key.subkey``                — dict navigation
      - ``list_key.len``              — list length
      - ``list_key.where_X_eq_Y.len`` — filter then length
      - ``list_key[N]``               — index accessor
      - ``key.keys.len``              — dict-keys length
      - ``key.startswith.prefix``     — string prefix predicate

    Raises ``KeyError`` if any segment is missing, ``TypeError`` if a
    segment's type is wrong for the operation (e.g. ``.len`` on a non-list).
    Callers catch and convert to a Verdict.FAIL on missing keys.

    >>> _resolve_path({"a": {"b": [1, 2, 3]}}, "a.b.len")
    3
    >>> _resolve_path({"a": [1, 2, 3]}, "a[0]")
    1
    >>> _resolve_path({"a": {"x": 1, "y": 2}}, "a.keys.len")
    2
    >>> _resolve_path({"rows": [{"status": "ok"}, {"status": "err"}, {"status": "ok"}]},
    ...               "rows.where_status_eq_ok.len")
    2
    >>> _resolve_path({"kegg_gene_id": "ath:AT1G01010"}, "kegg_gene_id.startswith.ath:")
    True
    >>> _resolve_path({"kegg_gene_id": "osa:Os01g0100100"}, "kegg_gene_id.startswith.ath:")
    False
    """
    segments = dotted_path.split(".")
    current: Any = actual
    i = 0
    while i < len(segments):
        seg = segments[i]
        if seg == "len":
            if isinstance(current, (list, tuple, str, dict)):
                return len(current)
            raise TypeError(f".len applied to non-sized type {type(current).__name__}")
        if seg == "keys":
            if isinstance(current, dict):
                current = list(current.keys())
                i += 1
                continue
            raise TypeError(f".keys applied to non-dict type {type(current).__name__}")
        if seg == "startswith":
            if i + 1 >= len(segments):
                raise ValueError(".startswith requires a prefix segment")
            prefix = ".".join(segments[i + 1 :])
            if isinstance(current, str):
                return current.startswith(prefix)
            raise TypeError(f".startswith applied to non-str type {type(current).__name__}")
        if seg.startswith("where_") and "_eq_" in seg:
            # where_FIELD_eq_VALUE.len — filter list of dicts where FIELD == VALUE
            body = seg[len("where_") :]
            field, _, value = body.partition("_eq_")
            if not isinstance(current, list):
                raise TypeError(f".{seg} applied to non-list type {type(current).__name__}")
            current = [
                item for item in current if isinstance(item, dict) and item.get(field) == value
            ]
            i += 1
            continue
        if "[" in seg and seg.endswith("]"):
            key, _, idx_part = seg.partition("[")
            idx = int(idx_part.rstrip("]"))
            if key:
                if not isinstance(current, dict):
                    raise TypeError(f"index on non-dict-then-list at {seg}")
                current = current[key]
            if not isinstance(current, list):
                raise TypeError(f"index applied to non-list type {type(current).__name__}")
            current = current[idx]
            i += 1
            continue
        # Plain dict-key segment
        if not isinstance(current, dict):
            raise TypeError(f"key '{seg}' on non-dict type {type(current).__name__}")
        if seg not in current:
            raise KeyError(seg)
        current = current[seg]
        i += 1
    return current


def _apply_assertion(
    expected_value: Any,
    actual: Any,
    *,
    tolerance_pct: float = DEFAULT_TOLERANCE_PCT,
    floor: float | None = None,
    ceiling: float | None = None,
    is_variable: bool = False,
) -> AssertionResult:
    """Compare ``actual`` to ``expected_value`` and emit an AssertionResult.

    stable_facts (``is_variable=False``):
      - exact equality → PASS
      - otherwise → FAIL

    variable_facts (``is_variable=True``):
      - within ``baseline * (1 ± tolerance_pct/100)`` → PASS
      - outside band but ``floor <= actual <= ceiling`` (or floor/ceiling unset
        on that side) → DRIFT
      - below floor / above ceiling → FAIL

    >>> r = _apply_assertion("ath", "ath")
    >>> r.verdict
    <Verdict.PASS: 'PASS'>
    >>> r = _apply_assertion("ath", "osa")
    >>> r.verdict
    <Verdict.FAIL: 'FAIL'>
    >>> r = _apply_assertion(30, 28, tolerance_pct=25, floor=10, is_variable=True)
    >>> r.verdict
    <Verdict.PASS: 'PASS'>
    >>> r = _apply_assertion(30, 21, tolerance_pct=25, floor=10, is_variable=True)
    >>> r.verdict
    <Verdict.DRIFT: 'DRIFT'>
    >>> r = _apply_assertion(30, 5, tolerance_pct=25, floor=10, is_variable=True)
    >>> r.verdict
    <Verdict.FAIL: 'FAIL'>
    """
    if not is_variable:
        if actual == expected_value:
            return AssertionResult(Verdict.PASS, actual, expected_value)
        return AssertionResult(
            Verdict.FAIL,
            actual,
            expected_value,
            note=f"stable fact: expected {expected_value!r}, got {actual!r}",
        )

    # variable_facts path
    baseline = float(expected_value)
    actual_f = float(actual)
    band_lo = baseline * (1 - tolerance_pct / 100.0)
    band_hi = baseline * (1 + tolerance_pct / 100.0)
    if band_lo <= actual_f <= band_hi:
        return AssertionResult(Verdict.PASS, actual, expected_value)

    # outside tolerance band — check floor/ceiling
    below_floor = floor is not None and actual_f < floor
    above_ceiling = ceiling is not None and actual_f > ceiling
    if below_floor or above_ceiling:
        bound = f"floor={floor}" if below_floor else f"ceiling={ceiling}"
        return AssertionResult(
            Verdict.FAIL,
            actual,
            expected_value,
            note=f"variable fact: actual {actual} outside {bound}",
        )
    return AssertionResult(
        Verdict.DRIFT,
        actual,
        expected_value,
        note=(
            f"variable fact: actual {actual} outside band "
            f"[{band_lo:.1f}, {band_hi:.1f}] (baseline {baseline}, ±{tolerance_pct}%)"
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="plant-genomics-mcp v1.6 benchmark — scientific validation + drift detector"
    )
    parser.add_argument(
        "--loci",
        type=str,
        default=None,
        help="Comma-separated locus_id subset (default: all in expected.json)",
    )
    parser.add_argument(
        "--tools",
        type=str,
        default=None,
        help="Comma-separated backend-module-name subset (e.g. 'kegg,ensembl_plants'). Default: all.",
    )
    parser.add_argument(
        "--include-blast",
        action="store_true",
        help="Opt-in BLAST probe (default: skip; BLAST queue ~5-10 min wall)",
    )
    parser.add_argument(
        "--regenerate-baseline-all",
        action="store_true",
        help="Re-capture variable_facts for every (locus, tool, key). Requires interactive confirmation.",
    )
    parser.add_argument(
        "--regenerate-baseline",
        nargs=2,
        metavar=("LOCUS_ID", "DOTTED_KEY"),
        default=None,
        help="Re-capture variable_facts for one specific (locus, tool.key) pair",
    )
    parser.add_argument(
        "--expected-json",
        type=Path,
        default=DEFAULT_EXPECTED_JSON,
        help=f"Path to expected.json baseline (default: {DEFAULT_EXPECTED_JSON})",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=DEFAULT_LAST_RUN_JSON,
        help=f"Path to last-run sidecar (default: {DEFAULT_LAST_RUN_JSON})",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="JSON sidecar only, no markdown stdout",
    )
    args = parser.parse_args(argv)

    # Task 1 lands a no-op main() — subsequent tasks wire in behavior.
    print("benchmark_annotations.py — scaffolding only (no probes yet)", file=sys.stderr)
    print(f"  --loci             = {args.loci}", file=sys.stderr)
    print(f"  --tools            = {args.tools}", file=sys.stderr)
    print(f"  --include-blast    = {args.include_blast}", file=sys.stderr)
    print(f"  --expected-json    = {args.expected_json}", file=sys.stderr)
    print(f"  --output-json      = {args.output_json}", file=sys.stderr)
    print(f"  --quiet            = {args.quiet}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
