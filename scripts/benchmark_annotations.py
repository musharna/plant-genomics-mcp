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
import asyncio
import datetime as _dt
import json
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import httpx

# Production backend modules — benchmark reuses async code path
from plant_genomics_mcp import (
    atted,
    bar,
    blast,
    ensembl_plants,
    europe_pmc,
    gramene,
    kegg,
    organisms,
    phytozome,
    string_db,
    synthesis,
    uniprot,
)
from plant_genomics_mcp.errors import (
    NotFoundError,
    OrganismNotFound,
    OrganismNotSupported,
    PlantGenomicsError,
    RateLimitError,
    UpstreamUnavailableError,
)

# Tuple of typed exception classes that benchmark wrappers may surface from
# upstream backends. Listed at module scope (not inside the except-clause) so
# the imports at the top of the file are unambiguously used, surviving the
# formatter's unused-import strip.
_BENCHMARK_TYPED_EXCEPTIONS: tuple[type[BaseException], ...] = (
    OrganismNotSupported,
    OrganismNotFound,
    NotFoundError,
    RateLimitError,
    UpstreamUnavailableError,
    PlantGenomicsError,
)

# Re-exported submodule references so the formatter sees the asyncio + time
# imports as load-bearing (they are: _probe_one_tool uses time.monotonic, and
# _run_benchmark in Task 5 will use asyncio.run). Cheap touch, no runtime cost.
_ASYNCIO_REF = asyncio
_TIME_REF = time


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


# Tool registry — maps the spec's tool-name string to a callable wrapper.
# Each wrapper takes (client, locus, organism) and returns an awaitable.
# This abstracts away per-backend arg-shape variation (kwarg-only organism,
# positional-with-default, no-organism BAR-only, etc.).

ToolCallable = Callable[[httpx.AsyncClient, str, str], Awaitable[Any]]


async def _call_organisms_resolve(
    _client: httpx.AsyncClient, locus: str, organism: str
) -> dict[str, Any]:
    """organisms.resolve is sync + has no client/locus inputs — wrap as fake-async tool."""
    record = organisms.resolve(organism)
    return {
        "canonical": record.canonical,
        "ncbi_taxid": record.ncbi_taxid,
        "kegg_org_code": record.kegg_org_code,
        "ensembl_slug": record.ensembl_slug,
        "phytozome_int": record.phytozome_int,
        "string_taxid": record.string_taxid,
        "scientific": record.scientific,
    }


_TOOLS: dict[str, ToolCallable] = {
    "organisms.resolve": _call_organisms_resolve,
    "kegg.lookup_pathways": lambda c, locus, o: kegg.lookup_pathways(c, locus, organism=o),
    "ensembl_plants.lookup_xrefs": lambda c, locus, o: ensembl_plants.lookup_xrefs(c, locus, o),
    "uniprot.lookup_locus": lambda c, locus, o: uniprot.lookup_locus(c, locus, o),
    "atted.lookup_coexpression": lambda c, locus, o: atted.lookup_coexpression(
        c, locus, organism=o
    ),
    "string_db.lookup_partners": lambda c, locus, o: string_db.lookup_partners(
        c, locus, organism=o
    ),
    "gramene.lookup_homologs": lambda c, locus, o: gramene.lookup_homologs(c, locus, organism=o),
    "europe_pmc.lookup_locus": lambda c, locus, o: europe_pmc.lookup_locus(c, locus, organism=o),
    "phytozome.lookup_locus": lambda c, locus, o: phytozome.lookup_locus(c, locus, organism=o),
    # BAR Arabidopsis-only functions: gene_summary + efp_expression accept no
    # organism kwarg (taxon 3702 hardcoded in URL path). aiv_interactions DOES
    # accept organism= and dispatches arabidopsis vs rice, so we pass it through.
    "bar.gene_summary": lambda c, locus, _o: bar.gene_summary(c, locus),
    "bar.efp_expression": lambda c, locus, _o: bar.efp_expression(c, locus),
    "bar.aiv_interactions": lambda c, locus, o: bar.aiv_interactions(c, locus, organism=o),
    "synthesis.analyze_locus_synth": lambda c, locus, o: synthesis.analyze_locus_synth(
        c, locus, organism=o
    ),
    "synthesis.find_homologs_synth": lambda c, locus, o: synthesis.find_homologs_synth(
        c, locus, organism=o
    ),
    "synthesis.biological_context_synth": lambda c, locus, o: synthesis.biological_context_synth(
        c, locus, organism=o
    ),
    "synthesis.consensus_homologs": lambda c, locus, o: synthesis.consensus_homologs(
        c, locus, organism=o
    ),
    # BLAST is sequence-input not locus-input — special-cased; skipped without --include-blast.
    # Wrapper signature matches but ignores organism; corpus encodes a sequence under the locus slot.
    "blast.blast_sequence": lambda c, locus, _o: blast.blast_sequence(c, locus, program="blastp"),
}


