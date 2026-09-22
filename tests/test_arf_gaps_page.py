"""`PAGE.md`'s gap list is generated, and these tests are what keeps it so.

The page shows all 49 gap rows from the 248-call ARF family dossier run. Hand
typing them would produce a page that is right the day it is written and
silently wrong afterwards — `gaps.jsonl` has already been corrected once,
four rows of it, after the first pass filed claims that turned out to be
false. So `examples/arf_family/render_gaps.py` renders the section and
`test_page_gaps_section_is_generated_from_the_gap_log` asserts the text
in the page is byte-identical to it.

Each unit test below pairs its negative assertion with the legitimate
case in the same test, because a renderer test that only ever sees good
input cannot be shown to discriminate:

- the origin router is checked on a row it accepts AND a row it must
  refuse, and refusing means raising, never dropping the row;
- `truncate` is checked on a string that must be cut AND one that must
  come back untouched, so "everything got an ellipsis" cannot pass;
- `subject_of` is checked on text naming a tool, text naming two (first
  by position wins), and text naming none;
- `extract_section` is checked on a page that has the heading AND one
  that does not.
"""

from __future__ import annotations

import json

import pytest

from examples.arf_family import render_gaps
from examples.arf_family.chain import CHAIN
from examples.arf_family.render_gaps import (
    ELLIPSIS,
    ORIGIN_HEADINGS,
    SECTION_HEADING,
    SEVERAL_TOOLS,
    escape_markdown,
    extract_section,
    read_rows,
    render_row,
    render_section,
    subject_of,
    truncate,
)

TOOL_NAMES = [name for name, _ in CHAIN]

# Synthetic rows: the same shape as gaps.jsonl / gaps_auto.jsonl, none of
# the real content. A fixture that reproduced the real rows would only
# test that the renderer agrees with itself.
HAND_TOOL_ROW = {
    "kind": "synthetic-tool-kind",
    "attempted": "interpro_domains({'locus': 'FAKE1'})",
    "returned": "a short synthetic answer",
    "expected": "a different synthetic answer",
    "raw": "raw/FAKE1__interpro_domains.json",
    "origin": "tool",
    "auto": False,
}
HAND_UPSTREAM_ROW = {
    "kind": "synthetic-upstream-kind",
    "attempted": "kegg_pathways({'locus': 'FAKE1'})",
    "returned": "a synthetic value upstream sent through unchanged",
    "expected": "a normalised value",
    "raw": "raw/FAKE1__kegg_pathways.json",
    "origin": "upstream-passthrough",
    "auto": False,
}
AUTO_ROW = {
    "kind": "oversize",
    "locus": "FAKE1",
    "tool": "gene_report",
    "attempted": "gene_report({'locus': 'FAKE1'})",
    "returned": "999999 bytes",
    "expected": "a usable answer within 200 kB",
    "auto": True,
}


def _section_of(heading: str, text: str) -> list[str]:
    """The lines under `heading`, up to the next `###` heading."""
    lines = text.splitlines()
    start = lines.index(heading) + 1
    end = len(lines)
    for i in range(start, len(lines)):
        if lines[i].startswith("### "):
            end = i
            break
    return [line for line in lines[start:end] if line.strip()]


def test_render_section_files_each_row_under_the_heading_for_its_origin() -> None:
    out = render_section([HAND_TOOL_ROW, HAND_UPSTREAM_ROW], [AUTO_ROW], TOOL_NAMES)

    tool_lines = _section_of(ORIGIN_HEADINGS["tool"], out)
    upstream_lines = _section_of(ORIGIN_HEADINGS["upstream-passthrough"], out)

    # Both directions in one test: a router that put everything under one
    # heading would pass a one-sided assertion.
    assert any("synthetic-tool-kind" in line for line in tool_lines)
    assert not any("synthetic-tool-kind" in line for line in upstream_lines)
    assert any("synthetic-upstream-kind" in line for line in upstream_lines)
    assert not any("synthetic-upstream-kind" in line for line in tool_lines)

    # The auto row has no `origin` and belongs with the tool's own defects.
    assert any("999999 bytes" in line for line in tool_lines)

    assert out.splitlines()[0] == SECTION_HEADING
    assert "All 3 open rows" in out
    # Every row rendered exactly once, under one heading or the other.
    assert len(tool_lines) + len(upstream_lines) == 3


