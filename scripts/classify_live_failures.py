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

import re
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
    # failures whose shortened message could not be restored from the element text
    unreadable: list[str] = field(default_factory=list)


class UnreadableMessage(ValueError):
    """A shortened ``message`` attribute whose full text the report does not hold."""


_SHORTENED = re.compile(r"\n\[(\+\d+ more lines?|truncated); full report in element text\]\Z")
_WRAPPED = re.compile(r'failed on (setup|teardown) with "')


def _full_message(attr: str, text: str) -> str:
    """The failure's whole crash message, as pytest before 9.2 wrote it into ``message``.

    pytest 9.2 keeps ``message`` within 500 characters: whole lines while they
    fit (a first line longer than that is cut and ends ``...``), then a line
    ``[+N more lines; full report in element text]`` or ``[truncated; ...]``.
    A synthesis envelope's failed step sits far past that, so the tag the class
    rests on was dropped and an upstream outage read as a regression (every
    night from 2026-10-28, when CI's unpinned install moved to 9.2).

    The element text is the full report: the traceback with its source lines,
    then the crash message as the last block of lines marked ``E``. Only that
    block is read, never the source lines, which can name a tag the failure did
    not raise. It must continue what the attribute kept, or the report is not
    in the shape this reads and the failure is refused, not guessed at.
    """
    found = _SHORTENED.search(attr)
    if found is None:
        return attr
    kept = attr[: found.start()].removesuffix("...")
    wrapper = _WRAPPED.match(kept)  # an error in setup or teardown: failed on setup with "<msg>"
    head = kept[wrapper.end() :] if wrapper else kept
    lines = text.splitlines()
    marked = [i for i, line in enumerate(lines) if line.startswith("E")]
    if not marked:
        raise UnreadableMessage("the element text has no E-marked lines")
    end = start = marked[-1]
    while start > 0 and lines[start - 1].startswith("E"):
        start -= 1
    block = lines[start : end + 1]
    first_line = head.split("\n", 1)[0]
    # pytest marks every line of the block with "E" and the same run of spaces;
    # that width is the one at which the block's first line reads as the
    # attribute's first line.
    width = next(
        (
            n
            for n in range(1, len(block[0]) + 1)
            if block[0][:n] == "E" + " " * (n - 1) and block[0][n:].startswith(first_line)
        ),
        None,
    )
    if width is None:
        raise UnreadableMessage(f"the last E-marked block does not start with {first_line[:80]!r}")
    message = "\n".join(line[width:] for line in block)
    if not message.startswith(head):
        raise UnreadableMessage(f"the last E-marked block does not continue {head[:80]!r}")
    return f'{wrapper.group(0)}{message}"' if wrapper else message


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
                attr, text = node.get("message") or "", node.text or ""
                message = attr or text
                if attr:
                    try:
                        message = _full_message(attr, text)
                    except UnreadableMessage as err:
                        report.unreadable.append(f"`{test}`: {err}")
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
        problems += [
            f"cannot restore {u}, so the failure cannot be classed" for u in result.unreadable
        ]

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
