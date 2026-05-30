"""Discover one KEGG pathway-annotated native locus per supported organism.

The v1.6 benchmark corpus validates only the KEGG *exception* path: every
`kegg.lookup_pathways` entry is `expects_exception: NotFoundError` because the
chr1-first-gene loci it uses happen to carry zero KEGG pathway memberships
(documented sparse-coverage caveat). This probe closes that gap by finding, per
KEGG-supported organism, a real *happy-path* locus — one whose native Ensembl
identifier round-trips through the production bridge to >=1 KEGG pathway — so
v1.7 can add genuine KEGG success assertions to the corpus.

Method (per organism), reusing the chr1-region discovery from
``probe_kegg_bridge_candidates``:

  1. Discover the first nuclear chromosome (Ensembl /info/assembly karyotype).
  2. List protein-coding genes on a bounded window of that chromosome
     (/overlap/region, first ``WINDOW_BP``).
  3. Walk genes in order; for each:
       - Arabidopsis (native ``ath:`` path): KEGG /link/pathway/ath:<AGI>.
       - bridge organisms: Ensembl /xrefs/id/<gene> -> EntrezGene id, then
         KEGG /link/pathway/<code>:<entrez> (one cheap call, no per-pathway
         /get). A gene with no EntrezGene xref is skipped (bridge-ineligible).
     First gene with a non-empty KEGG pathway link is the candidate.
  4. Confirm the candidate through the **production** ``kegg.lookup_pathways``
     (same code path the benchmark exercises) to capture the real
     ``pathways.len`` baseline + ``kegg_gene_id`` + a sample pathway name.

Polite + bounded: small sleeps between upstream calls, 2s between organisms,
``GENE_SCAN_CAP`` genes scanned max per organism, and a global ``signal.alarm``
walltime guard. Re-runnable; deterministic given upstream stability.

Usage:
  python scripts/probe_kegg_happy_path.py [--out scripts/probe_kegg_happy_path.json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
import time
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from probe_kegg_bridge_candidates import (  # noqa: E402
    ENSEMBL_BASE,
    KEGG_BASE,
    _top_level_chromosome,
)

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from plant_genomics_mcp import kegg, organisms  # noqa: E402

# KEGG-supported organisms (kegg_org_code populated) per organisms.py matrix as
# of v1.6: Arabidopsis (native) + the six bridge organisms. Wheat/tomato are
# matrix-falsified (OrganismNotSupported) and excluded here by construction.
KEGG_ORGANISMS: list[str] = [
    "arabidopsis_thaliana",
    "oryza_sativa",
    "zea_mays",
    "glycine_max",
    "hordeum_vulgare",
    "populus_trichocarpa",
    "brachypodium_distachyon",
]

WINDOW_BP = 4_900_000  # chromosome window scanned (Ensembl /overlap/region caps at 5 Mb)
GENE_SCAN_CAP = 60  # max protein-coding genes probed per organism
KEGG_SLEEP = 0.4  # between KEGG calls (KEGG asks for restraint)
ENSEMBL_SLEEP = 0.25  # between Ensembl xref calls
ORG_SLEEP = 2.0  # between organisms
GLOBAL_WALLTIME_S = 1200


class _WalltimeError(RuntimeError):
    pass


def _alarm(_s: int, _f: Any) -> None:  # noqa: ANN401
    raise _WalltimeError("happy-path probe walltime exceeded")


def _protein_coding_genes(client: httpx.Client, slug: str, region: str) -> list[str]:
    """All protein-coding gene stable_ids in ``region``, in start-coord order."""
    url = f"{ENSEMBL_BASE}/overlap/region/{slug}/{region}"
    r = client.get(url, params={"feature": "gene"}, headers={"Accept": "application/json"})
    r.raise_for_status()
    genes = [
        (e.get("start", 0), e["gene_id"])
        for e in r.json()
        if e.get("biotype") == "protein_coding" and e.get("gene_id")
    ]
    genes.sort()
    return [gid for _, gid in genes]


def _entrez_id(client: httpx.Client, slug: str, locus: str) -> str | None:
    url = f"{ENSEMBL_BASE}/xrefs/id/{locus}"
    r = client.get(url, params={"species": slug}, headers={"Accept": "application/json"})
    r.raise_for_status()
    for x in r.json():
        if x.get("dbname") == "EntrezGene" and x.get("primary_id"):
            return str(x["primary_id"])
    return None


def _kegg_link_nonempty(client: httpx.Client, kegg_code: str, gene_id: str) -> bool:
    """True if KEGG /link/pathway/<code>:<gene_id> returns >=1 membership."""
    r = client.get(f"{KEGG_BASE}/link/pathway/{kegg_code}:{gene_id}")
    if r.status_code != 200:
        return False
    return bool(r.text.strip())


async def _confirm_production(canonical: str, locus: str) -> dict[str, Any]:
    """Run the real kegg.lookup_pathways the benchmark uses; capture facts."""
    async with httpx.AsyncClient() as client:
        result = await kegg.lookup_pathways(client, locus, organism=canonical)
    pathways = result.get("pathways", [])
    return {
        "kegg_gene_id": result.get("kegg_gene_id"),
        "organism_echo": result.get("organism"),
        "entrez_gene_id": result.get("entrez_gene_id"),
        "pathways_len": len(pathways),
        "sample_pathway": pathways[0] if pathways else None,
    }


def _discover_one(client: httpx.Client, canonical: str) -> dict[str, Any]:
    record = organisms.resolve(canonical)
    slug = record.ensembl_slug
    kegg_code = organisms.kegg_org_code_for(canonical)
    out: dict[str, Any] = {
        "organism": canonical,
        "slug": slug,
        "kegg_code": kegg_code,
        "locus": None,
        "verdict": "not_found",
    }

    chr_name = _top_level_chromosome(client, slug)
    if chr_name is None:
        out["verdict"] = "no_chromosome_region"
        return out
    region = f"{chr_name}:1-{WINDOW_BP}"
    out["region"] = region
    genes = _protein_coding_genes(client, slug, region)
    out["genes_in_window"] = len(genes)

    scanned = 0
    for gene in genes:
        if scanned >= GENE_SCAN_CAP:
            out["verdict"] = "scan_cap_reached"
            break
        scanned += 1
        if kegg_code == "ath":
            # Native path — the AGI IS the KEGG gene id.
            time.sleep(KEGG_SLEEP)
            if not _kegg_link_nonempty(client, kegg_code, gene):
                continue
        else:
            time.sleep(ENSEMBL_SLEEP)
            entrez = _entrez_id(client, slug, gene)
            if entrez is None:
                continue
            time.sleep(KEGG_SLEEP)
            if not _kegg_link_nonempty(client, kegg_code, entrez):
                continue
        # Candidate found — confirm through the production code path.
        confirmed = asyncio.run(_confirm_production(canonical, gene))
        if confirmed["pathways_len"] >= 1:
            out.update(
                locus=gene,
                verdict="found",
                genes_scanned=scanned,
                **confirmed,
            )
            return out
        # Production disagreed (rare) — keep scanning.
    out.setdefault("genes_scanned", scanned)
    if out["verdict"] == "not_found":
        out["verdict_reason"] = f"no pathway-annotated gene in first {scanned} scanned"
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--out",
        default=str(Path(__file__).with_suffix(".json")),
        help="write the per-organism JSON results here",
    )
    ap.add_argument(
        "--only",
        default=None,
        help="comma-separated canonical organism slugs to probe (default: all 7)",
    )
    args = ap.parse_args(argv)

    targets = KEGG_ORGANISMS
    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        targets = [o for o in KEGG_ORGANISMS if o in wanted]

    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(GLOBAL_WALLTIME_S)

    results: list[dict[str, Any]] = []
    with httpx.Client(timeout=30.0) as client:
        for i, canonical in enumerate(targets):
            if i:
                time.sleep(ORG_SLEEP)
            try:
                rec = _discover_one(client, canonical)
            except _WalltimeError:
                rec = {"organism": canonical, "verdict": "walltime"}
                results.append(rec)
                print(f"[{canonical}] WALLTIME — aborting", file=sys.stderr)
                break
            except httpx.HTTPError as exc:
                rec = {"organism": canonical, "verdict": "http_error", "error": str(exc)}
            results.append(rec)
            tag = rec.get("verdict")
            extra = (
                f"locus={rec.get('locus')} pathways={rec.get('pathways_len')} "
                f"kegg_gene_id={rec.get('kegg_gene_id')}"
                if tag == "found"
                else rec.get("verdict_reason", "")
            )
            print(f"[{canonical}] {tag} {extra}", file=sys.stderr)

    Path(args.out).write_text(json.dumps(results, indent=2) + "\n")
    found = sum(1 for r in results if r.get("verdict") == "found")
    print(
        f"\n{found}/{len(results)} organisms got a happy-path locus -> {args.out}", file=sys.stderr
    )
    return 0 if found == len(targets) else 1


if __name__ == "__main__":
    raise SystemExit(main())
