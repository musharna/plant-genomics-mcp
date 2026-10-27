"""Draw the 2026-10-24 field families P1 and P3 and fetch their answers upstream.

Design: ``fieldbench_genomics_data_jobs.md`` (mcp-bench-api state), rows P1, P3
and P5. Each field scores a set, not a single fact, and scores abstention.

- **P1, family roster** (10 questions, ``family_roster``): every rice
  (Oryza sativa Japonica) and maize (Zea mays B73) member of the Ensembl
  Compara (plants) gene tree that holds a random Arabidopsis gene. Key: the
  tree from Ensembl REST ``/genetree/member/id``, pruned to the three
  species, on the release REST serves (Ensembl 116 / Ensembl Genomes 63, the
  release plants.ensembl.org now archives as "release 63"). Scored by set F1.
- **P3, batch v4 -> v5 + UniProt mapping** (10 questions of 25 ids,
  ``id_batch``): MaizeGDB's B73 v4 -> v5 gene-model translation table, kept
  only where the Ensembl Plants ID history (``/archive/id``) gives the same
  answer, and the v5 genes' UniProtKB accessions from UniProt's ID mapping
  service. "UNMAPPABLE" is an answer.
- **P5, true negatives**: drawn at their natural rate, never chosen. P1
  draws a gene uniformly from the genome and keeps a gene with no tree, or a
  tree with no rice or maize gene, as a NONE question. P3 draws v4 ids
  uniformly from MaizeGDB's table, where 13% have no v5 model.

Retention: P1 discards a draw whose tree holds exactly one rice or maize
gene (a single lookup is then the whole answer, so the shortcut control
could not fail) or more than 40; P3 discards an id on which the two
ID-history sources disagree, or whose v5 gene Ensembl no longer serves.
Every draw and its reason go into the key's ``draws``.

This script never imports plant_genomics_mcp. The key goes outside the
repository (``--out``) with every release it was built from.

    uv run python benchmarks/agent_qa/field_2026-10-24/build_field.py \\
        --out ~/plant-genomics-mcp-artifacts/agent_qa/field-2026-10-24/questions.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import signal
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import build_questions  # noqa: E402
from build_questions import assembly, client, ensembl, random_gene  # noqa: E402

SEED = 20261024
N_QUESTIONS = 10
P3_ITEMS = 25
P1_MAX_MEMBERS = 40
ATH, RICE, MAIZE = "arabidopsis_thaliana", "oryza_sativa", "zea_mays"
TAXID = {RICE: 39947, MAIZE: 4577, ATH: 3702}
MAIZEGDB_V4_V5 = "https://download.maizegdb.org/Pan-genes/B73_gene_xref/B73v4_to_B73v5.tsv"
UNIPROT_IDMAPPING = "https://rest.uniprot.org/idmapping"
P1_FORMAT = (
    "a comma-separated list of Ensembl Plants gene IDs, e.g. Os01g0100100, Zm00001eb000010, or NONE"
)
P3_FORMAT = (
    "one line with one item per ID, separated by semicolons, each as "
    "`<v4 ID> -> <v5 gene ID(s)> [<UniProtKB accession(s)>, or none]`, "
    "`<v4 ID> -> UNMAPPABLE` or `<v4 ID> -> UNKNOWN`, e.g. "
    "Zm00001dNNNNNN -> Zm00001ebNNNNNN [A0A1D6NNNNN]; Zm00001dMMMMMM -> UNMAPPABLE"
)


def post(path: str, body: dict[str, Any], **params: Any) -> Any:
    """Ensembl REST POST with the same bounded retry as build_questions.get."""
    for attempt in range(5):
        try:
            r = client.post(f"{build_questions.ENSEMBL}{path}", params=params, json=body)
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
    raise RuntimeError(f"{path}: still {r.status_code} after 5 tries")


def _leaves(node: dict[str, Any], out: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if "children" in node:
        for c in node["children"]:
            _leaves(c, out)
    else:
        out.append(node)
    return out


def gene_tree(gene: str) -> dict[str, Any]:
    """The Compara tree holding ``gene``, pruned to Arabidopsis, rice and
    maize: its id and rice/maize members, or why there are none. The query
    gene must be a leaf of its own tree (a positive control on the route)."""
    try:
        t = ensembl(
            f"/genetree/member/id/{ATH}/{gene}",
            compara="plants",
            sequence="none",
            prune_species=[ATH, RICE, MAIZE],
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 400 and "No GeneTree found" in exc.response.text:
            return {"gene_tree": None, "members": [], "negative": "no_gene_tree"}
        raise
    leaves = _leaves(t["tree"], [])
    by_tax: dict[int, list[str]] = {}
    for leaf in leaves:
        by_tax.setdefault(leaf["taxonomy"]["id"], []).append(leaf["id"]["accession"])
    if gene not in by_tax.get(TAXID[ATH], []):
        raise RuntimeError(f"{gene}: not a leaf of its own tree {t['id']}")
    rice, maize = sorted(by_tax.get(TAXID[RICE], [])), sorted(by_tax.get(TAXID[MAIZE], []))
    return {
        "gene_tree": t["id"],
        "members": rice + maize,
        "rice": len(rice),
        "maize": len(maize),
        "negative": None if rice or maize else "tree_without_rice_or_maize",
    }


def orthologs(gene: str) -> list[str]:
    """Compara's rice and maize orthologs of ``gene``, in the order REST
    lists them: the single-lookup shortcut's material, never the key."""
    data = ensembl(
        f"/homology/id/{ATH}/{gene}",
        compara="plants",
        type="orthologues",
        target_species=[RICE, MAIZE],
        sequence="none",
        format="condensed",
    )["data"]
    return [str(h["id"]) for h in (data[0]["homologies"] if data else [])]


