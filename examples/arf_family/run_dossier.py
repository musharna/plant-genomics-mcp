"""Walk CHAIN over genes.tsv through the MCP; write raw/ and calls.jsonl.

Every call goes over the real stdio protocol through `McpClient` — nothing
here imports the server's Python package, so what is captured is what an
agent on the other end of the pipe would actually receive, including the
server version, which is read off the `initialize` handshake rather than
off whatever `plant_genomics_mcp` happens to be importable here.

Outputs, all next to this file:

- `raw/<locus>__<tool>.json` — one file per call. A failed call is written
  as `{"ok": false, "error": ...}`; it is never written as an empty or
  partial success object, which would read to a later pass as "the tool
  answered, there is just nothing there".
- `calls.jsonl` — one row per call, for the per-tool summary.
- `gaps_auto.jsonl` — the machine-detectable gaps (`"auto": true`), rewritten
  from scratch on every run.

The runner does not touch `gaps.jsonl`. That file holds the hand-logged rows
(`"auto": false`) and is the one destined to become public issues; a runner
that appended to it would duplicate its own rows on a re-run, and one that
truncated it would delete the hand-written ones. Writing to a separate file
opened `"w"` makes a re-run idempotent in both directions.

Calls are sequential on purpose: the tools sit in front of public APIs
(Ensembl Plants, InterPro, STRING, KEGG, ...) that should not be hit in
parallel from a demo.
"""

from __future__ import annotations

import asyncio
import csv
import json
import signal
import sys
import time
from pathlib import Path

from examples.arf_family.chain import CHAIN
from examples.arf_family.mcp_client import SERVER_CMD, McpClient

HERE = Path(__file__).parent
OVERSIZE = 200_000  # bytes; larger responses are auto-logged as gaps
WALLTIME_S = 3600


def find_version(obj: object) -> str | None:
    """First non-empty `upstream_version` anywhere in a payload, else None."""
    if isinstance(obj, dict):
        if obj.get("upstream_version"):
            return obj["upstream_version"]
        for v in obj.values():
            if (r := find_version(v)) is not None:
                return r
    if isinstance(obj, list):
        for v in obj:
            if (r := find_version(v)) is not None:
                return r
    return None


def _walltime_guard(*_: object) -> None:
    sys.stderr.write(f"aborting: walltime guard fired after {WALLTIME_S}s\n")
    sys.exit(2)


async def main(server_cmd: list[str] = SERVER_CMD, here: Path = HERE) -> int:
    signal.signal(signal.SIGALRM, _walltime_guard)
    signal.alarm(WALLTIME_S)

    with open(here / "genes.tsv") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    (here / "raw").mkdir(exist_ok=True)

    c = McpClient(server_cmd)
    await c.start()
    try:
        server_version = (c.server_info or {}).get("version")
        if not server_version:
            raise RuntimeError(
                f"the server's initialize response carried no version: {c.server_info!r}"
            )
        with (
            open(here / "calls.jsonl", "w") as calls,
            open(here / "gaps_auto.jsonl", "w") as gaps,
        ):
            for r in rows:
                for tool, build in CHAIN:
                    args = build(r["locus"], r["organism"])
                    res = await c.call(tool, args)
                    calls.write(
                        json.dumps(
                            {
                                "locus": r["locus"],
                                "organism": r["organism"],
                                "tool": tool,
                                "args": args,
                                "ok": res.ok,
                                "error": res.error,
                                "elapsed_s": round(res.elapsed_s, 2),
                                "n_bytes": res.n_bytes,
                                "upstream_version": find_version(res.payload),
                                "server_version": server_version,
                                "ts": int(time.time()),
                            }
                        )
                        + "\n"
                    )
                    # A failure is recorded as a failure. Writing the
                    # payload-shaped `{}` a failed call leaves behind would
                    # make an error indistinguishable from an empty answer.
                    raw = res.payload if res.ok else {"ok": False, "error": res.error}
                    (here / "raw" / f"{r['locus']}__{tool}.json").write_text(
                        json.dumps(raw, indent=1) + "\n"
                    )
                    if not res.ok or res.n_bytes > OVERSIZE:
                        gaps.write(
                            json.dumps(
                                {
                                    "kind": "error" if not res.ok else "oversize",
                                    "locus": r["locus"],
                                    "tool": tool,
                                    "attempted": f"{tool}({args})",
                                    "returned": (res.error or f"{res.n_bytes} bytes")[:300],
                                    "expected": "a usable answer within 200 kB",
                                    "auto": True,
                                }
                            )
                            + "\n"
                        )
                    print(
                        f"{r['locus']} {tool}: {'ok' if res.ok else 'ERR'} {res.n_bytes}B",
                        file=sys.stderr,
                    )
    finally:
        await c.close()
        # The guard's job ends with the walk. Leaving it armed would fire a
        # SIGALRM into whatever ran next in this interpreter.
        signal.alarm(0)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
