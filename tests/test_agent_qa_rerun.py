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

    gate = Gate(log, sleep=sleep, now=lambda: clock["t"], probe=_no_probe)
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


def _no_probe() -> dict:
    raise AssertionError("the gate probed below the seven-day line")


def _probe(seven: float | None):
    return lambda: None if seven is None else {"seven_day": seven, "five_hour": 0.1}


def test_the_gate_stops_at_seven_day_080(tmp_path: Path) -> None:
    gate = Gate(tmp_path / "p.jsonl", sleep=lambda s: None, now=lambda: 0.0, probe=_probe(0.85))
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


def test_another_model_writes_its_own_transcripts_and_versions_names_the_served_model(
    tmp_path: Path,
) -> None:
    import run

    try:
        run.set_model(run.DEFAULT_MODEL)
        assert run.RUNS == run.DEFAULT_RUNS
        run.set_model("claude-haiku-5-5")
        assert run.MODEL == "claude-haiku-5-5"
        assert run.RUNS != run.DEFAULT_RUNS
        own = run.DEFAULT_RUNS.with_name(f"{run.DEFAULT_RUNS.name}-claude-haiku-5-5")
        assert own == run.RUNS
    finally:
        run.set_model(run.DEFAULT_MODEL)
    results = tmp_path / "results.jsonl"
    results.write_text(
        "".join(json.dumps({"model": m}) + "\n" for m in ("claude-haiku-5-5", "claude-haiku-5-5",
                                                           "claude-opus-5-5"))
    )  # fmt: skip
    vpath = tmp_path / "versions.json"
    run.write_versions(vpath, {"model": "claude-haiku-5-5"}, results)
    v = json.loads(vpath.read_text())
    assert v["model"] == "claude-haiku-5-5"
    assert v["models_served"] == {"claude-haiku-5-5": 2, "claude-opus-5-5": 1}


def test_the_mechanism_counts_server_runs_that_never_called_the_server(tmp_path: Path) -> None:
    from analyze_rerun import mechanism, relabel

    (tmp_path / "mcp").mkdir()
    call = {
        "message": {
            "content": [
                {"type": "tool_use", "id": "t1", "name": "mcp__plant-genomics__gramene_homologs"}
            ]
        }
    }
    (tmp_path / "mcp" / "h1-1.jsonl").write_text(json.dumps(call) + "\n")
    (tmp_path / "mcp" / "h2-1.jsonl").write_text("")
    rows = [
        {"arm": "mcp", "qid": "h1", "rep": 1,
         "tool_calls": ["mcp__plant-genomics__gramene_homologs"]},
        {"arm": "mcp", "qid": "h2", "rep": 1, "tool_calls": []},
    ]  # fmt: skip
    out = {r["tool"]: r for r in mechanism(tmp_path, rows)}
    assert out["gramene_homologs"]["runs_using"] == 1
    assert out["(server runs with 0 server calls)"]["runs_using"] == 1
    # Relabelled rows feed win_rule's statistic: other arms' rows are dropped.
    mixed = [{"arm": "bare", "qid": "h1"}, {"arm": "mcp", "qid": "h1"}]
    assert relabel(mixed, "bare", "web") == [{"arm": "web", "qid": "h1"}]


