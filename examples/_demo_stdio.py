"""Paced demo of the stdio MCP transport — used to record examples/assets/demo.svg.

Re-record with:
    PATH=$HOME/.local/bin:$PATH \
      asciinema rec --overwrite -c "python3 examples/_demo_stdio.py" /tmp/demo.cast
    svg-term --in /tmp/demo.cast --out examples/assets/demo.svg \
      --window --width 88 --height 28
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

C_DIM = "\033[2m"
C_RST = "\033[0m"
C_GRN = "\033[32m"
C_CYA = "\033[36m"
C_YEL = "\033[33m"
C_BOLD = "\033[1m"


def banner(line: str) -> None:
    print(f"{C_BOLD}{C_GRN}{line}{C_RST}", flush=True)


def prompt(line: str) -> None:
    print(f"{C_DIM}$ {C_RST}{line}", flush=True)


def send_arrow(label: str) -> None:
    print(f"{C_CYA}→{C_RST} {label}", flush=True)


def recv_arrow(label: str) -> None:
    print(f"{C_GRN}←{C_RST} {label}", flush=True)


async def main() -> None:
    proc = await asyncio.create_subprocess_exec(
        ".venv/bin/plant-genomics-mcp",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        limit=8 * 1024 * 1024,
    )
    assert proc.stdin is not None and proc.stdout is not None

    async def rpc(req: dict) -> dict | None:
        proc.stdin.write((json.dumps(req) + "\n").encode())
        await proc.stdin.drain()
        if "id" not in req:
            return None
        line = await proc.stdout.readline()
        return json.loads(line)

    banner("🌱 plant-genomics-mcp — stdio MCP demo")
    print(flush=True)
    time.sleep(0.8)

    prompt("plant-genomics-mcp  # boot stdio server")
    time.sleep(0.6)

    send_arrow("initialize")
    resp = await rpc(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "demo", "version": "0"},
            },
        }
    )
    from plant_genomics_mcp import __version__ as pkg_ver

    recv_arrow(f"serverInfo: plant-genomics-mcp v{pkg_ver}")
    await rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
    time.sleep(0.7)
    print(flush=True)

    send_arrow("tools/list")
    resp = await rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = resp["result"]["tools"]
    recv_arrow(f"{len(tools)} tools registered:")
    time.sleep(0.4)
    sample = [
        "ensembl_plants_lookup_locus",
        "resolve_locus_to_uniprot",
        "locus_literature",
        "blast_sequence",
        "string_interactions",
        "atted_coexpression",
        "analyze_locus_synth",
        "biological_context_synth",
    ]
    by_name = {t["name"]: t for t in tools}
    for name in sample:
        if name in by_name:
            print(f"  {C_YEL}•{C_RST} {name}", flush=True)
            time.sleep(0.18)
    print(f"  {C_DIM}… +{len(tools) - len(sample)} more{C_RST}", flush=True)
    time.sleep(0.7)
    print(flush=True)

    prompt("# resources/read pgmcp://organisms/coverage")
    time.sleep(0.5)
    send_arrow("resources/read")
    resp = await rpc(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "resources/read",
            "params": {"uri": "pgmcp://organisms/coverage"},
        }
    )
    body = resp["result"]["contents"][0]["text"]
    head = "\n".join(body.splitlines()[:6])
    recv_arrow("coverage matrix (first 6 lines):")
    print(C_DIM + head + C_RST, flush=True)
    time.sleep(1.2)
    print(flush=True)
    banner("done.")
    time.sleep(0.5)

    proc.stdin.close()
    await proc.wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
