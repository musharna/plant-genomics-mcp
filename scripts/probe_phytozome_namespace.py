"""Diagnose why Phytozome rice/soybean lookups fail, and discover working IDs.

The v1.6 benchmark corpus marks ``phytozome.lookup_locus`` for rice
(``Os01g0100100``) and soybean (``GLYMA_01G001700``) as
``expects_exception: NotFoundError``, annotated "possibly upstream BioMart data
drift." That annotation is an unverified guess — and maize succeeds in the same
corpus, which argues against a uniform outage. Competing hypotheses:

  (a) genuine upstream removal / data drift,
  (b) ID-namespace mismatch — the corpus uses Ensembl-style IDs (RAP-DB
      ``Os01g0100100``; soybean ``GLYMA_01G001700``) but Phytozome's
      ``gene_name_filter`` expects its native names (rice MSU ``LOC_Os01g...``;
      soybean ``Glyma.01G...``),
  (c) wrong ``phytozome_int`` proteome mapping.

This probe determines the real cause PER organism via Approach A
(namespace-agnostic discovery):

  1. Discovery query — BioMart with the ``organism_id`` filter ONLY (no
     ``gene_name_filter``), streamed, capped at ``DISCOVERY_ROW_CAP`` rows.
     Yields real native ``gene_name`` values for that proteome.
  2. Pick a candidate — prefer the chr1 / lowest-start gene for determinism.
  3. Round-trip confirm — feed the native name through the PRODUCTION
     ``phytozome.lookup_locus`` (the same code path the benchmark exercises).
  4. Hypothesis test — for the flagged organisms, also re-attempt the corpus
     canonical locus through production and record whether it raises.

Verdict per organism: namespace_mismatch_confirmed / genuine_absence /
already_working / discovery_error / no_native_gene. The ``organism_name`` echo
is always captured so a wrong-``phytozome_int`` case (c) surfaces for follow-up.

Polite + bounded: ``signal.alarm`` walltime guard, ~2s sleep between organisms,
streamed discovery capped at ``DISCOVERY_ROW_CAP`` rows. Re-runnable.

Usage:
  python scripts/probe_phytozome_namespace.py [--out PATH] [--only slug,slug]
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

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from plant_genomics_mcp import organisms, phytozome  # noqa: E402
from plant_genomics_mcp.errors import NotFoundError, PlantGenomicsError  # noqa: E402

# Corpus loci flagged expects_exception today — the IDs whose failure we explain.
CANONICAL_FLAGGED: dict[str, str] = {
    "oryza_sativa": "Os01g0100100",
    "glycine_max": "GLYMA_01G001700",
}

DISCOVERY_ROW_CAP = 40  # native gene_names read from the streamed org-only query
ORG_SLEEP = 2.0  # between organisms (BioMart is the slowest backend)
STREAM_TIMEOUT_S = 60.0
GLOBAL_WALLTIME_S = 900

# Discovery attribute order — MUST match the <Attribute> order in the template.
_DISCOVERY_FIELDS = ("organism_name", "gene_name", "chromosome", "gene_start")

# Org-id-only discovery query: no gene_name_filter, minimal attributes.
_DISCOVERY_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE Query>
<Query virtualSchemaName="zome_mart" header="1" uniqueRows="0" count="" datasetConfigVersion="0.7">
  <Dataset name="phytozome" interface="default">
    <Filter name="organism_id" value="{organism_id}"/>
    <Attribute name="organism_name"/>
    <Attribute name="gene_name1"/>
    <Attribute name="chr_name1"/>
    <Attribute name="gene_chrom_start"/>
  </Dataset>
</Query>"""


class _WalltimeError(RuntimeError):
    pass


class BioMartQueryError(RuntimeError):
    """BioMart returned a 200 body beginning ``Query ERROR:``."""


def _alarm(_s: int, _f: Any) -> None:  # noqa: ANN401
    raise _WalltimeError("phytozome namespace probe walltime exceeded")


def _parse_discovery_rows(tsv_text: str, cap: int) -> list[dict[str, str]]:
    """Parse a BioMart discovery TSV into row dicts (pure; no I/O).

    - A body beginning ``Query ERROR:`` raises ``BioMartQueryError``.
    - The first non-empty line is the header (``header="1"``); skipped.
    - Empty / header-only bodies yield ``[]``.
    - Rows whose column count != len(_DISCOVERY_FIELDS) are skipped (defensive;
      BioMart occasionally emits stray partial lines).
    - At most ``cap`` data rows are returned.
    """
    if tsv_text.startswith("Query ERROR"):
        raise BioMartQueryError(tsv_text.strip()[:300])
    lines = [ln for ln in tsv_text.splitlines() if ln.strip()]
    rows: list[dict[str, str]] = []
    for ln in lines[1:]:  # drop header
        values = ln.split("\t")
        if len(values) != len(_DISCOVERY_FIELDS):
            continue
        rows.append(dict(zip(_DISCOVERY_FIELDS, values, strict=True)))
        if len(rows) >= cap:
            break
    return rows


def _start_key(row: dict[str, str]) -> tuple[str, int]:
    """Sort key: (chromosome, numeric start) for deterministic chr1-first pick."""
    raw = row.get("gene_start", "")
    try:
        start = int(raw)
    except (TypeError, ValueError):
        start = 1 << 62
    return (row.get("chromosome", ""), start)


