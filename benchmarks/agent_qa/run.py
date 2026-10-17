"""Ask every question to Claude Code under each arm, and score the replies.

Arms (same model, same prompt, isolated: no user settings, hooks or other MCP
servers, an empty working directory per run):

- ``bare``: no tools at all.
- ``web``:  WebSearch and WebFetch only.
- ``mcp``:  plant-genomics-mcp (pinned version from PyPI via uvx) only.

A row's ``valid`` says whether the run measured its arm, by one rule for
every arm (:func:`validity`); ``invalid_reason`` says why not. Every question
is a lookup of data the model cannot be sure of, so a tool arm that made no
tool call measured the model alone. A row whose transcript mentions the
answer key or the benchmark's own files (:func:`leak_hits`) is invalid too.
No run is retried in any arm: a timeout or non-zero exit stays in
``results.jsonl`` as an invalid row.

Each finished run is appended to ``results.jsonl`` with its score; a rerun
skips (arm, question, rep) triples already there, so an interrupted sweep
resumes. Raw stream-json transcripts go to ``<runs>/<arm>/<qid>-<rep>.jsonl``,
outside the repository by default, so no arm can come across them.

Before a sweep it refuses to start unless the key's own answers score 100%
right and shifted answers 0% (baselines.py), and unless Ensembl still serves
the release the key was built from.

    uv run python benchmarks/agent_qa/run.py --reps 3 --workers 4
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import httpx
from baselines import oracle, shifted
from score import score, score_ncbi

HERE = Path(__file__).resolve().parent
RUNS = Path.home() / "plant-genomics-mcp-artifacts" / "agent_qa_runs"
# What each arm must have been given (read from the init event). The bare arm
# has no tools and is the only one not required to call any.
INTENDED_TOOLS = {
    "bare": lambda names: not names,
    "web": lambda names: set(names) == {"WebSearch", "WebFetch"},
    "mcp": lambda names: bool(names) and all(n.startswith("mcp__plant-genomics__") for n in names),
}
# The key, the benchmark's own files and transcript directories, and the
# benchmark directory on GitHub. A transcript whose tool inputs or outputs name
# any of them could have read the answers, so its row is invalid. Not the
# run's own empty working directory (agentqa-<arm>-*): Claude Code names it in
# the path where it saves a large WebFetch body, which flagged 2 web runs
# before this was narrowed, and it holds nothing.
LEAK = re.compile(
    r"questions\.json|build_questions|ncbi_locations|baselines\.jsonl|results\.jsonl"
    r"|summary\.tsv|agent_qa_runs|benchmarks/agent_qa"
    r"|(?:github\.com|githubusercontent\.com)/musharna/plant-genomics-mcp\S*benchmarks",
    re.IGNORECASE,
)
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
    blocks = [
        b
        for e in events
        if e.get("type") in ("assistant", "user")
        for b in (e.get("message") or {}).get("content") or []
        if isinstance(b, dict)
    ]
    tools = [b["name"] for b in blocks if b.get("type") == "tool_use"]
    # Everything the tools were sent and sent back, for the leak scan.
    tool_io = [
        json.dumps(b.get("input") if b.get("type") == "tool_use" else b.get("content"))
        for b in blocks
        if b.get("type") in ("tool_use", "tool_result")
    ]
    return {
        "claude_code": init.get("claude_code_version"),
        "model": init.get("model"),
        "mcp_servers": init.get("mcp_servers"),
        "tools_available": len(init.get("tools") or []),
        "tool_calls": tools,
        "cost_usd": result.get("total_cost_usd"),
        "reply": result.get("result") or "",
        "_tool_names": init.get("tools") or [],
        "_has_result": bool(result),
        "_tool_io": tool_io,
    }


def leak_hits(tool_io: list[str]) -> list[str]:
    """Distinct leak patterns found in a run's tool inputs and outputs."""
    return sorted({m.group(0).lower() for text in tool_io for m in LEAK.finditer(text)})


def validity(arm: str, exit_code: object, t: dict[str, Any]) -> tuple[bool, str | None]:
    """One rule for every arm: (valid, reason if not).

    A run is invalid if it did not exit 0, wrote no result, was not given
    exactly its arm's tools (and, for mcp, a connected server), made no tool
    call when its arm has tools, or touched the key (:func:`leak_hits`).
    """
    if exit_code != 0:
        return False, f"exit {exit_code}"
    if not t["_has_result"]:
        return False, "no result event"
    if not INTENDED_TOOLS[arm](t["_tool_names"]):
        return False, "tools not as intended"
    if arm == "mcp" and not any(s.get("status") == "connected" for s in t["mcp_servers"] or []):
        return False, "server not connected"
    if arm != "bare" and not t["tool_calls"]:
        return False, "no tool call"
    hits = leak_hits(t["_tool_io"])
    if hits:
        return False, "leak: " + ", ".join(hits)
    return True, None


