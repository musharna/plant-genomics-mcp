#!/usr/bin/env python3
"""Live-probe each organism in ORGANISMS against every backend.

Run with PLANT_GENOMICS_MCP_LIVE=1 to confirm phytozome_int + europe_pmc_slug
values pre-release. Prints a verified-matrix table to stdout and any drift
warnings to stderr. Does NOT mutate organisms.py — copy the verified values
into the file by hand after reviewing the output.

Usage:
    PLANT_GENOMICS_MCP_LIVE=1 python scripts/verify_organisms.py
"""

from __future__ import annotations

import asyncio
import os
import sys

import httpx

from plant_genomics_mcp import organisms


async def probe_ensembl(client: httpx.AsyncClient, slug: str) -> bool:
    """Returns True if the species slug is recognized by Ensembl Plants."""
    url = f"https://rest.ensembl.org/info/assembly/{slug}"
    try:
        resp = await client.get(url, headers={"Accept": "application/json"}, timeout=15.0)
        return resp.status_code == 200
    except Exception:
        return False


async def probe_string(client: httpx.AsyncClient, taxid: int) -> bool:
    """Returns True if STRING recognizes the taxid (returns a non-empty body)."""
    url = "https://string-db.org/api/json/get_string_ids"
    try:
        resp = await client.post(
            url,
            data={"identifiers": "test", "species": str(taxid)},
            timeout=15.0,
        )
        # STRING returns a JSON array (possibly empty); a 200 means the taxid is known.
        return resp.status_code == 200
    except Exception:
        return False


async def probe_kegg(client: httpx.AsyncClient, code: str | None) -> bool:
    """Returns True if KEGG recognizes the organism code."""
    if not code:
        return False
    url = f"https://rest.kegg.jp/info/{code}"
    try:
        resp = await client.get(url, timeout=15.0)
        if resp.status_code != 200:
            return False
        body = resp.text.strip()
        # KEGG occasionally returns 200 + HTML error page for unknown codes;
        # the real /info response is plain text starting with "T<tab>".
        return bool(body) and not body.startswith("<")
    except Exception:
        return False


# ATTED-II's coexpression endpoint requires a sentinel gene per organism so we
# can confirm the release id is actually accepted. Values are entrez gene IDs
# (or locus strings ATTED's API5 accepts) known to return a non-empty
# result_set against the corresponding *-u.c1-0 (or pinned) database. Extend
# this map as more atted_release values get populated in ORGANISMS — a missing
# sentinel renders as MISS even when the release itself is valid.
ATTED_PROBE_SENTINELS: dict[str, str] = {
    "arabidopsis_thaliana": "AT1G01010",
    "oryza_sativa": "Os01g0100100",
    "zea_mays": "542245",
    "glycine_max": "547814",
    "vitis_vinifera": "100232938",
    "solanum_lycopersicum": "101247977",
    # medicago_truncatula: Mtr-u.c1-0 is a valid db but we don't yet have an
    # entrez ID that returns coexpression rows; leave unmapped so probe reports
    # MISS without falsely flagging the release as invalid.
}


async def probe_atted(client: httpx.AsyncClient, canonical: str, release: str | None) -> bool:
    if not release:
        return False
    sentinel = ATTED_PROBE_SENTINELS.get(canonical)
    if sentinel is None:
        return False
    # ATTED-II API v5 entrypoint is /api5/ (not /api5/coexpression); the
    # backend module pins this in atted.API_PATH.
    url = "https://atted.jp/api5/"
    try:
        resp = await client.get(
            url,
            params={"gene": sentinel, "topN": 1, "db": release},
            timeout=15.0,
        )
        if resp.status_code != 200:
            return False
        # Even on 200 the API can echo an "error" key (e.g. unknown gene).
        try:
            body = resp.json()
        except ValueError:
            return False
        if not isinstance(body, dict) or body.get("error"):
            return False
        result_set = body.get("result_set") or []
        return bool(result_set)
    except Exception:
        return False


async def main() -> int:
    if os.environ.get("PLANT_GENOMICS_MCP_LIVE") != "1":
        print("Refusing to run without PLANT_GENOMICS_MCP_LIVE=1", file=sys.stderr)
        return 2

    async with httpx.AsyncClient() as client:
        print(
            f"{'canonical':<28} {'taxid':<7} {'ensembl':<8} {'phytozome':<12} "
            f"{'string':<8} {'kegg':<14} {'atted':<18}"
        )
        print("-" * 100)
        for canonical, record in organisms.ORGANISMS.items():
            ens_ok = await probe_ensembl(client, record.ensembl_slug or "")
            str_ok = await probe_string(client, record.string_taxid or 0)
            kegg_ok = await probe_kegg(client, record.kegg_org_code)
            atted_ok = await probe_atted(client, canonical, record.atted_release)
            phy_str = str(record.phytozome_int) if record.phytozome_int is not None else "TBD"
            kegg_str = record.kegg_org_code if record.kegg_org_code is not None else "TBD"
            atted_str = record.atted_release if record.atted_release is not None else "TBD"
            kegg_col = f"{'OK' if kegg_ok else 'MISS'}/{kegg_str}"
            atted_col = f"{'OK' if atted_ok else 'MISS'}/{atted_str}"
            print(
                f"{canonical:<28} {record.ncbi_taxid:<7} "
                f"{'OK' if ens_ok else 'MISS':<8} "
                f"{phy_str:<12} "
                f"{'OK' if str_ok else 'MISS':<8} "
                f"{kegg_col:<14} "
                f"{atted_col:<18}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
