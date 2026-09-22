"""Per-tool coverage table for the ARF dossier run.

Every tool the live server publishes (`tools/list`, 50 of them) gets one
row. The 16 the dossier's `CHAIN` actually calls (`chain.py`) come from
`calls.jsonl`; the other 34 are written as `unused` — the table is never
built from `CHAIN` alone, because that would silently drop a tool the
server added or renamed since `CHAIN` was last hand-written (the same
drift `tests/test_arf_chain.py` checks against the live schema).

Three columns beyond the brief's original five:

- `upstream_version_null` — how many of a tool's calls carried no
  `upstream_version` (see `run_dossier.find_version`). Most tools never
  surface one under that specific key.
- `median_elapsed_s` — wall-clock cost per tool, for spotting the slow
  ones without opening `calls.jsonl` by hand.
- `release_status` — a null `upstream_version` does not mean "no release
  reported": `gaps.jsonl`'s hand-logged `version-under-another-key` row
  says three tools (`atted_coexpression`, `gramene_homologs`,
  `alphafold_structure`) return a release under a different field name
  entirely. `release_status` is three-valued —
  `RELEASE_UPSTREAM_FIELD` / `RELEASE_UNDER_ANOTHER_KEY` /
  `RELEASE_ABSENT` — plus `RELEASE_UNUSED` for tools never called.
  `release_under_another_key_tools` reads the tool names for the middle
  value out of that gap row's own `raw` field (not hardcoded here), and
  checks each referenced raw file actually exists before trusting it.

`build_rows` takes the tool-name list, the parsed calls, and that
release-under-another-key set, all as plain Python data, so it can be
unit-tested (`tests/test_arf_coverage.py`) without spinning up the MCP
server subprocess or reading real files. Only `all_tool_names` talks to
the server, and only over the real stdio `tools/list` call — no network,
matching `tests/test_arf_mcp_client.py`.
"""

from __future__ import annotations

import asyncio
import csv
import json
import re
import statistics
from collections import defaultdict
from pathlib import Path

from examples.arf_family.mcp_client import SERVER_CMD, McpClient

HERE = Path(__file__).parent
CALLS_PATH = HERE / "calls.jsonl"
GAPS_PATH = HERE / "gaps.jsonl"
COVERAGE_PATH = HERE / "coverage.tsv"

RELEASE_UPSTREAM_FIELD = "upstream_version field"
RELEASE_UNDER_ANOTHER_KEY = "release under another key"
RELEASE_ABSENT = "no release in the payload"
RELEASE_UNUSED = "unused"

_RAW_TOOL_RE = re.compile(r"__(.+)\.json$")

FIELDNAMES = [
    "tool",
    "calls",
    "ok",
    "errors",
    "median_bytes",
    "upstream_version_null",
    "median_elapsed_s",
    "release_status",
    "status",
    # Task 7 (full family, batch forms): a row of calls.jsonl is one MCP
    # call, which for a batch_* tool covers up to 50 loci. `loci` is how
    # many loci the tool's calls covered in all; `locus_errors` counts the
    # per-locus failures inside envelopes plus failed single calls;
    # `expected` counts loci the tool refused for an organism it documents
    # as unsupported (`run_dossier.EXPECTED_ERROR_TAGS`) — never an error.
    "loci",
    "locus_errors",
    "expected",
]


async def all_tool_names(server_cmd: list[str] = SERVER_CMD) -> list[str]:
    """Sorted tool names from the live server's `tools/list` (no network)."""
    c = McpClient(server_cmd)
    await c.start()
    try:
        return sorted(t["name"] for t in await c.list_tools())
    finally:
        await c.close()


def load_calls(calls_path: Path) -> dict[str, list[dict]]:
    """Group `calls.jsonl` rows by tool name."""
    by_tool: dict[str, list[dict]] = defaultdict(list)
    with open(calls_path) as f:
        for line in f:
            row = json.loads(line)
            by_tool[row["tool"]].append(row)
    return by_tool


