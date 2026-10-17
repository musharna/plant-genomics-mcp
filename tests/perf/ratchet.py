"""Work-count ratchet: each workload's cost must equal the committed baseline.

    python -m tests.perf.ratchet                    # check (CI): exit 1 on any difference
    python -m tests.perf.ratchet --update           # write a lower or equal baseline
    python -m tests.perf.ratchet --update --allow-increase   # accept a rise, as a reviewed diff
    python -m tests.perf.ratchet --update --allow-output-change   # accept a new answer
    python -m tests.perf.ratchet --record           # re-record fixtures from the live upstreams

The gate counts work, never milliseconds (see harness.py for what is
counted). Every number is identical on every run and every Python version
in the CI matrix, so the ceiling is the baseline itself, with no headroom:
one extra call on a hot path fails. A drop fails too, until ``--update``
writes it, so a gain lands in the PR that earned it and cannot be given
back unnoticed. A rise needs ``--allow-increase``. A workload whose answer
changed (output hash) is no longer measuring what its baseline measured, so
``--update`` refuses it too unless ``--allow-output-change`` says it was meant.

Each workload runs in a fresh interpreter (``python -m tests.perf.harness``).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from tests.perf.harness import WORKLOADS

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
BASELINE = HERE / "baseline.json"


def measure(name: str, *, record: bool = False) -> dict[str, Any]:
    cmd = [sys.executable, "-m", "tests.perf.harness", name, *(["--record"] if record else [])]
    proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise SystemExit(f"workload {name} failed (exit {proc.returncode}):\n{proc.stderr}")
    result: dict[str, Any] = json.loads(proc.stdout)
    return result


def _deltas(old: dict[str, int], new: dict[str, int], top: int = 10) -> list[str]:
    keys = set(old) | set(new)
    d = [(new.get(k, 0) - old.get(k, 0), k) for k in keys if new.get(k, 0) != old.get(k, 0)]
    d.sort(key=lambda t: (-abs(t[0]), t[1]))
    return [f"    {delta:+d}  {key}" for delta, key in d[:top]]


def compare(base: dict[str, Any] | None, got: dict[str, Any]) -> tuple[list[str], bool]:
    """Differences between one workload's baseline row and its new measurement.

    Returns (lines describing every difference, whether any cost rose).
    """
    if base is None:
        return ["  not in the baseline (run --update)"], False
    lines: list[str] = []
    rose = False
    if got["tool"] != base["tool"]:
        lines.append(f"  tool {base['tool']} -> {got['tool']}")
    if got["output_sha256"] != base["output_sha256"]:
        lines.append(
            f"  output_sha256 {base['output_sha256'][:12]} -> {got['output_sha256'][:12]}"
            " (the answer changed: did the workload run what it names?)"
        )
    if got["calls_total"] != base["calls_total"]:
        delta = got["calls_total"] - base["calls_total"]
        rose |= delta > 0
        lines.append(f"  calls_total {base['calls_total']} -> {got['calls_total']} ({delta:+d})")
        lines += _deltas(base["calls"], got["calls"])
    elif got["calls"] != base["calls"]:
        lines.append("  same calls_total, different functions:")
        lines += _deltas(base["calls"], got["calls"])
    for host in sorted(set(base["http"]) | set(got["http"])):
        b, g = base["http"].get(host, 0), got["http"].get(host, 0)
        if b != g:
            rose |= g > b
            lines.append(f"  http {host} {b} -> {g} ({g - b:+d})")
    return lines, rose


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--update", action="store_true", help="write the measured counts")
    ap.add_argument("--allow-increase", action="store_true", help="let --update raise a count")
    ap.add_argument(
        "--allow-output-change", action="store_true", help="let --update take a changed answer"
    )
    ap.add_argument("--record", action="store_true", help="re-record fixtures (live network)")
    ap.add_argument("--only", default="", help="comma-separated workload names")
    a = ap.parse_args(argv)
    names = a.only.split(",") if a.only else list(WORKLOADS)
    unknown = [n for n in names if n not in WORKLOADS]
    if unknown:
        raise SystemExit(f"unknown workloads {unknown}; known: {list(WORKLOADS)}")
    baseline: dict[str, Any] = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}
    rows: dict[str, Any] = baseline.get("workloads", {})
    if not a.only:
        for stale in sorted(set(rows) - set(WORKLOADS)):
            print(f"{stale}: in the baseline but no longer a workload")
    failed = rose_any = output_changed = False
    measured: dict[str, Any] = {}
    for name in names:
        got = measure(name, record=a.record)
        measured[name] = got
        lines, rose = compare(rows.get(name), got)
        rose_any |= rose
        base = rows.get(name)
        output_changed |= base is not None and (
            got["output_sha256"] != base["output_sha256"] or got["tool"] != base["tool"]
        )
        http = sum(got["http"].values())
        status = "ok" if not lines else ("ROSE" if rose else "CHANGED")
        print(f"{name:22} {status:7} calls={got['calls_total']:5} http={http:3} {got['tool']}")
        for line in lines:
            print(line)
        failed |= bool(lines)
    if a.update:
        if rose_any and not a.allow_increase:
            print("\nrefused: a count rose; rerun with --update --allow-increase to accept it")
            return 1
        if output_changed and not a.allow_output_change:
            print("\nrefused: an answer changed; rerun with --allow-output-change if it was meant")
            return 1
        new_rows = {**rows, **measured} if a.only else measured
        BASELINE.write_text(
            json.dumps({"workloads": dict(sorted(new_rows.items()))}, indent=1) + "\n"
        )
        print(f"\nwrote {BASELINE.relative_to(REPO)}")
        return 0
    if a.record:
        return 0
    total = sum(m["calls_total"] for m in measured.values())
    print(f"\n{len(measured)} workloads, {total} calls: {'DIFFERS' if failed else 'all equal'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
