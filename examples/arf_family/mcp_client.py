"""Minimal stdio MCP client — calls the server the way an agent does."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import dataclass

SERVER_CMD = [sys.executable, "-m", "plant_genomics_mcp.server"]


@dataclass
class CallResult:
    ok: bool
    payload: dict | None
    error: str | None
    elapsed_s: float
    n_bytes: int


class McpClient:
    def __init__(self, server_cmd: list[str], timeout_s: float = 900.0) -> None:
        self._cmd = server_cmd
        self._timeout_s = timeout_s
        self._proc: asyncio.subprocess.Process | None = None
        self._id = 0
        # The `serverInfo` object the server sent back in its `initialize`
        # response — the only place the version of the process on the other
        # end of this pipe is available. `None` until `start()` completes a
        # handshake; a client that never handshook must not look as though
        # it knows what it is talking to.
        self.server_info: dict | None = None

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *self._cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=64 * 1024 * 1024,
        )
        try:
            resp, _ = await self._rpc(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "arf-dossier", "version": "0"},
                },
            )
            if "error" in resp:
                raise RuntimeError(f"initialize failed: {resp['error']!r}")
            self.server_info = resp.get("result", {}).get("serverInfo")
            await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except Exception:
            self._proc.terminate()
            await self._proc.wait()
            raise

    async def _send(self, req: dict) -> None:
        assert self._proc and self._proc.stdin
        self._proc.stdin.write((json.dumps(req) + "\n").encode())
        await self._proc.stdin.drain()

    async def _rpc(self, method: str, params: dict | None = None) -> tuple[dict, int]:
        assert self._proc and self._proc.stdout
        self._id += 1
        req = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            req["params"] = params
        await self._send(req)
        try:
            line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=self._timeout_s)
        except TimeoutError as e:
            raise TimeoutError(
                f"stdio MCP client timed out after {self._timeout_s}s waiting for a "
                f"response to {method} {params!r}"
            ) from e
        if not line:
            raise RuntimeError(f"server closed stdout during {method} {params!r}")
        return json.loads(line), len(line)

    async def list_tools(self) -> list[dict]:
        resp, _ = await self._rpc("tools/list")
        return resp["result"]["tools"]

    async def call(self, name: str, args: dict) -> CallResult:
        t0 = time.monotonic()
        resp, n = await self._rpc("tools/call", {"name": name, "arguments": args})
        dt = time.monotonic() - t0
        if "error" in resp:
            return CallResult(False, None, json.dumps(resp["error"]), dt, n)
        result = resp["result"]
        text = "".join(c.get("text", "") for c in result.get("content", []))
        if result.get("isError"):
            return CallResult(False, None, text, dt, n)
        return CallResult(True, json.loads(text), None, dt, n)

    async def close(self) -> None:
        # returncode is None only while the process is still alive — start()
        # already terminates and reaps the process itself on a failed
        # handshake, so close() must tolerate being called again afterwards
        # (callers use start()/close() in a try/finally).
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            await self._proc.wait()
