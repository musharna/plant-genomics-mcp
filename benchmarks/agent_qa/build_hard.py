"""Draw the harder 2026-10-18 question set and fetch its answers from upstream.

The 2026-10-16 questions were single lookups, and both tool setups got
nearly all of them (web search 151 of 156, the server 156 of 156), so they
cannot show a difference. These questions chain two or three lookups, map
identifiers between databases, or ask what an assembly holds at a position
or in an interval, which are the server's documented use cases (README tool
matrix: homology, cross-references, region query, UniProt resolution).

Six kinds, six questions each:

- ``ortholog_location``: an Arabidopsis gene's one-to-one ortholog in another
  plant, then where that ortholog starts in the named assembly.
- ``ortholog_protein_length``: the same ortholog step, then the length of
  the ortholog's canonical protein.
- ``symbol_ortholog``: an Arabidopsis gene symbol, to its locus, to its
  one-to-one ortholog in another plant.
- ``id_mapping``: a rice RAP-DB locus to its MSU locus (UniProt), and an
  Arabidopsis AGI to its NCBI Gene ID (Ensembl xrefs, confirmed at NCBI).
- ``position_to_gene``: the one protein-coding gene at a position of an
  assembly.
- ``interval_count``: how many protein-coding genes overlap an interval.

Every answer comes from Ensembl REST, UniProt REST or NCBI E-utilities over
httpx (the helpers in build_questions.py). This script never imports
plant_genomics_mcp, so a bug in the server cannot also be in the key. Every
question names the database whose answer the key holds, because the 10-16
location questions did not, and an NCBI reading of them was fair.

The key is written outside the repository (``--out``), so neither tool setup
can come across it during a sweep, with the date, the Ensembl and Ensembl
Genomes releases, and the UniProt release it was built from.

    uv run python benchmarks/agent_qa/build_hard.py --out ~/plant-genomics-mcp-artifacts/agent_qa/rerun-2026-10-18/questions.json
"""

from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import random
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import build_questions
from build_questions import (
    ORGANISMS,
    assembly,
    chromosome_names,
    client,
    ensembl,
    random_gene,
    strip,
    uniprot,
)

SEED = 20261018
ORG = {slug: (name, taxid, prefix) for name, slug, taxid, prefix in ORGANISMS}
ATH = "arabidopsis_thaliana"
KINDS = (
    "ortholog_location",
    "ortholog_protein_length",
    "symbol_ortholog",
    "id_mapping",
    "position_to_gene",
    "interval_count",
)
# Targets per kind, one question each. Tomato is left out of the region kinds:
# its chromosomes are GenBank accessions, which would make the question text
# itself a lookup. Wheat is left out of interval_count: 40 drawn intervals
# held no 8 to 45 genes (first build, 2026-10-18), so sorghum stands in.
TARGETS = {
    "ortholog_location": [
        "oryza_sativa", "zea_mays", "sorghum_bicolor",
        "solanum_lycopersicum_gca000188115v5cm", "glycine_max", "vitis_vinifera",
    ],
    "ortholog_protein_length": [
        "hordeum_vulgare", "brachypodium_distachyon", "populus_trichocarpa",
        "medicago_truncatula", "oryza_sativa", "zea_mays",
    ],
    "symbol_ortholog": [
        "oryza_sativa", "zea_mays", "solanum_lycopersicum_gca000188115v5cm",
        "sorghum_bicolor", "glycine_max", "populus_trichocarpa",
    ],
    "position_to_gene": [
        "zea_mays", "triticum_aestivum", "glycine_max",
        "sorghum_bicolor", "medicago_truncatula", "hordeum_vulgare",
    ],
    "interval_count": [
        ATH, "oryza_sativa", "populus_trichocarpa",
        "brachypodium_distachyon", "vitis_vinifera", "sorghum_bicolor",
    ],
}  # fmt: skip
INTERVAL_LENGTHS = (150_000, 250_000, 400_000)
INTERVAL_COUNT_RANGE = (8, 45)


def one_to_one(gene: str, target: str) -> str | None:
    """The Arabidopsis gene's ortholog in ``target`` when Ensembl Compara
    (plants) lists exactly one ortholog there and it is one-to-one."""
    data = ensembl(
        f"/homology/id/{ATH}/{gene}",
        compara="plants",
        type="orthologues",
        target_species=target,
        sequence="none",
        format="condensed",
    )["data"]
    rows = data[0]["homologies"] if data else []
    if len(rows) == 1 and rows[0]["type"] == "ortholog_one2one":
        return str(rows[0]["id"])
    return None