def _discover_native_genes(client: httpx.Client, phyto_id: int) -> list[dict[str, str]]:
    """Stream the org-only discovery query; return up to DISCOVERY_ROW_CAP rows."""
    xml_payload = _DISCOVERY_TEMPLATE.format(organism_id=phyto_id)
    buf: list[str] = []
    with client.stream(
        "POST", phytozome.BASE_URL, data={"query": xml_payload}, timeout=STREAM_TIMEOUT_S
    ) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            buf.append(line)
            # header + cap data rows + a little slack for blank/partial lines.
            if len(buf) >= DISCOVERY_ROW_CAP + 5:
                break
    return _parse_discovery_rows("\n".join(buf), DISCOVERY_ROW_CAP)


async def _production_lookup(canonical: str, locus: str) -> tuple[bool, dict[str, Any] | str]:
    """Run the real phytozome.lookup_locus. Returns (ok, record-or-error-str)."""
    async with httpx.AsyncClient() as client:
        try:
            rec = await phytozome.lookup_locus(client, locus, organism=canonical)
        except (NotFoundError, PlantGenomicsError) as exc:
            return False, f"{type(exc).__name__}: {exc}"
    return True, rec


def _discover_one(client: httpx.Client, canonical: str) -> dict[str, Any]:
    record = organisms.resolve(canonical)
    phyto_id = record.phytozome_int
    out: dict[str, Any] = {
        "organism": canonical,
        "phytozome_int": phyto_id,
        "canonical_name": CANONICAL_FLAGGED.get(canonical),
        "canonical_raised": None,
        "native_name": None,
        "native_ok": False,
        "organism_name_echo": None,
        "verdict": "no_native_gene",
        "note": "",
    }

    # Step 4 (do first; cheap, explains the corpus): re-test the flagged canonical.
    canonical_name = CANONICAL_FLAGGED.get(canonical)
    if canonical_name is not None:
        ok, _ = asyncio.run(_production_lookup(canonical, canonical_name))
        out["canonical_raised"] = not ok

    # Step 1: discover native gene names.
    try:
        rows = _discover_native_genes(client, phyto_id)
    except BioMartQueryError as exc:
        out["verdict"] = "discovery_error"
        out["note"] = f"BioMart Query ERROR: {exc}"
        return out
    out["genes_discovered"] = len(rows)
    if not rows:
        out["verdict"] = "no_native_gene"
        out["note"] = "discovery query returned 0 rows"
        return out

    # Step 2: deterministic chr1 / lowest-start pick.
    candidate = sorted(rows, key=_start_key)[0]
    native = candidate["gene_name"]
    out["native_name"] = native
    out["organism_name_echo"] = candidate.get("organism_name")

    # Step 3: round-trip confirm through production.
    ok, rec = asyncio.run(_production_lookup(canonical, native))
    out["native_ok"] = ok
    if ok and isinstance(rec, dict):
        out["organism_name_echo"] = rec.get("organism_name") or out["organism_name_echo"]
        out["native_record"] = rec

    # Verdict interpretation rule (per design spec).
    if not ok:
        out["verdict"] = "genuine_absence"
        out["note"] = f"native name {native!r} did not round-trip through production: {rec}"
    elif canonical_name is not None and out["canonical_raised"]:
        out["verdict"] = "namespace_mismatch_confirmed"
        out["note"] = (
            f"canonical {canonical_name!r} raised; native {native!r} succeeded — "
            "namespace mismatch, not data drift"
        )
    elif canonical_name is not None and not out["canonical_raised"]:
        out["verdict"] = "already_working"
        out["note"] = f"canonical {canonical_name!r} also succeeds"
    else:
        out["verdict"] = "native_ok"
        out["note"] = f"native {native!r} round-trips (no flagged canonical to compare)"
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(Path(__file__).with_suffix(".json")))
    ap.add_argument(
        "--only", default=None, help="comma-separated canonical slugs (default: all phytozome orgs)"
    )
    args = ap.parse_args(argv)

    targets = [c for c, r in organisms.ORGANISMS.items() if r.phytozome_int is not None]
    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        targets = [o for o in targets if o in wanted]

    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(GLOBAL_WALLTIME_S)

    results: list[dict[str, Any]] = []
    with httpx.Client(timeout=STREAM_TIMEOUT_S) as client:
        for i, canonical in enumerate(targets):
            if i:
                time.sleep(ORG_SLEEP)
            print(
                f"... probing {canonical} (phytozome_int={organisms.resolve(canonical).phytozome_int})",
                file=sys.stderr,
            )
            try:
                rec = _discover_one(client, canonical)
            except _WalltimeError:
                results.append({"organism": canonical, "verdict": "walltime"})
                print(f"[{canonical}] WALLTIME — aborting", file=sys.stderr)
                break
            except httpx.HTTPError as exc:
                rec = {"organism": canonical, "verdict": "http_error", "note": str(exc)}
            results.append(rec)
            print(
                f"[{canonical}] {rec.get('verdict')} native={rec.get('native_name')} "
                f"canonical_raised={rec.get('canonical_raised')}",
                file=sys.stderr,
            )

    Path(args.out).write_text(json.dumps(results, indent=2) + "\n")
    confirmed = sum(1 for r in results if r.get("verdict") == "namespace_mismatch_confirmed")
    print(
        f"\n{confirmed} namespace mismatches confirmed; {len(results)} organisms probed "
        f"-> {args.out}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
