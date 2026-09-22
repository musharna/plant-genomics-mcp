"""Walk CHAIN over genes.tsv through the MCP; write raw/ and calls.jsonl.

Every call goes over the real stdio protocol through `McpClient` — nothing
here imports the server's Python package, so what is captured is what an
agent on the other end of the pipe would actually receive, including the
server version, which is read off the `initialize` handshake rather than
off whatever `plant_genomics_mcp` happens to be importable here.

`genes.tsv` may hold any number of rows in any number of organisms. The
walk is per organism, then per chain tool, then per locus:

- A chain tool with a `batch_` form on the live server (`BATCH_FORMS`) is
  called through that form, `BATCH_MAX` loci per call, one call per
  chunk. The batch envelope (`results[locus]` / `errors[locus]`) is split
  back into one `raw/<locus>__<chain_tool>.json` per locus, so the
  per-gene evidence has one layout whichever way the call went; the
  envelope itself is kept too, as `raw/_batch__<batch_tool>__<organism>__<n>.json`.
- The other chain tools are called once per locus.

Outputs, all next to this file:

- `raw/<locus>__<tool>.json` — one file per (locus, chain tool). A failed
  call is written as `{"ok": false, "error": ...}`; it is never written
  as an empty or partial success object, which would read to a later
  pass as "the tool answered, there is just nothing there". A refusal the
  tool documents for that organism (`EXPECTED_ERROR_TAGS`) is written as
  `{"ok": false, "expected": true, "error": ...}`.
- `calls.jsonl` — one row per MCP call, batch or single. `tool` is the
  tool actually called; `chain_tool` is the chain entry it served;
  `locus` is set for a single call and `loci` for a batch call; `n_bytes`
  is the size of the JSON-RPC response line on the wire; `kind` is
  `ok` / `error` / `expected` for the call as a whole, and `n_ok` /
  `n_error` / `n_expected` count the loci inside it.
- `gaps_auto.jsonl` — the machine-detectable gaps (`"auto": true`),
  rewritten from scratch on every run. One row per (chain tool, kind,
  error class), carrying the list of loci it covers and their count:
  with a hundred genes, one row per locus would bury the page under
  copies of the same finding.

The runner does not touch `gaps.jsonl`. That file holds the hand-logged
rows (`"auto": false`) and is the one destined to become public issues; a
runner that appended to it would duplicate its own rows on a re-run, and
one that truncated it would delete the hand-written ones. Writing to a
separate file opened `"w"` makes a re-run idempotent in both directions.

Calls are sequential on purpose: the tools sit in front of public APIs
(Ensembl Plants, InterPro, STRING, KEGG, ...) that should not be hit in
parallel from a demo.
"""

from __future__ import annotations

import asyncio
import csv
import json
import re
import signal
import sys
import time
from collections import defaultdict
from pathlib import Path

from examples.arf_family.chain import CHAIN
from examples.arf_family.mcp_client import SERVER_CMD, McpClient

HERE = Path(__file__).parent
OVERSIZE = 200_000  # bytes; larger responses are auto-logged as gaps
WALLTIME_S = 4 * 3600
BATCH_MAX = 50  # every batch_* tool's `maxItems` on the live schema

# chain tool -> (batch tool, name of its list argument, takes `organism`).
# Checked against the live `tools/list` by `tests/test_arf_chain.py`.
# `batch_gramene_homologs`, like `gramene_homologs`, has no `organism`
# property (`gaps.jsonl`, `argument-name`), and the live schemas set
# `additionalProperties: false`, so passing one would be rejected.
BATCH_FORMS: dict[str, tuple[str, str, bool]] = {
    "ensembl_plants_lookup_locus": ("batch_ensembl_plants_lookup_locus", "loci", True),
    "resolve_locus_to_uniprot": ("batch_resolve_locus_to_uniprot", "loci", True),
    "gramene_homologs": ("batch_gramene_homologs", "loci", False),
    "atted_coexpression": ("batch_atted_coexpression", "loci", True),
    "string_interactions": ("batch_string_interactions", "loci_or_accessions", True),
    "locus_go_annotations": ("batch_locus_go_annotations", "loci", True),
    "kegg_pathways": ("batch_kegg_pathways", "loci", True),
    "locus_literature": ("batch_locus_literature", "loci", True),
}

