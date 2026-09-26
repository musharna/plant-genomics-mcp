"""The dossier chain's tool and argument names must match the LIVE server.

`examples/arf_family/chain.py` is a hand-written table of tool names and
argument builders. Nothing in the repo stops it drifting from the server's
real `tools/list` schemas: a renamed argument, a dropped tool or a wrong
required key would only surface as a run-time error part-way through a
248-call run, one gene at a time.

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
from examples.arf_family.run_dossier import BATCH_FORMS

from ._fake_mcp_server import CHAIN_FAILING_TOOL, CHAIN_UNSUPPORTED_ORGANISM

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

    # The runner's batch table must name real batch tools whose list
    # argument is the required one, and must cover EVERY chain tool that
    # has a batch form on the live server — a chain tool whose batch_ form
    # exists but is missing from BATCH_FORMS would silently run per locus.
    for chain_tool, (batch_tool, list_arg, takes_organism) in BATCH_FORMS.items():
        assert chain_tool in dict(CHAIN), chain_tool
        args = {list_arg: [LOCUS], "organism": ORGANISM} if takes_organism else {list_arg: [LOCUS]}
        assert schema_violations(batch_tool, args, schemas) == [], (batch_tool, args)
        # ...and the flag is not a free choice: it must match the schema.
        assert takes_organism == ("organism" in schemas[batch_tool]["properties"]), batch_tool
        assert schemas[batch_tool]["properties"][list_arg]["maxItems"] == run_dossier.BATCH_MAX
    live_batch_forms = {f"batch_{name}" for name, _ in CHAIN} & set(schemas)
    assert live_batch_forms == {b for b, _, _ in BATCH_FORMS.values()}
    assert schema_violations("batch_kegg_pathways", {"locus": [LOCUS]}, schemas) != []

    # #131: the chain tools with no dedicated batch form go through the one
    # generic form, and LOCUS_BATCHED must be exactly the ones the live
    # server accepts there — one it gains later would otherwise stay on one
    # round trip per locus. The shared `args` are checked against the chain
    # tool's own schema too, which is what the server does before fanning out.
    locus_batch = schemas[run_dossier.LOCUS_BATCH]["properties"]
    assert locus_batch["loci"]["maxItems"] == run_dossier.BATCH_MAX
    generic = set(locus_batch["tool"]["enum"])
    assert (set(dict(CHAIN)) - set(BATCH_FORMS)) & generic == run_dossier.LOCUS_BATCHED
    for chain_tool in run_dossier.LOCUS_BATCHED:
        args = run_dossier.locus_batch_args(chain_tool, [LOCUS], ORGANISM)
        assert schema_violations(run_dossier.LOCUS_BATCH, args, schemas) == [], args
        inner = {**args["args"], "locus": LOCUS}
        assert schema_violations(chain_tool, inner, schemas) == [], (chain_tool, inner)


def test_the_runner_reads_every_backend_the_live_upstream_release_offers():
    schemas = live_schemas()
    enum = schemas[run_dossier.RELEASE_TOOL]["properties"]["backend"]["enum"]
    assert list(run_dossier.RELEASE_BACKENDS) == enum
    assert schemas[run_dossier.RELEASE_TOOL]["required"] == ["backend"]


def test_the_release_bracket_flags_a_change_and_never_reads_a_failed_read_as_none() -> None:
    def reads(**over: dict) -> dict[str, dict]:
        base = {b: {"backend": b, "release": "1"} for b in run_dossier.RELEASE_BACKENDS}
        base["pdbe"] = {"backend": "pdbe", "release": None}
        return {**base, **over}

    same = run_dossier.release_bracket(reads(), reads())
    # Positive control: identical reads, including a backend that publishes
    # none (null at both ends), are neither changed nor unread.
    assert (same["changed"], same["unread"]) == ([], [])
    rolled = run_dossier.release_bracket(
        reads(ensembl_plants={"backend": "ensembl_plants", "release": "63"}),
        reads(ensembl_plants={"backend": "ensembl_plants", "release": "64"}),
    )
    assert (rolled["changed"], rolled["unread"]) == (["ensembl_plants"], [])
    failed = run_dossier.release_bracket(reads(), reads(kegg={"ok": False, "error": "timeout"}))
    assert (failed["changed"], failed["unread"]) == ([], ["kegg"])


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
    calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert [c["chain_tool"] for c in calls] == [name for name, _ in CHAIN], (
        "the runner did not walk the whole chain"
    )
    # Every chain tool with a batch form went through it, with the one
    # locus in `loci` — its dedicated batch_ form, else batch_locus_call
    # naming it; every other tool was called by its own name.
    for c in calls:
        if c["chain_tool"] in BATCH_FORMS:
            assert c["tool"] == BATCH_FORMS[c["chain_tool"]][0]
            assert c["loci"] == ["AT1G19850"] and c["locus"] is None
        elif c["chain_tool"] in run_dossier.LOCUS_BATCHED:
            assert c["tool"] == run_dossier.LOCUS_BATCH
            assert c["args"]["tool"] == c["chain_tool"]
            assert c["loci"] == ["AT1G19850"] and c["locus"] is None
        else:
            assert c["tool"] == c["chain_tool"]
            assert c["locus"] == "AT1G19850" and c["loci"] is None
    # The failing tool is reported as an error at BOTH levels — the call
    # row and the per-locus count — and the split raw file says so too.
    failing = [c for c in calls if c["chain_tool"] == CHAIN_FAILING_TOOL]
    assert [c["kind"] for c in failing] == ["error"]
    assert failing[0]["n_error"] == 1 and failing[0]["n_ok"] == 0
    assert json.loads((tmp_path / "raw" / f"AT1G19850__{CHAIN_FAILING_TOOL}.json").read_text()) == {
        "ok": False,
        "error": f"fake chain failure for {CHAIN_FAILING_TOOL}",
    }
    assert sum(c["n_ok"] for c in calls) == len(CHAIN) - 1
    assert len(list(RAW.glob("*"))) > 0  # the committed captures are untouched
    # The release bracket: one upstream_release read per backend before the
    # walk and one after, in its own file and never among the chain's calls.
    bracket = json.loads((tmp_path / "raw" / "_upstream_release.json").read_text())
    assert list(bracket["start"]) == list(bracket["end"]) == list(run_dossier.RELEASE_BACKENDS)
    for backend in run_dossier.RELEASE_BACKENDS:
        start, end = bracket["start"][backend], bracket["end"][backend]
        assert start["backend"] == end["backend"] == backend, (start, end)
        assert end["read"] > start["read"], (start, end)  # read again, not reused
    assert (bracket["changed"], bracket["unread"]) == ([], [])
    assert len(first_rows) == 1, first_rows
    assert first_rows[0]["tool"] == CHAIN_FAILING_TOOL
    assert first_rows[0]["kind"] == "error"
    assert first_rows[0]["loci"] == ["AT1G19850"]
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


def test_a_documented_organism_refusal_is_expected_not_a_gap(tmp_path):
    # Two rows: one in an organism every fake tool answers for, one in the
    # organism the fake refuses with the live server's
    # `[OrganismNotSupported]` tag. The refusals must be recorded as
    # `expected` on the call row and in the split raw file, and must NOT
    # produce an auto gap row; the ordinary failure on the other row
    # (CHAIN_FAILING_TOOL) still must — same run, same file.
    (tmp_path / "genes.tsv").write_text(
        "locus\tsymbol\torganism\tpanther_subfamily\thas_pb1_domain\n"
        "AT1G19850\tARF5\tarabidopsis_thaliana\tPTHR31384:SF10\ttrue\n"
        f"XX1\tX\t{CHAIN_UNSUPPORTED_ORGANISM}\tPTHR0\tfalse\n"
    )
    _run_once(tmp_path)
    calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    refused = [c for c in calls if c["organism"] == CHAIN_UNSUPPORTED_ORGANISM]
    assert len(refused) == len(CHAIN)
    # gramene_homologs takes no organism, so the fake cannot refuse it.
    assert {c["kind"] for c in refused if c["chain_tool"] != "gramene_homologs"} == {"expected"}
    assert sum(c["n_expected"] for c in refused) == len(CHAIN) - 1
    assert sum(c["n_error"] for c in refused) == 0
    raw = json.loads((tmp_path / "raw" / "XX1__interpro_domains.json").read_text())
    assert raw["ok"] is False and raw["expected"] is True
    assert "[OrganismNotSupported]" in raw["error"]
    gaps = [json.loads(line) for line in (tmp_path / "gaps_auto.jsonl").read_text().splitlines()]
    assert [(g["tool"], g["kind"], g["loci"]) for g in gaps] == [
        (CHAIN_FAILING_TOOL, "error", ["AT1G19850"])
    ]
    # Positive control for the classifier itself: an error without the
    # tag is not expected; an error with it is.
    assert not run_dossier.is_expected("[NotFoundError] KEGG: no pathway memberships for X")
    assert run_dossier.is_expected("[OrganismNotSupported] backend 'kegg' has no ID for 'x'")
    assert not run_dossier.is_expected(None)


def test_every_hand_logged_gap_row_is_checkable():
    # A row that names a raw file that does not exist, or that omits `origin`,
    # cannot be checked by the person who reads the issue it becomes.
    rows = [json.loads(line) for line in GAPS.read_text().splitlines()]
    assert rows, "gaps.jsonl is empty"
    for i, row in enumerate(rows, start=1):
        assert row["auto"] is False, f"row {i}: gaps.jsonl holds hand-logged rows only"
        # `closed` is the one optional key: {commit, returned} from the later
        # run that no longer reproduces the row (render_gaps.CLOSED_HEADING).
        keys = set(row) - {"closed"}
        assert keys == HAND_LOGGED_KEYS, f"row {i}: keys {sorted(keys ^ HAND_LOGGED_KEYS)}"
        if "closed" in row:
            assert set(row["closed"]) == {"commit", "returned"}, f"row {i}: closed keys"
            assert row["closed"]["commit"] and row["closed"]["returned"], f"row {i}: closed empty"
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


def test_error_class_groups_one_failure_across_loci_by_its_wording() -> None:
    """An auto-gap row is one kind of failure, whatever per-locus ids its
    message carries. #155's STRING error names the UniProt accession it
    queried, which differs per locus; blanking only the locus and bare
    numbers split 18 identical wheat misses into 18 rows. Positive control in
    the same test: failures worded differently stay apart."""
    miss = (
        "[NotFoundError] STRING has no protein for {l} in triticum_aestivum "
        "(queried as {a}): STRING /api/json/interaction_partners → HTTP 404"
    )
    a = run_dossier.error_class(
        miss.format(l="TraesCS2A02G309300", a="A0A3B6B034"), "TraesCS2A02G309300"
    )
    b = run_dossier.error_class(
        miss.format(l="TraesCS6D02G127600", a="A0A3B6QEX9"), "TraesCS6D02G127600"
    )
    assert a == b, (a, b)
    no_partners = run_dossier.error_class(
        "[NotFoundError] STRING: no interaction partners for TraesCS2D02G577800 "
        "(queried as A0A3B6DPF9)",
        "TraesCS2D02G577800",
    )
    assert no_partners != a
    # Release names differ by their letters, so two species' misses stay apart.
    ath = run_dossier.error_class("ATTED-II: X is not in the Ath-u.c4-0 release", "X")
    osa = run_dossier.error_class("ATTED-II: X is not in the Osa-u.c3-0 release", "X")
    assert ath != osa
