"""Real-execution integration check for genomics-mcp.

Per CLAUDE.md feedback_real_execution_testing: synthetic-fixture unit
tests can't see process boundaries. This script:

  1. Spawns the genomics-mcp server as a subprocess via stdio.
  2. Initializes the MCP protocol against it (real client/server handshake).
  3. Calls ListTools — verifies the 9 tools are registered as the wire format.
  4. Calls ensembl_lookup_id against the real rest.ensembl.org for a
     well-known stable ID and parses the result.
  5. Calls kegg_find against real rest.kegg.jp.

Exits 0 on success, non-zero with a descriptive error otherwise.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

VENV_BIN = Path(__file__).resolve().parent.parent / ".venv" / "bin" / "genomics-mcp"

EXPECTED_TOOLS = {
    "ensembl_lookup_id",
    "ensembl_lookup_symbol",
    "ensembl_sequence_by_id",
    "ensembl_xrefs_by_id",
    "ensembl_homology_by_id",
    "kegg_find",
    "kegg_get",
    "kegg_link",
    "kegg_conv",
}

# BRCA2 — extremely stable Ensembl human gene ID, used for golden-path checks
BRCA2 = "ENSG00000139618"


async def _run() -> int:
    if not VENV_BIN.exists():
        print(f"FAIL: {VENV_BIN} not found", file=sys.stderr)
        return 2

    params = StdioServerParameters(command=str(VENV_BIN), args=[], env=None)

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            print("[1/5] initialising MCP protocol …", flush=True)
            await session.initialize()

            print("[2/5] listing tools …", flush=True)
            tools_resp = await session.list_tools()
            actual = {t.name for t in tools_resp.tools}
            missing = EXPECTED_TOOLS - actual
            extra = actual - EXPECTED_TOOLS
            if missing or extra:
                print(f"FAIL: tool surface drift. missing={missing} extra={extra}", file=sys.stderr)
                return 3
            print(f"      {len(actual)} tools registered ✓", flush=True)

            print(f"[3/5] live ensembl_lookup_id({BRCA2}) …", flush=True)
            result = await session.call_tool("ensembl_lookup_id", arguments={"ensembl_id": BRCA2})
            if result.isError:
                print(
                    f"FAIL: ensembl call returned isError. content={result.content}",
                    file=sys.stderr,
                )
                return 4
            payload_text = _first_text(result.content)
            payload = json.loads(payload_text)
            if payload.get("display_name") != "BRCA2":
                print(f"FAIL: expected display_name=BRCA2, got {payload}", file=sys.stderr)
                return 5
            print(
                f"      display_name={payload['display_name']} biotype={payload['biotype']} ✓",
                flush=True,
            )

            print("[4/5] live kegg_find(genes, BRCA2) …", flush=True)
            result = await session.call_tool(
                "kegg_find", arguments={"database": "genes", "query": "BRCA2"}
            )
            if result.isError:
                print(
                    f"FAIL: kegg call returned isError. content={result.content}", file=sys.stderr
                )
                return 6
            payload_text = _first_text(result.content)
            rows = json.loads(payload_text)
            if not any("BRCA2" in r.get("value", "").upper() for r in rows):
                print(f"FAIL: no BRCA2 hit in {rows[:3]}", file=sys.stderr)
                return 7
            print(f"      {len(rows)} hits, top: {rows[0]['id']} ✓", flush=True)

            print("[5/5] error-path: ensembl_lookup_id(NOTREAL) …", flush=True)
            result = await session.call_tool(
                "ensembl_lookup_id", arguments={"ensembl_id": "NOTREAL"}
            )
            payload_text = _first_text(result.content)
            if "error" not in payload_text.lower():
                print(f"FAIL: expected error text, got {payload_text}", file=sys.stderr)
                return 8
            print("      error wrapped as text content ✓", flush=True)

    print("\nALL REAL-EXEC CHECKS PASSED ✓")
    return 0


def _first_text(content: list[types.ContentBlock]) -> str:
    for block in content:
        if isinstance(block, types.TextContent):
            return block.text
    raise AssertionError(f"no TextContent in {content}")


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
