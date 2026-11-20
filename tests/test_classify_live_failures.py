"""Tests for scripts/classify_live_failures.py.

The failure messages are verbatim from a full live pass on 2026-09-29, the
night the classifier was written for: four regression-class failures (a
protein one residue long, Phytozome's BioMart answering 404) among twelve
upstream-side ones (Ensembl 500, pytest-timeout on NCBI BLAST, a synthesis
envelope whose Ensembl step failed). The script is run as the workflow runs
it, a subprocess reading a JUnit file.
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
import shlex
import subprocess
import sys
import zipfile
from collections.abc import Callable, Sequence
from pathlib import Path
from types import ModuleType
from xml.sax.saxutils import escape, quoteattr

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import _http
from plant_genomics_mcp.errors import PlantGenomicsError

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "classify_live_failures.py"
WORKFLOW = ROOT / ".github" / "workflows" / "live-nightly.yml"

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
# The nightly of 2026-09-30, verbatim from its JUnit report: Planteome refused a
# GitHub runner (the same code answered this host that day), and the untagged
# base class made it one of that night's three "regressions".
PLANTEOME_403 = (
    "plant_genomics_mcp.errors.PlantGenomicsError: Planteome /select → HTTP 403: "
    '<!DOCTYPE HTML PUBLIC "-//IETF//DTD HTML 2.0//EN">\n<html><head>\n'
    "<title>403 Forbidden</title>\n</head><body>\n<h1>Forbidden</h1>\n"
    "<p>You don't have permission to access this resource.</p>\n</body></html>"
)
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


# An envelope-shaped failure: the outage tag sits deep in a long repr, as a
# synthesis tool's failed step does, beside a plain assertion that is a
# regression.
_LONG_REPR_TESTS = """
from dataclasses import dataclass, field


@dataclass
class Envelope:
    steps: list = field(default_factory=lambda: [
        "x" * 5000,
        "error='[UpstreamUnavailableError] Ensembl Plants /lookup/id/AT1G01010 exhausted 3 retries'",
        "y" * 5000,
    ])
    result: object = None


def test_synth():
    assert Envelope().result is not None


def test_plain():
    assert 430 == 429
"""


def _workflow_pytest_flags() -> list[str]:
    """The verbosity flag and ``-o`` options of the workflow's pytest line."""
    lines = [ln.strip() for ln in WORKFLOW.read_text(encoding="utf-8").splitlines()]
    (line,) = [ln for ln in lines if ln.startswith("pytest ")]
    tokens = shlex.split(line.rstrip("\\"))
    flags = [t for t in tokens if re.fullmatch(r"-(q+|v+)", t)]
    for i, token in enumerate(tokens):
        if token == "-o":
            flags += ["-o", tokens[i + 1]]
    return flags