def test_a_closed_row_is_listed_last_with_both_observations() -> None:
    closed = {
        **HAND_TOOL_ROW,
        "kind": "synthetic-closed-kind",
        "closed": {"commit": "abc1234", "returned": "the synthetic answer now expected"},
    }
    out = render_section([HAND_TOOL_ROW, closed], [AUTO_ROW], TOOL_NAMES)
    closed_lines = _section_of(render_gaps.CLOSED_HEADING, out)
    tool_lines = _section_of(ORIGIN_HEADINGS["tool"], out)
    # The closed row moves out of its origin heading; the open one stays.
    assert len(closed_lines) == 1 and "synthetic-closed-kind" in closed_lines[0]
    assert "was: a short synthetic answer" in closed_lines[0]
    assert "now, at `abc1234`: the synthetic answer now expected" in closed_lines[0]
    assert not any("synthetic-closed-kind" in line for line in tool_lines)
    assert any("synthetic-tool-kind" in line for line in tool_lines)
    assert "All 2 open rows" in out and "The 1 rows logged against an earlier run" in out
    # Positive control: no closed rows, no closed heading, no closed sentence.
    plain = render_section([HAND_TOOL_ROW], [AUTO_ROW], TOOL_NAMES)
    assert render_gaps.CLOSED_HEADING not in plain and "earlier run" not in plain
    assert "All 2 open rows" in plain


def test_render_section_raises_on_an_origin_it_has_no_heading_for() -> None:
    """An unclassified row must stop the render, not disappear from the page."""
    # Positive control: the same call with a known origin succeeds.
    assert render_section([HAND_TOOL_ROW], [], TOOL_NAMES)

    unverified = {**HAND_TOOL_ROW, "origin": "unverified"}
    out = render_section([unverified], [], TOOL_NAMES)
    assert any(
        "synthetic-tool-kind" in line for line in _section_of(ORIGIN_HEADINGS["unverified"], out)
    )

    unclassified = {**HAND_TOOL_ROW, "origin": "probably-upstream"}
    with pytest.raises(ValueError, match="probably-upstream"):
        render_section([unclassified], [], TOOL_NAMES)


def test_truncate_cuts_only_what_is_too_long_and_never_mid_word() -> None:
    short = "a short line"
    assert truncate(short, limit=40) == short
    assert ELLIPSIS not in truncate(short, limit=40)

    long = "alpha bravo charlie delta echo foxtrot golf hotel india juliett"
    cut = truncate(long, limit=30)
    assert cut.endswith(ELLIPSIS)
    assert len(cut) <= 31
    assert long.startswith(cut[: -len(ELLIPSIS)].rstrip())
    # A word boundary, not a character count: no partial word survives.
    assert all(word in long.split() for word in cut[: -len(ELLIPSIS)].split())


def test_subject_of_reads_the_tool_out_of_the_rows_own_text() -> None:
    assert subject_of("interpro_domains on three genes", TOOL_NAMES) == "interpro_domains"
    # First by position, not first in the chain table.
    assert subject_of("read gene_report, then interpro_domains", TOOL_NAMES) == "gene_report"
    # A row about the surface as a whole names no tool.
    assert subject_of("run the chain without 48 round trips", TOOL_NAMES) == SEVERAL_TOOLS


def test_render_row_escapes_emphasis_so_a_formatter_cannot_edit_the_quoted_text() -> None:
    """prettier rewrote `batch_ form` to `batch* form` on the real page."""
    row = {**HAND_TOOL_ROW, "returned": "no batch_ form, and no batch_ form here either"}
    line = render_row(row, TOOL_NAMES)
    assert "batch\\_ form" in line
    assert "batch_ form" not in line.replace("batch\\_ form", "")
    # Positive control: an underscore inside a word is not emphasis and
    # must be left alone, or every tool name on the page grows a backslash.
    assert escape_markdown("interpro_domains") == "interpro_domains"