def release_under_another_key_tools(gaps_path: Path, root: Path) -> set[str]:
    """Tool names that report an upstream release under a field other than
    `upstream_version`, per the hand-logged `gaps.jsonl` row with
    `kind == "version-under-another-key"`.

    The tool names come from that row's own `raw` field — a comma-separated
    list of `raw/<locus>__<tool>.json` paths — never hardcoded here, so a
    tool added to or dropped from that gap row is picked up automatically.
    Each referenced raw file must actually exist under `root`; a row citing
    evidence that has moved or been deleted is not silently trusted. If the
    row itself is missing, this raises rather than quietly falling back to
    "no tool reports under another key" — the coverage table would then
    read as if the gap this row records had been fixed, when nobody fixed
    it (a null `upstream_version` would read as "no release reported"
    when three tools report one under a different key).
    """
    row = None
    with open(gaps_path) as f:
        for line in f:
            candidate = json.loads(line)
            if candidate.get("kind") == "version-under-another-key":
                row = candidate
                break
    if row is None:
        raise RuntimeError(
            f"{gaps_path} has no row with kind == 'version-under-another-key'. "
            "release_status would silently read every tool with a null "
            "upstream_version as RELEASE_ABSENT, including any that report "
            "a release under a different field name."
        )
    tools = set()
    for raw_ref in row["raw"].split(", "):
        raw_path = root / raw_ref
        if not raw_path.is_file():
            raise RuntimeError(f"gaps.jsonl row {row!r} cites {raw_path}, which does not exist")
        match = _RAW_TOOL_RE.search(raw_path.name)
        if not match:
            raise RuntimeError(f"can't parse a tool name out of raw path {raw_path}")
        tools.add(match.group(1))
    return tools


def build_rows(
    tool_names: list[str],
    by_tool: dict[str, list[dict]],
    release_under_another_key: set[str] = frozenset(),
) -> list[list]:
    """One row per tool in `tool_names`, in that order.

    A tool absent from `by_tool` (never called) gets `status == "unused"`
    and zeroed numeric columns. A tool with at least one call of
    `kind == "error"` gets `status == "error"` even if some of its calls
    succeeded — any failure is a gap worth a row that says so, not one
    averaged away. A call of `kind == "expected"` (a documented organism
    refusal) counts in neither `ok` nor `errors`. Rows without a `kind`
    (the thin-slice format) derive it from `ok`.

    `release_status` is derived per tool, independent of `status`: a tool
    with at least one non-null `upstream_version` gets
    `RELEASE_UPSTREAM_FIELD` even if some of its calls came back null; a
    never-called tool gets `RELEASE_UNUSED`; a called tool whose
    `upstream_version` is null on every call gets `RELEASE_UNDER_ANOTHER_KEY`
    if it's in `release_under_another_key`, else `RELEASE_ABSENT`.
    """
    rows = []
    for tool in tool_names:
        calls = by_tool.get(tool, [])
        n = len(calls)
        kinds = [r.get("kind") or ("ok" if r["ok"] else "error") for r in calls]
        ok = kinds.count("ok")
        errors = kinds.count("error")
        loci = sum(len(r["loci"]) if r.get("loci") else 1 for r in calls)
        locus_errors = sum(
            r.get("n_error", int(k == "error")) for r, k in zip(calls, kinds, strict=True)
        )
        expected = sum(
            r.get("n_expected", int(k == "expected")) for r, k in zip(calls, kinds, strict=True)
        )
        if n == 0:
            status = "unused"
        elif errors == 0:
            status = "ok"
        else:
            status = "error"
        median_bytes = int(statistics.median(r["n_bytes"] for r in calls)) if calls else 0
        upstream_version_null = sum(1 for r in calls if r.get("upstream_version") is None)
        median_elapsed_s = (
            round(statistics.median(r["elapsed_s"] for r in calls), 2) if calls else 0.0
        )
        if n == 0:
            release_status = RELEASE_UNUSED
        elif upstream_version_null < n:
            release_status = RELEASE_UPSTREAM_FIELD
        elif tool in release_under_another_key or (
            # A batch_ form carries each locus's payload, release key and
            # all, inside its envelope: the gap row cites the split
            # per-locus raw files, which are the same payloads.
            tool.removeprefix("batch_") in release_under_another_key
        ):
            release_status = RELEASE_UNDER_ANOTHER_KEY
        else:
            release_status = RELEASE_ABSENT
        rows.append(
            [
                tool,
                n,
                ok,
                errors,
                median_bytes,
                upstream_version_null,
                median_elapsed_s,
                release_status,
                status,
                loci,
                locus_errors,
                expected,
            ]
        )
    return rows


def write_coverage(rows: list[list], out_path: Path) -> None:
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(FIELDNAMES)
        w.writerows(rows)


async def main(
    server_cmd: list[str] = SERVER_CMD,
    calls_path: Path = CALLS_PATH,
    gaps_path: Path = GAPS_PATH,
    out_path: Path = COVERAGE_PATH,
) -> int:
    tool_names = await all_tool_names(server_cmd)
    by_tool = load_calls(calls_path)
    release_under_another_key = release_under_another_key_tools(gaps_path, root=HERE)
    rows = build_rows(tool_names, by_tool, release_under_another_key)
    write_coverage(rows, out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
