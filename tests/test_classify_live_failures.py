"""Tests for scripts/classify_live_failures.py.

The failure messages are verbatim from a full live pass on 2026-09-29, the
night the classifier was written for: four regression-class failures (a
protein one residue long, Phytozome's BioMart answering 404) among twelve
upstream-side ones (Ensembl 500, pytest-timeout on NCBI BLAST, a synthesis
envelope whose Ensembl step failed). The script is run as the workflow runs
it, a subprocess reading a JUnit file.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from xml.sax.saxutils import quoteattr

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "classify_live_failures.py"

ENSEMBL_500 = (
    "plant_genomics_mcp.errors.UpstreamUnavailableError: [UpstreamUnavailableError] "
    "Ensembl Plants /lookup/id/AT1G01010 exhausted 3 retries (last HTTP 500)"
)
BLAST_TIMEOUT = "Failed: Timeout (>240.0s) from pytest-timeout."
SYNTH_STEP = (
    "AssertionError: assert None is not None  +  where None = SynthesisEnvelope("
    "tool='analyze_locus_synth', steps=[StepRow(step=1, tool='ensembl_plants_lookup_locus', "
    "status='error', result=None, error='[UpstreamUnavailableError] Ensembl Plants "
    "/lookup/id/AT1G01010 exhausted 3 retries (last HTTP 500)')])"
)
RATE_LIMIT = "[RateLimitError] UniProt exhausted 3 retries (HTTP 429)"
PHYTOZOME_404 = (
    "plant_genomics_mcp.errors.NotFoundError: [NotFoundError] Phytozome BioMart → HTTP 404: "
    '<!DOCTYPE HTML PUBLIC "-//IETF//DTD HTML 2.0//EN"> <html><head> <title>404 Not Found</title>'
)
WRONG_LENGTH = "assert 430 == 429"
GATE_SKIP = "set PLANT_GENOMICS_MCP_LIVE=1 to hit rest.ensembl.org"
GRAMENE_SKIP = "live"  # tests/test_gramene.py's reason: it does not name the variable
# The skip verify_genes takes when a direct probe finds its backend down
# (tests/_live_outage.py builds it as "upstream outage on N calls: ...").
OUTAGE_SKIP = "upstream outage on 3 calls: PANTHER geneinfo HTTP 503"


def _report(
    tmp_path: Path,
    cases: Sequence[tuple[str, str | None, str]],
    name: str = "r.xml",
    gate: str | None = "1",
) -> Path:
    """A JUnit file as pytest writes it: each case is (test name, outcome
    element or None, message); ``gate`` is the property tests/conftest.py
    records, None for a report without it."""
    rows = []
    for test, outcome, message in cases:
        inner = f"<{outcome} message={quoteattr(message)}/>" if outcome else ""
        rows.append(f'<testcase classname="tests.test_live" name="{test}">{inner}</testcase>')
    props = (
        ""
        if gate is None
        else f'<properties><property name="PLANT_GENOMICS_MCP_LIVE" value={quoteattr(gate)} />'
        "</properties>"
    )
    path = tmp_path / name
    path.write_text(
        f'<?xml version="1.0"?><testsuites><testsuite name="pytest">{props}{"".join(rows)}'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )
    return path


def _run(
    report: Path, pytest_exit: int, summary: Path | None = None
) -> subprocess.CompletedProcess:
    args = [sys.executable, str(SCRIPT), str(report), str(pytest_exit)]
    if summary is not None:
        args.append(str(summary))
    return subprocess.run(args, capture_output=True, text=True, check=False)  # nosec B603 - fixed argv built here, no shell


UPSTREAM = [
    ("test_ensembl", "failure", ENSEMBL_500),
    ("test_blast", "failure", BLAST_TIMEOUT),
    ("test_synth", "failure", SYNTH_STEP),
    ("test_uniprot", "failure", RATE_LIMIT),
]
REGRESSIONS = [
    ("test_phytozome", "failure", PHYTOZOME_404),
    ("test_protein", "failure", WRONG_LENGTH),
    ("test_fixture", "error", "fixture 'client' not found"),
]


def test_a_regression_fails_the_run_and_upstream_failures_alone_do_not(tmp_path: Path) -> None:
    """Each class from a real night, run by the script: the regressions fail
    it and are listed first. Positive control, same classifier: the same
    upstream failures with the regressions gone pass, still listed."""
    summary = tmp_path / "summary.md"
    red = _run(_report(tmp_path, [("test_ok", None, ""), *UPSTREAM, *REGRESSIONS]), 1, summary)
    assert red.returncode == 1, red.stdout + red.stderr
    assert "8 test cases; 3 regression(s), 4 upstream-side failure(s), 0 skipped." in red.stdout
    lines = [ln for ln in red.stdout.splitlines() if ln.startswith("| ") and "`" in ln]
    assert [ln.split(" | ")[0] for ln in lines] == ["| regression"] * 3 + ["| upstream"] * 4
    assert "`tests.test_live::test_phytozome`" in lines[0]
    assert "`tests.test_live::test_synth`" in red.stdout
    assert summary.read_text(encoding="utf-8").strip() == red.stdout.strip()

    green = _run(_report(tmp_path, [("test_ok", None, ""), *UPSTREAM], "g.xml"), 1)
    assert green.returncode == 0, green.stdout + green.stderr
    assert "0 regression(s), 4 upstream-side failure(s), 0 skipped." in green.stdout
    assert "`tests.test_live::test_blast`" in green.stdout


def test_an_unusable_run_fails_whatever_its_failures(tmp_path: Path) -> None:
    """A run that did not complete, never set the live gate, or whose report
    disagrees with pytest cannot be classed, so it fails even with nothing
    but passes. Positive control: the clean run of the same shape passes."""
    passed = [("test_ok", None, "")]
    clean = _report(tmp_path, passed, "clean.xml")
    assert _run(clean, 0).returncode == 0

    cases = {
        "interrupted": (clean, 2, "pytest exited 2"),
        "no report": (tmp_path / "missing.xml", 0, "no JUnit report"),
        "empty": (_report(tmp_path, [], "empty.xml"), 0, "holds no test cases"),
        "gate unset": (
            _report(tmp_path, passed, "gate.xml", gate=""),
            0,
            "PLANT_GENOMICS_MCP_LIVE was '' in the run: the live gate was not set",
        ),
        "gate unrecorded": (
            _report(tmp_path, passed, "nogate.xml", gate=None),
            0,
            "the report does not record PLANT_GENOMICS_MCP_LIVE",
        ),
        "0 with failures": (
            _report(tmp_path, [*passed, UPSTREAM[0]], "zero.xml"),
            0,
            "exited 0 but the report lists 1 failure",
        ),
        "1 without": (clean, 1, "exited 1 but the report lists no failure"),
    }
    for label, (report, code, why) in cases.items():
        out = _run(report, code)
        assert out.returncode == 1, (label, out.stdout, out.stderr)
        assert "### Live run unusable" in out.stdout and why in out.stdout, (label, out.stdout)


def test_the_gate_is_the_recorded_value_not_the_skip_wording(tmp_path: Path) -> None:
    """Gramene's live tests skip with the reason "live", which names no
    variable: a run without the gate that skipped only those is refused on
    the recorded value. Positive control: with the gate recorded as "1", a
    skip whose reason names the variable does not make the run unusable."""
    gramene_only = [("test_ok", None, ""), ("test_gramene", "skipped", GRAMENE_SKIP)]
    out = _run(_report(tmp_path, gramene_only, "g.xml", gate=""), 0)
    assert out.returncode == 1 and "the live gate was not set" in out.stdout, out.stdout

    named = [("test_ok", None, ""), ("test_live", "skipped", GATE_SKIP)]
    ok = _run(_report(tmp_path, named, "n.xml", gate="1"), 0)
    assert ok.returncode == 0, ok.stdout


def test_every_skip_is_listed_by_reason(tmp_path: Path) -> None:
    """A test that skipped itself on a probed outage is in the summary with
    its reason, counted, and does not fail the run; a passing run with no
    skips has no skip table."""
    cases = [
        ("test_ok", None, ""),
        ("test_verify_genes", "skipped", OUTAGE_SKIP),
        ("test_smoke_a", "skipped", "set PLANT_GENOMICS_MCP_STDIO_SMOKE=1 to run"),
        ("test_smoke_b", "skipped", "set PLANT_GENOMICS_MCP_STDIO_SMOKE=1 to run"),
    ]
    out = _run(_report(tmp_path, cases, "sk.xml"), 0)
    assert out.returncode == 0, out.stdout
    assert "4 test cases; 0 regression(s), 0 upstream-side failure(s), 3 skipped." in out.stdout
    assert f"| 1 | {OUTAGE_SKIP} |" in out.stdout
    assert "| 2 | set PLANT_GENOMICS_MCP_STDIO_SMOKE=1 to run |" in out.stdout

    none = _run(_report(tmp_path, [("test_ok", None, "")], "ok.xml"), 0)
    assert none.returncode == 0 and "| skipped | reason |" not in none.stdout, none.stdout
