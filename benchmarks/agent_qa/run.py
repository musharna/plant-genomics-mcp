"""Ask every question to Claude Code under each arm, and score the replies.

Arms (same model, same prompt, isolated: no user settings, hooks or other MCP
servers, an empty working directory per run):

- ``bare``: no tools at all.
- ``web``:  WebSearch and WebFetch only.
- ``mcp``:  plant-genomics-mcp (pinned version from PyPI via uvx) only.

A row's ``valid`` says whether the run measured its arm: an ``mcp`` run is
valid only if the server connected and the model called at least one of its
tools. A server run that answered from memory measured the model alone.

Each finished run is appended to ``results.jsonl`` with its score; a rerun
skips (arm, question, rep) triples already there, so an interrupted sweep
resumes. Raw stream-json transcripts go to ``runs/<arm>/<qid>-<rep>.jsonl``.

    uv run python benchmarks/agent_qa/run.py --reps 3 --workers 4
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from score import score

HERE = Path(__file__).resolve().parent
MODEL = "claude-opus-5-5"
SERVER = "plant-genomics-mcp==1.29.2"
# uvx resolves the server's dependencies afresh, so pinning the server alone
# would let a later mcp or httpx release change what a rerun measures. Only
# packages uploaded before this instant are candidates: the end of the key's
# build day (questions.json "built": 2026-10-16) in UTC. On 2026-10-16 it
# resolved the same 33 packages as the sweep's start (19:45 UTC); 1.29.2 itself
# was uploaded at 17:57 UTC, and a cutoff of 2026-10-15 cannot resolve it.
EXCLUDE_NEWER = "2026-10-17T00:00:00Z"
RUN_TIMEOUT_S = 900
# Every arm gets this in place of Claude Code's own system prompt. That prompt
# describes tools even when --tools "" removes them all, and in the pilot the
# no-tools arm wrote fake <invoke name="Bash"> calls as text instead of
# answering. Tools still reach the model through the API in the other arms.
SYSTEM = "You are a helpful assistant. Answer the user's question."
PROMPT = (
    "{question}\n\nGive the answer as {answer_format}. Keep your reply short, and end it "
    "with one line of the form\nANSWER: <answer>\nIf you cannot determine the answer, end "
    "with\nANSWER: UNKNOWN"
)


def arm_flags(arm: str, tmp: Path) -> list[str]:
    empty = tmp / "no-mcp.json"
    empty.write_text('{"mcpServers": {}}')
    if arm == "bare":
        return ["--tools", "", "--strict-mcp-config", "--mcp-config", str(empty)]
    if arm == "web":
        return [
            "--tools", "WebSearch,WebFetch", "--allowedTools", "WebSearch,WebFetch",
            "--strict-mcp-config", "--mcp-config", str(empty),
        ]  # fmt: skip
    if arm == "mcp":
        cfg = tmp / "mcp.json"
        cfg.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "plant-genomics": {
                            "command": "uvx",
                            "args": ["--exclude-newer", EXCLUDE_NEWER, SERVER],
                        }
                    }
                }
            )
        )
        return [
            "--tools", "", "--allowedTools", "mcp__plant-genomics",
            "--strict-mcp-config", "--mcp-config", str(cfg),
        ]  # fmt: skip
    raise ValueError(arm)


def parse_transcript(stdout: str) -> dict[str, Any]:
    """The fields a row takes from a run's stream-json transcript."""
    events = [json.loads(ln) for ln in stdout.splitlines() if ln.startswith("{")]
    init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), {})
    result = next((e for e in events if e.get("type") == "result"), {})
    tools = [
        b["name"]
        for e in events
        if e.get("type") == "assistant"
        for b in e["message"].get("content", [])
        if b.get("type") == "tool_use"
    ]
    return {
        "claude_code": init.get("claude_code_version"),
        "model": init.get("model"),
        "mcp_servers": init.get("mcp_servers"),
        "tools_available": len(init.get("tools") or []),
        "tool_calls": tools,
        "cost_usd": result.get("total_cost_usd"),
        "reply": result.get("result") or "",
    }


def is_valid(arm: str, mcp_servers: list[dict[str, Any]] | None, tool_calls: list[str]) -> bool:
    """Whether a run measured its arm. Only the server arm can fail this: its
    server must have connected and been called at least once."""
    if arm != "mcp":
        return True
    connected = any(s.get("status") == "connected" for s in mcp_servers or [])
    return connected and any(t.startswith("mcp__") for t in tool_calls)


