#!/usr/bin/env python3
"""v1.5 KEGG-bridge candidate probe.

For each organism in the deferred set, mechanically pick chromosome 1's
first protein-coding gene via Ensembl Plants /overlap/region, then probe
the Ensembl /xrefs/id → EntrezGene → KEGG /link/pathway round-trip that
the v1.4.0 bridge depends on. Emits a markdown table to stdout and a JSON
sidecar to scripts/probe_kegg_bridge_candidates.json (overridable via
--output-json).

The "chr1" region name is discovered per-organism by calling Ensembl's
/info/assembly endpoint and picking the first chromosome-coord_system
region in a numeric-aware sort (so "1" beats "10"/"Mt"; "1H" / "1A" beat
"2"; organelle chromosomes Mt/Pt/Cp are filtered out so the picked chr1
is always nuclear). This avoids hardcoding per-organism locus formats —
the spec explicitly disallows author-recall of region names as
load-bearing input.

Pass condition (both must hold):
  1. Ensembl /xrefs returns >=1 cross-reference with dbname == "EntrezGene"
  2. KEGG /link/pathway/<kegg_code>:<entrez_id> returns a non-empty body

Falsified verdicts:
  - no_chromosome_region — Ensembl /info/assembly returned no
                            chromosome-coord_system region (organism may
                            only have scaffolds in current assembly)
  - no_chr1_gene_found   — Ensembl /overlap returned no protein_coding gene
  - no_entrez_xref       — Ensembl /xrefs returned no EntrezGene xref
  - kegg_no_pathway      — KEGG /link/pathway returned empty body
  - bad_kegg_code        — KEGG /link/pathway returned HTTP 400
  - ensembl_http_error   — Ensembl returned a non-2xx (status recorded in
                            the record under "ensembl_http_status")
  - probe_walltime       — signal.alarm tripped on a single-organism call

Walltime guarded per-organism (signal.alarm(60)); 2s sleep between
organisms (polite to Ensembl + KEGG). Re-runnable; deterministic given
upstream stability.
"""

from __future__ import annotations

import argparse
import json
import re
import signal
import sys
import time
from pathlib import Path
from typing import Any

import httpx

ENSEMBL_BASE = "https://rest.ensembl.org"
KEGG_BASE = "https://rest.kegg.jp"

# (organism canonical, ensembl_slug, KEGG 3-letter code candidate)
# kegg codes per the KEGG genome list; the probe verifies each by issuing a
# real /link/pathway call (bad_kegg_code surfaces if 400). The per-organism
# chr1 region name is discovered at runtime via /info/assembly — see
# _top_level_chromosome — so this table does NOT encode locus formats.
PROBE_TARGETS: list[tuple[str, str, str]] = [
    ("triticum_aestivum", "triticum_aestivum", "taes"),
    ("sorghum_bicolor", "sorghum_bicolor", "sbi"),
    ("hordeum_vulgare", "hordeum_vulgare", "hvg"),
    ("vitis_vinifera", "vitis_vinifera", "vvi"),
    ("populus_trichocarpa", "populus_trichocarpa", "pop"),
    ("medicago_truncatula", "medicago_truncatula", "mtr"),
    ("brachypodium_distachyon", "brachypodium_distachyon", "bdi"),
    ("solanum_lycopersicum", "solanum_lycopersicum", "sly"),
]

# Organelle chromosome names (case-insensitive exact-match) — excluded from
# the chr1 pick so we always land on a nuclear chromosome. Exact-match
# rather than prefix-match to avoid catching GenBank accessions like
# "CP126648.1" (vitis) or "MT9..." scaffolds.
_ORGANELLE_NAMES = frozenset(
    {"mt", "pt", "cp", "chloroplast", "mitochondria", "mitochondrion", "plastid"}
)


class _WalltimeError(RuntimeError):
    pass


def _alarm_handler(_signum: int, _frame: Any) -> None:  # noqa: ANN401
    raise _WalltimeError("probe walltime exceeded")


_NUMERIC_PREFIX_RE = re.compile(r"^(\d+)(.*)$")


def _chr_sort_key(name: str) -> tuple[int, int, str]:
    """Numeric-aware sort key for chromosome names.

    Sort order: numeric-prefixed names first (1, 1A, 1H, 2, 10, ...), then
    non-numeric names (alphabetical, GenBank accessions, etc.). Within the
    numeric set, sort by (int_part, suffix) so "1" < "1A" < "1H" < "2" < "10".

    Returns (bucket, int_part, tiebreak_string) where bucket=0 for
    numeric-prefixed and bucket=1 for non-numeric.
    """
    m = _NUMERIC_PREFIX_RE.match(name)
    if m is None:
        return (1, 0, name)
    int_part = int(m.group(1))
    suffix = m.group(2)
    return (0, int_part, suffix)


