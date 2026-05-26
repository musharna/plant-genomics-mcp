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
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_EXPECTED_JSON = SCRIPT_DIR / "benchmark_annotations.expected.json"
DEFAULT_LAST_RUN_JSON = SCRIPT_DIR / "benchmark_annotations.last_run.json"

PER_ORGANISM_WALLTIME_S = 120
INTER_ORGANISM_SLEEP_S = 2.0
DEFAULT_TOLERANCE_PCT = 25


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