def run_one(arm: str, q: dict[str, Any], rep: int) -> dict[str, Any]:
    out = HERE / "runs" / arm / f"{q['id']}-{rep}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    prompt = PROMPT.format(question=q["question"], answer_format=q["answer_format"])
    with tempfile.TemporaryDirectory(prefix=f"agentqa-{arm}-") as d:
        tmp = Path(d)
        cmd = [
            "claude", "-p", prompt, "--model", MODEL, "--setting-sources", "",
            "--output-format", "stream-json", "--verbose", "--system-prompt", SYSTEM,
            *arm_flags(arm, tmp),
        ]  # fmt: skip
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                cmd, cwd=tmp, capture_output=True, text=True, timeout=RUN_TIMEOUT_S
            )
            stdout, rc = proc.stdout, proc.returncode
        except subprocess.TimeoutExpired as exc:
            stdout = (
                (exc.stdout or b"").decode() if isinstance(exc.stdout, bytes) else exc.stdout or ""
            )
            rc = "timeout"
        seconds = round(time.monotonic() - t0, 1)
    out.write_text(stdout)
    t = parse_transcript(stdout)
    return {
        "arm": arm,
        "qid": q["id"],
        "category": q["category"],
        "rep": rep,
        "exit": rc,
        "seconds": seconds,
        **t,
        "valid": is_valid(arm, t["mcp_servers"], t["tool_calls"]),
        **score(q, t["reply"]),
    }


def rescore(results: Path, by_id: dict[str, dict[str, Any]]) -> None:
    """Score each stored reply again, so a truth or scorer fix reaches runs
    made before it; prints every verdict that changed."""
    rows = [json.loads(ln) for ln in results.read_text().splitlines()]
    for r in rows:
        new = score(by_id[r["qid"]], r["reply"])
        if new["verdict"] != r["verdict"] or new["wrong_id"] != r["wrong_id"]:
            print(f"{r['arm']:4} {r['qid']} rep{r['rep']} {r['verdict']} -> {new['verdict']}"
                  f" ({new['value']!r})")  # fmt: skip
        r.update(new)
    results.write_text("".join(json.dumps(r) + "\n" for r in rows))


def backfill_valid(results: Path, runs: Path) -> None:
    """Set ``valid`` on every stored row from its raw transcript in ``runs``
    (``<arm>/<qid>-<rep>.jsonl``), and fail if a transcript disagrees with
    the row it should have produced."""
    rows = [json.loads(ln) for ln in results.read_text().splitlines()]
    for r in rows:
        t = parse_transcript((runs / r["arm"] / f"{r['qid']}-{r['rep']}.jsonl").read_text())
        for field in ("mcp_servers", "tool_calls", "reply"):
            if t[field] != r[field]:
                raise SystemExit(f"{r['arm']} {r['qid']} rep{r['rep']}: transcript {field} differs")
        r["valid"] = is_valid(r["arm"], t["mcp_servers"], t["tool_calls"])
        if not r["valid"]:
            print(f"invalid: {r['arm']} {r['qid']} rep{r['rep']} ({r['verdict']})")
    results.write_text("".join(json.dumps(r) + "\n" for r in rows))
    print(f"{sum(not r['valid'] for r in rows)} of {len(rows)} rows invalid")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", default="bare,web,mcp")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--only", default="", help="comma-separated question ids")
    ap.add_argument("--results", default=str(HERE / "results.jsonl"))
    ap.add_argument(
        "--rescore",
        action="store_true",
        help="re-score every stored reply against the current questions.json, run nothing",
    )
    ap.add_argument(
        "--backfill-valid",
        metavar="RUNS_DIR",
        help="set each stored row's valid field from its transcript in RUNS_DIR, run nothing",
    )
    a = ap.parse_args()
    questions = json.loads((HERE / "questions.json").read_text())["questions"]
    if a.backfill_valid:
        backfill_valid(Path(a.results), Path(a.backfill_valid))
        return
    if a.rescore:
        rescore(Path(a.results), {q["id"]: q for q in questions})
        return
    if a.only:
        keep = set(a.only.split(","))
        questions = [q for q in questions if q["id"] in keep]
    results = Path(a.results)
    done = set()
    if results.exists():
        for ln in results.read_text().splitlines():
            r = json.loads(ln)
            done.add((r["arm"], r["qid"], r["rep"]))
    todo = [
        (arm, q, rep)
        for rep in range(1, a.reps + 1)
        for q in questions
        for arm in a.arms.split(",")
        if (arm, q["id"], rep) not in done
    ]
    print(f"{len(todo)} runs to do ({len(done)} already in {results.name})", flush=True)
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        futures = [pool.submit(run_one, *t) for t in todo]
        for n, f in enumerate(as_completed(futures), 1):
            r = f.result()
            with lock, results.open("a") as fh:
                fh.write(json.dumps(r) + "\n")
            print(
                f"[{n}/{len(todo)}] {r['arm']:4} {r['qid']} rep{r['rep']} {r['verdict']:9}"
                f" exit={r['exit']} {r['seconds']}s calls={len(r['tool_calls'])}",
                flush=True,
            )
    sys.exit(0)


if __name__ == "__main__":
    main()