def canonical_protein_length(gene_id: str, slug: str) -> int | None:
    rec = ensembl(f"/lookup/id/{gene_id}", species=slug, expand=1)
    tx = [t for t in rec.get("Transcript") or [] if t.get("is_canonical")]
    if len(tx) != 1 or "Translation" not in tx[0]:
        return None
    return int(tx[0]["Translation"]["length"])


def id_example(rng: random.Random, slug: str, chroms: list[dict[str, Any]], avoid: str) -> str:
    """A real gene ID from the same genome, for the answer format: the shape
    of the ID system, never the answer itself."""
    while True:
        g = strip(random_gene(rng, slug, chroms)["id"], ORG[slug][2])
        if g != avoid:
            return g


def ath_genes(rng: random.Random, chroms: list[dict[str, Any]]):
    while True:
        yield random_gene(rng, ATH, chroms)["id"]


def location_truth(slug: str, gene: str, karyotype: list[str]) -> dict[str, Any]:
    rec = ensembl(f"/lookup/id/{gene}", species=slug)
    return {
        "gene": strip(gene, ORG[slug][2]),
        "chromosome": chromosome_names(rec["seq_region_name"], karyotype),
        "start": rec["start"],
        "end": rec["end"],
        "strand": rec["strand"],
    }


def add(questions: list[dict[str, Any]], q: dict[str, Any]) -> None:
    questions.append(q)
    print(f"{len(questions):2d} {q['category']}: {q['source']}", file=sys.stderr, flush=True)


