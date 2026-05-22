"""Live-run all four v0.8 synthesis tools against AT1G01010 and dump JSON.

Used to generate the captures embedded in v0.8_synthesis_walkthrough.md.
Run with: PLANT_GENOMICS_MCP_LIVE=1 python examples/_run_synthesis_chain.py
"""

from __future__ import annotations

import asyncio
import json
import sys

import httpx

from plant_genomics_mcp.synthesis import (
    analyze_locus_synth,
    biological_context_synth,
    consensus_homologs,
    find_homologs_synth,
)

# AT1G01010 N-terminal protein sequence (Q0WV96)
AT1G01010_SEQ = (
    "MEDQVGFGFRPNDEELVGHYLRNKIEGNTSRDVEVAISEVNICSYDPWNLRFQSKYKSRDAMWYFFSRRENNKGNRQSRTTVSGKWKLTGES"
)


async def main() -> None:
    captures: dict[str, dict] = {}
    async with httpx.AsyncClient(timeout=900.0) as client:
        for label, coro in [
            ("analyze_locus_synth", analyze_locus_synth(client, "AT1G01010")),
            ("biological_context_synth", biological_context_synth(client, "AT1G01010", top_n=5)),
            (
                "find_homologs_synth",
                find_homologs_synth(client, AT1G01010_SEQ, program="blastp", top_n=5),
            ),
            ("consensus_homologs", consensus_homologs(client, "AT1G01010", top_n=5)),
        ]:
            print(f"-> {label}", file=sys.stderr)
            env = await coro
            captures[label] = env.model_dump()
            print(
                f"   {label}: {env.elapsed_s:.1f}s, "
                f"{sum(1 for s in env.steps if s.status == 'ok')}/{len(env.steps)} ok",
                file=sys.stderr,
            )
    print(json.dumps(captures, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