def test_the_workflow_keeps_a_long_repr_whole_for_the_classifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under -q pytest elides the middle of a long assertion repr; run
    36544299260 lost a synthesis step's [UpstreamUnavailableError] that way
    and classed an Ensembl outage a regression. Run with the workflow's own
    flags, the tag survives and the failure is upstream-side; the plain
    assertion beside it stays a regression."""
    spec = importlib.util.spec_from_file_location("classify_live_failures", SCRIPT)
    assert spec is not None and spec.loader is not None
    classifier = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, classifier)  # its dataclasses look it up
    spec.loader.exec_module(classifier)

    flags = _workflow_pytest_flags()
    assert "-q" in flags, flags
    (tmp_path / "test_envelope.py").write_text(_LONG_REPR_TESTS, encoding="utf-8")
    report = tmp_path / "out.xml"
    run = subprocess.run(  # nosec B603 - fixed argv built here, no shell
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            *flags,
            f"--junitxml={report}",
            "test_envelope.py",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 1, run.stdout + run.stderr
    classes = {f.test.split("::")[1]: f.cls for f in classifier.read_report(report).failures}
    assert classes == {"test_synth": "upstream", "test_plain": "regression"}, classes


async def _junit_message(httpx_mock: HTTPXMock, status: int, body: str) -> str:
    """The JUnit message pytest writes for what ``_http`` raises on this answer."""
    url = f"https://example.test/{status}"
    httpx_mock.add_response(url=url, status_code=status, text=body)
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlantGenomicsError) as raised:
            await _http.request_with_retry(client, "GET", url, service="Planteome /select")
    err = raised.value
    return f"{type(err).__module__}.{type(err).__qualname__}: {err}"


async def test_a_refusal_is_upstream_side_and_a_bad_request_is_a_regression(
    tmp_path: Path, httpx_mock: HTTPXMock
) -> None:
    """The night's Planteome 403, raised by the real ``_http`` from the page it
    answered, run through the script: upstream-side. Control, same run: a 400
    ``_http`` does not map, which our own request causes, is a regression."""
    body = PLANTEOME_403.split("HTTP 403: ", 1)[1]
    refused = await _junit_message(httpx_mock, 403, body)
    assert refused == PLANTEOME_403.replace(
        "PlantGenomicsError: ", "UpstreamUnavailableError: [UpstreamUnavailableError] ", 1
    )
    bad = await _junit_message(httpx_mock, 400, "Bad Request")
    run = _run(
        _report(tmp_path, [("test_planteome", "failure", refused), ("test_bad", "failure", bad)]),
        1,
    )
    assert run.returncode == 1, run.stdout + run.stderr
    assert "2 test cases; 1 regression(s), 1 upstream-side failure(s), 0 skipped." in run.stdout
    rows = {ln.split(" | ")[1]: ln.split(" | ")[0] for ln in run.stdout.splitlines() if "`" in ln}
    assert rows == {
        "`tests.test_live::test_bad`": "| regression",
        "`tests.test_live::test_planteome`": "| upstream",
    }, run.stdout


# pytest 9.2's JUnit report for three failures, verbatim from a 9.2.0 run (the
# long runs of x, y and z written as products, as is the one line of trailing
# spaces, which a whitespace hook would strip; hostname and timestamp dropped).
# From 9.2 the ``message`` attribute keeps about 500 characters and points to
# the element text for the rest: the synthesis step's tag and the setup
# error's tag are both past that cut.
_X, _Y, _Z = "x" * 300, "y" * 300, "z" * 600
_STEP = "error='[UpstreamUnavailableError] Ensembl Plants /lookup/id/AT1G01010 exhausted 3 retries'"
_ENVELOPE = f"Envelope(steps=['{_X}', \"{_STEP}\", '{_Y}'], result=None)"
_RAISED = f"test_small.UpstreamUnavailableError: {_Z} [UpstreamUnavailableError] Ensembl Plants exhausted 3 retries"
_PYTEST_92 = f"""<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests"><testsuite name="pytest" errors="1" failures="2" skipped="0" tests="3" time="0.097"><properties><property name="PLANT_GENOMICS_MCP_LIVE" value="1" /></properties><testcase classname="test_small" name="test_synth" time="0.002"><failure message="assert None is not None&#10;[+2 more lines; full report in element text]">def test_synth():
&gt;       assert Envelope().result is not None
E       assert None is not None
E        +  where None = {escape(_ENVELOPE)}.result
E        +    where {escape(_ENVELOPE)} = Envelope()

test_small.py:25: AssertionError</failure></testcase><testcase classname="test_small" name="test_setup" time="0.001"><error message="failed on setup with &quot;test_small.UpstreamUnavailableError: {"z" * 441}...&#10;[truncated; full report in element text]">@pytest.fixture
    def backend():
&gt;       raise UpstreamUnavailableError("z" * 600 + " [UpstreamUnavailableError] Ensembl Plants exhausted 3 retries")
E       {_RAISED}

test_small.py:21: UpstreamUnavailableError</error></testcase><testcase classname="test_small" name="test_plain" time="0.001"><failure message="AssertionError: assert '{"x" * 476}...&#10;[+3 more lines; full report in element text]">def test_plain():
&gt;       assert "x" * 600 == "y" * 600
E       AssertionError: assert '{"x" * 600}' == '{"y" * 600}'
E{" " * 9}
E         - {"y" * 600}
E         + {"x" * 600}

