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

A test labeled upstream night after night was listed and the run stayed green:
the live BLAST test ended WAITING in almost every scheduled run for six weeks,
and verify_genes's stdio test hit the timeout twelve recorded nights running.
So with ``--history WORKFLOW_FILE`` the run also fails on a *repeat upstream*:
a test labeled upstream in this run and in each of the scheduled nights before
it, ``REPEAT_UPSTREAM_NIGHTS`` in a row. The nights are read from the
``live-junit`` report each scheduled run keeps as an artifact, classed by this
same code, through the GitHub API with ``$GITHUB_TOKEN`` (``$GITHUB_REPOSITORY``
and ``$GITHUB_RUN_ID`` name the repo and this run). A scheduled run that kept
no report (cancelled at the job's time limit, or its artifact expired), or
whose report failed the checks that made that night's run unusable, is a night
with no record: it neither extends a streak nor breaks one, and the summary
lists it. History that
exists but cannot be read (an API error, a corrupt archive or report) makes the
run unusable, never a silent pass.

Usage: ``classify_live_failures.py REPORT.xml PYTEST_EXIT [SUMMARY.md]
[--history WORKFLOW_FILE]``; the summary (markdown) is appended to, as
``$GITHUB_STEP_SUMMARY`` expects. Exit 0 when no regression and no repeat
upstream, 1 when there is either or the run is unusable.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import zipfile
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

import httpx
from defusedxml import DefusedXmlException, ElementTree

UPSTREAM_TAGS = ("[UpstreamUnavailableError]", "[RateLimitError]")
TIMEOUT_MARK = "Timeout (>"  # pytest-timeout: "Failed: Timeout (>240.0s) from pytest-timeout."
LIVE_GATE = "PLANT_GENOMICS_MCP_LIVE"

# A test labeled upstream on this many scheduled nights in a row fails the run.
# Read over the 42 recorded nights the history held when this was chosen: four
# tests could not pass (the three BLAST tests, streaks of 25 to 42 nights;
# verify_genes's stdio test, 12). Leaving those out, the run would have failed
# on 8 nights at 2 (two-night blips), on 1 at 3 (two Ensembl lookups through a
# three-night outage) and on none at 4. 3 is the smallest count a two-night
# blip does not reach.
REPEAT_UPSTREAM_NIGHTS = 3
# Scheduled runs read back at most; a streak still alive after them is shown
# as a lower bound.
HISTORY_RUNS = 14
REPORT_ARTIFACT = "live-junit"  # the name live-nightly.yml uploads the report under
API = "https://api.github.com"


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


def read_report(path: Path | IO[bytes]) -> Report:
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


class HistoryUnreadable(RuntimeError):
    """History that exists could not be read: the run cannot be judged on it."""


@dataclass(frozen=True)
class Night:
    """One earlier scheduled run, as read back."""

    run_id: int
    date: str
    upstream: frozenset[str] | None  # tests labeled upstream; None when the run kept no record
    note: str = ""  # why there is no record


Get = Callable[[str], bytes]


def github_get(token: str) -> Get:
    """GET from the GitHub API with the workflow's token; any non-200 raises.

    An artifact's archive URL answers with a redirect to storage on another
    host; httpx drops the Authorization header when it follows one.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    def get(url: str) -> bytes:
        try:
            with httpx.Client(headers=headers, follow_redirects=True, timeout=60.0) as client:
                resp = client.get(url)
        except httpx.HTTPError as err:
            raise HistoryUnreadable(f"GET {url}: {err!r}") from err
        if resp.status_code != 200:
            raise HistoryUnreadable(f"GET {url}: HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.content

    return get


def _json(get: Get, url: str) -> dict:
    try:
        return json.loads(get(url))
    except ValueError as err:
        raise HistoryUnreadable(f"GET {url}: not JSON: {err}") from err


def _night(get: Get, repo: str, run: dict) -> Night:
    run_id, date = int(run["id"]), str(run["created_at"])[:10]
    if run.get("conclusion") not in ("success", "failure"):
        # The classifier step did not finish (the job hit its time limit, or
        # the run was cancelled or skipped): pytest wrote no whole report.
        return Night(run_id, date, None, f"run {run.get('conclusion')}")
    listing = _json(
        get, f"{API}/repos/{repo}/actions/runs/{run_id}/artifacts?name={REPORT_ARTIFACT}"
    )
    kept = [a for a in listing.get("artifacts", []) if not a.get("expired")]
    if not kept:
        return Night(run_id, date, None, f"no unexpired {REPORT_ARTIFACT} artifact")
    newest = max(kept, key=lambda a: str(a.get("created_at", "")))
    url = newest["archive_download_url"]
    try:
        with zipfile.ZipFile(io.BytesIO(get(url))) as archive:
            names = archive.namelist()
            if len(names) != 1:
                raise HistoryUnreadable(
                    f"run {run_id}: {REPORT_ARTIFACT} holds {names}, not one report"
                )
            report = read_report(io.BytesIO(archive.read(names[0])))
    except (zipfile.BadZipFile, ElementTree.ParseError, DefusedXmlException) as err:
        raise HistoryUnreadable(f"run {run_id}: {REPORT_ARTIFACT} unreadable: {err!r}") from err
    if report.cases == 0:
        raise HistoryUnreadable(f"run {run_id}: {REPORT_ARTIFACT} holds no test cases")
    unusable = []
    if report.gate != "1":
        unusable.append(f"{LIVE_GATE} was {report.gate!r}")
    if report.unreadable:
        unusable.append(f"{len(report.unreadable)} failure(s) could not be classed")
    if unusable:
        # main() failed that night as unusable when it ran, on these same
        # checks: its classes are not a record. Failing every later night on
        # it too would keep the check red for as long as the night is read.
        return Night(run_id, date, None, "report unusable: " + "; ".join(unusable))
    return Night(run_id, date, frozenset(f.test for f in report.failures if f.cls == "upstream"))


def scheduled_nights(get: Get, repo: str, workflow: str, current_run: int) -> Iterator[Night]:
    """The scheduled runs before this one, newest first, each read when asked for.

    The run list is read before the first night is yielded, so a token that
    cannot read the Actions API fails every run, not only one that needs
    history.
    """
    listing = _json(
        get,
        f"{API}/repos/{repo}/actions/workflows/{workflow}/runs"
        f"?event=schedule&status=completed&per_page={HISTORY_RUNS + 1}",
    )
    runs = [r for r in listing.get("workflow_runs", []) if int(r["id"]) != current_run]
    runs.sort(key=lambda r: str(r["created_at"]), reverse=True)

    def nights() -> Iterator[Night]:
        for run in runs[:HISTORY_RUNS]:
            yield _night(get, repo, run)

    return nights()


@dataclass
class Streaks:
    nights: dict[str, int]  # test -> scheduled nights in a row labeled upstream, this run included
    open_ended: set[str]  # tests still upstream when the history read ran out
    read: list[Night]  # the earlier nights read, newest first


def upstream_streaks(today: Iterable[str], history: Iterable[Night]) -> Streaks:
    """How many nights in a row each of today's upstream tests has been upstream.

    A night with no record is passed over; a recorded night on which the test
    passed, skipped, failed as a regression or did not run ends its streak.
    Reads the (lazy) history only while some streak is still open.
    """
    nights = dict.fromkeys(today, 1)
    alive = set(nights)
    read: list[Night] = []
    nights_left = iter(history)
    # Asked for only while a streak is open: asking reads the night's report.
    while alive and (night := next(nights_left, None)) is not None:
        read.append(night)
        if night.upstream is None:
            continue
        for test in sorted(alive):
            if test in night.upstream:
                nights[test] += 1
            else:
                alive.discard(test)
    return Streaks(nights, alive, read)


def _cell(text: str, limit: int = 200) -> str:
    line = " ".join(text.split())
    if len(line) > limit:
        line = line[: limit - 1] + "…"
    return line.replace("|", "\\|")


def summarize_repeats(streaks: Streaks) -> str:
    repeats = {t: n for t, n in streaks.nights.items() if n >= REPEAT_UPSTREAM_NIGHTS}
    out = []
    if repeats:
        out += [
            f"### Repeat upstream: {len(repeats)} test(s) labeled upstream on "
            f"{REPEAT_UPSTREAM_NIGHTS} or more nights in a row",
            "",
            "Not a regression in this run: each test below has been labeled upstream "
            "on every recorded scheduled night of its streak, this run included. A long "
            "outage and a test that cannot pass read the same; look at the test. "
            "A + means the streak reaches back to the oldest night read.",
            "",
            "| nights in a row | test |",
            "|---|---|",
        ]
        for test, n in sorted(repeats.items(), key=lambda kv: (-kv[1], kv[0])):
            shown = f"{n}+" if test in streaks.open_ended else str(n)
            out.append(f"| {shown} | `{test}` |")
        out.append("")
    if streaks.read:
        nights = ", ".join(
            f"{n.date} (run {n.run_id}{': no record, ' + n.note if n.upstream is None else ''})"
            for n in streaks.read
        )
        out += [f"Earlier scheduled nights read: {nights}.", ""]
    return "".join(f"{line}\n" for line in out)


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
            "that breaks our own calls reads the same, so a test listed here on "
            f"{REPEAT_UPSTREAM_NIGHTS} scheduled nights in a row fails the run (with "
            "--history)."
        )
        out.append("")
    if report.skips:
        out += ["| skipped | reason |", "|---|---|"]
        for reason, n in report.skips.most_common():
            out.append(f"| {n} | {_cell(reason)} |")
        out.append("")
    return "\n".join(out)


def main(argv: list[str]) -> int:
    argv = list(argv)
    workflow = None
    if "--history" in argv:
        at = argv.index("--history")
        workflow = argv[at + 1] if at + 1 < len(argv) else ""
        del argv[at : at + 2]
    if len(argv) not in (3, 4) or workflow == "":
        print(
            "usage: classify_live_failures.py REPORT.xml PYTEST_EXIT [SUMMARY.md] "
            "[--history WORKFLOW_FILE]",
            file=sys.stderr,
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
    upstream_today = {f.test for f in failures if f.cls == "upstream"}
    streaks = Streaks(dict.fromkeys(upstream_today, 1), set(), [])
    if workflow is not None:
        env = {
            k: os.environ.get(k, "") for k in ("GITHUB_TOKEN", "GITHUB_REPOSITORY", "GITHUB_RUN_ID")
        }
        missing = [k for k, v in env.items() if not v]
        if missing:
            problems.append(f"cannot read the nightly history: {', '.join(missing)} not set")
        else:
            try:
                history = scheduled_nights(
                    github_get(env["GITHUB_TOKEN"]),
                    env["GITHUB_REPOSITORY"],
                    workflow,
                    int(env["GITHUB_RUN_ID"]),
                )
                streaks = upstream_streaks(upstream_today, history)
            except HistoryUnreadable as err:
                problems.append(f"cannot read the nightly history: {err}")
    repeats = [t for t, n in streaks.nights.items() if n >= REPEAT_UPSTREAM_NIGHTS]
    text = summarize_repeats(streaks) + summarize(result)
    if problems:
        text = "### Live run unusable\n\n" + "".join(f"- {p}\n" for p in problems) + "\n" + text
    print(text)
    if summary is not None:
        with summary.open("a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    regressions = [f for f in failures if f.cls == "regression"]
    return 1 if problems or regressions or repeats else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