def test_the_tier_verdict_holm_adjusts_and_needs_a_lift_without_a_loss() -> None:
    from tiers_verdict import holm, verdict

    assert holm([0.01, 0.04]) == [0.02, 0.04]
    assert holm([0.04, 0.01]) == [0.04, 0.02]

    def rule(only_mcp: int, only_web: int, p: float, lo: float, hi: float) -> dict:
        return {"mcnemar_p": p, "only_mcp": only_mcp, "only_web": only_web, "diff_lo95": lo,
                "diff_hi95": hi, "mcp_right_all": 0, "web_right_all": 0, "diff": 0}  # fmt: skip

    lift, null = rule(12, 0, 0.0005, 0.2, 0.5), rule(1, 1, 1.0, -0.1, 0.1)
    assert verdict({"haiku": lift, "sonnet": null})["verdict"] == "WIN"
    # Raw p 0.03 passes alone, but Holm doubles the smaller p: not a lift.
    near = rule(6, 0, 0.03, 0.05, 0.3)
    assert verdict({"haiku": near, "sonnet": rule(5, 0, 0.0625, 0.0, 0.3)})["verdict"] == "TIE"
    loss = rule(0, 12, 0.0005, -0.5, -0.2)
    assert verdict({"haiku": lift, "sonnet": loss})["verdict"] == "TIE"
    assert verdict({"haiku": null, "sonnet": loss})["verdict"] == "LOSS"
    assert verdict({"haiku": lift, "sonnet": None})["verdict"] == "PENDING"
    # A tier left off the command line is pending, not a one-tier verdict.
    alone = verdict({"haiku": lift})
    assert alone["verdict"] == "PENDING" and alone["tiers"]["sonnet"] == {"status": "pending"}
    try:
        verdict({"haiku": lift, "sonet": null})
    except ValueError as exc:
        assert "sonet" in str(exc)
    else:
        raise AssertionError("a misspelt tier was accepted")


def test_the_win_rule_refuses_an_arm_with_missing_runs() -> None:
    n = 36
    qids = [f"q{i}" for i in range(n)]
    every = set(qids)
    full = _rows("mcp", every, n) + _rows("web", every - set(qids[:1]), n)
    assert win_rule(full, qids)["verdict"] == "TIE"  # the complete sweep is judged
    for rows, arm in ((_rows("mcp", every, n), "web"), (full[1:], "mcp")):
        try:
            win_rule(rows, qids)
        except ValueError as exc:
            assert str(exc).startswith(f"{arm}:")
        else:
            raise AssertionError(f"win_rule judged a sweep whose {arm} arm is incomplete")


def test_stop_at_raises_the_seven_day_stop(tmp_path: Path) -> None:
    import run

    gate = Gate(tmp_path / "p.jsonl", sleep=lambda s: None, now=lambda: 0.0, probe=_probe(0.95))
    try:
        run.set_stop_at(0.90)
        gate.update(rate_limit(_rl(0.1, seven=0.85)), "ok at 0.85")
        try:
            gate.update(rate_limit(_rl(0.1, seven=0.90)), "sonnet h3 rep2")
        except UsageStop as exc:
            assert "sonnet h3 rep2" in str(exc)
        else:
            raise AssertionError("seven_day 0.90 did not stop the sweep at --stop-at 0.90")
    finally:
        run.set_stop_at(0.80)


def test_a_resume_accepts_a_new_claude_code_only_when_asked_and_counts_each_version(
    tmp_path: Path,
) -> None:
    import run

    old = {"claude_code": "2.1.306", "model": "m", "models_served": {"m": 51},
           "claude_code_served": {"2.1.306": 51}}  # fmt: skip
    now = {"claude_code": "2.1.307", "model": "m"}
    assert run.resume_mismatch(old, now, False) == {"claude_code": ("2.1.306", "2.1.307")}
    assert run.resume_mismatch(old, now, True) == {}
    # Anything else that moved still refuses, flag or not.
    assert run.resume_mismatch(old, {**now, "model": "x"}, True) == {"model": ("m", "x")}
    results = tmp_path / "results.jsonl"
    results.write_text(
        "".join(json.dumps({"model": "m", "claude_code": v}) + "\n"
                for v in ("2.1.306", "2.1.306", "2.1.307"))
    )  # fmt: skip
    run.write_versions(tmp_path / "versions.json", now, results)
    v = json.loads((tmp_path / "versions.json").read_text())
    assert v["claude_code_served"] == {"2.1.306": 2, "2.1.307": 1}


