"""Sort the nightly live run's failures into upstream-side and regressions.

Before this, 105 of the 107 live tests ran in no CI job (only the ARF
verify_genes pair runs per PR), so a live-only break went unseen: the tomato
region test failed on main for days, and Phytozome's BioMart answering 404
was found by hand (2026-09-29). Run nightly they would be red most days for
reasons outside this repo: of 16 failures in a full live pass that night, 9
were upstream outages (Ensembl 500, Europe PMC 503, a PlantCyc challenge page)
and 3 were NCBI BLAST taking longer than the per-test cap.

So each failure in the JUnit report is classed:

- ``upstream``: its message names ``[UpstreamUnavailableError]`` or
  ``[RateLimitError]`` (the rendered class tag, which also appears inside a
  synthesis envelope's failed step), or it hit pytest-timeout's cap.
- ``regression``: anything else, including errors in setup or collection.

The run fails only on a regression. "Upstream" means the tool *reported* the
upstream as failing, which a regression that breaks our own calls (a wrong
host or path) also does, so upstream failures stay listed in the summary
rather than hidden; ``tests/_live_outage.py`` probes the backends directly for
the one check that needs to tell the two apart.

The run itself is checked before any class is trusted: a pytest exit other
than 0 or 1 (interrupted, internal error, no tests), a missing or empty
report, a report that disagrees with pytest's exit status, or a report whose
``PLANT_GENOMICS_MCP_LIVE`` property is not "1" is a failure of its own.
tests/conftest.py records that property as the pytest process saw the
variable. It was read from skip reasons first, which holds only while every
live test's reason names the variable; two Gramene tests' reason is "live".

Skips are listed by reason, so a test that skipped itself (verify_genes does,
when a direct probe finds its backend down) is in the summary, not missing
from it.

Usage: ``classify_live_failures.py REPORT.xml PYTEST_EXIT [SUMMARY.md]``;
the summary (markdown) is appended to, as ``$GITHUB_STEP_SUMMARY`` expects.
Exit 0 when no regression, 1 when there is one or the run is unusable.
"""

from __future__ import annotations

import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from defusedxml import ElementTree

UPSTREAM_TAGS = ("[UpstreamUnavailableError]", "[RateLimitError]")
TIMEOUT_MARK = "Timeout (>"  # pytest-timeout: "Failed: Timeout (>240.0s) from pytest-timeout."
LIVE_GATE = "PLANT_GENOMICS_MCP_LIVE"


@dataclass(frozen=True)
class Failure:
    test: str
    kind: str  # "failure" (assertion or raised in the test) or "error" (setup/teardown/collection)
    message: str

    @property
    def cls(self) -> str:
        return classify(self.message)


def classify(message: str) -> str:
    """``upstream`` or ``regression`` for one failure message."""
    if any(tag in message for tag in UPSTREAM_TAGS) or TIMEOUT_MARK in message:
        return "upstream"
    return "regression"


@dataclass
class Report:
    cases: int = 0
    failures: list[Failure] = field(default_factory=list)
    skips: Counter[str] = field(default_factory=Counter)  # reason -> tests
    gate: str | None = None  # the PLANT_GENOMICS_MCP_LIVE property; None when absent


def read_report(path: Path) -> Report:
    root = ElementTree.parse(path).getroot()
    report = Report()
    for prop in root.iter("property"):
        if prop.get("name") == LIVE_GATE:
            report.gate = prop.get("value", "")
    for case in root.iter("testcase"):
        report.cases += 1
        test = f"{case.get('classname', '')}::{case.get('name', '')}"
        for kind in ("failure", "error"):
            node = case.find(kind)
            if node is not None:
                message = node.get("message") or (node.text or "")
                report.failures.append(Failure(test, kind, message))
        skipped = case.find("skipped")
        if skipped is not None:
            report.skips[skipped.get("message") or ""] += 1
    return report


def _cell(text: str, limit: int = 200) -> str:
    line = " ".join(text.split())
    if len(line) > limit:
        line = line[: limit - 1] + "…"
    return line.replace("|", "\\|")


def summarize(report: Report) -> str:
    failures = report.failures
    regressions = [f for f in failures if f.cls == "regression"]
    upstream = [f for f in failures if f.cls == "upstream"]
    out = [
        "### Live tests",
        "",
        f"{report.cases} test cases; {len(regressions)} regression(s), "
        f"{len(upstream)} upstream-side failure(s), {sum(report.skips.values())} skipped.",
        "",
    ]
    if failures:
        out += ["| class | test | message |", "|---|---|---|"]
        for f in regressions + upstream:
            out.append(f"| {f.cls} | `{f.test}` | {_cell(f.message)} |")
        out.append("")
    if upstream:
        out.append(
            "Upstream-side means the tool reported the upstream failing; a regression "
            "that breaks our own calls reads the same, so a test listed here night "
            "after night is worth a look."
        )
        out.append("")
    if report.skips:
        out += ["| skipped | reason |", "|---|---|"]
        for reason, n in report.skips.most_common():
            out.append(f"| {n} | {_cell(reason)} |")
        out.append("")
    return "\n".join(out)


def main(argv: list[str]) -> int:
    if len(argv) not in (3, 4):
        print(
            "usage: classify_live_failures.py REPORT.xml PYTEST_EXIT [SUMMARY.md]", file=sys.stderr
        )
        return 1
    report, pytest_exit = Path(argv[1]), int(argv[2])
    summary = Path(argv[3]) if len(argv) == 4 else None

    problems: list[str] = []
    result = Report()
    if pytest_exit not in (0, 1):
        problems.append(f"pytest exited {pytest_exit}: the run did not complete")
    if not report.is_file():
        problems.append(f"no JUnit report at {report}")
    else:
        result = read_report(report)
        if result.cases == 0:
            problems.append("the JUnit report holds no test cases")
        if result.gate is None:
            problems.append(f"the report does not record {LIVE_GATE} (tests/conftest.py writes it)")
        elif result.gate != "1":
            problems.append(
                f"{LIVE_GATE} was {result.gate!r} in the run: the live gate was not set"
            )
        if pytest_exit == 0 and result.failures:
            problems.append(
                f"pytest exited 0 but the report lists {len(result.failures)} failure(s)"
            )
        if pytest_exit == 1 and not result.failures:
            problems.append("pytest exited 1 but the report lists no failure")

    failures = result.failures
    text = summarize(result)
    if problems:
        text = "### Live run unusable\n\n" + "".join(f"- {p}\n" for p in problems) + "\n" + text
    print(text)
    if summary is not None:
        with summary.open("a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    regressions = [f for f in failures if f.cls == "regression"]
    return 1 if problems or regressions else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
