"""Prose counts about the ARF dossier run are claims about committed files
and are derived from them here: the two README sentences carry the call
and gap counts, and the PAGE.md figure paragraph carries how many
distinct positions the figure draws for each tool. Nothing else in the suite
reads these sentences, so a re-run that changes a count would otherwise
leave stale numbers in public text.
"""

from __future__ import annotations

import csv
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
        # plot_coverage.R draws a call at its largest answer and leaves out a
        # call that answered no locus (max_answer_chars 0). One figure row per
        # chain tool, named on the page as the chain names it.
        if r["max_answer_chars"] > 0:
            seen.setdefault(r["chain_tool"], set()).add((r["organism"], r["max_answer_chars"]))
    return {tool: len(v) for tool, v in seen.items()}


def _tie_claims(counts: dict[str, int]) -> list[str]:
    """The page's tie sentence, one fragment per level: the most common
    count by number of tools, every other count by the tools it names."""
    levels: dict[int, list[str]] = {}
    for tool, n in counts.items():
        levels.setdefault(n, []).append(tool)
    common = max(levels, key=lambda n: len(levels[n]))
    claims = [f"{common} positions for {len(levels[common])} tools"]
    for n in sorted(levels, reverse=True):
        if n == common:
            continue
        names = [f"`{t}`" for t in sorted(levels[n])]
        named = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
        claims.append(f"{n} for {named}")
    return claims


def _check_ties(text: str, counts: dict[str, int]) -> None:
    for claim in _tie_claims(counts):
        assert claim in text, claim


def test_page_figure_paragraph_range_is_derived_from_the_calls_log():
    """The figure draws exact ties on one point, so a tool draws one point
    per distinct (organism, largest answer); PAGE.md names how many each
    tool draws and that must match a recount of calls.jsonl."""
    counts = _distinct_positions_per_tool()
    assert len(counts) == 16, counts  # one row per chain tool, all drawn
    assert len(set(counts.values())) > 1, counts  # the ties differ by tool
    text = (ARF / "PAGE.md").read_text().replace("\n", " ")
    _check_ties(text, counts)
    # The sentence is about the tie rule, and says so.
    assert "exact ties draw on one point" in text

    # Negative control: the same page against a recount with one tool's
    # count off by one must fail, naming the level that no longer holds.
    tool = min(counts, key=lambda t: (counts[t], t))
    off = {**counts, tool: counts[tool] + 1}
    with pytest.raises(AssertionError, match=re.escape(f"`{tool}`")):
        _check_ties(text, off)


def _false_rows_with_the_pb1_profile() -> list[str]:
    """Symbols of the genes.tsv rows marked `false` whose kept candidate row
    lists the PB1 profile entry IPR053793."""
    entries: dict[str, set[str]] = {}
    with (ARF / "family_candidates.tsv").open() as f:
        for row in csv.DictReader(f, delimiter="\t"):
            if row["kept"] == "true":
                entries.setdefault(row["locus"], set()).update(row["interpro_entries"].split(","))
    with (ARF / "genes.tsv").open() as f:
        return [
            row["symbol"]
            for row in csv.DictReader(f, delimiter="\t")
            if row["has_pb1_domain"] == "false" and "IPR053793" in entries.get(row["locus"], set())
        ]


def _check_pb1_paragraph(text: str, symbols: list[str]) -> None:
    para = next(p for p in text.split("\n\n") if "`has_pb1_domain` column" in p).replace(
        "\n", " "
    )  # exactly one paragraph defines the column; StopIteration if none does
    assert f"{len(symbols)} rows marked `false`" in para, para
    for named in ("ARF4", "ARF10", "ARF16"):
        assert named in para, (named, para)
        assert named in symbols, (named, symbols)


def test_page_says_what_has_pb1_domain_means_and_counts_the_profile_only_rows():
    """The column is true only on InterPro's IPR033389 / PF02309. PAGE.md says
    so, and says how many `false` rows carry the PB1 profile entry all the
    same; that count is a recount of the two committed tables."""
    symbols = _false_rows_with_the_pb1_profile()
    assert len(symbols) > 5, symbols  # counted from real tables, not empty ones
    # ARF8 has the Pfam hit, so it is `true` and not one of them.
    assert "ARF8" not in symbols, symbols
    text = (ARF / "PAGE.md").read_text()
    _check_pb1_paragraph(text, symbols)
    assert "IPR033389 or Pfam PF02309" in text.replace("\n", " ")

    # Negative control: the same page with the count off by one must fail,
    # and the edit must really have landed.
    stale = text.replace(f"{len(symbols)} rows marked", f"{len(symbols) + 1} rows marked")
    assert stale != text
    with pytest.raises(AssertionError, match=re.escape(f"{len(symbols) + 1} rows marked")):
        _check_pb1_paragraph(stale, symbols)
