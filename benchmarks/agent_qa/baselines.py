"""Controls for the agent benchmark that need no model, scored by the real scorer.

- **Oracle (positive control)**: the key's own answers, written as replies.
  They must all score right, and the same answers moved to the next
  question of the same kind must all score wrong. A scorer that cannot
  tell the two apart cannot be trusted with a model's replies. run.py checks
  this before a sweep, and tests/test_agent_qa_score.py checks it in CI.
- **Baselines (negative controls)**: answers needing no lookup, to show
  what a guess earns.
  - ``constant``: one answer per kind of question, the key's most common
    answer of that kind. For a location it is the most common chromosome
    with the median start; for a protein length, the median length.
  - ``id_chromosome``: location questions only. It reads the chromosome
    from the gene ID where the ID encodes it (AT5G..., Os12g..., TraesCS6B...)
    and takes the median start of the key's genes.

    uv run python benchmarks/agent_qa/baselines.py   # writes baselines.jsonl
"""

from __future__ import annotations

import json
import re
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

from score import score, score_ncbi

HERE = Path(__file__).resolve().parent

# Chromosome encoded in a gene ID, per naming scheme in the key's genomes.
_ID_CHROMOSOME = [
    re.compile(r"\bAT([1-5])G\d{5}", re.I),  # Arabidopsis AGI
    re.compile(r"\bOs(\d{2})g\d{7}", re.I),  # rice RAP-DB
    re.compile(r"\bTraesCS(\d[ABD])\d{2}G", re.I),  # wheat IWGSC
    re.compile(r"\bSolyc(\d{2})g", re.I),  # tomato ITAG
    re.compile(r"\bGLYMA_(\d{2})G", re.I),  # soybean
    re.compile(r"\bSORBI_3(\d{3})G", re.I),  # sorghum
    re.compile(r"\bHORVU\.MOREX\.r3\.(\d)H", re.I),  # barley
    re.compile(r"\bVitis(\d{2})g", re.I),  # grapevine
    re.compile(r"\bPotri\.(\d{3})G", re.I),  # poplar
    re.compile(r"\bBRADI_(\d)g", re.I),  # Brachypodium
]


def answer(q: dict[str, Any]) -> str:
    """The key's answer for ``q`` in the form the prompt asks for."""
    t = q["truth"]
    match q["category"]:
        case "location":
            return f"{t['chromosome'][0]}:{t['start']}"
        case "protein_length":
            return str(t["length"])
        case "symbol_to_locus":
            return str(t["loci"][0])
        case "uniprot_accession":
            return str(t["accession"])
        case "ortholog":
            return str(t["gene"])
    raise ValueError(q["category"])


def reply(value: str) -> str:
    return f"From the key.\n\nANSWER: {value}"


def oracle(questions: list[dict[str, Any]]) -> list[str]:
    return [score(q, reply(answer(q)))["verdict"] for q in questions]


def shifted(questions: list[dict[str, Any]]) -> list[str]:
    """Each question answered with the next same-kind question's key answer."""
    verdicts = []
    for q in questions:
        same = [p for p in questions if p["category"] == q["category"]]
        other = same[(same.index(q) + 1) % len(same)]
        verdicts.append(score(q, reply(answer(other)))["verdict"])
    return verdicts


def id_chromosome(q: dict[str, Any]) -> str | None:
    gene = re.search(r"the gene (\S+),", q["question"])
    for pattern in _ID_CHROMOSOME:
        m = pattern.search(gene.group(1)) if gene else None
        if m:
            return m.group(1).lstrip("0") or "0"
    return None


def baseline_rows(questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for q in questions:
        by_kind.setdefault(q["category"], []).append(q)
    median_start = {
        "location": int(statistics.median(q["truth"]["start"] for q in by_kind["location"]))
    }
    constant: dict[str, str] = {}
    for kind, qs in by_kind.items():
        if kind == "location":
            chrom = Counter(q["truth"]["chromosome"][0] for q in qs).most_common(1)[0][0]
            constant[kind] = f"{chrom}:{median_start['location']}"
        elif kind == "protein_length":
            constant[kind] = str(int(statistics.median(q["truth"]["length"] for q in qs)))
        else:
            counts = Counter(answer(q) for q in qs)
            top = max(counts.values())
            constant[kind] = min(a for a, n in counts.items() if n == top)
    rows = []
    for q in questions:
        values = {"constant": constant[q["category"]]}
        if q["category"] == "location":
            chrom = id_chromosome(q)
            values["id_chromosome"] = (
                f"{chrom}:{median_start['location']}" if chrom is not None else "UNKNOWN"
            )
        for arm, value in values.items():
            r = reply(value)
            rows.append(
                {
                    "arm": arm,
                    "qid": q["id"],
                    "category": q["category"],
                    **score(q, r),
                    "verdict_ncbi": score_ncbi(q, r),
                }
            )
    return rows


def main() -> None:
    questions = json.loads((HERE / "questions.json").read_text())["questions"]
    rows = baseline_rows(questions)
    (HERE / "baselines.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    for arm in dict.fromkeys(r["arm"] for r in rows):
        mine = [r for r in rows if r["arm"] == arm]
        print(arm, Counter(r["verdict"] for r in mine), f"of {len(mine)}")
    print("oracle", Counter(oracle(questions)), "shifted", Counter(shifted(questions)))


if __name__ == "__main__":
    main()
