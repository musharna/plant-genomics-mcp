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
import signal
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
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
    # Gramene takes homology_type, NOT organism — organism is encoded in the locus stem.
    "gramene.lookup_homologs": lambda c, locus, _o: gramene.lookup_homologs(c, locus),
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


class _WalltimeError(RuntimeError):
    pass


def _alarm_handler(_signum: int, _frame: Any) -> None:  # noqa: ANN401
    raise _WalltimeError(f"per-organism walltime exceeded ({PER_ORGANISM_WALLTIME_S}s)")


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


# ---- cross-source consistency invariants (v1.7 seed 2) ---------------------
# Assertions that check agreement ACROSS backends for one locus, beyond the
# per-tool stable/variable facts. Reuse-only: they read the tool responses the
# benchmark already collected in this run (no new fetching). Each invariant
# gates itself via applies(); a non-applicable invariant is SKIPPED, not FAIL.
# Design: docs/superpowers/specs/2026-05-29-cross-source-invariants-design.md


@dataclass(frozen=True)
class Invariant:
    name: str
    applies: Callable[[dict[str, Any], dict[str, Any]], bool]
    check: Callable[[dict[str, Any]], tuple[bool, str]]


def _inv_kegg_entrez_in_ensembl_xrefs_applies(
    record: dict[str, Any], responses: dict[str, Any]
) -> bool:
    # Bridge organisms only — Arabidopsis uses the native ath: path (no Entrez).
    if record.get("organism") == "arabidopsis_thaliana":
        return False
    kegg = responses.get("kegg.lookup_pathways")
    xrefs = responses.get("ensembl_plants.lookup_xrefs")
    return bool(kegg) and kegg.get("entrez_gene_id") is not None and bool(xrefs)


def _inv_kegg_entrez_in_ensembl_xrefs_check(responses: dict[str, Any]) -> tuple[bool, str]:
    entrez = str(responses["kegg.lookup_pathways"]["entrez_gene_id"])
    attested = [
        str(x)
        for x in responses["ensembl_plants.lookup_xrefs"].get("by_db", {}).get("EntrezGene", [])
    ]
    ok = entrez in attested
    rel = "in" if ok else "NOT in"
    return ok, f"KEGG bridge Entrez {entrez!r} {rel} Ensembl /xrefs EntrezGene {attested!r}"


def _inv_kegg_orgcode_matches_resolver_applies(
    record: dict[str, Any], responses: dict[str, Any]
) -> bool:
    return "kegg.lookup_pathways" in responses and "organisms.resolve" in responses


def _inv_kegg_orgcode_matches_resolver_check(responses: dict[str, Any]) -> tuple[bool, str]:
    prefix = str(responses["kegg.lookup_pathways"].get("kegg_gene_id", "")).split(":", 1)[0]
    code = responses["organisms.resolve"].get("kegg_org_code")
    ok = prefix == code
    rel = "==" if ok else "!="
    return ok, f"kegg_gene_id org-code prefix {prefix!r} {rel} resolver kegg_org_code {code!r}"


INVARIANTS: list[Invariant] = [
    Invariant(
        "kegg_entrez_in_ensembl_xrefs",
        _inv_kegg_entrez_in_ensembl_xrefs_applies,
        _inv_kegg_entrez_in_ensembl_xrefs_check,
    ),
    Invariant(
        "kegg_orgcode_matches_resolver",
        _inv_kegg_orgcode_matches_resolver_applies,
        _inv_kegg_orgcode_matches_resolver_check,
    ),
]