def build(rng: random.Random) -> list[dict[str, Any]]:
    asm = {slug: assembly(slug) for _, slug, _, _ in ORGANISMS}
    karyo = {slug: [c["name"] for c in chroms] for slug, (_, chroms) in asm.items()}
    questions: list[dict[str, Any]] = []
    used: set[str] = set()
    draws = ath_genes(rng, asm[ATH][1])

    def ortholog_pair(target: str) -> tuple[str, str]:
        for _ in range(80):
            g = next(draws)
            if g in used:
                continue
            o = one_to_one(g, target)
            if o:
                used.add(g)
                return g, o
        raise RuntimeError(f"{target}: no one-to-one ortholog in 80 draws")

    for target in TARGETS["ortholog_location"]:
        name, _, prefix = ORG[target]
        g, o = ortholog_pair(target)
        add(
            questions,
            {
                "category": "ortholog_location",
                "question": (
                    f"According to Ensembl Plants (Ensembl Compara), the Arabidopsis thaliana "
                    f"gene {g} has one one-to-one ortholog in {name}. In the {name} reference "
                    f"assembly {asm[target][0]}, as annotated by Ensembl Plants, on which "
                    f"chromosome is that ortholog, and at what coordinate does it start?"
                ),
                "answer_format": "chromosome:start, e.g. 3:1234567",
                "truth": location_truth(target, o, karyo[target]),
                "source": f"Ensembl Compara (plants) /homology {g} -> {target}; /lookup/id/{o}",
            },
        )

    for target in TARGETS["ortholog_protein_length"]:
        name, _, prefix = ORG[target]
        for _ in range(20):
            g, o = ortholog_pair(target)
            length = canonical_protein_length(o, target)
            if length:
                break
        else:
            raise RuntimeError(f"{target}: no ortholog with a canonical translation")
        add(
            questions,
            {
                "category": "ortholog_protein_length",
                "question": (
                    f"According to Ensembl Plants (Ensembl Compara), the Arabidopsis thaliana "
                    f"gene {g} has one one-to-one ortholog in {name}. How many amino acids "
                    f"long is the protein encoded by that ortholog's canonical transcript in "
                    f"Ensembl Plants ({asm[target][0]})?"
                ),
                "answer_format": "an integer, e.g. 412",
                "truth": {"length": length, "gene": strip(o, prefix)},
                "source": f"Ensembl Compara (plants) /homology {g} -> {target}; "
                f"/lookup/id/{o}?expand=1, canonical translation",
            },
        )

    # Symbols: reviewed Arabidopsis UniProt entries whose primary gene name
    # names exactly one entry and one AGI locus.
    rows = uniprot("organism_id:3702 AND reviewed:true AND gene:*")
    rng.shuffle(rows)
    pool = iter(rows)
    for target in TARGETS["symbol_ortholog"]:
        name, _, prefix = ORG[target]
        for row in pool:
            sym = row["Gene Names (primary)"].strip()
            olns = [
                s.upper()
                for s in row["Gene Names (ordered locus)"].split()
                if s.upper().startswith("AT") and "G" in s.upper()
            ]
            if not sym or " " in sym or len(olns) != 1 or olns[0] in used:
                continue
            # Organelle genes (ATCG..., ATMG...) carry the same name in every
            # plant: rice's psbH is Ensembl gene "gene-psbH", so the question
            # would give its answer away (first build, 2026-10-18).
            if olns[0].startswith(("ATCG", "ATMG")):
                continue
            same = uniprot(f'organism_id:3702 AND reviewed:true AND gene_exact:"{sym}"')
            if len(same) != 1:
                continue
            o = one_to_one(olns[0], target)
            if o is None:
                continue
            used.add(olns[0])
            break
        else:
            raise RuntimeError(f"{target}: ran out of symbols")
        answer = strip(o, prefix)
        add(
            questions,
            {
                "category": "symbol_ortholog",
                "question": (
                    f"What is the one-to-one ortholog in {name}, according to Ensembl Plants "
                    f"(Ensembl Compara), of the Arabidopsis thaliana gene named {sym} "
                    f"(UniProt gene name)?"
                ),
                "answer_format": (
                    f"the Ensembl Plants gene ID, e.g. "
                    f"{id_example(rng, target, asm[target][1], answer)}"
                ),
                "truth": {"gene": answer, "arabidopsis_locus": olns[0]},
                "source": f"UniProt {row['Entry']} ({sym} -> {olns[0]}); "
                f"Ensembl Compara (plants) /homology -> {target}",
            },
        )

    # Rice RAP-DB -> MSU, from reviewed UniProt entries that name exactly one
    # of each, and the only reviewed entry naming that RAP locus.
    rice = uniprot("organism_id:39947 AND reviewed:true AND gene:*")
    rng.shuffle(rice)
    got = 0
    for row in rice:
        names = row["Gene Names (ordered locus)"].split()
        rap = [s for s in names if s.startswith("Os") and "g" in s]
        msu = [s for s in names if s.startswith("LOC_Os")]
        if len(rap) != 1 or len(msu) != 1:
            continue
        hits = uniprot(f'organism_id:39947 AND reviewed:true AND gene:"{rap[0]}"')
        if [h["Entry"] for h in hits if rap[0] in h["Gene Names (ordered locus)"].split()] != [
            row["Entry"]
        ]:
            continue
        add(
            questions,
            {
                "category": "id_mapping",
                "question": (
                    f"According to UniProt, which MSU Rice Genome Annotation locus ID "
                    f"corresponds to the rice (Oryza sativa) RAP-DB gene {rap[0]}?"
                ),
                "answer_format": "an MSU locus ID, e.g. LOC_Os01g01010",
                "truth": {"gene": msu[0], "id_kind": "msu"},
                "source": f"UniProt {row['Entry']} ordered locus names {names}",
            },
        )
        got += 1
        if got == 3:
            break
    else:
        raise RuntimeError("rice: fewer than 3 RAP/MSU pairs")

    # AGI -> NCBI Gene ID: Ensembl's one EntrezGene xref, confirmed at NCBI
    # (the record's name or aliases carry the AGI, and it is Arabidopsis).
    got = 0
    while got < 3:
        g = next(draws)
        if g in used:
            continue
        xrefs = [x for x in ensembl(f"/xrefs/id/{g}", species=ATH) if x["dbname"] == "EntrezGene"]
        if len({x["primary_id"] for x in xrefs}) != 1:
            continue
        gid = xrefs[0]["primary_id"]
        rec = build_questions.get(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
            db="gene",
            id=gid,
            retmode="json",
        )["result"][gid]
        aliases = {a.strip().upper() for a in (rec.get("otheraliases") or "").split(",")}
        if rec.get("organism", {}).get("taxid") != 3702 or g not in aliases | {
            rec.get("name", "").upper()
        }:
            continue
        used.add(g)
        add(
            questions,
            {
                "category": "id_mapping",
                "question": (
                    f"What is the NCBI Gene ID (Entrez Gene) of the Arabidopsis thaliana gene {g}?"
                ),
                "answer_format": "an integer NCBI Gene ID, e.g. 839580",
                "truth": {"gene": gid, "id_kind": "ncbi_gene"},
                "source": f"Ensembl /xrefs/id/{g} EntrezGene {gid}; NCBI esummary gene {gid}",
            },
        )
        got += 1

    for target in TARGETS["position_to_gene"]:
        name, _, prefix = ORG[target]
        for _ in range(40):
            g = random_gene(rng, target, asm[target][1])
            pos = (g["start"] + g["end"]) // 2
            at = ensembl(
                f"/overlap/region/{target}/{g['seq_region_name']}:{pos}-{pos}", feature="gene"
            )
            if [x["id"] for x in at] == [g["id"]] and g["id"] not in used:
                used.add(g["id"])
                break
        else:
            raise RuntimeError(f"{target}: no position with exactly one gene")
        answer = strip(g["id"], prefix)
        add(
            questions,
            {
                "category": "position_to_gene",
                "question": (
                    f"In the {name} reference assembly {asm[target][0]}, as annotated by "
                    f"Ensembl Plants, which protein-coding gene overlaps position "
                    f"{pos:,} of chromosome {g['seq_region_name']}?"
                ),
                "answer_format": (
                    f"the Ensembl Plants gene ID, e.g. "
                    f"{id_example(rng, target, asm[target][1], answer)}"
                ),
                "truth": {"gene": answer},
                "source": f"Ensembl /overlap/region/{target}/{g['seq_region_name']}:{pos}-{pos}"
                " (the only gene of any biotype there)",
            },
        )

    counts: set[int] = set()
    for target in TARGETS["interval_count"]:
        name = ORG[target][0]
        chroms = {c["name"]: c["length"] for c in asm[target][1]}
        for _ in range(40):
            g = random_gene(rng, target, asm[target][1])
            span = rng.choice(INTERVAL_LENGTHS)
            mid = (g["start"] + g["end"]) // 2
            a = max(1, mid - rng.randrange(span))
            b = min(chroms[g["seq_region_name"]], a + span - 1)
            genes = ensembl(
                f"/overlap/region/{target}/{g['seq_region_name']}:{a}-{b}",
                feature="gene",
                biotype="protein_coding",
            )
            n = len({x["id"] for x in genes})
            lo, hi = INTERVAL_COUNT_RANGE
            # Distinct counts, so the shifted-answer control can score 0.
            if lo <= n <= hi and n not in counts:
                counts.add(n)
                break
        else:
            raise RuntimeError(f"{target}: no interval with {INTERVAL_COUNT_RANGE} genes")
        add(
            questions,
            {
                "category": "interval_count",
                "question": (
                    f"According to Ensembl Plants, how many protein-coding genes (biotype "
                    f"protein_coding) overlap the interval {g['seq_region_name']}:{a:,}-{b:,} "
                    f"of the {name} reference assembly {asm[target][0]}? Count a gene if any "
                    f"part of it lies inside the interval."
                ),
                "answer_format": "an integer, e.g. 12",
                "truth": {
                    "count": n,
                    "genes": sorted(strip(x["id"], ORG[target][2]) for x in genes),
                },
                "source": f"Ensembl /overlap/region/{target}/{g['seq_region_name']}:{a}-{b}"
                "?feature=gene;biotype=protein_coding",
            },
        )
    return questions


