"""The README's tool counts are the server's.

#130 added "2 member lists" to one of the README's two breakdowns and not
the other, which then summed to 51 under a stated 53: a count kept in two
places is updated in one. The README states the count once; the breakdown
lives once, in docs/reference.md (moved there 2026-10-18 to keep the front
page short), which also restates the count beside it. All three are checked
against ``server.TOOLS``, and a breakdown reappearing in the README fails here.
"""

from __future__ import annotations

import re
from pathlib import Path

from plant_genomics_mcp import server

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
REFERENCE = ROOT / "docs" / "reference.md"

STATED = re.compile(r"\*\*(\d+) tools\b")
REFERENCE_STATED = re.compile(r"The (\d+) tools are\b")
# "29 single-locus + 1 motif lookup + ... + 5 cross-source synthesis", wrapped
# across lines.
FIRST = re.compile(r"(\d+) single-locus")
END = "cross-source synthesis"
PART = re.compile(r"\+\s*(\d+)")


def _breakdown_sums(text: str) -> list[int]:
    sums = []
    for match in FIRST.finditer(text):
        rest = text[match.end() : text.index(END, match.end())]
        sums.append(int(match.group(1)) + sum(int(n) for n in PART.findall(rest)))
    return sums


def test_every_stated_tool_count_and_breakdown_matches_the_server() -> None:
    readme = README.read_text(encoding="utf-8")
    reference = REFERENCE.read_text(encoding="utf-8")
    tools = len(server.TOOLS)

    stated = [int(n) for n in STATED.findall(readme)]
    reference_stated = [int(n) for n in REFERENCE_STATED.findall(reference)]
    breakdowns = _breakdown_sums(reference)
    # Positive control: each statement and the breakdown are found, so a
    # pattern that matches nothing cannot pass by checking nothing.
    assert len(stated) == 1, stated
    assert len(reference_stated) == 1, reference_stated
    assert len(breakdowns) == 1, breakdowns
    # The breakdown is kept in one place only.
    assert _breakdown_sums(readme) == [], "the README restates the tool breakdown"

    assert stated == [tools], (stated, tools)
    assert reference_stated == [tools], (reference_stated, tools)
    assert breakdowns == [tools], (breakdowns, tools)
