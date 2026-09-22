"""Fake stdio MCP server — test helper only, never imported by product code.

Answers the MCP handshake and a small set of tools over stdio, stdlib only,
so tests can exercise `McpClient` and `examples/arf_family/verify_genes.py`
without spawning the real `plant-genomics-mcp` server (which always
succeeds against the network) or touching the network.

Usage: ``python3 _fake_mcp_server.py <mode>``

Modes:
  ok         — ``initialize`` succeeds. ``tools/call`` for tool ``echo``
               returns ``{"echo": <arguments>}`` as its JSON payload; any
               other tool name returns an ``isError`` result with a
               plain-text message.
  init-error — ``initialize`` responds with a JSON-RPC ``"error"`` object.
  chain      — ``initialize`` succeeds. Every ``tools/call`` returns a small
               ok payload ``{"tool": <name>, "args": <arguments>}`` except
               ``CHAIN_FAILING_TOOL``, which returns an ``isError`` result,
               for `tests/test_arf_chain.py`'s runner tests.
  arf        — ``initialize`` succeeds. ``tools/call`` serves canned
               ``interpro_domains`` / ``panther_family`` payloads keyed by
               ``arguments["locus"]`` from ``ARF_FIXTURES`` (see below), for
               `tests/test_verify_genes.py`. Any other tool name, or a
               locus not in the fixture table, returns an ``isError``
               result.

An unrecognised mode string is a hard error (exit 2) at startup — it must
never silently fall back to ``ok`` behaviour, which would let a typo in a
test's mode argument pass as the wrong fixture set instead of failing loud.
"""

from __future__ import annotations

import json
import sys

_MODES = {"ok", "init-error", "arf", "chain"}

# `chain` mode: every tools/call succeeds with a tiny stub echoing its
# arguments, EXCEPT this one tool, which returns an isError result. The runner
# must therefore produce exactly one auto gap row per gene per run, beside
# fifteen calls that produce none — so a test can assert both that a failure
# IS logged and that a success is NOT, rather than only that some rows exist.
CHAIN_FAILING_TOOL = "kegg_pathways"

# Keyed by locus. Shapes mirror the real interpro_domains / panther_family
# tool payloads closely enough for `verify_genes.py` to read: `domains` (list
# of dicts carrying `accession`/`interpro` InterPro ids, checked as a JSON
# substring the same way the real-stdio test in test_arf_mcp_client.py does)
# and `subfamily_id` (a single structured field). `None` for a tool means
# that tool call fails for this locus (isError), for the "a failed call must
# not read as absence" case. A `list` value is a legitimately-decoded,
# ok=True non-dict payload (verify_genes.py's payload-shape defect).
ARF_FIXTURES: dict[str, dict[str, dict | list | None]] = {
    "GOOD_ARF_PB1": {
        "interpro_domains": {
            "locus": "GOOD_ARF_PB1",
            "found": True,
            "domain_count": 2,
            "truncated": False,
            "domains": [
                {"accession": "IPR010525", "interpro": "IPR010525"},
                {"accession": "IPR033389", "interpro": "IPR033389"},
            ],
            "count_by_type": {},
        },
        "panther_family": {
            "locus": "GOOD_ARF_PB1",
            "found": True,
            "family_id": "PTHR31384",
            "subfamily_id": "PTHR31384:SF10",
        },
    },
    "GOOD_ARF_NO_PB1": {
        "interpro_domains": {
            "locus": "GOOD_ARF_NO_PB1",
            "found": True,
            "domain_count": 1,
            "truncated": False,
            "domains": [{"accession": "IPR010525", "interpro": "IPR010525"}],
            "count_by_type": {},
        },
        "panther_family": {
            "locus": "GOOD_ARF_NO_PB1",
            "found": True,
            "family_id": "PTHR31384",
            "subfamily_id": "PTHR31384:SF193",
        },
    },
    "NOT_ARF": {
        "interpro_domains": {
            "locus": "NOT_ARF",
            "found": True,
            "domain_count": 1,
            "truncated": False,
            "domains": [{"accession": "IPR005225", "interpro": "IPR005225"}],
            "count_by_type": {},
        },
        "panther_family": {
            "locus": "NOT_ARF",
            "found": True,
            "family_id": "PTHR11711",
            "subfamily_id": "PTHR11711:SF462",
        },
    },
    "CALL_FAILS": {
        "interpro_domains": None,
        "panther_family": None,
    },
    # `payload` is `dict | None` per CallResult's own type, but every helper
    # in verify_genes.py assumed `dict`. A `list` here is not the "call
    # failed" sentinel (that's Python `None`, used by CALL_FAILS above) — it
    # is a legitimately-decoded, ok=True JSON value that just isn't an
    # object, which `json.loads` on a server response can produce.
    "INTERPRO_NON_DICT_PAYLOAD": {
        "interpro_domains": ["not", "a", "dict"],
        "panther_family": {
            "locus": "INTERPRO_NON_DICT_PAYLOAD",
            "found": True,
            "family_id": "PTHR31384",
            "subfamily_id": "PTHR31384:SF10",
        },
    },
    "PANTHER_NON_DICT_PAYLOAD": {
        "interpro_domains": {
            "locus": "PANTHER_NON_DICT_PAYLOAD",
            "found": True,
            "domain_count": 2,
            "truncated": False,
            "domains": [
                {"accession": "IPR010525", "interpro": "IPR010525"},
                {"accession": "IPR033389", "interpro": "IPR033389"},
            ],
            "count_by_type": {},
        },
        "panther_family": ["not", "a", "dict"],
    },
}