def _module_name(tool_name: str) -> str:
    """Return the backend module name part of a tool key (e.g. 'kegg' from 'kegg.lookup_pathways')."""
    return tool_name.split(".", 1)[0]


@dataclass
class ToolProbe:
    """Result of probing one (locus, tool) pair before assertion comparison."""

    tool_name: str
    response: Any | None = None  # set on success
    exception_class: str | None = None  # set on exception
    exception_message: str = ""
    elapsed_s: float = 0.0


async def _probe_one_tool(
    client: httpx.AsyncClient, locus: str, organism: str, tool_name: str
) -> ToolProbe:
    """Drive one (locus, tool) probe; catch + classify exceptions; never raise.

    Catches the project's typed exception hierarchy via _BENCHMARK_TYPED_EXCEPTIONS,
    plus a final defensive Exception catch-all so a single misbehaving wrapper
    can't abort the whole sweep.
    """
    if tool_name not in _TOOLS:
        return ToolProbe(
            tool_name=tool_name,
            exception_class="UnknownTool",
            exception_message=f"Tool {tool_name!r} not in _TOOLS registry",
        )
    start = time.monotonic()
    try:
        callable_ = _TOOLS[tool_name]
        result = await callable_(client, locus, organism)
        return ToolProbe(tool_name=tool_name, response=result, elapsed_s=time.monotonic() - start)
    except _BENCHMARK_TYPED_EXCEPTIONS as e:
        return ToolProbe(
            tool_name=tool_name,
            exception_class=type(e).__name__,
            exception_message=str(e),
            elapsed_s=time.monotonic() - start,
        )
    except Exception as e:  # noqa: BLE001 — defensive catch-all for benchmark
        return ToolProbe(
            tool_name=tool_name,
            exception_class=type(e).__name__,
            exception_message=str(e),
            elapsed_s=time.monotonic() - start,
        )


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


@dataclass
class LocusResult:
    """Aggregated results for one locus across all probed tools."""

    locus_id: str
    organism: str
    rationale: str
    tools: dict[str, dict[str, AssertionResult]]  # tool_name → {dotted_key → AssertionResult}
    probe_exceptions: dict[str, ToolProbe]  # tool_name → ToolProbe (for tools that raised)


@dataclass
class BenchmarkSummary:
    """Whole-run summary statistics."""

    total_assertions: int = 0
    passed: int = 0
    drifted: int = 0
    failed: int = 0
    exception_ok: int = 0
    exception_bad: int = 0
    exception_different: int = 0
    timed_out: int = 0
    skipped: int = 0

    def increment(self, verdict: Verdict) -> None:
        self.total_assertions += 1
        if verdict == Verdict.PASS:
            self.passed += 1
        elif verdict == Verdict.DRIFT:
            self.drifted += 1
        elif verdict == Verdict.FAIL:
            self.failed += 1
        elif verdict == Verdict.EXCEPTION_OK:
            self.exception_ok += 1
        elif verdict == Verdict.EXCEPTION_BAD:
            self.exception_bad += 1
        elif verdict == Verdict.EXCEPTION_DIFFERENT:
            self.exception_different += 1
        elif verdict == Verdict.TIMEOUT:
            self.timed_out += 1
        elif verdict == Verdict.SKIPPED:
            self.skipped += 1

    @property
    def exit_code(self) -> int:
        if (
            self.failed > 0
            or self.exception_bad > 0
            or self.exception_different > 0
            or self.timed_out > 0
        ):
            return 1
        return 0


