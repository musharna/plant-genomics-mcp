"""The 2026-10-24 field families: P1 (gene-tree roster, set F1) and P3 (batch
maize v4 -> v5 + UniProt mapping, accuracy/precision/coverage).

Every negative assertion sits beside the positive one for the same input
shape. The key-backed tests read the frozen key outside the repository and
skip, saying so, where it is absent (CI); the reference-solution test also
needs PLANT_GENOMICS_MCP_LIVE=1, as it drives Ensembl and UniProt.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

AGENT_QA = Path(__file__).resolve().parent.parent / "benchmarks" / "agent_qa"
FIELD = AGENT_QA / "field_2026-10-24"
sys.path.insert(0, str(AGENT_QA))
sys.path.insert(0, str(FIELD))

import run  # noqa: E402
from baselines import answer, oracle, reply, shifted  # noqa: E402
from field_controls import DEFAULT_KEY, controls, first_hit, first_ortholog  # noqa: E402
from score import score  # noqa: E402

KEY = Path(os.environ.get("FIELD_KEY", str(DEFAULT_KEY)))
LIVE = os.environ.get("PLANT_GENOMICS_MCP_LIVE") == "1"
needs_key = pytest.mark.skipif(not KEY.exists(), reason=f"frozen field key not at {KEY}")

ROSTER = {
    "id": "p1_a", "category": "family_roster",
    "truth": {"gene": "AT5G60450", "members": ["Os02g0164900", "Os06g0702600", "Zm00001eb207450"]},
    "controls": {"orthologs": ["Os06g0702600", "Zm00001eb207450"]},
}  # fmt: skip
NONE_Q = {
    "id": "p1_b", "category": "family_roster",
    "truth": {"gene": "AT2G03800", "members": []}, "controls": {"orthologs": []},
}  # fmt: skip
NONE_Q2 = {**NONE_Q, "id": "p1_c", "truth": {"gene": "AT1G36510", "members": []}}
ITEMS = [
    {"v4": "Zm00001d013272", "v5": ["Zm00001eb214910"], "uniprot": ["A0A1D6GH76"]},
    {"v4": "Zm00001d040499", "v5": ["Zm00001eb129260", "Zm00001eb129270"], "uniprot": []},
    {"v4": "Zm00001d004509", "v5": ["Zm00001eb089360"], "uniprot": ["A0A1D6EFT8", "A0A804MIX8"]},
    {"v4": "Zm00001d004067", "v5": [], "uniprot": []},
]
BATCH = {"id": "p3_a", "category": "id_batch", "truth": {"items": ITEMS},
         "controls": {"nearest_mapped": {"Zm00001d004067": "Zm00001eb090000"}}}  # fmt: skip


def _s(q: dict, value: str) -> dict:
    return score(q, f"Looked it up.\nANSWER: {value}")


def test_a_roster_is_right_only_at_f1_one_and_none_answers_a_true_negative() -> None:
    full = _s(ROSTER, "Os06g0702600, Os02g0164900, Zm00001eb207450_P003")
    assert full["verdict"] == "right" and full["f1"] == 1.0
    one = _s(ROSTER, first_ortholog(ROSTER))
    assert one["verdict"] == "wrong" and one["f1"] == 0.5 and one["precision"] == 1.0
    extra = _s(ROSTER, "Os02g0164900, Os06g0702600, Zm00001eb207450, Zm00001eb000010")
    assert extra["verdict"] == "wrong" and extra["recall"] == 1.0 and extra["f1"] < 1
    assert _s(NONE_Q, "NONE")["verdict"] == "right" and _s(NONE_Q, "none")["f1"] == 1.0
    assert _s(ROSTER, "NONE")["verdict"] == "wrong" and _s(ROSTER, "NONE")["f1"] == 0.0
    assert _s(NONE_Q, "Os06g0702600")["verdict"] == "wrong"
    assert _s(ROSTER, "UNKNOWN")["verdict"] == "declined"
    assert _s(ROSTER, "see the list above")["verdict"] == "wrong"


def test_a_batch_scores_items_lab_bench_style_and_the_question_needs_all() -> None:
    gold = _s(BATCH, answer(BATCH))
    assert gold["verdict"] == "right"
    assert (gold["items_right"], gold["accuracy"], gold["precision"], gold["coverage"]) == (
        4, 1.0, 1.0, 1.0)  # fmt: skip
    # One id left UNKNOWN: coverage drops, precision does not; not right.
    part = answer(BATCH).replace("Zm00001d004067 -> UNMAPPABLE", "Zm00001d004067 -> UNKNOWN")
    assert part != answer(BATCH)
    p = _s(BATCH, part)
    assert p["verdict"] == "wrong" and p["coverage"] == 0.75 and p["precision"] == 1.0
    # The nearest hit for an unmappable id, and one accession of two: wrong
    # items, both attempted.
    sc = _s(BATCH, first_hit(BATCH))
    assert sc["verdict"] == "wrong" and sc["items_attempted"] == 4
    assert sc["items_right"] == 1 and sc["items_v5_right"] == 2
    # A split gene answered with one of its two v5 genes is wrong.
    half = answer(BATCH).replace("Zm00001eb129260, Zm00001eb129270", "Zm00001eb129260")
    assert half != answer(BATCH) and _s(BATCH, half)["items_v5_right"] == 3
    # Two different answers for one id is a wrong item, whichever comes last.
    for twice in (answer(BATCH) + "; Zm00001d013272 -> Zm00001eb000020 [none]",
                  "Zm00001d013272 -> Zm00001eb000020 [none]; " + answer(BATCH)):  # fmt: skip
        assert _s(BATCH, twice)["items_right"] == 3
    assert _s(BATCH, "UNKNOWN")["verdict"] == "declined"


def test_oracle_and_shifted_hold_when_two_true_negatives_share_none() -> None:
    qs = [ROSTER, NONE_Q, NONE_Q2]
    assert oracle(qs) == ["right"] * 3
    # Each NONE question is shifted onto the roster's answer, never the other NONE.
    assert shifted(qs) == ["wrong"] * 3
    roster2 = copy.deepcopy(ROSTER)
    roster2["id"], roster2["truth"]["members"] = "p1_d", ["Os01g0100100", "Os01g0100200"]
    assert shifted([ROSTER, roster2]) == ["wrong", "wrong"]


def test_dry_run_calls_no_model_and_the_web_arm_keeps_web_search(monkeypatch) -> None:
    def no_subprocess(*a, **k):
        raise AssertionError(f"dry run started a process: {a}")

    monkeypatch.setattr(subprocess, "run", no_subprocess)
    q = {**ROSTER, "question": "List them.", "answer_format": "ids"}
    text = run.dry_run([q], ["web", "mcp"])
    assert "== arm web: --tools 'WebSearch,WebFetch' --allowedTools 'WebSearch,WebFetch'" in text
    assert "== arm mcp: --tools '' --allowedTools 'mcp__plant-genomics'" in text
    assert "List them." in text and "ANSWER: UNKNOWN" in text
    assert run.INTENDED_TOOLS["web"](["WebSearch", "WebFetch"])
    assert not run.INTENDED_TOOLS["web"](["WebSearch"])


def test_the_leak_scan_sees_the_field_key_and_builder() -> None:
    assert run.leak_hits(["cat ~/plant-genomics-mcp-artifacts/agent_qa/field-2026-10-24/q"])
    assert run.leak_hits(["benchmarks/agent_qa/field_2026-10-24/build_field.py"])
    assert not run.leak_hits(
        ["https://download.maizegdb.org/Pan-genes/B73_gene_xref/B73v4_to_B73v5.tsv"]
    )


@needs_key
def test_on_the_frozen_key_the_oracle_passes_and_each_shortcut_fails_where_it_must() -> None:
    key = json.loads(KEY.read_text())
    qs = key["questions"]
    assert [q["category"] for q in qs].count("family_roster") == 10
    assert [q["category"] for q in qs].count("id_batch") == 10
    rows = controls(qs)
    assert all(r["verdict"] == "right" for r in rows if r["arm"] == "oracle")
    must = [r for r in rows if r["must_fail"]]
    assert {r["arm"] for r in must} == {"first_ortholog", "first_hit"}
    assert all(r["verdict"] == "wrong" for r in must)
    # The shortcut is right where the family says it may be: a NONE question.
    negatives = [
        q["id"] for q in qs if q["category"] == "family_roster" and not q["truth"]["members"]
    ]
    fo = {r["qid"]: r["verdict"] for r in rows if r["arm"] == "first_ortholog"}
    assert all(fo[i] == "right" for i in negatives)
    assert oracle(qs).count("right") == 20 and shifted(qs).count("right") == 0
    # Retention is recorded for both families.
    assert (
        key["draws"]["P1"] and key["draws"]["P3"]["screened"] >= key["draws"]["P3"]["kept"] == 250
    )


@needs_key
def test_a_broken_key_fails_the_oracle() -> None:
    """Seen to fail: one dropped member or one flipped item turns the
    oracle wrong on exactly that question, so the oracle can see a bad key."""
    qs = json.loads(KEY.read_text())["questions"]
    p1 = next(q for q in qs if q["category"] == "family_roster" and q["truth"]["members"])
    p3 = next(q for q in qs if q["category"] == "id_batch")
    gold1, gold3 = reply(answer(p1)), reply(answer(p3))
    bad1, bad3 = copy.deepcopy(p1), copy.deepcopy(p3)
    bad1["truth"]["members"] = bad1["truth"]["members"][1:]
    bad3["truth"]["items"][0]["v5"] = [] if bad3["truth"]["items"][0]["v5"] else ["Zm00001eb000010"]
    assert score(p1, gold1)["verdict"] == score(p3, gold3)["verdict"] == "right"
    assert score(bad1, gold1)["verdict"] == score(bad3, gold3)["verdict"] == "wrong"


@needs_key
@pytest.mark.skipif(not LIVE, reason="PLANT_GENOMICS_MCP_LIVE=1 drives Ensembl and UniProt")
def test_a_reference_solution_by_other_routes_passes_every_question() -> None:
    """Real execution: P1 from the unpruned tree, P3 from Ensembl's ID
    history by GET and UniProt's cross-reference search, scored on the key."""
    rows = controls(json.loads(KEY.read_text())["questions"], live=True)
    ref = [r for r in rows if r["arm"] == "reference"]
    assert len(ref) == 20
    assert [r["qid"] for r in ref if r["verdict"] != "right"] == []


def test_headroom_needs_three_not_right_and_counts_an_invalid_run_as_not_right() -> None:
    from summarize import summarize

    def row(i: int, verdict: str, valid: bool = True) -> dict:
        return {"arm": "web", "category": "family_roster", "qid": f"p1_{i:02d}", "rep": 1,
                "valid": valid, "verdict": verdict, "f1": 1.0 if verdict == "right" else 0.5}  # fmt: skip

    two = [row(i, "right") for i in range(8)] + [row(8, "wrong"), row(9, "declined")]
    (s,) = summarize(two)
    assert (s["right"], s["headroom"]) == (8, False)
    three = two[:7] + [row(7, "right", valid=False)] + two[8:]
    (s,) = summarize(three)
    assert (s["right"], s["headroom"], s["invalid"]) == (7, True, ["p1_07-1"])
