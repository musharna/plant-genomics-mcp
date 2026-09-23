"""Prose counts about the ARF dossier run are claims about committed files
and are derived from them here: the two README sentences carry the call
and gap counts, and the PAGE.md figure paragraph carries the per-tool
range of distinct positions the figure draws. Nothing else in the suite
reads these sentences, so a re-run that changes a count would otherwise
leave stale numbers in public text.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from examples.arf_family.coverage import call_key

ROOT = Path(__file__).resolve().parents[1]
ARF = ROOT / "examples" / "arf_family"


def _line_count(path: Path) -> int:
    return sum(1 for line in path.read_text().splitlines() if line.strip())


def _check_sentence(text: str, calls: int, gaps: int, name: str) -> None:
    sentence = next(
        para for para in text.split("\n\n") if "arf_family/PAGE.md" in para
    )  # exactly one paragraph links the page; StopIteration if none does
    assert f"{calls}-call" in sentence, (name, sentence)
    assert f"{gaps} gaps" in sentence, (name, sentence)


def test_readme_sentences_carry_the_run_counts():
    calls = _line_count(ARF / "calls.jsonl")
    gaps = _line_count(ARF / "gaps.jsonl") + _line_count(ARF / "gaps_auto.jsonl")
    # The counts really came from the run, not from empty files. Every chain
    # tool goes through a batch form now, so a 114-gene run is 64 calls.
    assert calls > 50 and gaps > 30, (calls, gaps)

    checked = 0
    for readme in (ROOT / "README.md", ROOT / "examples" / "README.md"):
        text = readme.read_text()
        _check_sentence(text, calls, gaps, readme.name)
        checked += 1

        # Positive control: the same check on the same README with either
        # count off by one must fail, and each edit must really have landed.
        for old, new in (
            (f"{gaps} gaps", f"{gaps - 1} gaps"),
            (f"{calls}-call", f"{calls - 1}-call"),
        ):
            stale = text.replace(old, new)
            assert stale != text, old
            with pytest.raises(AssertionError, match=re.escape(new)):
                _check_sentence(stale, calls, gaps, readme.name)
    assert checked == 2


def _distinct_positions_per_tool() -> dict[str, int]:
    seen: dict[str, set[tuple[str, int]]] = {}
    for line in (ARF / "calls.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        # One figure row per tool run, as coverage.call_key keys it.
        seen.setdefault(call_key(r), set()).add((r["organism"], r["n_bytes"]))
    return {tool: len(v) for tool, v in seen.items()}


def test_page_figure_paragraph_range_is_derived_from_the_calls_log():
    """The figure draws exact ties on one point, so a tool draws one point
    per distinct (organism, byte count); PAGE.md names the two extremes of
    that range and they must match a recount of calls.jsonl."""
    counts = _distinct_positions_per_tool()
    assert len(counts) == 16, counts  # one row per chain tool
    lo, hi = min(counts.values()), max(counts.values())
    assert lo < hi
    # Name every tool at the low end and count the rest, rather than one
    # arbitrary example of each (a reviewer read the pick as meaningful).
    lo_tools = sorted(t for t, n in counts.items() if n == lo)
    rest = [t for t, n in counts.items() if n != lo]
    assert all(counts[t] == hi for t in rest), counts  # the claim assumes two levels
    text = (ARF / "PAGE.md").read_text().replace("\n", " ")
    named = " and ".join(f"`{t}`" for t in lo_tools)
    claim = f"{lo} positions for {named}, {hi} for the other {len(rest)}"
    assert claim in text, claim
    # The sentence is about the tie rule, and says so.
    assert "exact ties draw on one point" in text
