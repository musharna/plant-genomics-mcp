"""The two README sentences that describe the ARF dossier run carry its
numbers — call count and gap count — and nothing else in the suite reads
them: after the full-family run they still said "48-call" and "30 gaps"
(round 1 of the task-7 review). Both numbers are derived here from the
committed artifacts and must appear in both sentences.
"""

from __future__ import annotations

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

        # Positive control: the same check on the same README with the gap
        # count off by one must fail, and the edit must really have landed.
        stale = text.replace(f"{gaps} gaps", f"{gaps - 1} gaps")
        assert stale != text
        with pytest.raises(AssertionError, match=f"{gaps - 1} gaps"):
            _check_sentence(stale, calls, gaps, readme.name)
    assert checked == 2
