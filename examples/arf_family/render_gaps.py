"""Render the run's gap log as the `## Known gaps` section of `PAGE.md`.

`gaps.jsonl` (27 hand-logged rows) and `gaps_auto.jsonl` (3 rows the
runner flagged itself) are the record of what the 48-call dossier run
turned up. `PAGE.md` shows all 30 of them. Typing them into the page by
hand would mean a page that is correct on the day it is written and
silently wrong the next time a row is corrected — which has already
happened once here: four rows in `gaps.jsonl` were false and were
rewritten in a later pass.

So the section is generated, not written, and
`tests/test_arf_gaps_page.py` fails if the text in `PAGE.md` is not
byte-identical to what `render_section` produces from the two JSONL
files. The page cannot drift from the log.

Everything except `main` is a pure function over plain Python data, so
the rendering can be tested on synthetic rows without the real files:

- `truncate` cuts on a word boundary, so a long `returned` becomes one
  scannable line without a mid-word break. It is NOT lossy silently: the
  page says, once, where the full text lives.
- `subject_of` names the row's subject by finding the first chain tool
  name that occurs in the row's own `attempted` text. It is deliberately
  not a lookup table: a table would be one more thing to keep in sync.
  Rows whose `attempted` names no single tool (they are about the tool
  surface as a whole) fall back to `SEVERAL_TOOLS`.
- `render_section` raises on an origin it has no heading for, rather
  than dropping the row. A renderer that silently skips a row it does
  not understand turns an unclassified gap into no gap at all.

Run `python -m examples.arf_family.render_gaps` to print the section, or
with `--check` to diff it against the committed `PAGE.md`.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

HERE = Path(__file__).parent
GAPS_PATH = HERE / "gaps.jsonl"
GAPS_AUTO_PATH = HERE / "gaps_auto.jsonl"
PAGE_PATH = HERE / "PAGE.md"

SECTION_HEADING = "## Known gaps"

# One heading per `origin` value in gaps.jsonl. `unverified` is for rows
# whose cause could not be attributed to the tool or to upstream from
# inside the MCP (the family run may not leave it: no direct HTTP). A row
# with any other origin makes `render_section` raise rather than be filed
# under the wrong heading.
ORIGIN_HEADINGS: dict[str, str] = {
    "tool": "### Defects in this tool",
    "upstream-passthrough": "### Upstream values passed through unchanged",
    "unverified": "### Not attributable from inside the MCP",
}

# Rows the runner logged itself carry no `origin`: an oversize response
# is this server's envelope, not an upstream value.
AUTO_ORIGIN = "tool"

SEVERAL_TOOLS = "several tools"

RETURNED_CHARS = 120
ELLIPSIS = "…"

# A lone `_` (one not inside a word) is emphasis syntax in Markdown, and
# a formatter run over the page rewrites a pair of them — `batch_ form`
# twice on one line became `batch* form` twice under prettier 3.x, which
# silently edits a value quoted from the gap log. Escaping exactly the
# non-intraword underscores renders the same and survives the formatter;
# escaping every underscore does not, because the formatter strips the
# unnecessary ones back out.
_LONE_UNDERSCORE = re.compile(r"(?<![A-Za-z0-9])_|_(?![A-Za-z0-9])")


def escape_markdown(text: str) -> str:
    """Escape the emphasis syntax in text quoted from the gap log."""
    return _LONE_UNDERSCORE.sub(r"\\_", text)


def balanced_backticks(text: str) -> str:
    """Drop a trailing unclosed code span left behind by truncation.

    `truncate` cuts on a word boundary and can land between the two
    backticks of an inline code span, which turns the rest of the line
    into code. Cutting back to the opening backtick is the only change
    made to the text after truncation.
    """
    if text.count("`") % 2 == 0:
        return text
    return text[: text.rfind("`")].rstrip() + ELLIPSIS


def truncate(text: str, limit: int = RETURNED_CHARS) -> str:
    """Cut `text` to at most `limit` characters on a word boundary.

    Returns `text` unchanged when it already fits, so the `…` in a
    rendered line always means something was dropped.
    """
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rstrip()
    space = cut.rfind(" ")
    if space > 0:
        cut = cut[:space]
    return cut.rstrip(" ,;:.") + ELLIPSIS


def subject_of(attempted: str, tool_names: Sequence[str]) -> str:
    """Name the tool a gap row is about, from the row's own text.

    The first tool name that occurs in `attempted` wins, by position in
    the string. A row that names no tool is about the surface as a whole
    and gets `SEVERAL_TOOLS`.
    """
    hits = [(attempted.find(name), name) for name in tool_names if name in attempted]
    if not hits:
        return SEVERAL_TOOLS
    return min(hits)[1]


def render_row(row: dict, tool_names: Sequence[str]) -> str:
    """One gap row as one Markdown list item: subject — what happens — expected."""
    if row.get("auto"):
        subject = row["tool"]
        # A full-family auto row groups every locus with the same outcome;
        # `locus` then reads "N loci" and the list is in the JSONL.
        happened = f"{row['locus']}: {row['returned']}"
    else:
        subject = subject_of(row["attempted"], tool_names)
        happened = row["returned"]
    # Backticks mark a real tool name; the cross-tool fallback is prose.
    label = SEVERAL_TOOLS if subject == SEVERAL_TOOLS else f"`{subject}`"
    what = escape_markdown(balanced_backticks(truncate(happened)))
    expected = escape_markdown(row["expected"])
    return f"- **{label}** ({row['kind']}) — {what} — expected: {expected}"


def origin_of(row: dict) -> str:
    return AUTO_ORIGIN if row.get("auto") else row["origin"]


def render_section(
    hand_rows: Iterable[dict],
    auto_rows: Iterable[dict],
    tool_names: Sequence[str],
) -> str:
    """The whole `## Known gaps` section, heading included.

    Raises `ValueError` on a row whose `origin` has no heading: an
    unclassified gap must stop the build, not vanish from the page.
    """
    rows = list(hand_rows) + list(auto_rows)
    grouped: dict[str, list[dict]] = {key: [] for key in ORIGIN_HEADINGS}
    for row in rows:
        origin = origin_of(row)
        if origin not in grouped:
            raise ValueError(
                f"gap row {row.get('kind')!r} has origin {origin!r}, "
                f"which has no heading in ORIGIN_HEADINGS "
                f"({sorted(ORIGIN_HEADINGS)})"
            )
        grouped[origin].append(row)

    out = [
        SECTION_HEADING,
        "",
        f"All {len(rows)} rows the run logged, one line each: what was "
        f"attempted, what came back, what was expected instead. Full text "
        f"and the raw response each row was read from are in "
        f"[`gaps.jsonl`](gaps.jsonl) and [`gaps_auto.jsonl`](gaps_auto.jsonl).",
    ]
    for origin, heading in ORIGIN_HEADINGS.items():
        if not grouped[origin]:
            continue  # an empty heading would read as a category with no findings
        out += ["", heading, ""]
        out += [render_row(row, tool_names) for row in grouped[origin]]
    return "\n".join(out)


def extract_section(page_text: str, heading: str = SECTION_HEADING) -> str:
    """The committed section, from its heading to the next same-level heading.

    Raises `LookupError` when the heading is absent, so a renamed or
    deleted section fails loudly instead of comparing empty-to-empty.
    """
    lines = page_text.splitlines()
    try:
        start = lines.index(heading)
    except ValueError as exc:
        raise LookupError(f"{heading!r} not found in the page") from exc
    level = heading.split(" ", 1)[0] + " "
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith(level):
            end = i
            break
    return "\n".join(lines[start:end]).rstrip("\n")


def read_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare the rendered section against PAGE.md; exit 1 if they differ",
    )
    args = parser.parse_args(argv)

    from examples.arf_family.chain import CHAIN

    section = render_section(
        read_rows(GAPS_PATH), read_rows(GAPS_AUTO_PATH), [name for name, _ in CHAIN]
    )
    if not args.check:
        print(section)
        return 0

    committed = extract_section(PAGE_PATH.read_text())
    if committed == section:
        print(f"{PAGE_PATH.name}: Known gaps section matches the gap log")
        return 0
    print(
        f"{PAGE_PATH.name}: Known gaps section is stale. Replace it with the "
        f"output of `python -m examples.arf_family.render_gaps`.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
