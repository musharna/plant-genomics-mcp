"""The 2026-10-18 re-run's harness: the harder kinds' scoring, the usage gate,
the harness-failure rule, the leak scan's new paths and the win rule.

Every negative assertion sits beside the positive one for the same input
shape, so a function that always says yes (or always no) fails here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

AGENT_QA = Path(__file__).resolve().parent.parent / "benchmarks" / "agent_qa"
sys.path.insert(0, str(AGENT_QA))

from analyze_rerun import win_rule  # noqa: E402
from baselines import baseline_rows, oracle, shifted  # noqa: E402
from run import Gate, UsageStop, api_failure, drop_api_failures, leak_hits, rate_limit  # noqa: E402
from score import score  # noqa: E402

HARD = [
    {"id": "h1", "category": "ortholog_location",
     "truth": {"chromosome": ["5"], "start": 1_000, "end": 2_000, "strand": 1}},
    {"id": "h2", "category": "ortholog_location",
     "truth": {"chromosome": ["3"], "start": 9_000, "end": 9_500, "strand": -1}},
    {"id": "h3", "category": "ortholog_protein_length", "truth": {"length": 412, "gene": "x"}},
    {"id": "h4", "category": "ortholog_protein_length", "truth": {"length": 1_204, "gene": "y"}},
    {"id": "h5", "category": "interval_count", "truth": {"count": 12, "genes": []}},
    {"id": "h6", "category": "interval_count", "truth": {"count": 31, "genes": []}},
    {"id": "h7", "category": "symbol_ortholog", "truth": {"gene": "Os01g0100100"}},
    {"id": "h8", "category": "symbol_ortholog", "truth": {"gene": "Os02g0100200"}},
    {"id": "h9", "category": "position_to_gene", "truth": {"gene": "Zm00001eb000010"}},
    {"id": "h10", "category": "position_to_gene", "truth": {"gene": "Zm00001eb000020"}},
    {"id": "h11", "category": "id_mapping", "truth": {"gene": "LOC_Os01g01010", "id_kind": "msu"}},
    {"id": "h12", "category": "id_mapping", "truth": {"gene": "839580", "id_kind": "ncbi_gene"}},
]  # fmt: skip


def _v(q: dict, value: str) -> str:
    return score(q, f"Looked it up.\nANSWER: {value}")["verdict"]


def test_the_harder_kinds_score_right_only_on_their_answer() -> None:
    by = {q["id"]: q for q in HARD}
    assert _v(by["h1"], "5:1000") == _v(by["h1"], "chr5:2,000") == "right"
    assert _v(by["h1"], "3:1000") == _v(by["h1"], "5:1500") == "wrong"
    assert _v(by["h4"], "1,204 aa") == "right" and _v(by["h4"], "1205") == "wrong"
    assert _v(by["h5"], "12") == "right" and _v(by["h5"], "13") == "wrong"
    assert _v(by["h5"], "about a dozen") == "wrong"
    assert _v(by["h9"], "zm00001eb000010_T001") == "right"
    assert _v(by["h9"], "Zm00001eb000011") == "wrong"
    poplar = {"category": "symbol_ortholog", "truth": {"gene": "Potri.008G159900.v4.1"}}
    assert _v(poplar, "Potri.008G159900") == _v(poplar, "potri.008g159900.v4.1") == "right"
    assert _v(poplar, "Potri.008G159910") == "wrong"
    assert _v(by["h11"], "LOC_Os01g01010.1") == "right" and _v(by["h11"], "Os01g0100100") == "wrong"
    assert _v(by["h12"], "839580") == "right" and _v(by["h12"], "839581") == "wrong"


def test_the_oracle_holds_on_the_harder_kinds_and_a_guess_does_not() -> None:
    assert oracle(HARD) == ["right"] * len(HARD)
    assert shifted(HARD) == ["wrong"] * len(HARD)
    # A constant per kind can be right at most once per kind here (two
    # distinct answers each); the median of two lengths is neither.
    rights = [r for r in baseline_rows(HARD) if r["verdict"] == "right"]
    assert 0 < len(rights) <= 6


def _rl(five: float, seven: float = 0.1, status: str = "allowed") -> str:
    info = {"status": status, "unifiedWindows": {
        "five_hour": {"utilization": five, "resetsAt": 2_000},
        "seven_day": {"utilization": seven, "resetsAt": 9_000}}}  # fmt: skip
    return json.dumps({"type": "rate_limit_event", "rate_limit_info": info})


def _result(ok: bool = True) -> str:
    return json.dumps({"type": "result", "subtype": "success" if ok else "error",
                       "is_error": not ok, "result": "ANSWER: 1"})  # fmt: skip


def test_the_gate_pauses_until_reset_at_085_and_records_it(tmp_path: Path) -> None:
    log = tmp_path / "pauses.jsonl"
    clock = {"t": 1_000.0}
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        clock["t"] += s

    gate = Gate(log, sleep=sleep, now=lambda: clock["t"])
    gate.update(rate_limit(_rl(0.84)), "web h1 rep1")
    gate.wait()
    assert slept == [] and not log.exists()  # below the line: no pause
    gate.update(rate_limit(_rl(0.85)), "mcp h1 rep1")
    gate.wait()
    assert clock["t"] >= 2_060 and sum(slept) == 1_060
    entry = json.loads(log.read_text())
    assert entry["after"] == "mcp h1 rep1" and entry["pause_until"] == 2_060
    gate.update(rate_limit(_rl(0.2, status="rejected")), "web h2 rep1")
    assert len(log.read_text().splitlines()) == 2  # a rejection pauses too


def test_the_gate_stops_at_seven_day_080(tmp_path: Path) -> None:
    gate = Gate(tmp_path / "p.jsonl", sleep=lambda s: None, now=lambda: 0.0)
    gate.update(rate_limit(_rl(0.1, seven=0.79)), "ok")
    try:
        gate.update(rate_limit(_rl(0.1, seven=0.80)), "web h3 rep2")
    except UsageStop as exc:
        assert "web h3 rep2" in str(exc)
    else:
        raise AssertionError("seven_day 0.80 did not stop the sweep")


def test_a_rejection_before_any_answer_is_a_harness_failure_and_runs_once_more(
    tmp_path: Path,
) -> None:
    assert api_failure(_rl(1.0, status="rejected")) == "rate_limited"
    assert api_failure(_rl(1.0, status="rejected") + "\n" + _result(ok=True)) is None
    assert api_failure(_rl(0.3) + "\n" + _result(ok=False)) is None
    assert api_failure(json.dumps({"type": "result", "terminal_reason": "api_error"})) == (
        "api_error"
    )
    results = tmp_path / "results.jsonl"
    rows = [{"arm": "web", "qid": "h1", "rep": 1, "api_failure": "rate_limited"},
            {"arm": "mcp", "qid": "h1", "rep": 1, "api_failure": None}]  # fmt: skip
    results.write_text("".join(json.dumps(r) + "\n" for r in rows))
    drop_api_failures(results)
    assert [r["arm"] for r in map(json.loads, results.read_text().splitlines())] == ["mcp"]
    # The re-run failed the same way: it stays in this time.
    with results.open("a") as fh:
        fh.write(json.dumps(rows[0]) + "\n")
    drop_api_failures(results)
    assert sorted(r["arm"] for r in map(json.loads, results.read_text().splitlines())) == [
        "mcp",
        "web",
    ]


def test_the_leak_scan_sees_the_rerun_key_and_not_a_runs_own_tmpdir() -> None:
    key = '"/home/u/plant-genomics-mcp-artifacts/agent_qa/rerun-2026-10-18/questions.json"'
    assert "plant-genomics-mcp-artifacts" in leak_hits([key])
    assert leak_hits(['{"url": "https://x/benchmarks/agent_qa/build_hard.py"}']) != []
    assert leak_hits(['"/tmp/agentqa-web-1x2y/tmp/fetch-3.txt"', '{"id": "AT1G01010"}']) == []


def _rows(arm: str, right_qids: set[str], n: int, reps: int = 3) -> list[dict]:
    return [
        {"arm": arm, "qid": f"q{i}", "rep": k, "valid": True,
         "verdict": "right" if f"q{i}" in right_qids else "wrong"}
        for i in range(n) for k in range(1, reps + 1)
    ]  # fmt: skip


def test_the_win_rule_needs_a_significant_lead_not_just_a_bigger_count() -> None:
    n = 36
    qids = [f"q{i}" for i in range(n)]
    every = {f"q{i}" for i in range(n)}
    # 12 questions only the server got right: McNemar p = 0.0005, CI above 0.
    win = win_rule(_rows("mcp", every, n) + _rows("web", every - set(qids[:12]), n), qids)
    assert win["verdict"] == "WIN" and win["only_mcp"] == 12 and win["mcnemar_p"] < 0.05
    # 3 against 0 is a bigger count but p = 0.25: a tie.
    tie = win_rule(_rows("mcp", every, n) + _rows("web", every - set(qids[:3]), n), qids)
    assert tie["verdict"] == "TIE"
    # Web 3 ahead is outside the 2-question margin: a loss.
    loss = win_rule(_rows("mcp", every - set(qids[:3]), n) + _rows("web", every, n), qids)
    assert loss["verdict"] == "LOSS" and not loss["noninferior"]
    # One invalid run of a question makes it not right in all runs.
    rows = _rows("mcp", every, n) + _rows("web", every - set(qids[:12]), n)
    rows[0]["valid"] = False
    assert win_rule(rows, qids)["mcp_right_all"] == n - 1