def _check_invariants(
    locus_record: dict[str, Any], responses: dict[str, Any]
) -> dict[str, AssertionResult]:
    """Run every cross-source invariant over one locus's collected responses.

    SKIPPED when an invariant doesn't apply (missing/exception tool, or organism
    out of scope); PASS/FAIL otherwise. ``responses`` is {tool_name: response}
    for the tools that returned successfully for this locus.
    """
    out: dict[str, AssertionResult] = {}
    for inv in INVARIANTS:
        if not inv.applies(locus_record, responses):
            out[inv.name] = AssertionResult(
                Verdict.SKIPPED, None, None, note="not applicable for this locus"
            )
            continue
        ok, detail = inv.check(responses)
        out[inv.name] = AssertionResult(
            Verdict.PASS if ok else Verdict.FAIL, detail, None, note=detail
        )
    return out


@dataclass
class LocusResult:
    """Aggregated results for one locus across all probed tools."""

    locus_id: str
    organism: str
    rationale: str
    tools: dict[str, dict[str, AssertionResult]]  # tool_name → {dotted_key → AssertionResult}
    probe_exceptions: dict[str, ToolProbe]  # tool_name → ToolProbe (for tools that raised)
    invariants: dict[str, AssertionResult] = field(
        default_factory=dict
    )  # cross-source invariant name → result


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

    # Cross-source invariants (v1.7 seed 2). Show every applied (PASS/FAIL)
    # invariant; SKIPPED (not-applicable) ones are counted but not listed.
    applied = [
        (r.locus_id, name, ar)
        for r in results
        for name, ar in r.invariants.items()
        if ar.verdict != Verdict.SKIPPED
    ]
    n_skipped_inv = sum(
        1 for r in results for ar in r.invariants.values() if ar.verdict == Verdict.SKIPPED
    )
    if applied or n_skipped_inv:
        lines.append(
            f"CROSS-SOURCE INVARIANTS ({len(applied)} checked, {n_skipped_inv} skipped as N/A):"
        )
        for locus_id, name, ar in sorted(applied):
            lines.append(f"  {_VERDICT_GLYPH[ar.verdict]} {locus_id}  {name}: {ar.note}")
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
                "invariants": {
                    name: {"verdict": ar.verdict.value, "detail": ar.note}
                    for name, ar in r.invariants.items()
                },
            }
            for r in results
        ],
    }
    output_path.write_text(json.dumps(payload, indent=2, default=str) + "\n")


def _stable_key_paths_for_response(response: Any) -> dict[str, int]:
    """Capture a canonical set of count-derived metrics from a tool's response.

    Default capture set:
      - For every top-level list value: ``<key>.len``
      - For every top-level dict value with .keys(): ``<key>.keys.len``
      - For every nested list one level deep: ``<key>.<subkey>.len``

    Operator can hand-edit captured baselines to add finer-grained keys before
    commit. The auto-capture is a starting point, not a final corpus.
    """
    captured: dict[str, int] = {}
    if not isinstance(response, dict):
        return captured
    for key, value in response.items():
        if isinstance(value, list):
            captured[f"{key}.len"] = len(value)
        elif isinstance(value, dict):
            captured[f"{key}.keys.len"] = len(value)
            for subkey, subvalue in value.items():
                if isinstance(subvalue, list):
                    captured[f"{key}.{subkey}.len"] = len(subvalue)
    return captured


