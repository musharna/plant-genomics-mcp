"""The README's tool counts are the server's.

The README states the tool count twice and breaks it down twice. #130 added
"2 member lists" to one breakdown and not the other, which then summed to 51
under a stated 53 — a count kept in two places is updated in one. Every stated
count and every breakdown's sum is checked against ``server.TOOLS``.
"""

from __future__ import annotations

import re
from pathlib import Path

from plant_genomics_mcp import server

README = Path(__file__).resolve().parents[1] / "README.md"

STATED = re.compile(r"\*\*(\d+) tools\b")
# "29 single-locus + 1 motif lookup + ... + 5 cross-source synthesis", wrapped
# across lines in one of the two places.
BREAKDOWN = re.compile(r"(\d+) single-locus((?:\s*\+\s*\d+[^+]*?)+?)cross-source synthesis")


def test_every_stated_tool_count_and_breakdown_matches_the_server() -> None:
    text = README.read_text(encoding="utf-8")
    tools = len(server.TOOLS)

    stated = [int(n) for n in STATED.findall(text)]
    breakdowns = [
        int(first) + sum(int(n) for n in re.findall(r"\+\s*(\d+)", rest))
        for first, rest in BREAKDOWN.findall(text)
    ]
    # Positive control: both statements and both breakdowns are found, so a
    # pattern that matches nothing cannot pass by checking nothing.
    assert len(stated) == 2, stated
    assert len(breakdowns) == 2, breakdowns

    assert stated == [tools, tools], (stated, tools)
    assert breakdowns == [tools, tools], (breakdowns, tools)