def _top_level_chromosome(client: httpx.Client, slug: str) -> str | None:
    """Return the name of the first chromosome per Ensembl's own assembly metadata.

    Calls Ensembl /info/assembly/{slug}. Strategy:

    1. PREFERRED: use the response's "karyotype" field. Ensembl curates this
       per-assembly to list the "real" chromosomes (nuclear; organelles
       sometimes appended at the end; scaffolds excluded). Sorted by
       _chr_sort_key, the first entry is the first chromosome regardless of
       naming convention ("1" / "1A" / "1H" / GenBank accession).
    2. FALLBACK: if karyotype is absent or empty, filter top_level_region to
       entries whose coord_system == "chromosome", drop organelles, and pick
       the first by _chr_sort_key. This is the original strategy and works
       for assemblies where Ensembl populated coord_system="chromosome".

    Returns None when neither strategy yields a candidate (organism may only
    have scaffolds in the current assembly).
    """
    url = f"{ENSEMBL_BASE}/info/assembly/{slug}"
    r = client.get(url, headers={"Accept": "application/json"})
    r.raise_for_status()
    payload = r.json()

    # Strategy 1: karyotype (preferred). Filter organelles defensively.
    karyotype = payload.get("karyotype") or []
    karyotype = [name for name in karyotype if name.lower() not in _ORGANELLE_NAMES]
    if karyotype:
        karyotype.sort(key=_chr_sort_key)
        return karyotype[0]

    # Strategy 2: top_level_region filtered to coord_system=="chromosome".
    regions = payload.get("top_level_region") or []
    chroms = [
        entry["name"]
        for entry in regions
        if entry.get("coord_system") == "chromosome"
        and entry.get("name", "").lower() not in _ORGANELLE_NAMES
    ]
    if not chroms:
        return None
    chroms.sort(key=_chr_sort_key)
    return chroms[0]


def _chr1_first_protein_coding(client: httpx.Client, slug: str, region: str) -> str | None:
    """Return the first protein-coding gene's stable_id on chr1 region, or None."""
    url = f"{ENSEMBL_BASE}/overlap/region/{slug}/{region}"
    r = client.get(url, params={"feature": "gene"}, headers={"Accept": "application/json"})
    r.raise_for_status()
    for entry in r.json():
        if entry.get("biotype") == "protein_coding":
            return entry["gene_id"]
    return None


def _entrez_xrefs(client: httpx.Client, slug: str, locus: str) -> tuple[list[str], list[str]]:
    """Return (entrez_ids, all_observed_dbnames)."""
    url = f"{ENSEMBL_BASE}/xrefs/id/{locus}"
    r = client.get(url, params={"species": slug}, headers={"Accept": "application/json"})
    r.raise_for_status()
    xrefs = r.json()
    observed = sorted({x.get("dbname", "?") for x in xrefs})
    entrez = [x["primary_id"] for x in xrefs if x.get("dbname") == "EntrezGene"]
    return entrez, observed


def _kegg_pathway_count(
    client: httpx.Client, kegg_code: str, entrez_id: str
) -> tuple[int | None, int | None]:
    """Return (pathway_count, http_status). pathway_count=None when status != 200."""
    url = f"{KEGG_BASE}/link/pathway/{kegg_code}:{entrez_id}"
    r = client.get(url)
    if r.status_code != 200:
        return None, r.status_code
    body = r.text.strip()
    if not body:
        return 0, 200
    return len(body.splitlines()), 200


