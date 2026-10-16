"""The agent benchmark's scorer: one reply, one verdict, decided by its ANSWER line.

Each test pairs the wrong or declined reply with the right one for the same
question, so a scorer that called everything right (or wrong) fails here.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "benchmarks" / "agent_qa"))

from build_questions import chromosome_from_title, chromosome_names  # noqa: E402
from run import is_valid, parse_transcript  # noqa: E402
from score import extract, score  # noqa: E402

LIVE = os.environ.get("PLANT_GENOMICS_MCP_LIVE")
LOCATION = {
    "category": "location",
    "truth": {
        "chromosome": ["CM001064.4", "1"],
        "start": 1_000_000,
        "end": 1_004_000,
        "strand": -1,
    },
}
LENGTH = {"category": "protein_length", "truth": {"length": 429}}
SYMBOL = {"category": "symbol_to_locus", "truth": {"loci": ["AT1G19850", "F14P1.1"]}}
ACCESSION = {"category": "uniprot_accession", "truth": {"accession": "Q0WV96"}}
ORTHOLOG = {"category": "ortholog", "truth": {"gene": "Solyc01g005610.3"}}


def _v(q: dict, reply: str) -> str:
    return score(q, reply)["verdict"]


def test_the_last_answer_line_decides_and_markdown_around_it_is_ignored() -> None:
    reply = "ANSWER: AT1G01010\nChecking that again...\n**ANSWER:** `AT1G19850`"
    assert extract(reply) == "AT1G19850"
    assert _v(SYMBOL, reply) == "right"
    assert _v(SYMBOL, "**ANSWER:** AT1G01010") == "wrong"


def test_unknown_is_declined_and_a_reply_without_the_line_is_neither() -> None:
    assert _v(SYMBOL, "Not sure.\nANSWER: UNKNOWN") == "declined"
    assert _v(SYMBOL, "It is AT1G19850.") == "no_answer"
    assert _v(SYMBOL, "It is AT1G19850.\nANSWER: AT1G19850") == "right"


def test_an_answer_wrapped_in_answer_tags_is_read_without_them() -> None:
    """Seen in the pilot: "ANSWER: Q75QN6</answer>" was scored wrong."""
    assert _v(ACCESSION, "<answer>Looked it up.\n\nANSWER: Q0WV96</answer>") == "right"
    assert _v(ACCESSION, "ANSWER: <answer>Q0WV97</answer>") == "wrong"


def test_location_accepts_the_chromosome_number_or_name_and_either_end() -> None:
    assert _v(LOCATION, "ANSWER: 1:1000000") == "right"
    assert _v(LOCATION, "ANSWER: Chr01:1,004,000") == "right"  # 5' end of a minus-strand gene
    assert _v(LOCATION, "ANSWER: CM001064.4:1000001") == "right"  # off by one: 0- vs 1-based
    assert _v(LOCATION, "ANSWER: chromosome 1, 1000000") == "right"
    assert _v(LOCATION, "ANSWER: 2:1000000") == "wrong"
    assert _v(LOCATION, "ANSWER: 1:1000500") == "wrong"
    assert _v(LOCATION, "ANSWER: somewhere on 1") == "wrong"


def test_a_bare_chromosome_number_is_a_name_only_where_no_other_chromosome_shares_it() -> None:
    """Seen in the sweep: "5:424548576" for barley 5H had the right start and
    was scored wrong. In wheat, "6" could be 6A, 6B or 6D, so it stays wrong."""
    barley = [f"{n}H" for n in range(1, 8)]
    wheat = [f"{n}{g}" for n in range(1, 8) for g in "ABD"]
    assert chromosome_names("5H", barley) == ["5H", "5"]
    assert chromosome_names("6B", wheat) == ["6B"]
    assert chromosome_names("5", [str(n) for n in range(1, 6)]) == ["5"]
    q5h = {"category": "location", "truth": {"chromosome": chromosome_names("5H", barley),
           "start": 424548576, "end": 424548989, "strand": -1}}  # fmt: skip
    q6b = {"category": "location", "truth": {"chromosome": chromosome_names("6B", wheat),
           "start": 1000, "end": 2000, "strand": 1}}  # fmt: skip
    assert _v(q5h, "ANSWER: 5:424548576") == "right"
    assert _v(q5h, "ANSWER: 5H:424548576") == "right"
    assert _v(q6b, "ANSWER: 6B:1000") == "right"
    assert _v(q6b, "ANSWER: 6:1000") == "wrong"


def test_protein_length_is_exact() -> None:
    assert _v(LENGTH, "ANSWER: 429") == "right"
    assert _v(LENGTH, "ANSWER: 429 aa") == "right"
    assert _v(LENGTH, "ANSWER: 430") == "wrong"


def test_ids_compare_without_case_version_or_transcript_suffix() -> None:
    assert _v(SYMBOL, "ANSWER: at1g19850.1") == "right"
    assert _v(ACCESSION, "ANSWER: Q0WV96-1") == "right"
    assert _v(ACCESSION, "ANSWER: Q0WV97") == "wrong"
    assert _v(ORTHOLOG, "ANSWER: Solyc01g005610") == "right"
    assert _v(ORTHOLOG, "ANSWER: gene-Solyc01g005610.3") == "right"
    assert _v(ORTHOLOG, "ANSWER: Solyc01g005620.2") == "wrong"


def test_a_wrong_well_formed_id_is_flagged_and_a_wrong_phrase_is_not() -> None:
    assert score(SYMBOL, "ANSWER: AT1G01010")["wrong_id"] is True
    assert score(ACCESSION, "ANSWER: P12345")["wrong_id"] is True
    # Review #237: the isoform suffix the right/wrong check strips hid the shape.
    assert score(ACCESSION, "ANSWER: P12345-2")["wrong_id"] is True
    assert score(ACCESSION, "ANSWER: Q0WV96-2")["wrong_id"] is False  # right, so not a wrong id
    assert score(ORTHOLOG, "ANSWER: Solyc02g000010.1")["wrong_id"] is True
    assert score(SYMBOL, "ANSWER: probably an ARF gene")["wrong_id"] is False
    assert score(SYMBOL, "ANSWER: AT1G19850")["wrong_id"] is False  # right, so not a wrong id
    assert score(LENGTH, "ANSWER: 430")["wrong_id"] is False  # not an id category


def test_a_genbank_chromosome_gets_its_number_from_ncbis_title() -> None:
    """Titles copied verbatim from NCBI esummary (2026-10-16) for the three
    accession-named chromosomes in questions.json. Review #237: this branch
    had no test, and LOCATION above hardcodes what it was believed to return."""
    assert (
        chromosome_from_title(
            "Solanum lycopersicum cultivar Heinz 1706 chromosome 1, whole genome shotgun sequence"
        )
        == "1"
    )
    assert (
        chromosome_from_title(
            "Medicago truncatula cultivar Jemalong A17 chromosome 8, whole genome shotgun sequence"
        )
        == "8"
    )
    assert chromosome_from_title("Vitis vinifera cultivar Pinot Noir 40024 chromosome 19") == "19"
    with pytest.raises(ValueError, match="no chromosome number in NCBI title"):
        chromosome_from_title("Solanum lycopersicum cultivar Heinz 1706 unplaced genomic scaffold")


@pytest.mark.skipif(LIVE != "1", reason="set PLANT_GENOMICS_MCP_LIVE=1 to run")
def test_a_genbank_chromosome_resolves_through_the_real_ncbi_esummary() -> None:
    """The real call behind LOCATION's ["CM001064.4", "1"] (tomato chromosome
    1), and a karyotype-named chromosome beside it that makes no call."""
    assert chromosome_names("CM001064.4", []) == ["CM001064.4", "1"]
    assert chromosome_names("CP126666.1", []) == ["CP126666.1", "19"]
    assert chromosome_names("5H", [f"{n}H" for n in range(1, 8)]) == ["5H", "5"]


def _transcript(status: str, calls: list[str]) -> str:
    """Stream-json lines shaped as claude -p 2.1.302 writes them (init event,
    assistant tool_use blocks, result), trimmed to the fields read."""
    import json

    events: list[dict[str, object]] = [
        {
            "type": "system",
            "subtype": "init",
            "model": "claude-opus-5-5",
            "tools": ["mcp__plant-genomics__get_sequence"],
            "mcp_servers": [{"name": "plant-genomics", "status": status}],
        }
    ]
    events += [
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": c}]}}
        for c in calls
    ]
    events.append({"type": "result", "result": "ANSWER: AT3G23430", "total_cost_usd": 0.02})
    return "\n".join(json.dumps(e) for e in events)


def test_a_server_run_is_valid_only_if_the_connected_server_was_called() -> None:
    called = parse_transcript(
        _transcript("connected", ["mcp__plant-genomics__resolve_locus_to_uniprot"])
    )
    assert called["tool_calls"] == ["mcp__plant-genomics__resolve_locus_to_uniprot"]
    assert is_valid("mcp", called["mcp_servers"], called["tool_calls"])
    # q30 runs 1 and 3: connected, answered from memory, no call.
    memory = parse_transcript(_transcript("connected", []))
    assert memory["reply"] == "ANSWER: AT3G23430"
    assert not is_valid("mcp", memory["mcp_servers"], memory["tool_calls"])
    failed = parse_transcript(_transcript("failed", ["mcp__plant-genomics__get_sequence"]))
    assert not is_valid("mcp", failed["mcp_servers"], failed["tool_calls"])
    # A setup with no server to call has nothing to fail.
    assert is_valid("bare", [], []) and is_valid("web", [], ["WebFetch"])
