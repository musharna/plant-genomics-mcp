"""Per-arm results of the 2026-10-24 field families, by the PREREG's rules.

- P1 (``family_roster``): questions right (F1 = 1) of valid runs, and mean F1.
- P3 (``id_batch``): questions right (every item right), and item accuracy,
  precision (right/attempted) and coverage (attempted/total), pooled.
- Headroom: a family has headroom for an arm if that arm is not right on at
  least 3 of its 10 questions. An invalid run counts as not right, and is
  listed, so a timeout cannot hide.

    uv run python benchmarks/agent_qa/field_2026-10-24/summarize.py RESULTS.jsonl
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

HEADROOM_MISSES = 3


def summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for arm in sorted({r["arm"] for r in rows}):
        for cat in ("family_roster", "id_batch"):
            mine = [r for r in rows if r["arm"] == arm and r["category"] == cat]
            if not mine:
                continue
            right = [r for r in mine if r["valid"] and r["verdict"] == "right"]
            s: dict[str, Any] = {
                "arm": arm,
                "family": "P1" if cat == "family_roster" else "P3",
                "runs": len(mine),
                "invalid": sorted(f"{r['qid']}-{r['rep']}" for r in mine if not r["valid"]),
                "right": len(right),
                "not_right": len(mine) - len(right),
            }
            valid = [r for r in mine if r["valid"]]
            if cat == "family_roster":
                f1 = [r.get("f1", 0.0) for r in valid]
                s["mean_f1_valid"] = round(sum(f1) / len(f1), 3) if f1 else None
            else:
                tot = sum(r.get("items_total", 0) for r in valid)
                att = sum(r.get("items_attempted", 0) for r in valid)
                ok = sum(r.get("items_right", 0) for r in valid)
                s.update(items_total=tot, items_attempted=att, items_right=ok,
                         accuracy=round(ok / tot, 3) if tot else None,
                         precision=round(ok / att, 3) if att else None,
                         coverage=round(att / tot, 3) if tot else None)  # fmt: skip
            s["headroom"] = s["not_right"] >= HEADROOM_MISSES
            out.append(s)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("results")
    a = ap.parse_args()
    rows = [json.loads(ln) for ln in Path(a.results).read_text().splitlines()]
    for s in summarize(rows):
        print(json.dumps(s))


if __name__ == "__main__":
    main()
