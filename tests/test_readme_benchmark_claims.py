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
HEADING = "## Does it help?"


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


def test_rerun_sentence_matches_the_confirmatory_tables() -> None:
    """The 2026-10-18 re-run's line in "Does it help?" is pinned to its own
    stats.tsv and summary.tsv, so the README cannot keep a verdict, a count or
    a ratio the confirmatory sweep did not produce."""
    section = _section()
    rerun = BENCH / "rerun_2026-10-18"
    with (rerun / "stats.tsv").open(encoding="utf-8") as f:
        stats = {r["metric"]: r for r in csv.DictReader(f, delimiter="\t")}
    with (rerun / "summary.tsv").open(encoding="utf-8") as f:
        summary = {r["arm"]: r for r in csv.DictReader(f, delimiter="\t")}
    # Positive control: the tables were read and the section is the real one.
    assert "re-run 2026-10-18" in section and {"web", "mcp"} <= set(summary)
    verdict = [k for k in stats if k.startswith("win_rule_")]
    assert verdict == ["win_rule_TIE"], verdict  # the README calls it even
    right = {a: int(summary[a]["right"]) for a in ("web", "mcp")}
    assert right["web"] == right["mcp"], right
    assert f"both got all {right['mcp']} right" in section, section
    for metric in ("mcp_vs_web_seconds_median_ratio", "mcp_vs_web_cost_median_ratio"):
        r = stats[metric]
        est, lo = float(r["estimate"]), float(r["lo95"])
        claims = (f"{est:.2f}×, 95% CI {lo:.2f}", f"{est:.2f}×, {lo:.2f}")
        assert any(c in section for c in claims), (metric, claims, section)