def build_p1(rng: random.Random) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    _, chroms = assembly(ATH)
    questions: list[dict[str, Any]] = []
    draws: list[dict[str, Any]] = []
    seen: set[str] = set()
    while len(questions) < N_QUESTIONS:
        if len(draws) >= 120:
            raise RuntimeError("P1: 120 draws without 10 kept")
        gene = random_gene(rng, ATH, chroms)["id"]
        if gene in seen:
            continue
        seen.add(gene)
        tree = gene_tree(gene)
        n = len(tree["members"])
        reason = None
        if tree["gene_tree"] and tree["gene_tree"] in {q["truth"]["gene_tree"] for q in questions}:
            reason = "gene tree already asked about"
        elif n == 1:
            reason = "one rice/maize member: a single lookup is the whole answer"
        elif n > P1_MAX_MEMBERS:
            reason = f"over {P1_MAX_MEMBERS} rice/maize members"
        draws.append({"gene": gene, "gene_tree": tree["gene_tree"], "n_members": n,
                      "negative": tree["negative"], "kept": reason is None,
                      "discard_reason": reason})  # fmt: skip
        print(f"P1 draw {len(draws):3d} {gene} {tree['gene_tree']} n={n} "
              f"{reason or 'kept'}", file=sys.stderr, flush=True)  # fmt: skip
        if reason:
            continue
        orth = orthologs(gene)
        questions.append(
            {
                "family": "P1",
                "category": "family_roster",
                "question": (
                    f"In Ensembl Plants release 63 (Ensembl Compara, plants division), find the "
                    f"gene tree that contains the Arabidopsis thaliana gene {gene}. List every "
                    f"rice (Oryza sativa Japonica Group, assembly IRGSP-1.0) gene and every maize "
                    f"(Zea mays B73, assembly Zm-B73-REFERENCE-NAM-5.0) gene in that gene tree: "
                    f"all of its members from those two species, paralogs included, not only "
                    f"orthologs of {gene}. If {gene} is in no gene tree, or its tree has no rice "
                    f"or maize gene, answer NONE."
                ),
                "answer_format": P1_FORMAT,
                "truth": {"gene": gene, **tree},
                "controls": {"orthologs": orth},
                "source": (
                    f"Ensembl REST /genetree/member/id/{ATH}/{gene}?compara=plants;"
                    f"prune_species={ATH},{RICE},{MAIZE}"
                ),
            }
        )
    return questions, draws


def fetch_maizegdb() -> tuple[dict[str, list[str]], dict[str, Any]]:
    r = client.get(MAIZEGDB_V4_V5, headers={"Accept": "*/*"}, follow_redirects=True)
    r.raise_for_status()
    table: dict[str, list[str]] = {}
    for line in r.text.splitlines():
        v4, _, v5 = line.partition("\t")
        if v4 in table:
            raise RuntimeError(f"MaizeGDB: {v4} listed twice")
        table[v4] = sorted(x for x in v5.split(",") if x)
    meta = {
        "url": MAIZEGDB_V4_V5,
        "sha256": hashlib.sha256(r.content).hexdigest(),
        "last_modified": r.headers.get("last-modified"),
        "rows": len(table),
        "rows_without_v5": sum(not v for v in table.values()),
    }
    return table, meta


def ensembl_history(ids: list[str]) -> dict[str, list[str] | None]:
    """Ensembl Plants ID history for v4 ids: the possible replacements, or
    None where the archive has no record of the id."""
    found = post("/archive/id", {"id": ids}, species=MAIZE)
    out: dict[str, list[str] | None] = dict.fromkeys(ids)
    for rec in found:
        out[rec["id"]] = sorted(p["stable_id"] for p in rec.get("possible_replacement") or [])
    return out


