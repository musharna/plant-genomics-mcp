"""Controls for the 2026-10-24 field families, scored by the real scorer.

- **Oracle**: the key's own answers (baselines.answer) must all score right.
- **Shortcuts**, each named in the family's design, must fail where it says:
  - P1 ``first_ortholog``: a single lookup, the first rice or maize ortholog
    Compara lists, as the whole roster. Must fail every positive question
    (each holds at least 2 members by construction).
  - P1 ``orthologs_only``: every Compara ortholog, no paralogs. Reported, not
    required to fail: where a tree holds only orthologs it is the right answer.
  - P3 ``first_hit``: the first v5 gene and first UniProt accession of each
    id, and for an unmappable id the v5 gene of the nearest id that maps (the
    nearest hit). Must fail every question holding an unmappable id, a split
    gene or a gene with more than one accession.
- **Reference solution** (``--live``): the answers re-derived over plain HTTP
  by routes other than the builder's, never the server's: P1 from the full,
  unpruned gene tree filtered by taxon here; P3 from Ensembl's ID history by
  GET, one id at a time, and UniProt's search by EnsemblPlants cross-reference
  instead of its ID mapping service. Must score right on every question.

    uv run python benchmarks/agent_qa/field_2026-10-24/field_controls.py --key KEY [--live]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from baselines import answer, id_batch_answer, reply  # noqa: E402
from build_questions import ensembl  # noqa: E402
from score import score  # noqa: E402

DEFAULT_KEY = Path.home() / "plant-genomics-mcp-artifacts/agent_qa/field-2026-10-24/questions.json"
RICE_MAIZE_TAXA = {39947, 4577}


def first_ortholog(q: dict[str, Any]) -> str:
    orth = q["controls"]["orthologs"]
    return orth[0] if orth else "NONE"


def orthologs_only(q: dict[str, Any]) -> str:
    return ", ".join(q["controls"]["orthologs"]) or "NONE"


def first_hit(q: dict[str, Any]) -> str:
    near = q["controls"]["nearest_mapped"]
    items = [
        {"v4": it["v4"], "v5": it["v5"][:1], "uniprot": it["uniprot"][:1]}
        if it["v5"]
        else {"v4": it["v4"], "v5": [near[it["v4"]]], "uniprot": []}
        for it in q["truth"]["items"]
    ]
    return id_batch_answer(items)


SHORTCUTS = {
    "family_roster": {"first_ortholog": first_ortholog, "orthologs_only": orthologs_only},
    "id_batch": {"first_hit": first_hit},
}
MUST_FAIL = {"first_ortholog", "first_hit"}


def shortcut_must_fail(q: dict[str, Any]) -> bool:
    """Where the family says its shortcut fails: every positive P1 question;
    every P3 question with an unmappable, split or multi-accession item."""
    if q["category"] == "family_roster":
        return bool(q["truth"]["members"])
    return any(not it["v5"] or len(it["v5"]) > 1 or len(it["uniprot"]) > 1
               for it in q["truth"]["items"])  # fmt: skip


def _leaves(node: dict[str, Any], out: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for c in node.get("children") or []:
        _leaves(c, out)
    if "children" not in node:
        out.append(node)
    return out


def reference_roster(gene: str) -> str:
    try:
        t = ensembl(f"/genetree/member/id/arabidopsis_thaliana/{gene}", compara="plants",
                    sequence="none")  # fmt: skip
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 400 and "No GeneTree found" in exc.response.text:
            return "NONE"
        raise
    members = sorted(
        leaf["id"]["accession"]
        for leaf in _leaves(t["tree"], [])
        if leaf["taxonomy"]["id"] in RICE_MAIZE_TAXA
    )
    return ", ".join(members) or "NONE"


def _uniprot_by_xref(genes: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {g: [] for g in genes}
    if not genes:
        return out
    query = " OR ".join(f"xref:ensemblplants-{g}*" for g in genes)
    r = httpx.get(
        "https://rest.uniprot.org/uniprotkb/search",
        params={
            "query": f"({query})",
            "fields": "accession,xref_ensemblplants",
            "format": "tsv",
            "size": 500,
        },  # fmt: skip
        timeout=60,
    )
    r.raise_for_status()
    for ln in r.text.rstrip("\n").split("\n")[1:]:
        acc, xrefs = ln.split("\t")
        for g in genes:
            if g in xrefs:
                out[g].append(acc)
    return out


def reference_batch(q: dict[str, Any]) -> str:
    items = []
    for it in q["truth"]["items"]:
        try:
            rec = ensembl(f"/archive/id/{it['v4']}", species="zea_mays")
            v5 = sorted(p["stable_id"] for p in rec.get("possible_replacement") or [])
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in (400, 404) or "No object" not in exc.response.text:
                raise
            v5 = []
        items.append({"v4": it["v4"], "v5": v5})
    genes = sorted({g for it in items for g in it["v5"]})
    acc = _uniprot_by_xref(genes)
    for it in items:
        it["uniprot"] = sorted({a for g in it["v5"] for a in acc[g]})
    return id_batch_answer(items)


def reference(q: dict[str, Any]) -> str:
    if q["category"] == "family_roster":
        return reference_roster(q["truth"]["gene"])
    return reference_batch(q)


def controls(questions: list[dict[str, Any]], live: bool = False) -> list[dict[str, Any]]:
    """One row per (question, control): its verdict and set metrics."""
    rows = []
    for q in questions:
        arms = {"oracle": answer(q), **{k: f(q) for k, f in SHORTCUTS[q["category"]].items()}}
        if live:
            arms["reference"] = reference(q)
        for arm, value in arms.items():
            s = score(q, reply(value))
            rows.append({"qid": q["id"], "category": q["category"], "arm": arm,
                         "must_fail": arm in MUST_FAIL and shortcut_must_fail(q),
                         **{k: v for k, v in s.items() if k != "value"}})  # fmt: skip
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", default=str(DEFAULT_KEY))
    ap.add_argument("--live", action="store_true", help="also run the reference solution")
    a = ap.parse_args()
    key = json.loads(Path(a.key).read_text())
    rows = controls(key["questions"], live=a.live)
    for cat in dict.fromkeys(r["category"] for r in rows):
        for arm in dict.fromkeys(r["arm"] for r in rows if r["category"] == cat):
            mine = [r for r in rows if r["category"] == cat and r["arm"] == arm]
            extra = ""
            if cat == "family_roster":
                extra = f" mean F1 {sum(r['f1'] for r in mine) / len(mine):.3f}"
            else:
                tot = sum(r["items_total"] for r in mine)
                extra = (f" items right {sum(r['items_right'] for r in mine)}/{tot},"
                         f" v5 right {sum(r['items_v5_right'] for r in mine)}/{tot}")  # fmt: skip
            must = [r for r in mine if r["must_fail"]]
            fail = f"; must fail {sum(r['verdict'] != 'right' for r in must)}/{len(must)}"
            print(f"{cat:13} {arm:15} {dict(Counter(r['verdict'] for r in mine))}{extra}"
                  f"{fail if arm in MUST_FAIL else ''}")  # fmt: skip
    print(json.dumps(key["draws"]["P3"]))
    p1 = key["draws"]["P1"]
    print("P1 draws", len(p1), dict(Counter(d["discard_reason"] or "kept" for d in p1)),
          "negatives among draws", sum(bool(d["negative"]) for d in p1))  # fmt: skip


if __name__ == "__main__":
    main()