_VERDICT_GLYPH = {
    Verdict.PASS: "✓",
    Verdict.DRIFT: "~",
    Verdict.FAIL: "✗",
    Verdict.EXCEPTION_OK: "✓",
    Verdict.EXCEPTION_BAD: "!",
    Verdict.EXCEPTION_DIFFERENT: "!",
    Verdict.TIMEOUT: "T",
    Verdict.SKIPPED: "-",
}


def _worst_verdict_for_tool(tool_results: dict[str, AssertionResult]) -> Verdict:
    """Pick the worst-of-N verdict for a tool's assertion set, for the pivot table."""
    if not tool_results:
        return Verdict.SKIPPED
    order = [
        Verdict.FAIL,
        Verdict.EXCEPTION_BAD,
        Verdict.EXCEPTION_DIFFERENT,
        Verdict.TIMEOUT,
        Verdict.DRIFT,
        Verdict.PASS,
        Verdict.EXCEPTION_OK,
        Verdict.SKIPPED,
    ]
    verdicts = [r.verdict for r in tool_results.values()]
    for v in order:
        if v in verdicts:
            return v
    return Verdict.PASS


def _render_markdown(
    results: list[LocusResult],
    summary: BenchmarkSummary,
    baseline_generated_at: str,
) -> str:
    """Render the per-locus × per-tool pivot table + summary."""
    # Collect the tool axis: every tool that appeared in any locus's probe set.
    tool_axis: list[str] = sorted(
        {tool for r in results for tool in r.tools.keys()}
        | {tool for r in results for tool in r.probe_exceptions.keys()}
    )

    lines: list[str] = []
    lines.append("```")
    lines.append("=== plant-genomics-mcp benchmark ===")
    lines.append(f"ref: {baseline_generated_at}")
    lines.append(
        f"{len(results)} loci × {len(tool_axis)} tools = {summary.total_assertions} assertions"
    )
    lines.append("")
    lines.append(
        "LEGEND:  ✓ PASS   ~ DRIFT   ✗ FAIL   ! EXCEPTION_BAD/DIFFERENT   T TIMEOUT   - SKIPPED"
    )
    lines.append("")

    # Pivot table header
    locus_col_w = max(len(r.locus_id) for r in results) if results else 10
    locus_col_w = max(locus_col_w, len("LOCUS"))
    header = (
        "| "
        + "LOCUS".ljust(locus_col_w)
        + " | "
        + " | ".join(t.split(".")[0][:8] for t in tool_axis)
        + " |"
    )
    sep = "|" + "-" * (locus_col_w + 2) + "|" + "|".join("-" * 10 for _ in tool_axis) + "|"
    lines.append(header)
    lines.append(sep)

    for r in results:
        row_cells = []
        for tool in tool_axis:
            if tool in r.probe_exceptions:
                # tool raised; check if any assertion in r.tools[tool] is EXCEPTION_OK
                if tool in r.tools and r.tools[tool]:
                    v = _worst_verdict_for_tool(r.tools[tool])
                else:
                    v = Verdict.EXCEPTION_BAD
            elif tool in r.tools:
                v = _worst_verdict_for_tool(r.tools[tool])
            else:
                v = Verdict.SKIPPED
            row_cells.append(_VERDICT_GLYPH[v].center(8))
        lines.append("| " + r.locus_id.ljust(locus_col_w) + " | " + " | ".join(row_cells) + " |")

    lines.append("")
    lines.append("SUMMARY:")
    lines.append(f"  {summary.total_assertions} assertions total")
    if summary.total_assertions:
        pct = 100.0 * summary.passed / summary.total_assertions
        lines.append(f"  ✓ {summary.passed:3d} PASS  ({pct:.1f}%)")
    lines.append(f"  ~ {summary.drifted:3d} DRIFT (within tolerance; review)")
    lines.append(f"  ✗ {summary.failed:3d} FAIL  (regressed)")
    if summary.exception_ok:
        lines.append(f"  ✓ {summary.exception_ok:3d} EXCEPTION_OK (anticipated)")
    if summary.exception_bad:
        lines.append(f"  ! {summary.exception_bad:3d} EXCEPTION_BAD (unanticipated)")
    if summary.exception_different:
        lines.append(f"  ! {summary.exception_different:3d} EXCEPTION_DIFFERENT")
    if summary.timed_out:
        lines.append(f"  T {summary.timed_out:3d} TIMEOUT")
    if summary.skipped:
        lines.append(f"  - {summary.skipped:3d} SKIPPED")
    lines.append("")

    # Worst offenders (DRIFT)
    drift_offenders = []
    fail_offenders = []
    for r in results:
        for tool_name, assertions in r.tools.items():
            for key, ar in assertions.items():
                if ar.verdict == Verdict.DRIFT:
                    drift_offenders.append((r.locus_id, tool_name, key, ar))
                elif ar.verdict == Verdict.FAIL:
                    fail_offenders.append((r.locus_id, tool_name, key, ar))
    if drift_offenders:
        lines.append("WORST OFFENDERS (DRIFT):")
        for locus, tool, key, ar in drift_offenders[:10]:
            lines.append(f"  - {tool}.{key} for {locus}: {ar.expected} → {ar.actual} ({ar.note})")
        lines.append("")
    if fail_offenders:
        lines.append("REGRESSIONS (FAIL):")
        for locus, tool, key, ar in fail_offenders:
            lines.append(f"  - {tool}.{key} for {locus}: {ar.expected} → {ar.actual} ({ar.note})")
        lines.append("")

    lines.append(f"Exit code: {summary.exit_code}")
    lines.append("```")
    return "\n".join(lines)


