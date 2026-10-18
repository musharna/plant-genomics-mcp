"""The README's benchmark summary states the tested result, not an extreme.

#239 rewrote the benchmark page to lead with what is and is not separated,
but the README's "Does it help?" paragraph, written earlier and kept in a
second place, still set "48 of 52 vs all 52 right in every run" beside the
slowest single answers (86 s vs 705 s), with no test. A summary kept in two
places is updated in one. This pins the paragraph to ``stats.tsv`` and
``summary.tsv``: it must give the paired test's p-value with "not
separated", and must not quote either setup's single-run maximum time.
"""

from __future__ import annotations

import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
BENCH = ROOT / "benchmarks" / "agent_qa"
HEADING = "## 📊 Does it help?"


def _section() -> str:
    text = README.read_text(encoding="utf-8")
    start = text.index(HEADING)
    end = text.find("\n## ", start + len(HEADING))
    return text[start : end if end != -1 else None]


def _rows(name: str) -> list[dict[str, str]]:
    with (BENCH / name).open(encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def test_summary_states_the_paired_test_and_no_single_run_maximum() -> None:
    section = _section()
    # Positive control: the section is the real one and the tables were read,
    # so the absence checks below cannot pass on an empty string.
    assert "52 plant gene-lookup questions" in section, section[:200]
    mcnemar = [
        r
        for r in _rows("stats.tsv")
        if r["test"] == "mcnemar_exact" and r["arm"] == "mcp" and r["versus"] == "web"
    ]
    assert len(mcnemar) == 1, mcnemar
    p = float(mcnemar[0]["p_value"])
    assert f"p = {p:g}" in section, (p, section)
    assert "not separated" in section

    maxima = [
        r["seconds_max"]
        for r in _rows("summary.tsv")
        if r["level"] == "arm" and r["arm"] in {"web", "mcp"}
    ]
    assert len(maxima) == 2, maxima
    for value in maxima:
        whole = f"{float(value):g}".split(".")[0]
        assert f"{whole} s" not in section, (value, section)