def ensembl_serves(ids: list[str]) -> set[str]:
    found = post("/lookup/id", {"ids": ids}, species=MAIZE)
    return {k for k, v in found.items() if v}


def uniprot_idmapping(genes: list[str]) -> tuple[dict[str, list[str]], str, str]:
    """UniProt's ID mapping service, Ensembl Genomes gene id -> UniProtKB."""
    r = client.post(
        f"{UNIPROT_IDMAPPING}/run",
        data={"from": "Ensembl_Genomes", "to": "UniProtKB", "ids": ",".join(genes)},
    )
    r.raise_for_status()
    job = r.json()["jobId"]
    for _ in range(60):
        s = client.get(f"{UNIPROT_IDMAPPING}/status/{job}", follow_redirects=False)
        # A finished job answers 303 to its results, or 200 with FINISHED;
        # httpx's raise_for_status raises on a 3xx, so it is checked first.
        if s.status_code == 303:
            break
        s.raise_for_status()
        status = s.json()
        if "results" in status or status.get("jobStatus") == "FINISHED":
            break
        if status.get("jobStatus") not in ("NEW", "RUNNING", None):
            raise RuntimeError(f"UniProt idmapping job {job}: {status}")
        time.sleep(2)
    else:
        raise RuntimeError(f"UniProt idmapping job {job} did not finish in 120 s")
    res = client.get(
        f"{UNIPROT_IDMAPPING}/uniprotkb/results/stream/{job}",
        params={"format": "tsv", "fields": "accession,xref_ensemblplants"},
    )
    res.raise_for_status()
    out: dict[str, list[str]] = {g: [] for g in genes}
    lines = res.text.rstrip("\n").split("\n")
    if lines[0].split("\t")[:2] != ["From", "Entry"]:
        raise RuntimeError(f"UniProt idmapping: unexpected header {lines[0]!r}")
    for ln in lines[1:]:
        frm, acc, *_ = ln.split("\t")
        out[frm].append(acc)
    return (
        {g: sorted(set(a)) for g, a in out.items()},
        res.headers["x-uniprot-release"],
        res.headers.get("x-uniprot-release-date", ""),
    )


def nearest_mapped(v4: str, table: dict[str, list[str]]) -> str:
    """The v5 gene of the numerically nearest v4 id that maps: the "nearest
    hit" a lookup returns when the asked-for id has none (P5's named miss)."""
    n = int(v4[-6:])
    best = min(
        (k for k, v in table.items() if v and k != v4),
        key=lambda k: (abs(int(k[-6:]) - n), k),
    )
    return table[best][0]