async def _capture_baseline(expected: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    """Walk every locus × tool in expected.json, probe live, capture variable_facts."""
    new_expected = json.loads(json.dumps(expected))  # deep copy
    new_expected["generated_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
    signal.signal(signal.SIGALRM, _alarm_handler)
    async with httpx.AsyncClient(timeout=60) as client:
        for locus_record in new_expected["loci"]:
            print(
                f"... capturing {locus_record['locus_id']}",
                file=sys.stderr,
                flush=True,
            )
            signal.alarm(PER_ORGANISM_WALLTIME_S)
            try:
                for tool_name, tool_assertions in locus_record["tools"].items():
                    if "expects_exception" in tool_assertions:
                        continue
                    if tool_name == "blast.blast_sequence" and not args.include_blast:
                        continue
                    probe = await _probe_one_tool(
                        client,
                        locus_record["locus_id"],
                        locus_record["organism"],
                        tool_name,
                    )
                    if probe.exception_class is not None:
                        print(
                            f"  ! {tool_name} raised {probe.exception_class}: {probe.exception_message[:100]}",
                            file=sys.stderr,
                        )
                        continue
                    auto_captured = _stable_key_paths_for_response(probe.response)
                    tool_assertions["variable_facts"] = {
                        path: {
                            "baseline": count,
                            "tolerance_pct": DEFAULT_TOLERANCE_PCT,
                            "floor": max(0, int(count * 0.25)),
                        }
                        for path, count in auto_captured.items()
                    }
            except _WalltimeError:
                print(f"  T walltime for {locus_record['locus_id']}", file=sys.stderr)
            finally:
                signal.alarm(0)
            time.sleep(INTER_ORGANISM_SLEEP_S)
    return new_expected


async def _capture_baseline_one(
    expected: dict[str, Any],
    target_locus: str,
    target_key: str,
    args: argparse.Namespace,  # noqa: ARG001 — kept for symmetry with --regenerate-baseline-all
) -> dict[str, Any]:
    """Re-capture variable_facts for one specific (locus, tool.key) pair."""
    new_expected = json.loads(json.dumps(expected))
    parts = target_key.split(".")
    if len(parts) < 3:
        raise ValueError(
            f"target_key {target_key!r} too short — expected 'module.function.key.subkey'"
        )
    tool_name = f"{parts[0]}.{parts[1]}"
    dotted_path = ".".join(parts[2:])

    for locus_record in new_expected["loci"]:
        if locus_record["locus_id"] != target_locus:
            continue
        if tool_name not in locus_record["tools"]:
            raise KeyError(f"tool {tool_name!r} not in locus {target_locus!r}")
        async with httpx.AsyncClient(timeout=60) as client:
            probe = await _probe_one_tool(client, target_locus, locus_record["organism"], tool_name)
            if probe.exception_class is not None:
                raise RuntimeError(
                    f"probe raised {probe.exception_class}: {probe.exception_message}"
                )
            actual = _resolve_path(probe.response, dotted_path)
            vf = locus_record["tools"][tool_name].setdefault("variable_facts", {})
            vf[dotted_path] = {
                "baseline": actual,
                "tolerance_pct": DEFAULT_TOLERANCE_PCT,
                "floor": max(0, int(actual * 0.25)) if isinstance(actual, (int, float)) else None,
            }
        break
    new_expected["generated_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
    return new_expected


async def _process_locus(
    client: httpx.AsyncClient,
    locus_record: dict[str, Any],
    tools_filter: set[str] | None,
    include_blast: bool,
    summary: BenchmarkSummary,
) -> LocusResult:
    """Probe every tool for one locus, apply assertions, accumulate into LocusResult."""
    locus_id = locus_record["locus_id"]
    organism = locus_record["organism"]
    rationale = locus_record.get("rationale", "")
    tools_spec = locus_record.get("tools", {})

    result = LocusResult(
        locus_id=locus_id,
        organism=organism,
        rationale=rationale,
        tools={},
        probe_exceptions={},
    )
    # Successful tool responses, collected for the cross-source invariant
    # post-pass (reuse-only — no extra fetching). {tool_name: raw response}.
    responses: dict[str, Any] = {}

    for tool_name, tool_assertions in tools_spec.items():
        # Tool filtering by backend module name
        if tools_filter is not None and _module_name(tool_name) not in tools_filter:
            continue
        if tool_name == "blast.blast_sequence" and not include_blast:
            result.tools[tool_name] = {
                "_skipped": AssertionResult(
                    Verdict.SKIPPED, None, None, note="gated by --include-blast"
                )
            }
            summary.increment(Verdict.SKIPPED)
            continue

        # Per-locus per-tool skip flag from expected.json
        if tool_assertions.get("skip_for_this_locus"):
            result.tools[tool_name] = {
                "_skipped": AssertionResult(
                    Verdict.SKIPPED, None, None, note="expected.json skip_for_this_locus"
                )
            }
            summary.increment(Verdict.SKIPPED)
            continue

        probe = await _probe_one_tool(client, locus_id, organism, tool_name)

        # Expected-exception path
        expected_exception = tool_assertions.get("expects_exception")
        if expected_exception:
            if probe.exception_class is None:
                result.tools[tool_name] = {
                    "_exception": AssertionResult(
                        Verdict.FAIL,
                        None,
                        expected_exception,
                        note=f"expected {expected_exception} but tool returned a response",
                    )
                }
                summary.increment(Verdict.FAIL)
            elif probe.exception_class == expected_exception:
                result.tools[tool_name] = {
                    "_exception": AssertionResult(
                        Verdict.EXCEPTION_OK, probe.exception_class, expected_exception
                    )
                }
                summary.increment(Verdict.EXCEPTION_OK)
            else:
                result.tools[tool_name] = {
                    "_exception": AssertionResult(
                        Verdict.EXCEPTION_DIFFERENT,
                        probe.exception_class,
                        expected_exception,
                        note=f"expected {expected_exception}, got {probe.exception_class}: {probe.exception_message}",
                    )
                }
                summary.increment(Verdict.EXCEPTION_DIFFERENT)
            continue

        # Non-exception path — but tool unexpectedly raised
        if probe.exception_class is not None:
            result.probe_exceptions[tool_name] = probe
            result.tools[tool_name] = {
                "_exception": AssertionResult(
                    Verdict.EXCEPTION_BAD,
                    probe.exception_class,
                    None,
                    note=f"unanticipated {probe.exception_class}: {probe.exception_message}",
                )
            }
            summary.increment(Verdict.EXCEPTION_BAD)
            continue

        # Tool returned successfully — retain its response for the cross-source
        # invariant post-pass below.
        responses[tool_name] = probe.response

        # Apply each assertion
        tool_result: dict[str, AssertionResult] = {}
        for facts_kind, is_var in [("stable_facts", False), ("variable_facts", True)]:
            for dotted_path, expected_spec in tool_assertions.get(facts_kind, {}).items():
                if is_var:
                    if not isinstance(expected_spec, dict) or "baseline" not in expected_spec:
                        tool_result[dotted_path] = AssertionResult(
                            Verdict.FAIL,
                            None,
                            expected_spec,
                            note="malformed variable_facts entry: needs {baseline, tolerance_pct?, floor?, ceiling?}",
                        )
                        summary.increment(Verdict.FAIL)
                        continue
                    baseline = expected_spec["baseline"]
                    tolerance_pct = expected_spec.get("tolerance_pct", DEFAULT_TOLERANCE_PCT)
                    floor = expected_spec.get("floor")
                    ceiling = expected_spec.get("ceiling")
                else:
                    baseline = expected_spec
                    tolerance_pct = 0
                    floor = None
                    ceiling = None

                try:
                    actual = _resolve_path(probe.response, dotted_path)
                except (KeyError, TypeError, IndexError) as e:
                    tool_result[dotted_path] = AssertionResult(
                        Verdict.FAIL,
                        None,
                        baseline,
                        note=f"could not resolve path {dotted_path!r}: {type(e).__name__}: {e}",
                    )
                    summary.increment(Verdict.FAIL)
                    continue
                ar = _apply_assertion(
                    baseline,
                    actual,
                    tolerance_pct=tolerance_pct,
                    floor=floor,
                    ceiling=ceiling,
                    is_variable=is_var,
                )
                tool_result[dotted_path] = ar
                summary.increment(ar.verdict)
        result.tools[tool_name] = tool_result

    # Cross-source invariant post-pass over the responses collected above.
    result.invariants = _check_invariants(locus_record, responses)
    for ar in result.invariants.values():
        summary.increment(ar.verdict)

    return result


async def _run_benchmark(
    args: argparse.Namespace, expected: dict[str, Any]
) -> tuple[list[LocusResult], BenchmarkSummary]:
    """Drive the whole benchmark — per-organism walltime + 2s sleep + shared httpx client."""
    loci_filter: set[str] | None = None
    if args.loci:
        loci_filter = {s.strip() for s in args.loci.split(",") if s.strip()}
    tools_filter: set[str] | None = None
    if args.tools:
        tools_filter = {s.strip() for s in args.tools.split(",") if s.strip()}

    targets = [
        locus_record
        for locus_record in expected["loci"]
        if loci_filter is None or locus_record["locus_id"] in loci_filter
    ]

    signal.signal(signal.SIGALRM, _alarm_handler)
    summary = BenchmarkSummary()
    results: list[LocusResult] = []

    async with httpx.AsyncClient(timeout=60) as client:
        for i, locus_record in enumerate(targets):
            print(
                f"... probing {locus_record['locus_id']} ({locus_record['organism']})",
                file=sys.stderr,
                flush=True,
            )
            signal.alarm(PER_ORGANISM_WALLTIME_S)
            try:
                result = await _process_locus(
                    client, locus_record, tools_filter, args.include_blast, summary
                )
            except _WalltimeError as e:
                result = LocusResult(
                    locus_id=locus_record["locus_id"],
                    organism=locus_record["organism"],
                    rationale=locus_record.get("rationale", ""),
                    tools={
                        "_walltime": {
                            "_": AssertionResult(Verdict.TIMEOUT, None, None, note=str(e))
                        }
                    },
                    probe_exceptions={},
                )
                summary.increment(Verdict.TIMEOUT)
            finally:
                signal.alarm(0)
            results.append(result)
            if i < len(targets) - 1:
                time.sleep(INTER_ORGANISM_SLEEP_S)

    return results, summary


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

    if not args.expected_json.exists():
        print(f"ERROR: expected.json not found at {args.expected_json}", file=sys.stderr)
        print(
            "  → first-run? Author scripts/benchmark_annotations.expected.json manually (stable_facts),",
            file=sys.stderr,
        )
        print(
            "     then run --regenerate-baseline-all to capture variable_facts.",
            file=sys.stderr,
        )
        return 2
    try:
        expected = json.loads(args.expected_json.read_text())
    except json.JSONDecodeError as e:
        print(f"ERROR: malformed expected.json: {e}", file=sys.stderr)
        return 2

    if args.regenerate_baseline_all:
        prompt = (
            "About to overwrite variable_facts in the expected.json baseline "
            "for all loci. Type 'regenerate' to confirm: "
        )
        try:
            answer = input(prompt).strip()
        except EOFError:
            answer = ""
        if answer != "regenerate":
            print("aborted (didn't type 'regenerate')", file=sys.stderr)
            return 2
        new_expected = asyncio.run(_capture_baseline(expected, args))
        args.expected_json.write_text(json.dumps(new_expected, indent=2) + "\n")
        print(f"wrote new baseline to {args.expected_json}", file=sys.stderr)
        return 0

    if args.regenerate_baseline:
        target_locus, target_key = args.regenerate_baseline
        new_expected = asyncio.run(_capture_baseline_one(expected, target_locus, target_key, args))
        args.expected_json.write_text(json.dumps(new_expected, indent=2) + "\n")
        print(f"wrote per-key baseline update to {args.expected_json}", file=sys.stderr)
        return 0

    results, summary = asyncio.run(_run_benchmark(args, expected))

    if not args.quiet:
        print(_render_markdown(results, summary, expected.get("generated_at", "<unknown>")))
    _write_sidecar(args.output_json, results, summary, expected.get("generated_at", "<unknown>"))
    print(f"wrote {args.output_json}", file=sys.stderr)
    return summary.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
