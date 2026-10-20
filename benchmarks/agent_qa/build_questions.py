"""Draw the agent benchmark's questions and fetch their answers from upstream.

The answers come straight from Ensembl REST and UniProt REST over httpx. This
script never imports plant_genomics_mcp, so a bug in the server's code cannot
also be in the answer key. Genes are drawn at random (seeded) from each
genome, not picked: famous genes would favour a model's memory, and genes
chosen by hand would favour whatever the chooser expected.

    uv run python benchmarks/agent_qa/build_questions.py   # writes questions.json
"""

from __future__ import annotations

import json
import random
import re
import signal
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

import httpx

SEED = 20261016
HERE = Path(__file__).resolve().parent
ENSEMBL = "https://rest.ensembl.org"
UNIPROT = "https://rest.uniprot.org/uniprotkb/search"

# (name in the question, Ensembl species slug, UniProt taxid, wire-only id prefix)
ORGANISMS = [
    ("Arabidopsis thaliana", "arabidopsis_thaliana", 3702, ""),
    ("rice (Oryza sativa)", "oryza_sativa", 39947, ""),
    ("maize (Zea mays)", "zea_mays", 4577, ""),
    ("bread wheat (Triticum aestivum)", "triticum_aestivum", 4565, ""),
    ("tomato (Solanum lycopersicum)", "solanum_lycopersicum_gca000188115v5cm", 4081, "gene-"),
    ("soybean (Glycine max)", "glycine_max", 3847, ""),
    ("sorghum (Sorghum bicolor)", "sorghum_bicolor", 4558, ""),
    ("barley (Hordeum vulgare)", "hordeum_vulgare", 112509, ""),
    ("grapevine (Vitis vinifera)", "vitis_vinifera", 29760, ""),
    ("poplar (Populus trichocarpa)", "populus_trichocarpa", 3694, ""),
    ("Medicago truncatula", "medicago_truncatula", 3880, ""),
    ("Brachypodium distachyon", "brachypodium_distachyon", 15368, ""),
]

client = httpx.Client(timeout=60, headers={"Accept": "application/json"})


def get(url: str, **params: Any) -> Any:
    """GET with a bounded retry on 429/5xx and on a timed-out read (Ensembl
    stalled one for 60 s while the 2026-10-18 key was built); anything else
    raises, and so does a fifth timeout."""
    for attempt in range(5):
        try:
            r = client.get(url, params=params)
        except httpx.TimeoutException:
            if attempt == 4:
                raise
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(float(r.headers.get("Retry-After", 2 * (attempt + 1))))
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()
    raise RuntimeError(f"{url}: still {r.status_code} after 5 tries")


def ensembl(path: str, **params: Any) -> Any:
    return get(f"{ENSEMBL}{path}", **params)


def assembly(slug: str) -> tuple[str, list[dict[str, Any]]]:
    info = ensembl(f"/info/assembly/{slug}")
    chroms = [
        r
        for r in info["top_level_region"]
        # Tomato SL4.0 names its chromosomes by GenBank accession, under
        # coord_system "primary_assembly" (CM001064.4 is chromosome 1).
        if r["coord_system"] in ("chromosome", "primary_assembly")
        and r["name"] in info.get("karyotype", [])
    ]
    if not chroms:
        raise RuntimeError(f"{slug}: no karyotype chromosomes")
    return info["assembly_name"], chroms


def random_gene(rng: random.Random, slug: str, chroms: list[dict[str, Any]]) -> dict[str, Any]:
    """A protein-coding gene from a uniformly drawn 200 kb window of the genome."""
    total = sum(c["length"] for c in chroms)
    for _ in range(40):
        x = rng.randrange(total)
        for c in chroms:
            if x < c["length"]:
                break
            x -= c["length"]
        start = max(1, x - 100_000)
        genes = ensembl(
            f"/overlap/region/{slug}/{c['name']}:{start}-{start + 200_000}",
            feature="gene",
            biotype="protein_coding",
        )
        if genes:
            return rng.choice(sorted(genes, key=lambda g: g["id"]))
    raise RuntimeError(f"{slug}: no protein-coding gene in 40 windows")


