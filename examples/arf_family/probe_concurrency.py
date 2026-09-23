"""Does `batch_locus_call`'s fan-out width decide whether an upstream answers?

The b05bb18 chain run lost 87 of 114 `orthodb_orthologs` answers to
OrthoDB's "too high request rate" HTTP 403, 63 `panther_family` answers to
timeouts, and 18 `gene_report` Ensembl phases to HTTP 500. This runs the
same batches again — the first two over the 23 Arabidopsis members, the
third over the 25 rice ones — twice: one server started with
PLANT_GENOMICS_MCP_BATCH_CONCURRENCY=1, one with the default (8). Both
outcomes are written to
`raw/_probe_batch_concurrency.json`. A failure that follows the width is
the server's fan-out; one that does not is the upstream on the day.
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import signal
import sys
from pathlib import Path

from examples.arf_family.mcp_client import SERVER_CMD, McpClient
from examples.arf_family.run_dossier import server_commit

HERE = Path(__file__).parent
WALLTIME_S = 1800
# (tool, organism) — the three chain tools the run lost answers from.
CASES = (
    ("orthodb_orthologs", "arabidopsis_thaliana"),
    ("panther_family", "arabidopsis_thaliana"),
    ("gene_report", "oryza_sativa"),
)
WIDTHS = ("1", None)  # None: the server's default


def _walltime_guard(*_: object) -> None:
    sys.stderr.write(f"aborting: walltime guard fired after {WALLTIME_S}s\n")
    sys.exit(2)


def _failed(tool: str, payload: dict) -> dict[str, str]:
    """Per-locus failures: the envelope's errors, plus, for gene_report, a
    first (Ensembl) step that did not answer inside an ok result."""
    failed = dict(payload.get("errors") or {})
    if tool == "gene_report":
        for locus, result in (payload.get("results") or {}).items():
            step = result["steps"][0]
            if step["status"] != "ok":
                failed[locus] = step["error"]
    return failed


async def _one_width(width: str | None, loci: dict[str, list[str]]) -> list[dict]:
    # McpClient's server inherits this process's environment.
    if width is None:
        os.environ.pop("PLANT_GENOMICS_MCP_BATCH_CONCURRENCY", None)
    else:
        os.environ["PLANT_GENOMICS_MCP_BATCH_CONCURRENCY"] = width
    c = McpClient(SERVER_CMD)
    await c.start()
    rows = []
    try:
        for tool, organism in CASES:
            args = {"tool": tool, "loci": loci[organism], "args": {"organism": organism}}
            res = await c.call("batch_locus_call", args)
            payload = res.payload or {}
            errors = _failed(tool, payload)
            rows.append(
                {
                    "width": width or "default",
                    "tool": tool,
                    "organism": organism,
                    "ok": res.ok,
                    "error": res.error,
                    "n_results": len(payload.get("results") or {}),
                    "n_errors": len(errors),
                    "errors": errors,
                    "elapsed_s": round(res.elapsed_s, 2),
                }
            )
            print(f"width={width or 'default'} {tool}: {len(errors)} errors", file=sys.stderr)
    finally:
        await c.close()
    return rows


async def main(out: Path = HERE / "raw") -> int:
    signal.signal(signal.SIGALRM, _walltime_guard)
    signal.alarm(WALLTIME_S)
    with open(HERE / "genes.tsv") as f:
        genes = list(csv.DictReader(f, delimiter="\t"))
    loci = {o: [g["locus"] for g in genes if g["organism"] == o] for _, o in CASES}
    try:
        rows = [row for width in WIDTHS for row in await _one_width(width, loci)]
    finally:
        signal.alarm(0)
    doc = {"server_commit": server_commit(), "loci": loci, "runs": rows}
    (out / "_probe_batch_concurrency.json").write_text(json.dumps(doc, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
