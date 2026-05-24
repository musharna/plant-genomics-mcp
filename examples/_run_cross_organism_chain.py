"""Live-run v0.9+ synthesis tools against non-Arabidopsis loci and dump JSON.

Used to generate the captures embedded in cross_organism_walkthrough.md.
Demonstrates that the v0.9 multi-organism resolver routes the same synthesis
tools to backend-specific identifiers (Ensembl slug, UniProt taxid, Phytozome
proteome int, STRING taxid, Europe PMC slug) without caller changes.

Run with: PLANT_GENOMICS_MCP_LIVE=1 .venv/bin/python examples/_run_cross_organism_chain.py
"""

from __future__ import annotations

import asyncio
import json
import sys

import httpx

from plant_genomics_mcp.synthesis import (
    analyze_locus_synth,
    biological_context_synth,
)

CASES: list[tuple[str, str]] = [
    ("oryza_sativa", "Os01g0100100"),
    ("zea_mays", "Zm00001d027231"),
]


async def main() -> None:
    captures: dict[str, dict] = {}
    async with httpx.AsyncClient(timeout=900.0) as client:
        for organism, locus in CASES:
            for tool_name, coro_factory in [
                (
                    "analyze_locus_synth",
                    lambda c=client, loc=locus, o=organism: analyze_locus_synth(c, loc, organism=o),
                ),
                (
                    "biological_context_synth",
                    lambda c=client, loc=locus, o=organism: biological_context_synth(
                        c, loc, organism=o, top_n=5
                    ),
                ),
            ]:
                key = f"{organism}__{locus}__{tool_name}"
                print(f"-> {key}", file=sys.stderr)
                env = await coro_factory()
                captures[key] = env.model_dump()
                print(
                    f"   {key}: {env.elapsed_s:.1f}s, "
                    f"{sum(1 for s in env.steps if s.status == 'ok')}/{len(env.steps)} ok",
                    file=sys.stderr,
                )
    print(json.dumps(captures, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