test_small.py:33: AssertionError</failure></testcase></testsuite></testsuites>"""
# What pytest 9.0.3 wrote into ``message`` for the same three tests, verbatim.
_PYTEST_903_MESSAGES = {
    "test_synth": (
        "assert None is not None\n"
        f" +  where None = {_ENVELOPE}.result\n"
        f" +    where {_ENVELOPE} = Envelope()"
    ),
    "test_setup": f'failed on setup with "{_RAISED}"',
    "test_plain": f"AssertionError: assert '{'x' * 600}' == '{'y' * 600}'\n  \n  - {'y' * 600}\n  + {'x' * 600}",
}


def _load_classifier(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location("classify_live_failures", SCRIPT)
    assert spec is not None and spec.loader is not None
    classifier = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, classifier)  # its dataclasses look it up
    spec.loader.exec_module(classifier)
    return classifier


def test_a_pytest_92_report_is_read_back_to_the_whole_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever pytest CI installs, a 9.2 report reads back to the message
    9.0.3 wrote: the envelope and the setup error are upstream-side, the long
    plain assertion stays a regression, and the run passes on that alone."""
    report = tmp_path / "92.xml"
    report.write_text(_PYTEST_92, encoding="utf-8")
    failures = _load_classifier(monkeypatch).read_report(report).failures
    assert {f.test.split("::")[1]: f.message for f in failures} == _PYTEST_903_MESSAGES
    classes = {f.test.split("::")[1]: f.cls for f in failures}
    assert classes == {
        "test_synth": "upstream",
        "test_setup": "upstream",
        "test_plain": "regression",
    }
    run = _run(report, 1)
    assert run.returncode == 1 and "1 regression(s), 2 upstream-side failure(s)" in run.stdout, (
        run.stdout
    )


def test_only_the_failure_lines_count_and_an_unmatched_block_is_refused(tmp_path: Path) -> None:
    """The source lines in the element text are not the failure: with the tag
    only in the ``raise`` line the setup error is a regression. And when the
    E block does not continue what the attribute kept, the run is unusable
    rather than classed on a guess. Positive control: the unchanged report
    is readable and classes the setup error upstream-side."""
    plain_only = _PYTEST_92.replace(
        f"E       {_RAISED}", f"E       test_small.UpstreamUnavailableError: {_Z}", 1
    )
    assert plain_only != _PYTEST_92
    report = tmp_path / "src.xml"
    report.write_text(plain_only, encoding="utf-8")
    run = _run(report, 1)
    assert run.returncode == 1 and "2 regression(s), 1 upstream-side failure(s)" in run.stdout, (
        run.stdout
    )
    assert "### Live run unusable" not in run.stdout, run.stdout

    mismatched = _PYTEST_92.replace("E       assert None is not None", "E       assert 1 == 2", 1)
    assert mismatched != _PYTEST_92
    report = tmp_path / "bad.xml"
    report.write_text(mismatched, encoding="utf-8")
    run = _run(report, 1)
    assert run.returncode == 1, run.stdout
    assert "### Live run unusable" in run.stdout and "test_small::test_synth" in run.stdout, (
        run.stdout
    )

    good = tmp_path / "good.xml"
    good.write_text(_PYTEST_92, encoding="utf-8")
    run = _run(good, 1)
    assert "### Live run unusable" not in run.stdout, run.stdout


# ---------- repeat upstream: the same test upstream several nights running ----------
# The nightly listed upstream failures and stayed green, so a test that could
# not pass was listed for six weeks (the live BLAST test ended WAITING on almost
# every scheduled night). With --history the classifier reads the reports
# earlier scheduled runs kept and fails the run on a repeat. The GitHub API is
# faked here at the classifier's one seam (github_get); the workflow's own run
# on the PR reads the real one.

_API = "https://api.github.com"
_REPO, _RUN_ID = "owner/repo", 999
_RUNS_URL = (
    f"{_API}/repos/{_REPO}/actions/workflows/live-nightly.yml/runs"
    "?event=schedule&status=completed&per_page=15"
)
_HISTORY_ARGS = ["--history", "live-nightly.yml"]


