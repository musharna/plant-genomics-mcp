import asyncio
import json
import os
import re
import sys
from pathlib import Path

import pytest

from examples.arf_family.mcp_client import SERVER_CMD, McpClient

FAKE_SERVER = Path(__file__).parent / "_fake_mcp_server.py"
FAKE_OK = [sys.executable, str(FAKE_SERVER), "ok"]
FAKE_INIT_ERROR = [sys.executable, str(FAKE_SERVER), "init-error"]


def run(coro):
    return asyncio.run(coro)


def test_lists_all_tools_over_real_stdio():
    async def go():
        c = McpClient(SERVER_CMD)
        try:
            await c.start()
            return await c.list_tools()
        finally:
            await c.close()

    names = {t["name"] for t in run(go())}
    assert len(names) == 54
    assert {"interpro_domains", "gramene_homologs", "gene_report"} <= names


def test_unknown_tool_is_reported_not_raised():
    # No tool in this server is network-free (every tool.annotations sets
    # open_world_hint=True — confirmed by grep), and every live-network
    # tools/call in this repo is gated behind PLANT_GENOMICS_MCP_LIVE=1 so
    # both the default `pytest -q` run and the stdio smoke run
    # (PLANT_GENOMICS_MCP_STDIO_SMOKE=1, tests/test_server_stdio.py) stay
    # network-free. This test runs
    # unconditionally in `pytest -q`, so the positive control here is
    # list_tools() succeeding (real execution over the real stdio
    # subprocess, no external network) in the same client session as the
    # negative assertion below, rather than a live external API call.
    async def go():
        c = McpClient(SERVER_CMD)
        try:
            await c.start()
            tools = await c.list_tools()
            unknown = await c.call("no_such_tool", {})
            return tools, unknown
        finally:
            await c.close()

    tools, r = run(go())
    assert len(tools) == 54
    assert r.ok is False and r.error


def test_start_records_server_info_from_the_initialize_response():
    # The dossier runner stamps every calls.jsonl row with the version of
    # the server that answered it. That version must come from the wire —
    # from this client's own `initialize` handshake — and not from
    # `import plant_genomics_mcp.__version__`, which reports whatever
    # package happens to be importable in the runner's interpreter rather
    # than what the subprocess on the other end of the pipe is running.
    #
    # Real execution (the actual server over the actual stdio transport)
    # plus a fixture whose value is known exactly: the fake server answers
    # `initialize` with a serverInfo this repo controls, so the assertion
    # below pins a specific value instead of "something truthy".
    async def go(cmd):
        c = McpClient(cmd)
        try:
            await c.start()
            return c.server_info
        finally:
            await c.close()

    assert run(go(FAKE_OK)) == {"name": "fake-mcp-server", "version": "0"}

    real = run(go(SERVER_CMD))
    assert real is not None, "the real server's initialize response carried no serverInfo"
    assert real.get("name") == "plant-genomics-mcp", real
    assert re.fullmatch(r"\d+\.\d+\.\d+.*", str(real.get("version"))), real

    # Positive control for the attribute itself: it must not simply be a
    # constant that survives never being set. A client that never completed
    # a handshake has no server info.
    assert McpClient(SERVER_CMD).server_info is None


def test_start_times_out_on_a_silent_server():
    async def go():
        c = McpClient([sys.executable, "-c", "import time; time.sleep(30)"], timeout_s=1)
        with pytest.raises(TimeoutError):
            await c.start()
        return c

    c = run(go())
    assert c._proc is not None
    assert c._proc.returncode is not None, "child process was left running after the timeout"


def test_close_after_failed_start_does_not_raise():
    # start() already terminates+reaps the process on a failed handshake;
    # close() must tolerate being called again on top of that (callers use
    # start()/close() in a try/finally, as the other tests in this file do).
    async def go():
        c = McpClient([sys.executable, "-c", "import time; time.sleep(30)"], timeout_s=1)
        with pytest.raises(TimeoutError):
            await c.start()
        await c.close()
        return c

    c = run(go())
    assert c._proc.returncode is not None


def test_call_returns_payload_on_success_and_reports_unknown_tool():
    # Positive and negative controls in the same session: call() is a client
    # method in its own right (not covered by list_tools()'s real-transport
    # check), so its success branch — CallResult(True, json.loads(text), ...)
    # — needs its own legitimate-call assertion, paired here with the
    # unknown-tool negative case.
    async def go():
        c = McpClient(FAKE_OK)
        try:
            await c.start()
            ok_result = await c.call("echo", {"locus": "AT1G19850"})
            unknown_result = await c.call("no_such_tool", {})
            return ok_result, unknown_result
        finally:
            await c.close()

    ok_result, unknown_result = run(go())
    assert ok_result.ok is True
    assert ok_result.payload == {"echo": {"locus": "AT1G19850"}}
    assert ok_result.error is None
    assert ok_result.n_bytes > 0
    assert ok_result.elapsed_s >= 0
    assert unknown_result.ok is False


def test_start_raises_runtime_error_on_initialize_error_and_reaps_process():
    async def go():
        c = McpClient(FAKE_INIT_ERROR)
        with pytest.raises(RuntimeError, match="fake init failure"):
            await c.start()
        return c

    c = run(go())
    assert c._proc.returncode is not None, (
        "child process was left running after an initialize error"
    )


@pytest.mark.skipif(
    os.environ.get("PLANT_GENOMICS_MCP_LIVE") != "1",
    reason="set PLANT_GENOMICS_MCP_LIVE=1 to run (calls InterPro and PANTHER live)",
)
def test_interpro_domains_separates_arf_from_adp_ribosylation_factor_over_real_stdio():
    # Real-execution control against live InterPro, gated like the repo's
    # other live tests (PLANT_GENOMICS_MCP_LIVE=1), not like the network-free
    # stdio smoke test.
    # The ARF-vs-ADP-ribosylation-factor discriminator is a structured
    # InterPro accession, not Ensembl free text — a prior version of this
    # test asserted a regex against free-text wording that live data
    # contradicted (see LESSONS.md). AT1G19850 (ARF5/MONOPTEROS, an auxin
    # response factor) carries InterPro accession IPR010525; AT1G23490 (an
    # ADP-ribosylation factor that shares the symbol "ARF1" but is a
    # different gene family) does not.
    async def go():
        c = McpClient(SERVER_CMD)
        try:
            await c.start()
            arf = await c.call(
                "interpro_domains",
                {"locus": "AT1G19850", "organism": "arabidopsis_thaliana"},
            )
            adp_ribosylation_factor = await c.call(
                "interpro_domains",
                {"locus": "AT1G23490", "organism": "arabidopsis_thaliana"},
            )
            return arf, adp_ribosylation_factor
        finally:
            await c.close()

    arf, adp_ribosylation_factor = run(go())
    assert arf.ok is True, f"call failed: {arf.error}"
    assert "IPR010525" in json.dumps(arf.payload)
    # Negative control: proves the discriminator can discriminate, and that
    # the absence of IPR010525 below is not just a failed call.
    assert adp_ribosylation_factor.ok is True, f"call failed: {adp_ribosylation_factor.error}"
    assert "IPR010525" not in json.dumps(adp_ribosylation_factor.payload)
