"""The dossier chain's tool and argument names must match the LIVE server.

`examples/arf_family/chain.py` is a hand-written table of tool names and
argument builders. Nothing in the repo stops it drifting from the server's
real `tools/list` schemas: a renamed argument, a dropped tool or a wrong
required key would only surface as a run-time error part-way through a
48-call run, one gene at a time.

This test drives the real server over real stdio (`tools/list` needs no
network — the server answers it from its own registry, which is why this
test is not gated behind `PLANT_GENOMICS_MCP_STDIO_SMOKE` the way the
live-network tests in `tests/test_arf_mcp_client.py` are) and checks every
`CHAIN` entry against the schema the server actually publishes.

`schema_violations` is the discriminator, and it is exercised in both
directions inside `test_chain_arguments_match_the_live_tool_schemas`: the
legitimate chain must produce no violations, and a deliberately misspelled
argument and an unknown tool name must each produce a violation that names
the offending tool. A checker that only ever sees good input cannot be
shown to discriminate.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from examples.arf_family import run_dossier
from examples.arf_family.chain import CHAIN
from examples.arf_family.mcp_client import SERVER_CMD, McpClient

from ._fake_mcp_server import CHAIN_FAILING_TOOL

LOCUS = "AT1G19850"
ORGANISM = "arabidopsis_thaliana"

FAKE_SERVER = Path(__file__).parent / "_fake_mcp_server.py"
FAKE_CHAIN = [sys.executable, str(FAKE_SERVER), "chain"]

ARF_DIR = Path(__file__).parent.parent / "examples" / "arf_family"
GAPS = ARF_DIR / "gaps.jsonl"
GAPS_AUTO = ARF_DIR / "gaps_auto.jsonl"
RAW = ARF_DIR / "raw"

# Every hand-logged gap row must carry these. `raw` is what makes the row
# checkable by someone who did not watch the run; `origin` is what stops a
# value the tool passed through untouched being filed as a defect the tool
# introduced.
HAND_LOGGED_KEYS = {"kind", "attempted", "returned", "expected", "raw", "origin", "auto"}
VALID_ORIGINS = {"tool", "upstream-passthrough", "unverified"}

EXPECTED_TOOLS = [
    "ensembl_plants_lookup_locus",
    "resolve_locus_to_uniprot",
    "interpro_domains",
    "alphafold_structure",
    "experimental_structures",
    "tf_binding_motifs",
    "panther_family",
    "orthodb_orthologs",
    "gramene_homologs",
    "atted_coexpression",
    "string_interactions",
    "aragwas_associations",
    "locus_go_annotations",
    "kegg_pathways",
    "locus_literature",
    "gene_report",
]


def schema_violations(name: str, args: dict, schemas: dict[str, dict]) -> list[str]:
    """Reasons `args` would be rejected by the live schema for `name`.

    An empty list means the call is well-formed. Every message names the
    tool, so a failure points at the offending `CHAIN` row rather than at
    the chain as a whole.
    """
    if name not in schemas:
        return [f"{name}: no such tool on the live server"]
    props = set(schemas[name].get("properties", {}))
    required = set(schemas[name].get("required", []))
    reasons: list[str] = []
    if unknown := sorted(set(args) - props):
        reasons.append(f"{name}: argument(s) not in the live schema: {unknown}")
    if missing := sorted(required - set(args)):
        reasons.append(f"{name}: required argument(s) not supplied: {missing}")
    return reasons


def live_schemas() -> dict[str, dict]:
    async def go() -> dict[str, dict]:
        c = McpClient(SERVER_CMD)
        try:
            await c.start()
            return {t["name"]: t["inputSchema"] for t in await c.list_tools()}
        finally:
            await c.close()

    return asyncio.run(go())


def test_chain_lists_the_expected_tools_once_each():
    names = [name for name, _ in CHAIN]
    assert names == EXPECTED_TOOLS
    assert len(set(names)) == len(names), "a tool appears twice in CHAIN"


def test_chain_arguments_match_the_live_tool_schemas():
    schemas = live_schemas()
    # The listing really came back: without this, an empty `schemas` would
    # make every `name not in schemas` branch fire and the loop below would
    # report "no such tool" for all 16 instead of "the server said nothing".
    assert len(schemas) > len(CHAIN), f"live tools/list returned only {len(schemas)} tools"

    for name, build in CHAIN:
        args = build(LOCUS, ORGANISM)
        assert schema_violations(name, args, schemas) == [], (name, args)

    # Positive control for the discriminator, in this same test and against
    # these same live schemas: a misspelled argument must be caught both as
    # an unknown property and as a missing required key.
    misspelled = schema_violations(
        "interpro_domains", {"locuss": LOCUS, "organism": ORGANISM}, schemas
    )
    assert any("locuss" in reason for reason in misspelled), misspelled
    assert any("not supplied: ['locus']" in reason for reason in misspelled), misspelled
    assert schema_violations("no_such_tool_at_all", {}, schemas) == [
        "no_such_tool_at_all: no such tool on the live server"
    ]


def _run_once(tmp_path: Path) -> None:
    """Drive the real runner against the fake server, writing into tmp_path."""
    asyncio.run(run_dossier.main(server_cmd=FAKE_CHAIN, here=tmp_path))


def test_a_rerun_rewrites_auto_gaps_and_never_touches_the_hand_logged_file(tmp_path):
    # R22b. The runner's auto rows and the hand-logged rows used to share one
    # file, opened "a": a second run appended a duplicate of every auto row to
    # the file that becomes public issues. The runner now writes
    # gaps_auto.jsonl with "w" and does not open gaps.jsonl at all.
    #
    # This drives the REAL runner over the REAL stdio transport (fake server,
    # so no network), twice, and compares the files on disk afterwards.
    (tmp_path / "genes.tsv").write_text(
        "locus\tsymbol\torganism\tpanther_subfamily\thas_pb1_domain\n"
        "AT1G19850\tARF5\tarabidopsis_thaliana\tPTHR31384:SF10\ttrue\n"
    )
    hand_logged = (
        json.dumps(
            {
                "kind": "hand",
                "attempted": "x",
                "returned": "y",
                "expected": "z",
                "raw": "raw/AT1G19850__interpro_domains.json",
                "origin": "tool",
                "auto": False,
            }
        )
        + "\n"
    )
    (tmp_path / "gaps.jsonl").write_text(hand_logged)

    _run_once(tmp_path)

    first = (tmp_path / "gaps_auto.jsonl").read_text()
    first_rows = [json.loads(line) for line in first.splitlines()]
    # Positive control, in this same test: the run really happened and walked
    # the whole chain. Without it, an empty gaps_auto.jsonl would satisfy the
    # idempotence assertion below for the wrong reason.
    assert [
        json.loads(line)["tool"] for line in (tmp_path / "calls.jsonl").read_text().splitlines()
    ] == [name for name, _ in CHAIN], "the runner did not walk the whole chain"
    assert len(list(RAW.glob("*"))) > 0  # the committed captures are untouched
    assert len(first_rows) == 1, first_rows
    assert first_rows[0]["tool"] == CHAIN_FAILING_TOOL
    assert first_rows[0]["kind"] == "error"
    assert first_rows[0]["auto"] is True
    # ...and the fifteen succeeding calls produced no auto row, so the count
    # below is a real count and not "everything is logged".
    assert {r["tool"] for r in first_rows} == {CHAIN_FAILING_TOOL}

    _run_once(tmp_path)

    second_rows = [
        json.loads(line) for line in (tmp_path / "gaps_auto.jsonl").read_text().splitlines()
    ]
    assert len(second_rows) == len(first_rows), (
        f"a re-run changed the auto-gap row count: {len(first_rows)} -> {len(second_rows)}"
    )
    assert (tmp_path / "gaps.jsonl").read_text() == hand_logged, (
        "the runner modified gaps.jsonl, the hand-logged file"
    )


def test_every_hand_logged_gap_row_is_checkable():
    # A row that names a raw file that does not exist, or that omits `origin`,
    # cannot be checked by the person who reads the issue it becomes.
    rows = [json.loads(line) for line in GAPS.read_text().splitlines()]
    assert rows, "gaps.jsonl is empty"
    for i, row in enumerate(rows, start=1):
        assert row["auto"] is False, f"row {i}: gaps.jsonl holds hand-logged rows only"
        assert set(row) == HAND_LOGGED_KEYS, f"row {i}: keys {sorted(set(row) ^ HAND_LOGGED_KEYS)}"
        assert row["origin"] in VALID_ORIGINS, f"row {i}: origin {row['origin']!r}"
        for ref in row["raw"].split(", "):
            ref = ref.strip()
            if ref.startswith("raw/"):
                assert (ARF_DIR / ref).is_file(), f"row {i}: missing {ref}"

    # Positive control for the checker, on rows built to be wrong: it must
    # reject a missing raw file, a bad origin and an absent origin key.
    bad_ref = {**rows[0], "raw": "raw/AT9G99999__no_such_tool.json"}
    assert not (ARF_DIR / bad_ref["raw"]).is_file()
    assert {**rows[0], "origin": "probably-upstream"}["origin"] not in VALID_ORIGINS
    assert set({k: v for k, v in rows[0].items() if k != "origin"}) != HAND_LOGGED_KEYS


def test_auto_gap_rows_live_in_their_own_file():
    auto = [json.loads(line) for line in GAPS_AUTO.read_text().splitlines()]
    assert auto, "gaps_auto.jsonl is empty"
    assert all(row["auto"] is True for row in auto)
    hand = [json.loads(line) for line in GAPS.read_text().splitlines()]
    assert all(row["auto"] is False for row in hand)
