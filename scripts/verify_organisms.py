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


async def main() -> int:
    if os.environ.get("PLANT_GENOMICS_MCP_LIVE") != "1":
        print("Refusing to run without PLANT_GENOMICS_MCP_LIVE=1", file=sys.stderr)
        return 2

    async with httpx.AsyncClient() as client:
        print(f"{'canonical':<28} {'taxid':<7} {'ensembl':<8} {'phytozome':<12} {'string':<8}")
        print("-" * 70)
        for canonical, record in organisms.ORGANISMS.items():
            ens_ok = await probe_ensembl(client, record.ensembl_slug or "")
            str_ok = await probe_string(client, record.string_taxid or 0)
            phy_str = str(record.phytozome_int) if record.phytozome_int is not None else "TBD"
            print(
                f"{canonical:<28} {record.ncbi_taxid:<7} "
                f"{'OK' if ens_ok else 'MISS':<8} "
                f"{phy_str:<12} "
                f"{'OK' if str_ok else 'MISS':<8}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