def probe_one(client: httpx.Client, organism: str, slug: str, kegg_code: str) -> dict[str, Any]:
    """Probe one organism end-to-end; return a structured verdict dict."""
    signal.signal(signal.SIGALRM, _alarm_handler)
    signal.alarm(60)
    region: str | None = None
    try:
        try:
            chr1_name = _top_level_chromosome(client, slug)
        except httpx.HTTPStatusError as exc:
            return {
                "organism": organism,
                "slug": slug,
                "kegg_code": kegg_code,
                "chr1_region": None,
                "chr1_locus": None,
                "entrez_xrefs": [],
                "observed_dbs": [],
                "kegg_pathway_count": None,
                "ensembl_http_status": exc.response.status_code,
                "verdict": "falsified",
                "verdict_reason": "ensembl_http_error",
            }
        if chr1_name is None:
            return {
                "organism": organism,
                "slug": slug,
                "kegg_code": kegg_code,
                "chr1_region": None,
                "chr1_locus": None,
                "entrez_xrefs": [],
                "observed_dbs": [],
                "kegg_pathway_count": None,
                "verdict": "falsified",
                "verdict_reason": "no_chromosome_region",
            }
        region = f"{chr1_name}:1-1000000"
        try:
            locus = _chr1_first_protein_coding(client, slug, region)
        except httpx.HTTPStatusError as exc:
            return {
                "organism": organism,
                "slug": slug,
                "kegg_code": kegg_code,
                "chr1_region": region,
                "chr1_locus": None,
                "entrez_xrefs": [],
                "observed_dbs": [],
                "kegg_pathway_count": None,
                "ensembl_http_status": exc.response.status_code,
                "verdict": "falsified",
                "verdict_reason": "ensembl_http_error",
            }
        if locus is None:
            return {
                "organism": organism,
                "slug": slug,
                "kegg_code": kegg_code,
                "chr1_region": region,
                "chr1_locus": None,
                "entrez_xrefs": [],
                "observed_dbs": [],
                "kegg_pathway_count": None,
                "verdict": "falsified",
                "verdict_reason": "no_chr1_gene_found",
            }
        try:
            entrez, observed = _entrez_xrefs(client, slug, locus)
        except httpx.HTTPStatusError as exc:
            return {
                "organism": organism,
                "slug": slug,
                "kegg_code": kegg_code,
                "chr1_region": region,
                "chr1_locus": locus,
                "entrez_xrefs": [],
                "observed_dbs": [],
                "kegg_pathway_count": None,
                "ensembl_http_status": exc.response.status_code,
                "verdict": "falsified",
                "verdict_reason": "ensembl_http_error",
            }
        if not entrez:
            return {
                "organism": organism,
                "slug": slug,
                "kegg_code": kegg_code,
                "chr1_region": region,
                "chr1_locus": locus,
                "entrez_xrefs": [],
                "observed_dbs": observed,
                "kegg_pathway_count": None,
                "verdict": "falsified",
                "verdict_reason": "no_entrez_xref",
            }
        entrez_id = entrez[0]
        count, status = _kegg_pathway_count(client, kegg_code, entrez_id)
        if status == 400:
            return {
                "organism": organism,
                "slug": slug,
                "kegg_code": kegg_code,
                "chr1_region": region,
                "chr1_locus": locus,
                "entrez_xrefs": entrez,
                "observed_dbs": observed,
                "kegg_pathway_count": None,
                "verdict": "falsified",
                "verdict_reason": "bad_kegg_code",
            }
        if count == 0:
            return {
                "organism": organism,
                "slug": slug,
                "kegg_code": kegg_code,
                "chr1_region": region,
                "chr1_locus": locus,
                "entrez_xrefs": entrez,
                "observed_dbs": observed,
                "kegg_pathway_count": 0,
                "verdict": "falsified",
                "verdict_reason": "kegg_no_pathway",
            }
        return {
            "organism": organism,
            "slug": slug,
            "kegg_code": kegg_code,
            "chr1_region": region,
            "chr1_locus": locus,
            "entrez_xrefs": entrez,
            "observed_dbs": observed,
            "kegg_pathway_count": count,
            "verdict": "pass",
        }
    except _WalltimeError:
        return {
            "organism": organism,
            "slug": slug,
            "kegg_code": kegg_code,
            "chr1_region": region,
            "verdict": "falsified",
            "verdict_reason": "probe_walltime",
        }
    finally:
        signal.alarm(0)


def _print_markdown_table(results: list[dict[str, Any]]) -> None:
    print("| organism | chr1 locus | entrez | kegg pathways | verdict |")
    print("|----------|------------|--------|---------------|---------|")
    for r in results:
        locus = r.get("chr1_locus") or "—"
        entrez = ",".join(r.get("entrez_xrefs", [])) or "—"
        count = r.get("kegg_pathway_count")
        count_s = "—" if count is None else str(count)
        verdict = r["verdict"]
        if verdict == "falsified":
            verdict = f"falsified: {r.get('verdict_reason', '?')}"
        print(f"| {r['organism']} | {locus} | {entrez} | {count_s} | {verdict} |")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Probe KEGG-bridge viability for deferred organisms"
    )
    parser.add_argument(
        "--organisms",
        type=str,
        default=None,
        help="Comma-separated subset of canonical organism slugs (default: all 8)",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=Path(__file__).with_suffix(".json"),
        help="Path to JSON sidecar (default: scripts/probe_kegg_bridge_candidates.json)",
    )
    args = parser.parse_args(argv)

    if args.organisms:
        wanted = {s.strip() for s in args.organisms.split(",") if s.strip()}
        targets = [t for t in PROBE_TARGETS if t[0] in wanted]
        missing = wanted - {t[0] for t in PROBE_TARGETS}
        if missing:
            print(f"unknown organisms: {sorted(missing)}", file=sys.stderr)
            return 2
    else:
        targets = list(PROBE_TARGETS)

    with httpx.Client(timeout=30.0) as client:
        results: list[dict[str, Any]] = []
        for i, (organism, slug, kegg_code) in enumerate(targets):
            print(f"... probing {organism}", file=sys.stderr, flush=True)
            results.append(probe_one(client, organism, slug, kegg_code))
            if i < len(targets) - 1:
                time.sleep(2.0)

    sidecar = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
        "spec": "docs/superpowers/specs/2026-05-25-v1.5-deferred-organism-expansion-design.md",
        "organisms": results,
    }
    args.output_json.write_text(json.dumps(sidecar, indent=2) + "\n")
    print(f"\nwrote {args.output_json}", file=sys.stderr)
    print()
    _print_markdown_table(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