def cache_gets(path: Path) -> None:
    """Replay GETs from ``path`` and record new ones there, so a build that
    fails late can be re-run in minutes: same seed, same URLs, same answers.
    The cache is a speed-up for one build day, never a source; delete it to
    re-fetch everything."""
    cache: dict[str, Any] = json.loads(path.read_text()) if path.exists() else {}
    live = build_questions.get

    def get(url: str, **params: Any) -> Any:
        k = url + "?" + json.dumps(params, sort_keys=True)
        if k not in cache:
            cache[k] = live(url, **params)
        return cache[k]

    build_questions.get = get
    atexit.register(lambda: path.write_text(json.dumps(cache)))


def uniprot_release() -> tuple[str, str]:
    r = client.get(
        "https://rest.uniprot.org/uniprotkb/search",
        params={"query": "accession:Q0WV96", "fields": "accession", "format": "tsv"},
    )
    r.raise_for_status()
    return r.headers["x-uniprot-release"], r.headers["x-uniprot-release-date"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="where the key goes; keep it outside the repo")
    ap.add_argument("--cache", default="", help="replay and record Ensembl/NCBI GETs here")
    a = ap.parse_args()
    if a.cache:
        cache_gets(Path(a.cache).expanduser())
    # Armed here, not at import, so a test importing this module is not killed.
    signal.signal(signal.SIGALRM, lambda *_: sys.exit("aborting: walltime guard"))
    signal.alarm(3600)
    rng = random.Random(SEED)
    questions = build(rng)
    for i, q in enumerate(questions, 1):
        q["id"] = f"h{i:02d}"
    up, up_date = uniprot_release()
    out = {
        "built": datetime.now(UTC).isoformat(timespec="seconds"),
        "seed": SEED,
        "ensembl_release": ensembl("/info/software")["release"],
        "ensembl_genomes_release": ensembl("/info/eg_version")["version"],
        "uniprot_release": up,
        "uniprot_release_date": up_date,
        "questions": questions,
    }
    path = Path(a.out).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(out, indent=1) + "\n"
    path.write_text(text)
    by_kind = {k: sum(q["category"] == k for q in questions) for k in KINDS}
    print(len(questions), by_kind, "sha256", hashlib.sha256(text.encode()).hexdigest())


if __name__ == "__main__":
    main()