# An error carrying one of these tags is the tool refusing an organism it
# documents as unsupported — recorded as `expected`, not as a gap.
EXPECTED_ERROR_TAGS = ("[OrganismNotSupported]",)

_DIGITS = re.compile(r"\d+")


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


def is_expected(error: str | None) -> bool:
    return bool(error) and any(tag in error for tag in EXPECTED_ERROR_TAGS)


def error_class(error: str, locus: str) -> str:
    """The error text with the locus and every number blanked, so the same
    failure on different loci groups into one auto-gap row."""
    return _DIGITS.sub("N", error.replace(locus, "<locus>"))[:200]


def _walltime_guard(*_: object) -> None:
    sys.stderr.write(f"aborting: walltime guard fired after {WALLTIME_S}s\n")
    sys.exit(2)


class Runner:
    def __init__(self, client: McpClient, here: Path, server_version: str) -> None:
        self.c = client
        self.here = here
        self.server_version = server_version
        # Opened here and closed by `close()`: the log must be flushed row by
        # row through a walk that can take an hour, not held until exit.
        self.calls = open(here / "calls.jsonl", "w")  # noqa: SIM115
        # (chain_tool, kind, error class) -> loci
        self.auto: dict[tuple[str, str, str], list[str]] = defaultdict(list)
        self.batch_seq: dict[str, int] = defaultdict(int)

    def close(self) -> None:
        self.calls.close()

    def _write_raw(self, locus: str, chain_tool: str, ok: bool, payload, error) -> None:
        if ok:
            raw = payload
        else:
            raw = {"ok": False, "error": error}
            if is_expected(error):
                raw["expected"] = True
        (self.here / "raw" / f"{locus}__{chain_tool}.json").write_text(
            json.dumps(raw, indent=1) + "\n"
        )

    def _note(self, locus: str, chain_tool: str, ok: bool, error, n_bytes: int) -> str:
        """Record the per-locus outcome; return its kind."""
        if ok:
            if n_bytes > OVERSIZE:
                self.auto[(chain_tool, "oversize", f"{n_bytes} bytes")].append(locus)
            return "ok"
        if is_expected(error):
            return "expected"
        self.auto[(chain_tool, "error", error_class(error, locus))].append(locus)
        return "error"

    def _log_call(self, **row) -> None:
        row.update(server_version=self.server_version, ts=int(time.time()))
        self.calls.write(json.dumps(row) + "\n")
        self.calls.flush()

    async def single(self, locus: str, organism: str, chain_tool: str, build) -> None:
        args = build(locus, organism)
        res = await self.c.call(chain_tool, args)
        kind = self._note(locus, chain_tool, res.ok, res.error, res.n_bytes)
        self._write_raw(locus, chain_tool, res.ok, res.payload, res.error)
        self._log_call(
            organism=organism,
            tool=chain_tool,
            chain_tool=chain_tool,
            locus=locus,
            loci=None,
            args=args,
            ok=res.ok,
            kind=kind,
            error=res.error,
            n_ok=int(kind == "ok"),
            n_error=int(kind == "error"),
            n_expected=int(kind == "expected"),
            elapsed_s=round(res.elapsed_s, 2),
            n_bytes=res.n_bytes,
            upstream_version=find_version(res.payload),
        )
        print(f"{locus} {chain_tool}: {kind} {res.n_bytes}B", file=sys.stderr)

    async def batch(self, loci: list[str], organism: str, chain_tool: str) -> None:
        batch_tool, list_arg, takes_organism = BATCH_FORMS[chain_tool]
        args = {list_arg: loci, "organism": organism} if takes_organism else {list_arg: loci}
        res = await self.c.call(batch_tool, args)
        self.batch_seq[batch_tool] += 1
        n = self.batch_seq[batch_tool]
        (self.here / "raw" / f"_batch__{batch_tool}__{organism}__{n}.json").write_text(
            json.dumps(res.payload if res.ok else {"ok": False, "error": res.error}, indent=1)
            + "\n"
        )
        counts = {"ok": 0, "error": 0, "expected": 0}
        if res.ok:
            results = res.payload.get("results", {})
            errors = res.payload.get("errors", {})
            for locus in loci:
                if locus in results:
                    kind = self._note(locus, chain_tool, True, None, 0)
                    self._write_raw(locus, chain_tool, True, results[locus], None)
                elif locus in errors:
                    kind = self._note(locus, chain_tool, False, errors[locus], 0)
                    self._write_raw(locus, chain_tool, False, None, errors[locus])
                else:
                    missing = f"{batch_tool}: locus absent from both results and errors"
                    kind = self._note(locus, chain_tool, False, missing, 0)
                    self._write_raw(locus, chain_tool, False, None, missing)
                counts[kind] += 1
            if res.n_bytes > OVERSIZE:
                self.auto[(chain_tool, "oversize", f"{res.n_bytes} bytes (batch)")].extend(loci)
            # An ok envelope whose every locus was refused is a refused call,
            # not an ok one: the call row must not read better than its rows.
            if counts["error"]:
                call_kind = "error"
            elif counts["expected"] == len(loci):
                call_kind = "expected"
            else:
                call_kind = "ok"
        else:
            # The whole call failed: every locus inherits the one error.
            for locus in loci:
                kind = self._note(locus, chain_tool, False, res.error, 0)
                self._write_raw(locus, chain_tool, False, None, res.error)
                counts[kind] += 1
            call_kind = "expected" if is_expected(res.error) else "error"
        self._log_call(
            organism=organism,
            tool=batch_tool,
            chain_tool=chain_tool,
            locus=None,
            loci=loci,
            args=args,
            ok=res.ok,
            kind=call_kind,
            error=res.error,
            n_ok=counts["ok"],
            n_error=counts["error"],
            n_expected=counts["expected"],
            elapsed_s=round(res.elapsed_s, 2),
            n_bytes=res.n_bytes,
            upstream_version=find_version(res.payload),
        )
        print(
            f"{organism} {batch_tool} x{len(loci)}: {call_kind} {res.n_bytes}B {counts}",
            file=sys.stderr,
        )

    def write_auto_gaps(self) -> None:
        with open(self.here / "gaps_auto.jsonl", "w") as gaps:
            for (chain_tool, kind, cls), loci in self.auto.items():
                gaps.write(
                    json.dumps(
                        {
                            "kind": kind,
                            "tool": chain_tool,
                            "locus": loci[0] if len(loci) == 1 else f"{len(loci)} loci",
                            "loci": loci,
                            "attempted": f"{chain_tool} on {len(loci)} loci",
                            "returned": cls[:300],
                            "expected": "a usable answer within 200 kB"
                            if kind == "oversize"
                            else "a usable answer",
                            "auto": True,
                        }
                    )
                    + "\n"
                )


async def main(server_cmd: list[str] = SERVER_CMD, here: Path = HERE) -> int:
    signal.signal(signal.SIGALRM, _walltime_guard)
    signal.alarm(WALLTIME_S)

    with open(here / "genes.tsv") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    (here / "raw").mkdir(exist_ok=True)
    by_organism: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        by_organism[r["organism"]].append(r["locus"])

    c = McpClient(server_cmd)
    await c.start()
    try:
        server_version = (c.server_info or {}).get("version")
        if not server_version:
            raise RuntimeError(
                f"the server's initialize response carried no version: {c.server_info!r}"
            )
        runner = Runner(c, here, server_version)
        try:
            for organism, loci in by_organism.items():
                for tool, build in CHAIN:
                    if tool in BATCH_FORMS:
                        for i in range(0, len(loci), BATCH_MAX):
                            await runner.batch(loci[i : i + BATCH_MAX], organism, tool)
                    else:
                        for locus in loci:
                            await runner.single(locus, organism, tool, build)
        finally:
            runner.close()
        runner.write_auto_gaps()
    finally:
        await c.close()
        # The guard's job ends with the walk. Leaving it armed would fire a
        # SIGALRM into whatever ran next in this interpreter.
        signal.alarm(0)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
