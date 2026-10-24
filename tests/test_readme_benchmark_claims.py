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
HEADING = "## What the benchmark shows"


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
    """The 2026-10-18 re-run's line in the benchmark section is pinned to its own
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


def _sci(x: float) -> str:
    """2.098e-05 -> "2.1e-5", as the README writes small p-values."""
    s = f"{x:.2g}"
    if "e" not in s:
        return s
    mantissa, exponent = s.split("e")
    return f"{mantissa}e{int(exponent)}"


def test_tier_sentences_match_the_tier_tables() -> None:
    """The Haiku and Sonnet sentences are pinned to tiers_2026-10-20/{haiku,sonnet}/stats.tsv
    (PR #259 review: a result restated in a second place is fixed in one, LESSONS
    2026-10-17). Counts, the Holm-adjusted p over the two tiers, the verdict and the
    cost and time ratios must be the tables', so a re-run or a corrected key cannot
    leave the front page behind."""
    section = " ".join(_section().split())  # the README wraps lines anywhere
    tiers = BENCH / "tiers_2026-10-20"
    stats = {}
    for tier in ("haiku", "sonnet"):
        with (tiers / tier / "stats.tsv").open(encoding="utf-8") as f:
            stats[tier] = {r["metric"]: r for r in csv.DictReader(f, delimiter="\t")}
    verdicts = {t: [k for k in s if k.startswith("win_rule_")] for t, s in stats.items()}
    # Positive control: both tables were read and the section is the real one.
    assert verdicts == {"haiku": ["win_rule_WIN"], "sonnet": ["win_rule_TIE"]}, verdicts
    assert "Haiku 5.5" in section and "Sonnet 5.5" in section, section[:200]
    right = {}
    for tier, s in stats.items():
        note = s[verdicts[tier][0]]["note"]
        mcp = int(note.split("mcp ")[1].split(",")[0])
        web = int(note.split("web ")[1].split(" ")[0])
        right[tier] = (mcp, web)
    p = sorted(float(s[verdicts[t][0]]["p"]) for t, s in stats.items())
    holm_min = min(1.0, 2 * p[0])
    assert (
        f"got {right['haiku'][0]} right in every run with the server and "
        f"{right['haiku'][1]} with web search: a pre-registered win" in section
    ), (right, section)
    assert f"p = {_sci(holm_min)}" in section, (_sci(holm_min), section)
    cost = float(stats["haiku"]["mcp_vs_web_cost_median_ratio"]["estimate"])
    assert f"{cost:.2f} times the cost" in section, (cost, section)
    s = stats["sonnet"]
    secs = float(s["mcp_vs_web_seconds_median_ratio"]["estimate"])
    cost = float(s["mcp_vs_web_cost_median_ratio"]["estimate"])
    assert (
        f"tied ({right['sonnet'][0]} and {right['sonnet'][1]}; "
        f"{secs:.2f}× the time, {cost:.2f}× the cost)" in section
    ), (right, secs, cost, section)
