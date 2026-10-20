"""Statistics and the pre-registered decision for the 2026-10-18 re-run.

Runs no model. Reads the harder key (build_hard.py), the sweep's
``results.jsonl`` (rows already carry ``valid`` from run.py's one validity
rule; run ``run.py --audit`` first to recompute it and the leak scan from the
transcripts) and the transcripts, and writes to ``--outdir``:

- ``stats.tsv``: the win rule, the paired tests and bootstrap intervals;
- ``summary.tsv``: per arm (and the no-model baselines), right, wrong,
  declined, invalid, questions right in all runs, cost, seconds, tool calls;
- ``by_kind.tsv``: right per arm and kind of question;
- ``mechanism.tsv``: how many server runs called each tool, and calls the
  client refused or the server answered with an error.

The win rule is the one in PREREG_2026-10-18.md: the primary endpoint is
questions right in all 3 runs, mcp against web.

    uv run python benchmarks/agent_qa/analyze_rerun.py --key KEY --results RESULTS \\
        --runs RUNS --outdir benchmarks/agent_qa/rerun_2026-10-18
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
from collections import Counter, defaultdict
from math import comb
from pathlib import Path
from typing import Any

from baselines import baseline_rows
from score import score

SEED = 20261018
BOOT = 10_000
NONINFERIOR_MARGIN = 2  # questions; the ledger's win rule
ARMS = ("web", "mcp")
MODEL_ARMS = ("bare", *ARMS)  # arms a model ran; bare only in the 2026-10-20 tier sweeps


def binom_two_sided(k: int, n: int) -> float:
    """Exact two-sided sign-test p for k of n successes at p = 1/2."""
    if n == 0:
        return 1.0
    lo = min(k, n - k)
    return min(1.0, 2 * sum(comb(n, i) for i in range(lo + 1)) / 2**n)


def right_all(rows: list[dict[str, Any]], qids: list[str], reps: int) -> dict[str, bool]:
    """Per question: right in every one of ``reps`` runs. An invalid or
    missing run is not a right one."""
    by_q: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_q[r["qid"]].append(r)
    return {
        q: len(by_q[q]) == reps and all(r["valid"] and r["verdict"] == "right" for r in by_q[q])
        for q in qids
    }


def win_rule(rows: list[dict[str, Any]], qids: list[str], reps: int = 3) -> dict[str, Any]:
    """The pre-registered decision, on questions right in every run.

    - non-inferior: mcp's count >= web's - NONINFERIOR_MARGIN;
    - better: exact McNemar p < 0.05 with more questions only mcp got right,
      and the bootstrap 95% CI of (mcp - web) / questions wholly above 0;
    - WIN = non-inferior and better; LOSS = not non-inferior, or web
      significantly better (p < 0.05, CI wholly below 0); otherwise TIE.
    """
    ok = {a: right_all([r for r in rows if r["arm"] == a], qids, reps) for a in ARMS}
    n_web, n_mcp = sum(ok["web"].values()), sum(ok["mcp"].values())
    b = sum(ok["mcp"][q] and not ok["web"][q] for q in qids)
    c = sum(ok["web"][q] and not ok["mcp"][q] for q in qids)
    p = binom_two_sided(b, b + c)
    rng = random.Random(SEED)
    diffs = sorted(
        sum(ok["mcp"][q] - ok["web"][q] for q in (rng.choice(qids) for _ in qids)) / len(qids)
        for _ in range(BOOT)
    )
    lo, hi = diffs[int(0.025 * BOOT)], diffs[int(0.975 * BOOT) - 1]
    noninferior = n_mcp >= n_web - NONINFERIOR_MARGIN
    better = p < 0.05 and b > c and lo > 0
    worse = p < 0.05 and c > b and hi < 0
    verdict = "WIN" if noninferior and better else "LOSS" if (not noninferior or worse) else "TIE"
    return {
        "verdict": verdict, "questions": len(qids), "mcp_right_all": n_mcp,
        "web_right_all": n_web, "only_mcp": b, "only_web": c, "mcnemar_p": p,
        "diff": (n_mcp - n_web) / len(qids), "diff_lo95": lo, "diff_hi95": hi,
        "noninferior": noninferior,
    }  # fmt: skip


def _med(rs: list[dict[str, Any]], f) -> float:
    return statistics.median(f(r) for r in rs)


def secondary(rows: list[dict[str, Any]], qids: list[str]) -> list[dict[str, Any]]:
    """Bootstrap intervals over questions (a question's runs move together)
    and per-question sign tests, on every run of each arm."""
    by = {a: defaultdict(list) for a in ARMS}
    for r in rows:
        if r["arm"] in by:
            by[r["arm"]][r["qid"]].append(r)

    def m(arm: str, sample: list[str]) -> dict[str, float]:
        rs = [r for q in sample for r in by[arm][q]]
        return {
            "right_per_run": sum(r["valid"] and r["verdict"] == "right" for r in rs) / len(rs),
            "cost_median": _med(rs, lambda r: r["cost_usd"] or 0.0),
            "seconds_median": _med(rs, lambda r: r["seconds"]),
            "tool_calls_mean": statistics.fmean(len(r["tool_calls"]) for r in rs),
        }

    rng = random.Random(SEED)
    draws: dict[str, list[float]] = defaultdict(list)
    for _ in range(BOOT):
        s = [rng.choice(qids) for _ in qids]
        w, x = m("web", s), m("mcp", s)
        draws["right_per_run_diff"].append(x["right_per_run"] - w["right_per_run"])
        draws["cost_median_ratio"].append(x["cost_median"] / w["cost_median"])
        draws["seconds_median_ratio"].append(x["seconds_median"] / w["seconds_median"])
        draws["tool_calls_mean_diff"].append(x["tool_calls_mean"] - w["tool_calls_mean"])
    w, x = m("web", qids), m("mcp", qids)
    point = {
        "right_per_run_diff": x["right_per_run"] - w["right_per_run"],
        "cost_median_ratio": x["cost_median"] / w["cost_median"],
        "seconds_median_ratio": x["seconds_median"] / w["seconds_median"],
        "tool_calls_mean_diff": x["tool_calls_mean"] - w["tool_calls_mean"],
    }
    out = []
    for k, vals in draws.items():
        vals.sort()
        out.append(
            {"metric": f"mcp_vs_web_{k}", "estimate": point[k], "lo95": vals[int(0.025 * BOOT)],
             "hi95": vals[int(0.975 * BOOT) - 1], "p": None,
             "note": f"bootstrap over {len(qids)} questions, {BOOT} draws"}
        )  # fmt: skip
    for name, f in (
        ("cost_median", lambda rs: _med(rs, lambda r: r["cost_usd"] or 0.0)),
        ("seconds_median", lambda rs: _med(rs, lambda r: r["seconds"])),
    ):
        lower = sum(f(by["mcp"][q]) < f(by["web"][q]) for q in qids)
        higher = sum(f(by["mcp"][q]) > f(by["web"][q]) for q in qids)
        out.append(
            {"metric": f"mcp_vs_web_sign_test_{name}", "estimate": lower - higher, "lo95": None,
             "hi95": None, "p": binom_two_sided(lower, lower + higher),
             "note": f"per-question medians: mcp lower on {lower}, higher on {higher}"}
        )  # fmt: skip
    return out


def summary(rows: list[dict[str, Any]], base: list[dict[str, Any]], qids: list[str], reps: int):
    out = []
    ran = [a for a in MODEL_ARMS if any(r["arm"] == a for r in rows)]
    for arm in [*ran, *dict.fromkeys(r["arm"] for r in base)]:
        rs = [r for r in [*rows, *base] if r["arm"] == arm]
        is_base = arm not in MODEL_ARMS
        v = Counter(r["verdict"] for r in rs if is_base or r["valid"])
        ok = right_all(
            [{**r, "valid": True} for r in rs] if is_base else rs, qids, 1 if is_base else reps
        )
        out.append(
            {
                "arm": arm, "runs": len(rs), "right": v["right"], "wrong": v["wrong"],
                "declined": v["declined"], "no_answer": v["no_answer"],
                "invalid": 0 if is_base else sum(not r["valid"] for r in rs),
                "questions_right_all": sum(ok.values()),
                "cost_total": None if is_base else sum(r["cost_usd"] or 0 for r in rs),
                "cost_median": None if is_base else _med(rs, lambda r: r["cost_usd"] or 0.0),
                "seconds_median": None if is_base else _med(rs, lambda r: r["seconds"]),
                "tool_calls_mean": None if is_base
                else statistics.fmean(len(r["tool_calls"]) for r in rs),
            }
        )  # fmt: skip
    return out


def by_kind(rows: list[dict[str, Any]], key: list[dict[str, Any]], reps: int):
    out = []
    for kind in dict.fromkeys(q["category"] for q in key):
        qids = [q["id"] for q in key if q["category"] == kind]
        row: dict[str, Any] = {"kind": kind, "questions": len(qids)}
        for arm in [a for a in MODEL_ARMS if any(r["arm"] == a for r in rows)]:
            rs = [r for r in rows if r["arm"] == arm and r["qid"] in qids]
            row[f"{arm}_right"] = sum(r["valid"] and r["verdict"] == "right" for r in rs)
            row[f"{arm}_runs"] = len(rs)
            row[f"{arm}_right_all"] = sum(right_all(rs, qids, reps).values())
        out.append(row)
    return out


def mechanism(runs: Path, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per server tool: runs that called it, calls, and calls that errored;
    plus calls the client refused (a bare or unknown tool name)."""
    used: Counter[str] = Counter()
    calls: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    refused = 0
    zero = 0
    for r in rows:
        if r["arm"] != "mcp":
            continue
        zero += not any(t.startswith("mcp__plant-genomics__") for t in r["tool_calls"])
        path = runs / "mcp" / f"{r['qid']}-{r['rep']}.jsonl"
        names: dict[str, str] = {}
        seen = set()
        for ln in path.read_text().splitlines():
            if not ln.startswith("{"):
                continue
            e = json.loads(ln)
            for b in (e.get("message") or {}).get("content") or []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    names[b["id"]] = b["name"]
                    calls[b["name"]] += 1
                    seen.add(b["name"])
                elif b.get("type") == "tool_result" and b.get("is_error"):
                    name = names.get(b.get("tool_use_id"), "?")
                    text = json.dumps(b.get("content"))
                    if "No such tool" in text or "not available" in text or "permission" in text:
                        refused += 1
                    errors[name] += 1
            if e.get("type") == "result":
                refused += len(e.get("permission_denials") or [])
        used.update(seen)
    out = [
        {"tool": t.removeprefix("mcp__plant-genomics__"), "runs_using": used[t],
         "calls": calls[t], "error_results": errors[t]}
        for t in sorted(calls, key=lambda t: (-used[t], t))
    ]  # fmt: skip
    out.append({"tool": "(refused by client)", "runs_using": None, "calls": refused,
                "error_results": None})  # fmt: skip
    out.append({"tool": "(server runs with 0 server calls)", "runs_using": zero, "calls": 0,
                "error_results": None})  # fmt: skip
    return out


def relabel(rows: list[dict[str, Any]], arm: str, as_arm: str) -> list[dict[str, Any]]:
    """``arm``'s rows renamed ``as_arm``, so win_rule's statistic compares any
    two sets of rows (another arm, or another model's rows)."""
    return [{**r, "arm": as_arm} for r in rows if r["arm"] == arm]


def write_tsv(path: Path, rows: list[dict[str, Any]]) -> None:
    cols = list(rows[0])

    def fmt(v: Any) -> str:
        if v is None:
            return "NA"
        return f"{v:.6g}" if isinstance(v, float) else str(v)

    path.write_text(
        "\t".join(cols) + "\n" + "".join("\t".join(fmt(r[c]) for c in cols) + "\n" for r in rows)
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", required=True)
    ap.add_argument("--results", required=True)
    ap.add_argument("--runs", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument(
        "--reference-results",
        help="another sweep's results.jsonl (e.g. the 2026-10-18 Opus rows) for the "
        "non-concurrent substitute comparison: this sweep's mcp against its web",
    )
    ap.add_argument("--reference-label", default="reference")
    a = ap.parse_args()
    key = json.loads(Path(a.key).read_text())["questions"]
    by_id = {q["id"]: q for q in key}
    rows = [json.loads(ln) for ln in Path(a.results).read_text().splitlines()]
    rows = [r for r in rows if r["arm"] in MODEL_ARMS]
    for r in rows:
        r.update(score(by_id[r["qid"]], r["reply"]))  # never a stale verdict
    ref: list[dict[str, Any]] = []
    if a.reference_results:
        ref = [json.loads(ln) for ln in Path(a.reference_results).read_text().splitlines()]
        for r in ref:
            r.update(score(by_id[r["qid"]], r["reply"]))
    qids = [q["id"] for q in key]
    rule = win_rule(rows, qids, a.reps)
    base = [{**r, "rep": 1} for r in baseline_rows(key)]
    stats = [
        {"metric": f"win_rule_{rule['verdict']}", "estimate": rule["diff"],
         "lo95": rule["diff_lo95"], "hi95": rule["diff_hi95"], "p": rule["mcnemar_p"],
         "note": f"right in all {a.reps} runs: mcp {rule['mcp_right_all']}, web "
         f"{rule['web_right_all']} of {rule['questions']}; only mcp {rule['only_mcp']}, only web "
         f"{rule['only_web']}; non-inferior {rule['noninferior']}"},
        *secondary([r for r in rows if r["arm"] in ARMS], qids),
    ]  # fmt: skip
    extra = []
    if any(r["arm"] == "bare" for r in rows):
        extra.append(("mcp_vs_bare", "bare floor, same model",
                      [*relabel(rows, "mcp", "mcp"), *relabel(rows, "bare", "web")]))  # fmt: skip
    if ref:
        extra.append(("substitute_mcp_vs_reference_web",
                      f"NON-CONCURRENT: this sweep's mcp against {a.reference_label} web",
                      [*relabel(rows, "mcp", "mcp"), *relabel(ref, "web", "web")]))  # fmt: skip
    for name, label, rs in extra:
        x = win_rule(rs, qids, a.reps)
        stats.append(
            {"metric": name, "estimate": x["diff"], "lo95": x["diff_lo95"],
             "hi95": x["diff_hi95"], "p": x["mcnemar_p"],
             "note": f"{label}; right in all {a.reps} runs: mcp {x['mcp_right_all']}, other "
             f"{x['web_right_all']} of {x['questions']}; only mcp {x['only_mcp']}, only other "
             f"{x['only_web']}"}
        )  # fmt: skip
    out = Path(a.outdir)
    out.mkdir(parents=True, exist_ok=True)
    write_tsv(out / "stats.tsv", stats)
    write_tsv(out / "summary.tsv", summary(rows, base, qids, a.reps))
    write_tsv(out / "by_kind.tsv", by_kind(rows, key, a.reps))
    write_tsv(out / "mechanism.tsv", mechanism(Path(a.runs), rows))
    (out / "win_rule.json").write_text(json.dumps(rule, indent=1) + "\n")
    for name in ("summary", "by_kind", "stats", "mechanism"):
        print(f"== {name}\n" + (out / f"{name}.tsv").read_text())
    print("WIN RULE:", rule)


if __name__ == "__main__":
    main()