def chromosome_names(seq_region: str, karyotype: list[str]) -> list[str]:
    """The region's name, plus its chromosome number when the name is a
    GenBank accession (NCBI's own title for it, not inferred from order), or
    when the name is a number plus a genome letter that no other chromosome
    shares the number of: barley's "5H" is also "5", wheat's "6B" is not "6"
    (6A and 6D exist)."""
    if "." not in seq_region:
        number = seq_region.rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        same_number = [k for k in karyotype if k.rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ") == number]
        if number.isdigit() and number != seq_region and same_number == [seq_region]:
            return [seq_region, number]
        return [seq_region]
    r = client.get(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
        params={"db": "nuccore", "id": seq_region, "retmode": "json"},
    )
    r.raise_for_status()
    title = next(v for k, v in r.json()["result"].items() if k != "uids")["title"]
    return [seq_region, chromosome_from_title(title)]


def chromosome_from_title(title: str) -> str:
    """The chromosome number in an NCBI nuccore title, e.g. "Solanum
    lycopersicum cultivar Heinz 1706 chromosome 1, whole genome shotgun
    sequence" -> "1". A title without one (a scaffold) is an error, not a guess."""
    m = re.search(r"\bchromosome ([0-9A-Za-z]+)\b", title)
    if m is None:
        raise ValueError(f"no chromosome number in NCBI title {title!r}")
    return m.group(1)


def canonical_protein_length(gene_id: str, slug: str) -> int:
    rec = ensembl(f"/lookup/id/{gene_id}", species=slug, expand=1)
    tx = [t for t in rec["Transcript"] if t.get("is_canonical")]
    if len(tx) != 1 or "Translation" not in tx[0]:
        raise LookupError(f"{gene_id}: no single canonical coding transcript")
    return int(tx[0]["Translation"]["length"])


def uniprot(query: str) -> list[dict[str, str]]:
    r = client.get(
        UNIPROT,
        params={
            "query": query,
            "fields": "accession,gene_primary,gene_oln,length,xref_ensemblplants",
            "format": "tsv",
            "size": 500,
        },
    )
    r.raise_for_status()
    # rstrip("\n") only: strip() would also eat the last row's trailing empty
    # fields (tabs), and zip(strict=True) refused that short row.
    lines = r.text.rstrip("\n").split("\n")
    head = lines[0].split("\t")
    return [dict(zip(head, ln.split("\t"), strict=True)) for ln in lines[1:]]


def strip(gene: str, prefix: str) -> str:
    return gene[len(prefix) :] if prefix and gene.startswith(prefix) else gene


def main() -> None:
    # Armed here, not at import, so a test importing this module is not killed.
    signal.signal(signal.SIGALRM, lambda *_: sys.exit("aborting: walltime guard"))
    signal.alarm(1800)
    rng = random.Random(SEED)
    questions: list[dict[str, Any]] = []
    asm: dict[str, tuple[str, list[dict[str, Any]]]] = {}
    for _, slug, _, _ in ORGANISMS:
        asm[slug] = assembly(slug)

    # Location and protein length: one gene each per organism.
    for name, slug, _, prefix in ORGANISMS:
        asm_name, chroms = asm[slug]
        g = random_gene(rng, slug, chroms)
        locus = strip(g["id"], prefix)
        questions.append(
            {
                "category": "location",
                "question": (
                    f"In the {name} reference assembly {asm_name}, on which chromosome is the "
                    f"gene {locus}, and at what coordinate does it start?"
                ),
                "answer_format": "chromosome:start, e.g. 3:1234567",
                # end and strand: on the minus strand a gene's 5' end is its
                # highest coordinate, so "where it starts" has two fair readings.
                "truth": {
                    "chromosome": chromosome_names(
                        g["seq_region_name"], [c["name"] for c in chroms]
                    ),
                    "start": g["start"],
                    "end": g["end"],
                    "strand": g["strand"],
                },
                "source": f"Ensembl /overlap/region/{slug} ({asm_name})",
            }
        )
        for _ in range(20):
            g = random_gene(rng, slug, chroms)
            try:
                length = canonical_protein_length(g["id"], slug)
                break
            except LookupError:
                continue
        else:
            raise RuntimeError(f"{slug}: no gene with a canonical translation")
        questions.append(
            {
                "category": "protein_length",
                "question": (
                    f"How many amino acids long is the protein encoded by the canonical "
                    f"transcript of the {name} gene {strip(g['id'], prefix)} ({asm_name})?"
                ),
                "answer_format": "an integer, e.g. 412",
                "truth": {"length": length},
                "source": f"Ensembl /lookup/id/{g['id']}?expand=1, canonical translation",
            }
        )

    # Gene symbol -> locus, and locus -> reviewed UniProt accession (Arabidopsis
    # and rice: UniProt names their ordered locus ids, so the mapping is direct).
    for taxid, n_symbol, n_acc, locus_ok in (
        (3702, 6, 5, lambda s: s.upper().startswith("AT") and "G" in s.upper()),
        (39947, 4, 5, lambda s: s.startswith("Os") and "g" in s),
    ):
        rows = uniprot(f"organism_id:{taxid} AND reviewed:true AND gene:*")
        rng.shuffle(rows)
        species = "Arabidopsis thaliana" if taxid == 3702 else "rice (Oryza sativa)"
        got_sym = got_acc = 0
        for row in rows:
            olns = [s for s in row["Gene Names (ordered locus)"].split() if locus_ok(s)]
            sym = row["Gene Names (primary)"].strip()
            if len(olns) != 1:
                continue
            locus = olns[0].upper() if taxid == 3702 else olns[0]
            if got_sym < n_symbol and sym and " " not in sym:
                same = uniprot(f'organism_id:{taxid} AND reviewed:true AND gene_exact:"{sym}"')
                if len(same) == 1:
                    questions.append(
                        {
                            "category": "symbol_to_locus",
                            "question": f"What is the locus ID of the {species} gene {sym}?",
                            # Rice has two id systems (RAP-DB Os01g0100100, MSU
                            # LOC_Os01g01010); the question names the one it wants.
                            "answer_format": (
                                "the AGI locus ID, e.g. AT1G01010"
                                if taxid == 3702
                                else "the RAP-DB locus ID, e.g. Os01g0100100"
                            ),
                            "truth": {
                                "loci": [
                                    s.upper() if taxid == 3702 else s
                                    for s in row["Gene Names (ordered locus)"].split()
                                ]
                            },
                            "source": f"UniProt {row['Entry']} (reviewed; only entry named {sym})",
                        }
                    )
                    got_sym += 1
                    continue
            if got_acc < n_acc:
                hits = uniprot(f'organism_id:{taxid} AND reviewed:true AND gene:"{locus}"')
                accs = sorted(
                    {
                        h["Entry"]
                        for h in hits
                        if locus in h["Gene Names (ordered locus)"].upper().split()
                    }
                    | {h["Entry"] for h in hits if locus in h["Gene Names (ordered locus)"].split()}
                )
                if accs == [row["Entry"]]:
                    questions.append(
                        {
                            "category": "uniprot_accession",
                            "question": (
                                f"What is the reviewed (Swiss-Prot) UniProtKB accession of the "
                                f"protein encoded by the {species} gene {locus}?"
                            ),
                            "answer_format": "a UniProt accession, e.g. Q0WV96",
                            "truth": {"accession": row["Entry"]},
                            "source": f"UniProt reviewed entries naming {locus}: {accs}",
                        }
                    )
                    got_acc += 1
            if got_sym == n_symbol and got_acc == n_acc:
                break
        else:
            raise RuntimeError(f"taxid {taxid}: ran out of entries ({got_sym}, {got_acc})")

    # Orthologs: Arabidopsis genes with exactly one one-to-one ortholog in the target.
    ath_name, ath_slug, _, _ = ORGANISMS[0]
    for target_name, target_slug, prefix, n, id_form in (
        ("rice (Oryza sativa)", "oryza_sativa", "", 3, "the RAP-DB gene ID, e.g. Os01g0100100"),
        (
            "maize (Zea mays)",
            "zea_mays",
            "",
            3,
            "the B73 RefGen_v5 gene ID, e.g. Zm00001eb000010",
        ),
        (
            "tomato (Solanum lycopersicum)",
            "solanum_lycopersicum_gca000188115v5cm",
            "gene-",
            2,
            "the ITAG gene ID, e.g. Solyc01g005610.3",
        ),
    ):
        got = 0
        for _ in range(60):
            g = random_gene(rng, ath_slug, asm[ath_slug][1])
            data = ensembl(
                f"/homology/id/{ath_slug}/{g['id']}",
                compara="plants",
                type="orthologues",
                target_species=target_slug,
                sequence="none",
                format="condensed",
            )["data"]
            rows = data[0]["homologies"] if data else []
            one2one = [h["id"] for h in rows if h["type"] == "ortholog_one2one"]
            if len(one2one) == 1 and len(rows) == 1:
                questions.append(
                    {
                        "category": "ortholog",
                        "question": (
                            f"What is the one-to-one ortholog of the Arabidopsis thaliana gene "
                            f"{g['id']} in {target_name}?"
                        ),
                        "answer_format": id_form,
                        "truth": {"gene": strip(one2one[0], prefix)},
                        "source": f"Ensembl Compara (plants) /homology, {target_slug}",
                    }
                )
                got += 1
                if got == n:
                    break
        else:
            raise RuntimeError(f"{target_slug}: only {got} one-to-one orthologs in 60 draws")

    for i, q in enumerate(questions, 1):
        q["id"] = f"q{i:02d}"
    out = {
        "built": date.today().isoformat(),
        "seed": SEED,
        "ensembl_release": ensembl("/info/software")["release"],
        "ensembl_genomes_release": ensembl("/info/eg_version")["version"],
        "questions": questions,
    }
    (HERE / "questions.json").write_text(json.dumps(out, indent=1) + "\n")
    counts: dict[str, int] = {}
    for q in questions:
        counts[q["category"]] = counts.get(q["category"], 0) + 1
    print(len(questions), counts)


if __name__ == "__main__":
    main()