def _row_fields(arm: str, exit_code: object, t: dict[str, Any]) -> dict[str, Any]:
    valid, reason = validity(arm, exit_code, t)
    return {
        **{k: v for k, v in t.items() if not k.startswith("_")},
        "valid": valid,
        "invalid_reason": reason,
        "leak_hits": leak_hits(t["_tool_io"]),
    }


def run_one(arm: str, q: dict[str, Any], rep: int, runs: Path) -> dict[str, Any]:
    out = runs / arm / f"{q['id']}-{rep}.jsonl"
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
        **_row_fields(arm, rc, t),
        **score(q, t["reply"]),
        "verdict_ncbi": score_ncbi(q, t["reply"]),
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
        r["verdict_ncbi"] = score_ncbi(by_id[r["qid"]], r["reply"])
    results.write_text("".join(json.dumps(r) + "\n" for r in rows))


def audit(results: Path, runs: Path) -> None:
    """Recompute every stored row's validity and leak scan from its raw
    transcript in ``runs`` (``<arm>/<qid>-<rep>.jsonl``), and fail if a
    transcript disagrees with the row it should have produced."""
    rows = [json.loads(ln) for ln in results.read_text().splitlines()]
    for r in rows:
        t = parse_transcript((runs / r["arm"] / f"{r['qid']}-{r['rep']}.jsonl").read_text())
        for field in ("mcp_servers", "tool_calls", "reply", "cost_usd"):
            if t[field] != r[field]:
                raise SystemExit(f"{r['arm']} {r['qid']} rep{r['rep']}: transcript {field} differs")
        r.update(_row_fields(r["arm"], r["exit"], t))
        if not r["valid"]:
            print(f"invalid: {r['arm']:4} {r['qid']} rep{r['rep']} ({r['verdict']}): "
                  f"{r['invalid_reason']}")  # fmt: skip
    results.write_text("".join(json.dumps(r) + "\n" for r in rows))
    scanned = sum(1 for r in rows if r["tool_calls"])
    print(f"{sum(not r['valid'] for r in rows)} of {len(rows)} rows invalid; leak scan: "
          f"{sum(bool(r['leak_hits']) for r in rows)} hits in {len(rows)} transcripts "
          f"({scanned} with tool calls)")  # fmt: skip


def preflight(key: dict[str, Any], allow_release_drift: bool) -> None:
    """Refuse a sweep the key cannot score: the oracle must score 100% right
    and shifted answers 0%, and Ensembl must serve the key's release."""
    questions = key["questions"]
    o, s = oracle(questions), shifted(questions)
    if o.count("right") != len(questions) or s.count("right") != 0:
        raise SystemExit(f"oracle check failed: oracle {o.count('right')}/{len(questions)}"
                         f" right, shifted {s.count('right')} right")  # fmt: skip
    headers = {"Accept": "application/json"}
    live = (
        httpx.get("https://rest.ensembl.org/info/software", headers=headers, timeout=30)
        .raise_for_status()
        .json()["release"]
    )
    live_eg = (
        httpx.get("https://rest.ensembl.org/info/eg_version", headers=headers, timeout=30)
        .raise_for_status()
        .json()["version"]
    )
    built = (key["ensembl_release"], key["ensembl_genomes_release"])
    if (int(live), int(live_eg)) != built and not allow_release_drift:
        raise SystemExit(f"Ensembl serves {live}/EG {live_eg}, the key was built on "
                         f"{built[0]}/EG {built[1]}: rebuild the key or pass "
                         "--allow-release-drift")  # fmt: skip
    print(f"preflight: oracle {len(questions)}/{len(questions)} right, shifted 0 right; "
          f"Ensembl {live}/EG {live_eg}")  # fmt: skip


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
    ap.add_argument("--runs", default=str(RUNS), help="where raw transcripts go")
    ap.add_argument(
        "--audit",
        action="store_true",
        help="recompute validity and the leak scan from the transcripts in --runs, run nothing",
    )
    ap.add_argument("--allow-release-drift", action="store_true")
    a = ap.parse_args()
    key = json.loads((HERE / "questions.json").read_text())
    questions = key["questions"]
    if a.audit:
        audit(Path(a.results), Path(a.runs))
        return
    if a.rescore:
        rescore(Path(a.results), {q["id"]: q for q in questions})
        return
    preflight(key, a.allow_release_drift)
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
        futures = [pool.submit(run_one, *t, Path(a.runs)) for t in todo]
        for n, f in enumerate(as_completed(futures), 1):
            r = f.result()
            with lock, results.open("a") as fh:
                fh.write(json.dumps(r) + "\n")
            print(
                f"[{n}/{len(todo)}] {r['arm']:4} {r['qid']} rep{r['rep']} {r['verdict']:9}"
                f" exit={r['exit']} {r['seconds']}s calls={len(r['tool_calls'])} valid={r['valid']}",
                flush=True,
            )
    sys.exit(0)


if __name__ == "__main__":
    main()
