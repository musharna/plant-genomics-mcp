"""Add NCBI's coordinates for each location question's gene (``truth_ncbi``).

The key scores locations against Ensembl's gene model, and the questions name
the assembly but not the annotation. NCBI annotates the same assemblies with
its own gene models, whose ends can differ, so an answer read faithfully from
NCBI is defensible. This adds a second key for the secondary NCBI verdict
(``score.score_ncbi``); the primary key is unchanged.

The gene is found mechanically, with no hand-made aliases: NCBI Gene is
searched for the ID as the question writes it, and with a version suffix
removed. A record whose name or alias is that ID wins. Otherwise a search
with exactly one hit is taken, since that is NCBI's own cross-reference.
Anything else is recorded as no NCBI record, with the reason.
Like build_questions.py, it never imports plant_genomics_mcp.

    uv run python benchmarks/agent_qa/ncbi_locations.py   # rewrites questions.json
"""

from __future__ import annotations

import json
import re
import signal
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

import httpx

HERE = Path(__file__).resolve().parent
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
TAXA = {
    "Arabidopsis thaliana": 3702,
    "rice (Oryza sativa)": 39947,
    "maize (Zea mays)": 4577,
    "bread wheat (Triticum aestivum)": 4565,
    "tomato (Solanum lycopersicum)": 4081,
    "soybean (Glycine max)": 3847,
    "sorghum (Sorghum bicolor)": 4558,
    "barley (Hordeum vulgare)": 112509,
    "grapevine (Vitis vinifera)": 29760,
    "poplar (Populus trichocarpa)": 3694,
    "Medicago truncatula": 3880,
    "Brachypodium distachyon": 15368,
}
_Q = re.compile(r"^In the (.+?) reference assembly .+?, on which chromosome is the gene (\S+),")

client = httpx.Client(timeout=60)


def eutils(tool: str, **params: Any) -> Any:
    time.sleep(0.4)  # NCBI's limit without a key is 3 requests a second
    r = client.get(EUTILS + tool, params={**params, "retmode": "json"})
    r.raise_for_status()
    return r.json()


def _norm(name: str) -> str:
    return re.sub(r"\.(v\d+\.)?\d+$", "", name.strip().upper())


def forms(gene: str) -> list[str]:
    """The ID as written, and without a version suffix (Solyc10g038020.2,
    Potri.005G155000.v4.1)."""
    bare = re.sub(r"\.(v\d+\.)?\d+$", "", gene)
    return [gene] if bare == gene else [gene, bare]


def pick(gene: str, records: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
    """The NCBI record for ``gene`` among a search's hits, and how it was chosen."""
    named = [
        r
        for r in records
        if _norm(gene)
        in {_norm(r.get("name", ""))}
        | {_norm(a) for a in (r.get("otheraliases") or "").split(",") if a.strip()}
    ]
    if len(named) == 1:
        return named[0], "name or alias"
    if len(named) > 1:
        return None, f"{len(named)} records name it"
    if len(records) == 1:
        return records[0], "only search hit"
    return None, f"{len(records)} search hits, none named {gene}"


def ncbi_location(species: str, gene: str) -> dict[str, Any]:
    reason = "no search hit"
    for form in forms(gene):
        ids = eutils(
            "esearch.fcgi",
            db="gene",
            term=f"{form}[All Fields] AND txid{TAXA[species]}[Organism:exp]",
            retmax=50,
        )["esearchresult"]["idlist"]
        if not ids:
            continue
        summ = eutils("esummary.fcgi", db="gene", id=",".join(ids))["result"]
        rec, reason = pick(form, [summ[i] | {"uid": i} for i in ids])
        if rec is None:
            continue
        loc = (rec.get("genomicinfo") or [None])[0]
        if loc is None:
            reason = f"NCBI gene {rec['uid']} has no genomic location"
            continue
        # esummary gives 0-based ends, the 5' end first (start > stop on the minus strand).
        a, b = loc["chrstart"] + 1, loc["chrstop"] + 1
        return {
            "chromosome": [rec["chromosome"], loc["chraccver"]],
            "start": min(a, b),
            "end": max(a, b),
            "strand": 1 if a <= b else -1,
            "ncbi_gene_id": rec["uid"],
            "ncbi_name": rec["name"],
            "searched": form,
            "match": reason,
        }
    return {"none": reason}


def main() -> None:
    signal.signal(signal.SIGALRM, lambda *_: sys.exit("aborting: walltime guard"))
    signal.alarm(600)
    path = HERE / "questions.json"
    key = json.loads(path.read_text())
    for q in key["questions"]:
        if q["category"] != "location":
            continue
        m = _Q.match(q["question"])
        if m is None:
            raise SystemExit(f"{q['id']}: cannot read species and gene from {q['question']!r}")
        found = ncbi_location(m.group(1), m.group(2))
        if "none" in found:
            q["truth_ncbi"] = None
            q["truth_ncbi_note"] = found["none"]
        else:
            q["truth_ncbi"] = found
            q.pop("truth_ncbi_note", None)
        print(q["id"], m.group(2), q["truth_ncbi"] or q["truth_ncbi_note"])
    key["ncbi_locations_fetched"] = date.today().isoformat()
    path.write_text(json.dumps(key, indent=1) + "\n")


if __name__ == "__main__":
    main()