def build_p3(
    rng: random.Random,
    exclude: frozenset[str] = frozenset(),
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any], tuple[str, str]]:
    table, meta = fetch_maizegdb()
    # Held-out draws: ids already seen (an earlier key's, the repo's tests')
    # leave the pool before the shuffle, so the draw is uniform over the rest.
    pool = sorted(set(table) - exclude)
    rng.shuffle(pool)
    kept: list[dict[str, Any]] = []
    reasons: Counter[str] = Counter()
    screened = 0
    for start in range(0, len(pool), 400):
        chunk = pool[start : start + 400]
        hist = ensembl_history(chunk)
        targets = sorted({g for v4 in chunk for g in table[v4]})
        served = ensembl_serves(targets) if targets else set()
        for v4 in chunk:
            screened += 1
            mg, ens = table[v4], hist[v4]
            if not mg and ens is None:
                kept.append({"v4": v4, "v5": []})
                reasons["kept: unmappable in both"] += 1
            elif mg and ens == mg and set(mg) <= served:
                kept.append({"v4": v4, "v5": mg})
                reasons["kept: same v5 in both"] += 1
            elif mg and ens == mg:
                reasons["discarded: a v5 target not served by Ensembl Genomes 63"] += 1
            elif not mg:
                reasons["discarded: MaizeGDB no v5, Ensembl history has one"] += 1
            elif ens is None:
                reasons["discarded: MaizeGDB maps, Ensembl history has no record"] += 1
            else:
                reasons["discarded: the two sources name different v5 genes"] += 1
            if len(kept) == N_QUESTIONS * P3_ITEMS:
                break
        if len(kept) == N_QUESTIONS * P3_ITEMS:
            break
    else:
        raise RuntimeError("P3: ran out of v4 ids")
    genes = sorted({g for it in kept for g in it["v5"]})
    acc, up_release, up_date = uniprot_idmapping(genes)
    for it in kept:
        it["uniprot"] = sorted({a for g in it["v5"] for a in acc[g]})
    questions = []
    for i in range(N_QUESTIONS):
        items = kept[i * P3_ITEMS : (i + 1) * P3_ITEMS]
        ids = ", ".join(it["v4"] for it in items)
        questions.append(
            {
                "family": "P3",
                "category": "id_batch",
                "question": (
                    f"These {P3_ITEMS} maize (Zea mays B73) gene IDs are from the B73 RefGen_v4 "
                    f"annotation (Zm00001d): {ids}. For each one, give (a) the B73 RefGen_v5 "
                    f"gene ID or IDs (Zm00001eb) it maps to according to MaizeGDB's B73 v4 to v5 "
                    f"gene-model translation (the Ensembl Plants ID history gives the same "
                    f"answer for every ID here), and (b) every UniProtKB accession that UniProt's "
                    f"ID mapping service (from Ensembl Genomes gene IDs to UniProtKB, UniProt "
                    f"release {up_release}) returns for those v5 genes, or none if it returns "
                    f"none. If an ID maps to no v5 gene, say UNMAPPABLE for it; if you cannot "
                    f"determine an ID's mapping, say UNKNOWN for it."
                ),
                "answer_format": P3_FORMAT,
                "truth": {"items": items},
                "controls": {
                    "nearest_mapped": {
                        it["v4"]: nearest_mapped(it["v4"], table) for it in items if not it["v5"]
                    }
                },
                "source": f"MaizeGDB {MAIZEGDB_V4_V5} (sha256 {meta['sha256'][:12]}) agreeing "
                f"with Ensembl REST /archive/id; UniProt idmapping {up_release}",
            }
        )
    draws = {
        "screened": screened,
        "reasons": dict(reasons),
        "kept": len(kept),
        "kept_unmappable": sum(not it["v5"] for it in kept),
        "kept_multi_v5": sum(len(it["v5"]) > 1 for it in kept),
        "excluded_before_draw": len(set(table) & exclude),
    }
    return questions, draws, meta, (up_release, up_date)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="where the key goes; keep it outside the repo")
    ap.add_argument(
        "--seed",
        type=int,
        default=SEED,
        help=f"P1 draws Random(seed), P3 Random(seed + 3) (default {SEED})",
    )
    ap.add_argument("--families", default="P1,P3", help="families to build (default P1,P3)")
    ap.add_argument(
        "--exclude", type=Path, help="file of v4 ids (one per line) kept out of the P3 pool"
    )
    ap.add_argument(
        "--id-prefix", default="", help="appended to the family in question ids, e.g. c -> p3c_01"
    )
    a = ap.parse_args()
    families = set(a.families.split(","))
    exclude = (
        frozenset(ln.strip() for ln in a.exclude.read_text().splitlines() if ln.strip())
        if a.exclude
        else frozenset()
    )
    signal.signal(signal.SIGALRM, lambda *_: sys.exit("aborting: walltime guard"))
    signal.alarm(3600)
    release = (ensembl("/info/software")["release"], ensembl("/info/eg_version")["version"])
    p1, p1_draws = build_p1(random.Random(a.seed)) if "P1" in families else ([], [])
    p3, p3_draws, maizegdb, (up, up_date) = (
        build_p3(random.Random(a.seed + 3), exclude) if "P3" in families else ([], {}, {}, ("", ""))
    )
    for i, q in enumerate(p1, 1):
        q["id"] = f"p1{a.id_prefix}_{i:02d}"
    for i, q in enumerate(p3, 1):
        q["id"] = f"p3{a.id_prefix}_{i:02d}"
    after = (ensembl("/info/software")["release"], ensembl("/info/eg_version")["version"])
    if after != release:
        raise SystemExit(f"Ensembl moved from {release} to {after} during the build")
    out = {
        "built": datetime.now(UTC).isoformat(timespec="seconds"),
        "seed": a.seed,
        "families": sorted(families),
        "excluded_ids": len(exclude),
        "ensembl_release": release[0],
        "ensembl_genomes_release": release[1],
        "uniprot_release": up,
        "uniprot_release_date": up_date,
        "maizegdb": maizegdb,
        "draws": {"P1": p1_draws, "P3": p3_draws},
        "questions": p1 + p3,
    }
    path = Path(a.out).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(out, indent=1) + "\n"
    path.write_text(text)
    neg = sum(not q["truth"]["members"] for q in p1)
    print(f"P1 {len(p1)} kept of {len(p1_draws)} draws, {neg} NONE; P3 {p3_draws}")
    print("sha256", hashlib.sha256(text.encode()).hexdigest())


if __name__ == "__main__":
    main()