def test_a_high_seven_day_reading_stops_only_if_a_fresh_probe_confirms_it(tmp_path: Path) -> None:
    import run

    log = tmp_path / "pauses.jsonl"
    try:
        run.set_stop_at(0.90)
        # 2026-10-21: one run read 0.98 while the account stood near 0.66.
        spurious = Gate(log, sleep=lambda s: None, now=lambda: 0.0, probe=_probe(0.66))
        spurious.update(rate_limit(_rl(0.01, seven=0.98)), "bare h16 rep3")  # goes on
        entry = json.loads(log.read_text().splitlines()[-1])
        assert entry["kind"] == "seven_day_probe" and entry["run_seven_day"] == 0.98
        assert entry["probe"]["seven_day"] == 0.66
        for probe_reads in (0.95, None):  # confirmed, or no reading at all: stop
            gate = Gate(log, sleep=lambda s: None, now=lambda: 0.0, probe=_probe(probe_reads))
            try:
                gate.update(rate_limit(_rl(0.01, seven=0.98)), "web h17 rep3")
            except UsageStop as exc:
                assert "web h17 rep3" in str(exc) and f"probe read {probe_reads}" in str(exc)
            else:
                raise AssertionError(f"0.98 with a probe reading {probe_reads} did not stop")
    finally:
        run.set_stop_at(0.80)


def test_the_spend_gate_stops_before_a_run_and_lets_it_through_under_cap(
    tmp_path: Path, monkeypatch
) -> None:
    """API-billed sweeps (2026-10-23): Gate.wait asks BENCH_SPEND_GATE before every run."""
    gate_py = tmp_path / "gate.py"
    gate_py.write_text(
        "import os, sys\nprint('cap reached', file=sys.stderr)\nsys.exit(int(os.environ['GATE_RC']))\n"
    )
    gate = Gate(tmp_path / "p.jsonl", sleep=lambda s: None, now=lambda: 0.0)
    monkeypatch.setenv("BENCH_SPEND_GATE", str(gate_py))
    monkeypatch.setenv("GATE_RC", "3")
    try:
        gate.wait()
    except UsageStop as exc:
        assert "cap reached" in str(exc)
    else:
        raise AssertionError("spend gate exit 3 did not stop the sweep")
    monkeypatch.setenv("GATE_RC", "0")
    gate.wait()  # under the cap: no stop
    monkeypatch.delenv("BENCH_SPEND_GATE")
    monkeypatch.setenv("GATE_RC", "3")
    gate.wait()  # unset: subscription sweeps unchanged


def test_the_spend_gate_is_asked_again_after_a_pause(tmp_path: Path, monkeypatch) -> None:
    """A run queued behind a pause starts on what the spend gate reads once the
    pause clears: the gate passes before the pause and refuses after it, so
    wait() sleeps the pause out, asks again, and stops."""
    calls = tmp_path / "calls"
    gate_py = tmp_path / "gate.py"
    gate_py.write_text(
        "import pathlib, sys\n"
        f"p = pathlib.Path({str(calls)!r})\n"
        "n = int(p.read_text()) + 1 if p.exists() else 1\n"
        "p.write_text(str(n))\n"
        "print('cap reached', file=sys.stderr)\n"
        "sys.exit(0 if n == 1 else 3)\n"
    )
    clock = {"t": 0.0}
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        clock["t"] += s

    gate = Gate(tmp_path / "p.jsonl", sleep=sleep, now=lambda: clock["t"])
    gate.until = 400.0
    monkeypatch.setenv("BENCH_SPEND_GATE", str(gate_py))
    try:
        gate.wait()
    except UsageStop as exc:
        assert "cap reached" in str(exc)
    else:
        raise AssertionError("the gate refused after the pause, but the run went ahead")
    assert slept == [300, 100]  # the pause was slept out first
    assert calls.read_text() == "2"  # then the gate was asked again
    calls.unlink()
    gate.wait()  # positive control: no pause, gate passes on its one call
    assert calls.read_text() == "1"