def _zip(report: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.write(report, "live.xml")
    return buf.getvalue()


Night = tuple[str, str, "list[tuple[str, str | None, str]] | bytes | None"]


def _fake_api(
    tmp_path: Path, classifier: ModuleType, nights: Sequence[Night]
) -> tuple[Callable[[str], bytes], list[str]]:
    """The GitHub API as the classifier reads it, for scheduled ``nights``
    newest first: (date, conclusion, report cases | archive bytes | None for
    no live-junit artifact). Returns the getter and the URLs it was asked for;
    a URL it does not serve answers 404, as the API would."""
    pages: dict[str, bytes] = {}
    runs = []
    for i, (date, conclusion, report) in enumerate(nights):
        run_id = 500 - i
        runs.append({"id": run_id, "created_at": f"{date}T17:00:00Z", "conclusion": conclusion})
        artifacts = []
        if report is not None:
            url = f"https://storage.example/{run_id}.zip"
            artifacts.append(
                {
                    "name": "live-junit",
                    "expired": False,
                    "created_at": f"{date}T18:00:00Z",
                    "archive_download_url": url,
                }
            )
            pages[url] = (
                report
                if isinstance(report, bytes)
                else _zip(_report(tmp_path, report, f"night-{run_id}.xml"))
            )
        pages[f"{_API}/repos/{_REPO}/actions/runs/{run_id}/artifacts?name=live-junit"] = json.dumps(
            {"artifacts": artifacts}
        ).encode()
    # The run being judged is in progress, so the API lists it only if asked
    # for every status; it must not count as its own history either way.
    runs.append({"id": _RUN_ID, "created_at": "2026-12-31T17:00:00Z", "conclusion": "success"})
    pages[_RUNS_URL] = json.dumps({"workflow_runs": runs}).encode()
    asked: list[str] = []

    def get(url: str) -> bytes:
        asked.append(url)
        if url not in pages:
            raise classifier.HistoryUnreadable(f"GET {url}: HTTP 404")
        return pages[url]

    return get, asked


def _classify(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    today: Sequence[tuple[str, str | None, str]],
    nights: Sequence[Night],
    name: str,
) -> tuple[int, str, list[str]]:
    """Run the classifier in-process as the workflow does, over a fake history."""
    classifier = _load_classifier(monkeypatch)
    get, asked = _fake_api(tmp_path, classifier, nights)
    monkeypatch.setattr(classifier, "github_get", lambda token: get, raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_REPOSITORY", _REPO)
    monkeypatch.setenv("GITHUB_RUN_ID", str(_RUN_ID))
    exit_code = int(any(c[1] == "failure" for c in today))
    code = classifier.main(
        ["classify", str(_report(tmp_path, today, name)), str(exit_code), *_HISTORY_ARGS]
    )
    return code, capsys.readouterr().out, asked


_OK = ("test_ok", None, "")
_BLAST = ("test_blast", "failure", BLAST_TIMEOUT)
_ENSEMBL = ("test_ensembl", "failure", ENSEMBL_500)


def test_a_test_upstream_three_nights_running_fails_the_run_as_a_repeat_not_a_regression(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """BLAST upstream tonight and the two scheduled nights before: the run
    fails, and the summary names the test and its streak under its own
    heading, not as a regression. Positive controls, same history: Ensembl,
    upstream tonight only, passes; a regression still fails as a regression."""
    history: list[Night] = [
        ("2026-11-16", "success", [_OK, _BLAST, ("test_ensembl", None, "")]),
        ("2026-11-15", "failure", [_OK, _BLAST, _ENSEMBL]),
    ]
    code, out, _ = _classify(
        tmp_path, monkeypatch, capsys, [_OK, _BLAST, _ENSEMBL], history, "red.xml"
    )
    assert "### Repeat upstream: 1 test(s) labeled upstream on 3 or more nights in a row" in out
    # 3+: the streak reaches back to the oldest night the history holds.
    assert "| 3+ | `tests.test_live::test_blast` |" in out
    assert "`tests.test_live::test_ensembl` |" not in out.split("### Live tests")[0]
    assert "0 regression(s), 2 upstream-side failure(s)" in out
    assert "### Live run unusable" not in out
    assert code == 1

    code, out, _ = _classify(tmp_path, monkeypatch, capsys, [_OK, _ENSEMBL], history, "one.xml")
    assert code == 0, out
    assert "Repeat upstream" not in out
    assert "`tests.test_live::test_ensembl`" in out  # still listed, as before

    regression = ("test_protein", "failure", WRONG_LENGTH)
    code, out, _ = _classify(tmp_path, monkeypatch, capsys, [_OK, regression], history, "reg.xml")
    assert code == 1
    assert "1 regression(s), 0 upstream-side failure(s)" in out
    assert "Repeat upstream" not in out


def test_a_night_with_no_record_neither_extends_nor_breaks_a_streak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two cancelled nights (the job's time limit, as 2026-11-17 and -18) and
    one whose report expired are passed over; the first recorded night the
    test was not upstream ends the streak, and nothing older is read.
    Positive control: the same nights with the BLAST test passing on one of
    them leave a streak of 1 and the run green."""

    def nights(blast_on_16: tuple[str, str | None, str]) -> list[Night]:
        return [
            ("2026-11-18", "cancelled", None),
            ("2026-11-17", "cancelled", None),
            ("2026-11-16", "success", [_OK, blast_on_16]),
            ("2026-11-15", "success", None),  # kept no unexpired report
            ("2026-11-14", "failure", [_OK, _BLAST]),
            ("2026-11-13", "success", [_OK, ("test_blast", None, "")]),
            ("2026-11-12", "failure", b"never read: the streak ended on 11-13"),
        ]

    code, out, asked = _classify(
        tmp_path, monkeypatch, capsys, [_OK, _BLAST], nights(_BLAST), "gap.xml"
    )
    assert "| 3 | `tests.test_live::test_blast` |" in out, out
    assert "2026-11-18 (run 500: no record, run cancelled)" in out
    assert "2026-11-15 (run 497: no record, no unexpired live-junit artifact)" in out
    assert "2026-11-13 (run 495)" in out and "2026-11-12" not in out
    assert not any("/494" in url for url in asked)
    assert code == 1

    code, out, _ = _classify(
        tmp_path, monkeypatch, capsys, [_OK, _BLAST], nights(("test_blast", None, "")), "ok.xml"
    )
    assert code == 0, out
    assert "Repeat upstream" not in out


def test_history_that_cannot_be_read_makes_the_run_unusable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An API refusal, a corrupt archive or a missing token is not "no
    history": each fails the run as unusable, even one with no failures (the
    run list is read every night). Positive control: the same runs with
    history that reads are green."""
    readable: list[Night] = [("2026-11-16", "success", [_OK, _BLAST])]
    code, out, _ = _classify(tmp_path, monkeypatch, capsys, [_OK, _BLAST], readable, "g.xml")
    assert code == 0, out
    code, out, _ = _classify(tmp_path, monkeypatch, capsys, [_OK], readable, "c.xml")
    assert code == 0, out

    corrupt: list[Night] = [("2026-11-16", "success", b"not a zip archive")]
    code, out, _ = _classify(tmp_path, monkeypatch, capsys, [_OK, _BLAST], corrupt, "z.xml")
    assert code == 1
    assert "### Live run unusable" in out and "run 500: live-junit unreadable" in out, out

    classifier = _load_classifier(monkeypatch)

    def refused(token: str) -> Callable[[str], bytes]:
        def get(url: str) -> bytes:
            raise classifier.HistoryUnreadable(f"GET {url}: HTTP 403: Resource not accessible")

        return get

    monkeypatch.setattr(classifier, "github_get", refused, raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    clean = _report(tmp_path, [_OK], "clean.xml")
    code = classifier.main(["classify", str(clean), "0", *_HISTORY_ARGS])
    out = capsys.readouterr().out
    assert code == 1
    assert "cannot read the nightly history: GET" in out and "HTTP 403" in out, out

    monkeypatch.delenv("GITHUB_TOKEN")
    code = classifier.main(["classify", str(clean), "0", *_HISTORY_ARGS])
    out = capsys.readouterr().out
    assert code == 1
    assert "cannot read the nightly history: GITHUB_TOKEN not set" in out, out


def test_the_workflow_reads_history_with_a_token_that_can_read_actions() -> None:
    """The check runs only if the workflow asks for it, can read the Actions
    API, and uploads the report under the name the classifier reads back."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert re.search(r"(?m)^permissions:\n  contents: read\n  actions: read\b", workflow)
    assert "GITHUB_TOKEN: ${{ github.token }}" in workflow
    assert '"$GITHUB_STEP_SUMMARY" --history live-nightly.yml' in workflow
    assert re.search(r"(?m)^\s+name: live-junit$", workflow)
    assert WORKFLOW.name == "live-nightly.yml"


def test_a_night_whose_report_was_unusable_is_no_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A report without the live gate, or with a failure whose message cannot
    be restored, made its own night unusable; read back later, its classes are
    not a record either (a cut message can drop or keep the tag the class rests
    on). Each such night is passed over and named. Positive control: the same
    nights with a usable report extend the streak to 3 and fail the run."""
    cut = ("test_blast", "failure", f"{BLAST_TIMEOUT}\n[truncated; full report in element text]")
    for label, bad_night in {
        "gate unset": ("2026-11-16", "success", [_OK, _BLAST]),
        "message cut": ("2026-11-16", "failure", [_OK, cut]),
    }.items():
        history: list[Night] = [bad_night, ("2026-11-15", "failure", [_OK, _BLAST])]
        if label == "gate unset":
            report = _report(tmp_path, [_OK, _BLAST], "nogate.xml", gate="")
            history[0] = ("2026-11-16", "success", _zip(report))
        code, out, _ = _classify(
            tmp_path, monkeypatch, capsys, [_OK, _BLAST], history, f"{label}.xml"
        )
        assert "2026-11-16 (run 500: no record, report unusable:" in out, (label, out)
        assert "Repeat upstream" not in out, (label, out)
        assert code == 0, (label, out)

    usable: list[Night] = [
        ("2026-11-16", "success", [_OK, _BLAST]),
        ("2026-11-15", "failure", [_OK, _BLAST]),
    ]
    code, out, _ = _classify(tmp_path, monkeypatch, capsys, [_OK, _BLAST], usable, "u.xml")
    assert "| 3+ | `tests.test_live::test_blast` |" in out, out
    assert code == 1


def test_the_api_client_sends_the_token_to_github_only_and_refuses_a_non_200(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real client over a mock transport. An artifact's archive URL
    answers 302 to storage on another host: the archive comes back, and the
    token went to the API but not to the storage host. Negative cases, same
    client: a refusal and a transport error are HistoryUnreadable naming the
    URL, never an empty history."""
    classifier = _load_classifier(monkeypatch)
    archive = f"{_API}/repos/{_REPO}/actions/artifacts/7/zip"
    storage = "https://results.blob.example.net/artifacts/7.zip?sig=abc"
    httpx_mock.add_response(url=archive, status_code=302, headers={"Location": storage})
    httpx_mock.add_response(url=storage, content=b"PK archive bytes")
    get = classifier.github_get("tok")
    assert get(archive) == b"PK archive bytes"
    to_api, to_storage = httpx_mock.get_requests()
    assert to_api.headers["Authorization"] == "Bearer tok"
    assert to_api.headers["Accept"] == "application/vnd.github+json"
    assert "Authorization" not in to_storage.headers

    httpx_mock.add_response(url=_RUNS_URL, status_code=403, text="Resource not accessible")
    with pytest.raises(classifier.HistoryUnreadable, match="HTTP 403: Resource not accessible"):
        get(_RUNS_URL)
    listing = f"{_API}/repos/{_REPO}/actions/runs/1/artifacts?name=live-junit"
    httpx_mock.add_exception(httpx.ConnectTimeout("timed out"), url=listing)
    with pytest.raises(
        classifier.HistoryUnreadable, match=r"artifacts\?name=live-junit: ConnectTimeout"
    ):
        get(listing)
