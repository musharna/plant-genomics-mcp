"""Re-ask the hand-logged gap rows that the chain run does not answer.

Most rows in `gaps.jsonl` are read off the chain's own captures in `raw/`.
The rest were found by one-off calls outside the chain — a gene symbol in a
`locus` argument, an organism a backend does not cover, a family accession,
a second page — and a re-run can only close or keep them by asking again.
This makes those calls, over the same stdio transport as `run_dossier.py`,
and writes every call and the relevant live tool descriptions to
`raw/_probe_rerun.json`, so each `closed` field names a file, not a memory.

A failed call is recorded as it came back (`ok: false` plus the error);
nothing here decides whether a row is closed.
"""

from __future__ import annotations

import asyncio
import json
import signal
import sys
from pathlib import Path

from examples.arf_family.mcp_client import SERVER_CMD, McpClient
from examples.arf_family.run_dossier import server_commit

HERE = Path(__file__).parent
WALLTIME_S = 900
ATH = "arabidopsis_thaliana"

# (row kind, tool, arguments). A row may take several calls; `symbol-*`
# repeats the first run's six calls in `raw/_symbol_probe_ARF1.json`.
PROBES: list[tuple[str, str, dict]] = [
    ("shared-symbol", "resolve_locus_to_uniprot", {"locus": "ARF1", "organism": ATH}),
    ("shared-symbol", "resolve_locus_to_uniprot", {"locus": "ARF5", "organism": ATH}),
    (
        "symbol-rejected-elsewhere",
        "ensembl_plants_lookup_locus",
        {"locus": "ARF1", "organism": ATH},
    ),
    ("symbol-rejected-elsewhere", "tair_locus_info", {"locus": "ARF1"}),
    ("symbol-rejected-elsewhere", "phytozome_lookup_locus", {"locus": "ARF1", "organism": ATH}),
    ("no-family-enumeration", "entry_members", {"entry": "IPR010525", "organism": ATH}),
    ("no-family-enumeration", "entry_members", {"entry": "PTHR31384", "organism": ATH}),
    (
        "batch-refusal-shape",
        "kegg_pathways",
        {"locus": "Os01g0236300", "organism": "triticum_aestivum"},
    ),
    (
        "batch-refusal-shape",
        "batch_kegg_pathways",
        {"loci": ["Os01g0236300"], "organism": "triticum_aestivum"},
    ),
    ("identifier-with-no-tool", "gramene_homologs", {"locus": "AT1G19850", "with_organism": True}),
    # The gene_tree_id the probe above returns, dereferenced (#130).
    (
        "identifier-with-no-tool",
        "gene_tree_members",
        {"gene_tree_id": "EPlGT00940000167082", "limit": 1000},
    ),
    # Gramene carries no paralog for this gene; Compara's paralogues do.
    ("paralog-closure-empty", "ensembl_plants_paralogs", {"locus": "AT1G19850", "limit": 1000}),
    # Last: main() follows this probe's next_cursor to its second page.
    ("no-pagination", "gramene_homologs", {"locus": "AT1G19850"}),
]

# Tools whose live description or schema a row is about.
DESCRIBED = (
    "string_interactions",
    "batch_string_interactions",
    "alphafold_structure",
    "tf_binding_motifs",
    "resolve_locus_to_uniprot",
    "locus_literature",
    "kegg_pathways",
    "batch_kegg_pathways",
    "aragwas_associations",
    "ensembl_plants_lookup_locus",
    "experimental_structures",
    "locus_go_annotations",
    "gramene_homologs",
    "gene_tree_members",
    "entry_members",
    "batch_locus_call",
    "atted_coexpression",
    "panther_family",
    "jaspar_motif",
    "bar_aiv_interactions",
    "ensembl_plants_paralogs",
)


def _walltime_guard(*_: object) -> None:
    sys.stderr.write(f"aborting: walltime guard fired after {WALLTIME_S}s\n")
    sys.exit(2)


def _record(row: str, tool: str, args: dict, res) -> dict:
    return {
        "row": row,
        "tool": tool,
        "args": args,
        "ok": res.ok,
        "payload": res.payload,
        "error": res.error,
        "n_bytes": res.n_bytes,
    }


async def main(server_cmd: list[str] = SERVER_CMD, out: Path = HERE / "raw") -> int:
    signal.signal(signal.SIGALRM, _walltime_guard)
    signal.alarm(WALLTIME_S)
    c = McpClient(server_cmd)
    await c.start()
    try:
        tools = await c.list_tools()
        calls = []
        for row, tool, args in PROBES:
            res = await c.call(tool, args)
            calls.append(_record(row, tool, args, res))
            print(f"{row}: {tool} -> {'ok' if res.ok else res.error}", file=sys.stderr)
        # The second page of the last probe, through the cursor it returned.
        first = calls[-1]["payload"] or {}
        if first.get("next_cursor"):
            args = {"locus": "AT1G19850", "cursor": first["next_cursor"]}
            res = await c.call("gramene_homologs", args)
            calls.append(_record("no-pagination", "gramene_homologs", args, res))
        by_name = {t["name"]: t for t in tools}
        doc = {
            "server_version": (c.server_info or {}).get("version"),
            "server_commit": server_commit(),
            "tool_count": len(tools),
            "tools": {name: by_name[name] for name in DESCRIBED if name in by_name},
            "missing_tools": [name for name in DESCRIBED if name not in by_name],
            "calls": calls,
        }
    finally:
        await c.close()
        signal.alarm(0)
    (out / "_probe_rerun.json").write_text(json.dumps(doc, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
