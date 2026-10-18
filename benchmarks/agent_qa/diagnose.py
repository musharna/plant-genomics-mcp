"""Where a sweep's tokens and dollars go, per arm, read from the raw transcripts.

Per run it reads each API call's ``usage`` (deduplicated by message id, since
stream-json repeats a message once per content block), the tool calls and
results, and the result event's per-model ``modelUsage``. Per arm it prints:

- the context the first API call was sent (system prompt plus tool
  definitions; its gap to the bare arm is what the tool definitions cost),
  and the tool count from the init event;
- tool input and output characters, API calls and tool calls per run;
- the dollars split into cache writes, cache reads, plain input and output of
  the main model, other models (WebFetch's page reader) and web searches.

The per-token prices are fitted to each model's ``costUSD`` by least squares
over all runs, not assumed; the fit's worst residual is printed and must be
~0, or the split is wrong.

    uv run python benchmarks/agent_qa/diagnose.py --runs DIR [--json OUT]
"""

from __future__ import annotations

import argparse
import json
import statistics as st
from pathlib import Path
from typing import Any

MAIN = "claude-opus-5-5"
FIELDS = (
    "inputTokens",
    "cacheCreationInputTokens",
    "cacheReadInputTokens",
    "outputTokens",
    "webSearchRequests",
)
USAGE = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def read_run(path: Path) -> dict[str, Any] | None:
    """One run's accounting, or None if it has no result event."""
    calls: dict[str, int] = {}
    tools: list[str] = []
    tool_in = tool_out = n_tools = 0
    result: dict[str, Any] = {}
    for ln in path.read_text().splitlines():
        if not ln.startswith("{"):
            continue
        e = json.loads(ln)
        kind = e.get("type")
        if kind == "system" and e.get("subtype") == "init":
            n_tools = len(e.get("tools") or [])
        elif kind == "assistant":
            m = e["message"]
            calls.setdefault(m["id"], sum(int(m["usage"].get(k) or 0) for k in USAGE))
            for b in m.get("content") or []:
                if b.get("type") == "tool_use":
                    tools.append(b["name"])
                    tool_in += len(json.dumps(b.get("input")))
        elif kind == "user":
            for b in (e.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    c = b.get("content")
                    tool_out += len(c if isinstance(c, str) else json.dumps(c))
        elif kind == "result":
            result = e
    if not result or not calls:
        return None
    return {
        "run": f"{path.parent.name}/{path.stem}",
        "arm": path.parent.name,
        "tools_available": n_tools,
        "first_call_context": next(iter(calls.values())),
        "api_calls": len(calls),
        "tool_calls": len(tools),
        "tools_used": tools,
        "tool_in_chars": tool_in,
        "tool_out_chars": tool_out,
        "model_usage": {
            m: {k: u.get(k) or 0 for k in (*FIELDS, "costUSD")}
            for m, u in (result.get("modelUsage") or {}).items()
        },
        "cost_usd": result.get("total_cost_usd") or 0.0,
    }


def _solve(x: list[list[float]], y: list[float]) -> list[float]:
    """Least squares by the normal equations; a column that is all zero
    (a model that never searched) gets price 0."""
    n = len(x[0])
    a = [[sum(r[i] * r[j] for r in x) for j in range(n)] for i in range(n)]
    b = [sum(r[i] * v for r, v in zip(x, y, strict=True)) for i in range(n)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(a[r][c]))
        a[c], a[piv], b[c], b[piv] = a[piv], a[c], b[piv], b[c]
        if not a[c][c]:
            continue
        for r in range(n):
            if r != c:
                f = a[r][c] / a[c][c]
                a[r] = [ar - f * ac for ar, ac in zip(a[r], a[c], strict=True)]
                b[r] -= f * b[c]
    return [b[i] / a[i][i] if a[i][i] else 0.0 for i in range(n)]


def fit_prices(rows: list[dict[str, Any]]) -> dict[str, list[float]]:
    by_model: dict[str, list[dict[str, float]]] = {}
    for r in rows:
        for m, u in r["model_usage"].items():
            by_model.setdefault(m, []).append(u)
    prices = {}
    for m, us in by_model.items():
        p = _solve([[float(u[k]) for k in FIELDS] for u in us], [u["costUSD"] for u in us])
        worst = max(abs(sum(u[k] * q for k, q in zip(FIELDS, p, strict=True)) - u["costUSD"])
                    for u in us)  # fmt: skip
        print(f"{m}: $/Mtok in {p[0] * 1e6:.3f}, cache write {p[1] * 1e6:.3f}, cache read "
              f"{p[2] * 1e6:.3f}, out {p[3] * 1e6:.3f}; $/search {p[4]:.4f}; "
              f"worst residual ${worst:.2e} over {len(us)} runs")  # fmt: skip
        prices[m] = p
    return prices


def summarize(rows: list[dict[str, Any]], prices: dict[str, list[float]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for arm in sorted({r["arm"] for r in rows}):
        rs = [r for r in rows if r["arm"] == arm]
        split = dict.fromkeys(
            ("cache_write", "cache_read", "input", "output", "other_models", "web_search"), 0.0
        )
        for r in rs:
            for m, u in r["model_usage"].items():
                p = prices[m]
                split["web_search"] += u["webSearchRequests"] * p[4]
                if m != MAIN:
                    split["other_models"] += u["costUSD"] - u["webSearchRequests"] * p[4]
                    continue
                for name, k, q in zip(("input", "cache_write", "cache_read", "output"),
                                      FIELDS, p, strict=False):  # fmt: skip
                    split[name] += u[k] * q
        total = sum(r["cost_usd"] for r in rs)
        main = [r["model_usage"].get(MAIN, {}) for r in rs]
        out[arm] = {
            "runs": len(rs),
            "tools_available": st.median(r["tools_available"] for r in rs),
            "first_call_context_median": st.median(r["first_call_context"] for r in rs),
            "api_calls_mean": round(st.mean(r["api_calls"] for r in rs), 2),
            "tool_calls_mean": round(st.mean(r["tool_calls"] for r in rs), 2),
            "tool_in_chars_median": st.median(r["tool_in_chars"] for r in rs),
            "tool_out_chars_median": st.median(r["tool_out_chars"] for r in rs),
            "tool_out_chars_mean": round(st.mean(r["tool_out_chars"] for r in rs)),
            "cache_write_tokens_mean": round(st.mean(u.get(FIELDS[1], 0) for u in main)),
            "cache_read_tokens_mean": round(st.mean(u.get(FIELDS[2], 0) for u in main)),
            "output_tokens_mean": round(st.mean(u.get(FIELDS[3], 0) for u in main)),
            "cost_total": round(total, 2),
            "cost_median": round(st.median(r["cost_usd"] for r in rs), 4),
            "cost_share": {k: round(v / total, 3) for k, v in split.items()} if total else {},
        }
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", required=True, help="a sweep's transcripts: <runs>/<arm>/*.jsonl")
    ap.add_argument("--json", default="", help="also write per-run rows here")
    a = ap.parse_args()
    rows = [r for p in sorted(Path(a.runs).glob("*/*.jsonl")) if (r := read_run(p))]
    summary = summarize(rows, fit_prices(rows))
    print(json.dumps(summary, indent=1))
    if a.json:
        Path(a.json).write_text(json.dumps({"summary": summary, "runs": rows}, indent=1))


if __name__ == "__main__":
    main()
