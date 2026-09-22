"""The issue drafts quote the gap log, so the log is what they are checked against.

`examples/arf_family/issues/*.md` are hand-written from `gaps.jsonl`: each
`## \\`kind\\`` section restates one row's `attempted` / `returned` /
`expected` verbatim, and the text is what gets filed on the tracker and read
by whoever fixes the tool. Nothing re-derived them, so when the dossier was
re-run and a row's observation changed under them, the draft kept the old
sentence — `wheat-locus-unresolvable` said "all 8 fail the same way" for a
week after the log said "all 56 queried wheat loci fail the same way (8 of 8
on the first run)".

Same class as the page's typed counts (`test_arf_gaps_page.py`): text copied
out of an evidence file and never recounted. The page is rendered; the drafts
are not, so this test is what keeps them honest.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ISSUES_DIR = Path(__file__).resolve().parents[1] / "examples" / "arf_family" / "issues"
GAPS_PATH = Path(__file__).resolve().parents[1] / "examples" / "arf_family" / "gaps.jsonl"

SECTION_RE = re.compile(r"^## `([^`]+)`$(.*?)(?=^## |\Z)", re.M | re.S)
FIELD_RE = r"\*\*{label}:\*\*\s*(.+?)(?=\n- \*\*|\n\n|\Z)"


def _rows() -> dict[str, dict[str, object]]:
    return {
        row["kind"]: row
        for row in (json.loads(line) for line in GAPS_PATH.read_text().splitlines() if line.strip())
    }


def _normalise(text: str) -> str:
    """Compare meaning, not markdown.

    A draft escapes an underscore that would otherwise read as emphasis
    (``batch\\_ form``), which is a rendering concern and not a difference in
    what was observed.
    """
    return " ".join(text.replace("\\_", "_").split())


def _field(body: str, label: str) -> str | None:
    match = re.search(FIELD_RE.format(label=label), body, re.S)
    return match.group(1) if match else None


DRAFTS = sorted(ISSUES_DIR.glob("*.md"))


def test_the_drafts_directory_is_where_the_test_thinks_it_is() -> None:
    """Positive control for every parametrised test below.

    A glob that matched nothing, or a section regex that parsed nothing,
    would turn this file into a suite that asserts about an empty list and
    passes on any drift at all.
    """
    assert len(DRAFTS) >= 14, DRAFTS
    parsed = sum(len(SECTION_RE.findall(path.read_text())) for path in DRAFTS)
    assert parsed >= 20, f"parsed only {parsed} row sections out of {len(DRAFTS)} drafts"
    assert len(_rows()) == 40


@pytest.mark.parametrize("draft", DRAFTS, ids=lambda p: p.name)
def test_the_preamble_quotes_the_size_of_the_run_it_was_written_from(draft: Path) -> None:
    """Every draft opens by stating how big the run was. Recount it.

    These three numbers were typed once, against the first run, and stayed
    (`248 MCP calls`, `29 genes`, a `516-call` enumeration) through a re-run
    that made all three wrong — the same class as the page's typed counts,
    in a file the page's recount test does not read.
    """
    arf = GAPS_PATH.parent
    calls = len([ln for ln in (arf / "calls.jsonl").read_text().splitlines() if ln.strip()])
    enumeration = len(
        [ln for ln in (arf / "enumeration_calls.jsonl").read_text().splitlines() if ln.strip()]
    )
    genes = len([ln for ln in (arf / "genes.tsv").read_text().splitlines() if ln.strip()]) - 1
    assert calls and enumeration and genes  # positive control: the evidence is really there

    preamble = draft.read_text().split("## ", 1)[0]
    assert f"{calls} MCP calls" in preamble, draft.name
    assert f"{genes} genes" in preamble, draft.name
    assert f"{enumeration}-call" in preamble, draft.name


@pytest.mark.parametrize("draft", DRAFTS, ids=lambda p: p.name)
def test_the_covers_line_counts_the_rows_the_draft_actually_quotes(draft: Path) -> None:
    """One more typed number in text that gets filed on a public tracker.

    Correct today; here so that splitting or merging a draft cannot leave it
    claiming a coverage it no longer has.
    """
    text = draft.read_text()
    sections = SECTION_RE.findall(text)
    assert sections, f"no row sections parsed from {draft.name}"
    match = re.search(r"Covers (\d+) rows? of", text)
    assert match is not None, f"{draft.name} does not say how many rows it covers"
    assert int(match.group(1)) == len(sections), (
        f"{draft.name} says it covers {match.group(1)} rows and quotes {len(sections)}"
    )


@pytest.mark.parametrize("draft", DRAFTS, ids=lambda p: p.name)
def test_every_row_a_draft_quotes_still_says_what_the_gap_log_says(draft: Path) -> None:
    """Each quoted observation, against the row it was copied from."""
    rows = _rows()
    sections = SECTION_RE.findall(draft.read_text())
    assert sections, f"no row sections parsed from {draft.name}"

    for kind, body in sections:
        row = rows.get(kind)
        assert row is not None, f"{draft.name} quotes kind {kind!r}, which is not in gaps.jsonl"

        for label, key in (
            ("Attempted", "attempted"),
            ("Returned", "returned"),
            ("Expected", "expected"),
        ):
            quoted = _field(body, label)
            assert quoted is not None, f"{draft.name} [{kind}] has no {label} line"
            assert _normalise(quoted) == _normalise(str(row[key])), (
                f"{draft.name} [{kind}] {label} drifted from gaps.jsonl:\n"
                f"  draft: {_normalise(quoted)[:160]}\n"
                f"  log:   {_normalise(str(row[key]))[:160]}"
            )


@pytest.mark.parametrize("draft", DRAFTS, ids=lambda p: p.name)
def test_a_draft_for_a_closed_row_says_so(draft: Path) -> None:
    """A row closed by a later run must not be filed as an open defect.

    Seven rows closed between the two runs; a draft still describing one as
    broken would send a maintainer after a bug that is already fixed.
    """
    rows = _rows()
    text = draft.read_text()
    for kind, _ in SECTION_RE.findall(text):
        closed = rows.get(kind, {}).get("closed")
        if closed is not None:
            assert isinstance(closed, dict)
            assert "closed" in text.lower(), (
                f"{draft.name} quotes {kind!r}, which gaps.jsonl records as closed "
                f"at {closed['commit']}, without saying so"
            )
