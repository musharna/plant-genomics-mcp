"""The per-server verdict across model tiers (PREREG_2026-10-20_model-tiers.md).

Reads each tier's ``win_rule.json`` (written by analyze_rerun.py), adjusts the
tiers' McNemar p-values by Holm, and applies the rule:

- a tier's **lift**: Holm p < 0.05, more questions only mcp got right than only
  web, and the bootstrap 95% CI of the difference wholly above 0;
- a tier's **loss**: Holm p < 0.05, more only-web questions, CI wholly below 0;
- **WIN** = a lift at >= 1 tier and no loss at any; **LOSS** = a loss at any
  tier and no lift; anything else (including a lift at one tier with a loss at
  the other) is a **TIE**. A tier not yet run is reported as pending, and the
  verdict is pending until every tier is in.

    uv run python benchmarks/agent_qa/tiers_verdict.py haiku=DIR sonnet=DIR
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ALPHA = 0.05


def holm(ps: list[float]) -> list[float]:
    """Holm step-down adjusted p-values, in the input's order."""
    order = sorted(range(len(ps)), key=lambda i: ps[i])
    adj = [0.0] * len(ps)
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (len(ps) - rank) * ps[i]))
        adj[i] = running
    return adj


def verdict(rules: dict[str, dict[str, Any] | None]) -> dict[str, Any]:
    tiers = list(rules)
    done = [t for t in tiers if rules[t] is not None]
    adj = dict(zip(done, holm([rules[t]["mcnemar_p"] for t in done]), strict=True))
    per: dict[str, Any] = {}
    for t in tiers:
        r = rules[t]
        if r is None:
            per[t] = {"status": "pending"}
            continue
        lift = adj[t] < ALPHA and r["only_mcp"] > r["only_web"] and r["diff_lo95"] > 0
        loss = adj[t] < ALPHA and r["only_web"] > r["only_mcp"] and r["diff_hi95"] < 0
        per[t] = {"p_raw": r["mcnemar_p"], "p_holm": adj[t], "lift": lift, "loss": loss,
                  "mcp_right_all": r["mcp_right_all"], "web_right_all": r["web_right_all"],
                  "only_mcp": r["only_mcp"], "only_web": r["only_web"], "diff": r["diff"],
                  "diff_lo95": r["diff_lo95"], "diff_hi95": r["diff_hi95"]}  # fmt: skip
    if len(done) < len(tiers):
        overall = "PENDING"
    else:
        lifts = any(per[t]["lift"] for t in tiers)
        losses = any(per[t]["loss"] for t in tiers)
        overall = "WIN" if lifts and not losses else "LOSS" if losses and not lifts else "TIE"
    return {"verdict": overall, "tiers": per}


def main() -> None:
    rules: dict[str, dict[str, Any] | None] = {}
    for arg in sys.argv[1:]:
        tier, _, path = arg.partition("=")
        f = Path(path) / "win_rule.json"
        rules[tier] = json.loads(f.read_text()) if path and f.exists() else None
    print(json.dumps(verdict(rules), indent=1))


if __name__ == "__main__":
    main()
