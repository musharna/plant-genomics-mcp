"""The two README sentences that describe the ARF dossier run carry its
numbers — call count and gap count — and nothing else in the suite reads
them: after the full-family run they still said "48-call" and "30 gaps"
(round 1 of the task-7 review). Both numbers are derived here from the
committed artifacts and must appear in both sentences.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARF = ROOT / "examples" / "arf_family"


def _line_count(path: Path) -> int:
    return sum(1 for line in path.read_text().splitlines() if line.strip())


def test_readme_sentences_carry_the_run_counts():
    calls = _line_count(ARF / "calls.jsonl")
    gaps = _line_count(ARF / "gaps.jsonl") + _line_count(ARF / "gaps_auto.jsonl")
    # The counts really came from the run, not from empty files.
    assert calls > 100 and gaps > 30, (calls, gaps)

    checked = 0
    for readme in (ROOT / "README.md", ROOT / "examples" / "README.md"):
        text = readme.read_text()
        sentence = next(
            para for para in text.split("\n\n") if "arf_family/PAGE.md" in para
        )  # exactly one paragraph links the page; StopIteration if none does
        assert f"{calls}-call" in sentence, (readme.name, sentence)
        assert f"{gaps} gaps" in sentence, (readme.name, sentence)
        checked += 1
    assert checked == 2

    # Positive control: the same checks fire on the stale wording.
    stale = "A worked 48-call run over three loci, with the 30 gaps it turned up"
    assert f"{calls}-call" not in stale and f"{gaps} gaps" not in stale
