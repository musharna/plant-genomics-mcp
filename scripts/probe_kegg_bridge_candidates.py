#!/usr/bin/env python3
"""v1.5 KEGG-bridge candidate probe.

For each organism in the deferred set, mechanically pick chromosome 1's
first protein-coding gene via Ensembl Plants /overlap/region, then probe
the Ensembl /xrefs/id → EntrezGene → KEGG /link/pathway round-trip that
the v1.4.0 bridge depends on. Emits a markdown table to stdout and a JSON
sidecar to scripts/probe_kegg_bridge_candidates.json (overridable via
--output-json).

Pass condition (both must hold):
  1. Ensembl /xrefs returns >=1 cross-reference with dbname == "EntrezGene"
  2. KEGG /link/pathway/<kegg_code>:<entrez_id> returns a non-empty body

Falsified verdicts:
  - no_chr1_gene_found  — Ensembl /overlap returned no protein_coding gene
  - no_entrez_xref      — Ensembl /xrefs returned no EntrezGene xref
  - kegg_no_pathway     — KEGG /link/pathway returned empty body
  - bad_kegg_code       — KEGG /link/pathway returned HTTP 400
  - probe_walltime      — signal.alarm tripped on a single-organism call

Walltime guarded per-organism (signal.alarm(60)); 2s sleep between
organisms (polite to Ensembl + KEGG). Re-runnable; deterministic given
upstream stability.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path
from typing import Any

import httpx

ENSEMBL_BASE = "https://rest.ensembl.org"
KEGG_BASE = "https://rest.kegg.jp"

# (organism canonical, ensembl_slug, KEGG 3-letter code candidate, chr1 region)
# kegg codes per the KEGG genome list; the probe verifies each by issuing a
# real /link/pathway call (bad_kegg_code surfaces if 400).
PROBE_TARGETS: list[tuple[str, str, str, str]] = [
    ("triticum_aestivum", "triticum_aestivum", "taes", "1A:1-1000000"),
    ("sorghum_bicolor", "sorghum_bicolor", "sbi", "1:1-1000000"),
    ("hordeum_vulgare", "hordeum_vulgare", "hvg", "1:1-1000000"),
    ("vitis_vinifera", "vitis_vinifera", "vvi", "1:1-1000000"),
    ("populus_trichocarpa", "populus_trichocarpa", "pop", "1:1-1000000"),
    ("medicago_truncatula", "medicago_truncatula", "mtr", "1:1-1000000"),
    ("brachypodium_distachyon", "brachypodium_distachyon", "bdi", "1:1-1000000"),
    ("solanum_lycopersicum", "solanum_lycopersicum", "sly", "1:1-1000000"),
]


class _WalltimeError(RuntimeError):
    pass


def _alarm_handler(_signum: int, _frame: Any) -> None:  # noqa: ANN401
    raise _WalltimeError("probe walltime exceeded")


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


def probe_one(
    client: httpx.Client, organism: str, slug: str, kegg_code: str, region: str
) -> dict[str, Any]:
    """Probe one organism end-to-end; return a structured verdict dict."""
    signal.signal(signal.SIGALRM, _alarm_handler)
    signal.alarm(60)
    try:
        locus = _chr1_first_protein_coding(client, slug, region)
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
        entrez, observed = _entrez_xrefs(client, slug, locus)
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
        for i, (organism, slug, kegg_code, region) in enumerate(targets):
            print(f"... probing {organism}", file=sys.stderr, flush=True)
            results.append(probe_one(client, organism, slug, kegg_code, region))
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
