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
    # The counts really came from the run, not from empty files.
    assert calls > 100 and gaps > 30, (calls, gaps)

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
        seen.setdefault(r["tool"], set()).add((r["organism"], r["n_bytes"]))
    return {tool: len(v) for tool, v in seen.items() if not tool.startswith("batch_")}


def test_page_figure_paragraph_range_is_derived_from_the_calls_log():
    """The figure draws exact ties on one point, so a per-locus tool draws
    one point per distinct (organism, byte count); PAGE.md names the two
    extremes of that range and they must match a recount of calls.jsonl."""
    counts = _distinct_positions_per_tool()
    assert len(counts) == 8, counts
    lo_tool, lo = min(counts.items(), key=lambda kv: (kv[1], kv[0]))
    hi_tool, hi = max(counts.items(), key=lambda kv: (kv[1], kv[0]))
    assert lo < hi
    text = (ARF / "PAGE.md").read_text().replace("\n", " ")
    claim = f"from {lo} positions for `{lo_tool}` to {hi} for `{hi_tool}`"
    assert claim in text, claim
    # The sentence is about the tie rule, and says so.
    assert "exact ties draw on one point" in text