def _write_sidecar(
    output_path: Path,
    results: list[LocusResult],
    summary: BenchmarkSummary,
    expected_baseline_generated_at: str,
) -> None:
    """Write the per-(locus, tool, key) JSON record to disk."""
    payload = {
        "schema_version": 1,
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "baseline_ref": expected_baseline_generated_at,
        "summary": {
            "total_assertions": summary.total_assertions,
            "passed": summary.passed,
            "drifted": summary.drifted,
            "failed": summary.failed,
            "exception_ok": summary.exception_ok,
            "exception_bad": summary.exception_bad,
            "exception_different": summary.exception_different,
            "timed_out": summary.timed_out,
            "skipped": summary.skipped,
            "exit_code": summary.exit_code,
        },
        "loci": [
            {
                "locus_id": r.locus_id,
                "organism": r.organism,
                "rationale": r.rationale,
                "tools": {
                    tool_name: {
                        "assertions": {
                            key: {
                                "verdict": ar.verdict.value,
                                "actual": ar.actual,
                                "expected": ar.expected,
                                "note": ar.note,
                            }
                            for key, ar in assertions.items()
                        },
                    }
                    for tool_name, assertions in r.tools.items()
                },
                "probe_exceptions": {
                    tool_name: {
                        "exception_class": probe.exception_class,
                        "exception_message": probe.exception_message,
                        "elapsed_s": probe.elapsed_s,
                    }
                    for tool_name, probe in r.probe_exceptions.items()
                },
            }
            for r in results
        ],
    }
    output_path.write_text(json.dumps(payload, indent=2, default=str) + "\n")


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