def _respond(req_id: object, *, result: dict | None = None, error: dict | None = None) -> None:
    resp: dict = {"jsonrpc": "2.0", "id": req_id}
    if error is not None:
        resp["error"] = error
    else:
        resp["result"] = result
    sys.stdout.write(json.dumps(resp) + "\n")
    sys.stdout.flush()


def _text_result(payload: object) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload)}], "isError": False}


def _error_result(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def main() -> None:
    if len(sys.argv) <= 1:
        sys.stderr.write("_fake_mcp_server.py: a mode argument is required\n")
        raise SystemExit(2)
    mode = sys.argv[1]
    if mode not in _MODES:
        sys.stderr.write(
            f"_fake_mcp_server.py: unknown mode {mode!r}, expected one of {sorted(_MODES)}\n"
        )
        raise SystemExit(2)

    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        req = json.loads(line)
        method = req.get("method")
        req_id = req.get("id")

        if req_id is None:
            # Notification (e.g. notifications/initialized) — no response.
            continue

        if method == "initialize":
            if mode == "init-error":
                _respond(
                    req_id,
                    error={"code": -32000, "message": "fake init failure"},
                )
            else:
                _respond(
                    req_id,
                    result={
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "serverInfo": {"name": "fake-mcp-server", "version": "0"},
                    },
                )
        elif method == "tools/call":
            params = req.get("params", {})
            name = params.get("name")
            args = params.get("arguments", {})
            if mode == "chain":
                if name == CHAIN_FAILING_TOOL:
                    _respond(req_id, result=_error_result(f"fake chain failure for {name}"))
                else:
                    _respond(req_id, result=_text_result({"tool": name, "args": args}))
            elif mode == "arf" and name in ("interpro_domains", "panther_family"):
                locus = args.get("locus")
                fixture = ARF_FIXTURES.get(locus, {}).get(name)
                if fixture is None:
                    _respond(req_id, result=_error_result(f"no fixture for {name}({locus!r})"))
                else:
                    _respond(req_id, result=_text_result(fixture))
            elif name == "echo":
                text = json.dumps({"echo": args})
                _respond(
                    req_id,
                    result={"content": [{"type": "text", "text": text}], "isError": False},
                )
            else:
                _respond(
                    req_id,
                    result={
                        "content": [{"type": "text", "text": f"unknown tool: {name}"}],
                        "isError": True,
                    },
                )
        else:
            _respond(req_id, error={"code": -32601, "message": f"method not found: {method}"})


if __name__ == "__main__":
    main()