def test_extract_section_finds_the_section_and_raises_when_it_is_gone() -> None:
    page = "# Title\n\nintro\n\n## Known gaps\n\nbody\n\n## Next\n\ntail\n"
    assert extract_section(page) == "## Known gaps\n\nbody"

    with pytest.raises(LookupError, match="Known gaps"):
        extract_section("# Title\n\nno such section\n")


def test_page_gaps_section_is_generated_from_the_gap_log() -> None:
    """The real page against the real log — the anti-drift assertion.

    Regenerate with `python -m examples.arf_family.render_gaps` and paste
    the output over the section in `PAGE.md`.
    """
    expected = render_section(
        read_rows(render_gaps.GAPS_PATH),
        read_rows(render_gaps.GAPS_AUTO_PATH),
        TOOL_NAMES,
    )
    assert extract_section(render_gaps.PAGE_PATH.read_text()) == expected


def test_every_gap_row_reaches_the_page() -> None:
    """Positive control for the assertion above.

    Byte-equality passes on two empty strings and on a renderer that
    dropped half its input. This counts the rows on the page against the
    rows in the two logs, and pins the count to the real files.
    """
    hand = read_rows(render_gaps.GAPS_PATH)
    auto = read_rows(render_gaps.GAPS_AUTO_PATH)
    assert (len(hand), len(auto)) == (40, 9)

    section = extract_section(render_gaps.PAGE_PATH.read_text())
    bullets = [line for line in section.splitlines() if line.startswith("- **")]
    assert len(bullets) == len(hand) + len(auto)

    for row in hand:
        assert any(f"({row['kind']})" in line for line in bullets), row["kind"]


def test_prose_counts_of_open_and_closed_rows_match_the_gap_log() -> None:
    """Every file that types the open/closed split by hand, recounted.

    The rendered section is pinned byte-for-byte, but the sentences above
    it are typed, and a typed count drifted (8 for 7) the first time the
    log changed under it. The first version of this test read the page and
    the two READMEs — and CHANGELOG.md, which quotes the same number in its
    own words, kept the stale 8 for another week because it was outside the
    list. The rule is the number, not the file: anything that states the
    split gets recounted here.
    """
    hand = read_rows(render_gaps.GAPS_PATH)
    auto = read_rows(render_gaps.GAPS_AUTO_PATH)
    n_closed = sum(1 for row in hand if "closed" in row)
    n_open = len(hand) + len(auto) - n_closed
    assert 0 < n_closed < len(hand)  # positive control: the split is real

    page = render_gaps.PAGE_PATH.read_text()
    assert f"the {n_open} open rows" in page
    assert f"the {n_closed} closed ones" in page
    for readme in (
        render_gaps.PAGE_PATH.parents[2] / "README.md",
        render_gaps.PAGE_PATH.parents[1] / "README.md",
    ):
        text = readme.read_text()
        assert f"{len(hand) + len(auto)} gaps it" in text, readme
        assert f"({n_closed} since closed)" in text, readme

    changelog = render_gaps.PAGE_PATH.parents[2] / "CHANGELOG.md"
    assert f"{n_closed} of the {len(hand)} hand-logged gap rows" in changelog.read_text()


def test_main_check_exits_zero_against_the_committed_page() -> None:
    """Real execution of the CLI path, not only the pure functions."""
    assert render_gaps.main(["--check"]) == 0


def test_the_gap_log_is_still_jsonl_this_renderer_can_read() -> None:
    """Boundary check on the file format `read_rows` assumes."""
    text = render_gaps.GAPS_PATH.read_text()
    assert text.endswith("\n")
    for i, line in enumerate(text.splitlines(), 1):
        assert isinstance(json.loads(line), dict), f"line {i} is not a JSON object"
