"""Untrusted tool arguments are checked against the schema the server advertises.

Every tool in ``server.TOOLS`` ships a full JSON Schema — property types,
``required``, ``additionalProperties: False`` — and nothing enforced it. The
mcp SDK's lowlevel ``Server`` does not validate ``arguments`` against a tool's
``inputSchema`` before handing them to the call handler, so a JSON value of
the wrong shape reached helpers annotated ``str | int`` and crashed on
whatever they touched first.

The nightly fuzz (issue #118) found one instance: ``organism: 3.5`` raising
``AttributeError: 'float' object has no attribute 'strip'`` out of
``organisms.resolve``. The instance is not the mechanism — the same hole was
open on every string field, because ``validators.assert_valid_locus``,
``assert_valid_agi``, ``assert_valid_jaspar_matrix_id`` and
``assert_no_path_metachars`` all raise a bare ``TypeError`` on a non-``str``.
Guarding ``resolve`` alone would have closed one door in a corridor.

So the check lives at the boundary the untrusted arguments cross,
``server._call_tool``, and is driven by each tool's own declared schema: a
tool added later inherits it by declaring a schema, not by remembering a
guard. ``_dispatch`` is deliberately left unguarded — it is the trusted
in-process entry point, and its ``unknown tool`` contract is asserted in
tests/test_server_dispatch.py.

Each test below pairs the refusal with the legitimate call in the same test:
a validator that refused everything would satisfy a one-sided assertion.
"""

from __future__ import annotations

import socket
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator
from mcp import types

from plant_genomics_mcp import ensembl_plants, server
from tests.test_progress_bridge import _Ctx, _params, _RecordingSession

# The tool used for the behavioural tests: two declared properties, one
# required string and one optional ``["string", "integer"]`` — the exact
# shape the fuzz crash arrived through.
TOOL = "ensembl_plants_lookup_locus"


async def _call(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
    """Drive the real call handler, the way a client's request reaches it."""
    ctx = _Ctx(session=_RecordingSession(), meta={})
    return await server._call_tool(cast(Any, ctx), _params(name, arguments))


def _text(result: types.CallToolResult) -> str:
    """The single text block of a result, checked as the wire contract has it."""
    assert len(result.content) == 1
    block = result.content[0]
    assert block.type == "text"
    return block.text


@pytest.fixture
def recorded_backend(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """Record every backend call so "never reached the backend" is assertable."""
    calls: list[tuple[str, dict[str, Any]]] = []

    async def ok(client: Any, locus: str, **kwargs: Any) -> dict[str, Any]:
        calls.append((locus, kwargs))
        return {"locus": locus}

    monkeypatch.setattr(ensembl_plants, "lookup_locus", ok)
    return calls


# A JSON value can decode to any of these; none of them is a string or an
# integer, and every one of them reached ``organisms.resolve`` before this.
# ``True`` is here because JSON Schema does not count a boolean as an integer
# even though Python's ``isinstance(True, int)`` does — the old code returned
# OrganismNotFound for it by accident, not by design.
@pytest.mark.parametrize("bad_organism", [3.5, None, [1, 2], {"a": 1}, True])
@pytest.mark.asyncio
async def test_a_wrong_typed_argument_is_refused_before_any_backend_runs(
    bad_organism: Any, recorded_backend: list[tuple[str, dict[str, Any]]]
) -> None:
    """Issue #118's crash, at the boundary it actually arrives through.

    The assertion is not "an error came back" — a leaked ``AttributeError``
    also comes back as an error result, which is exactly how this shipped.
    It is that the error carries the documented ``[ClassName]`` prefix, names
    the tool and the offending field, and that no backend ran.
    """
    err = await _call(TOOL, {"locus": "AT1G01010", "organism": bad_organism})

    assert err.is_error is True
    text = _text(err)
    assert text.startswith("[InvalidArguments] "), text
    assert TOOL in text and "organism" in text, text
    # The old failure mode, spelled out so this cannot pass on it again.
    assert "strip" not in text and "AttributeError" not in text, text
    assert recorded_backend == []

    # Positive control, same test: the identical call with a value the schema
    # allows reaches the backend untouched.
    res = await _call(TOOL, {"locus": "AT1G01010", "organism": "oryza_sativa"})
    assert not res.is_error, _text(res)
    assert recorded_backend == [("AT1G01010", {"organism": "oryza_sativa"})]


@pytest.mark.asyncio
async def test_a_missing_required_argument_names_the_argument(
    recorded_backend: list[tuple[str, dict[str, Any]]],
) -> None:
    """``args["locus"]`` used to raise ``KeyError``, which reaches the client
    as the bare text ``'locus'`` — no type, no tool, no hint."""
    err = await _call(TOOL, {})

    assert err.is_error is True
    text = _text(err)
    assert text.startswith("[InvalidArguments] "), text
    assert "locus" in text, text
    assert recorded_backend == []

    res = await _call(TOOL, {"locus": "AT1G01010"})
    assert not res.is_error, _text(res)
    assert recorded_backend == [("AT1G01010", {"organism": "arabidopsis_thaliana"})]


@pytest.mark.asyncio
async def test_an_undeclared_argument_is_refused_rather_than_ignored(
    recorded_backend: list[tuple[str, dict[str, Any]]],
) -> None:
    """A typo in an optional argument silently ran against the default.

    ``organsim`` is not ``organism``: every schema here declares
    ``additionalProperties: False``, so the call is a contract violation, but
    before enforcement it ran happily and answered about Arabidopsis.
    """
    err = await _call(TOOL, {"locus": "AT1G01010", "organsim": "oryza_sativa"})

    assert err.is_error is True
    text = _text(err)
    assert text.startswith("[InvalidArguments] "), text
    assert "organsim" in text, text
    assert recorded_backend == []

    res = await _call(TOOL, {"locus": "AT1G01010", "organism": "oryza_sativa"})
    assert not res.is_error, _text(res)
    assert recorded_backend == [("AT1G01010", {"organism": "oryza_sativa"})]


def test_every_advertised_tool_has_an_enforceable_schema() -> None:
    """Coverage lock: the guard is only as wide as the schema registry.

    A tool added to ``TOOLS`` without a strict schema would be dispatched with
    unchecked arguments and no test would notice, so the three properties the
    enforcement leans on are asserted for all of them here.
    """
    assert {tool.name for tool in server.TOOLS} == set(server._TOOL_SCHEMAS)
    assert len(server.TOOLS) == len(server._TOOL_SCHEMAS) > 0

    for tool in server.TOOLS:
        schema = tool.input_schema
        Draft202012Validator.check_schema(schema)
        assert schema.get("additionalProperties") is False, tool.name
        assert schema.get("required"), tool.name


@pytest.mark.parametrize("tool", server.TOOLS, ids=lambda t: str(t.name))
@pytest.mark.asyncio
async def test_no_tool_accepts_an_undeclared_argument(tool: types.Tool) -> None:
    """The same refusal, driven through all 50 schemas.

    Safe to run without mocking any backend: every schema declares required
    properties, so each of these calls is refused during validation and
    nothing dispatches. If that ever stops being true this test starts making
    live network calls and the schema lock above fails first, by design.
    """
    err = await _call(str(tool.name), {"__not_a_declared_argument__": 1})

    assert err.is_error is True
    assert _text(err).startswith("[InvalidArguments] "), _text(err)


@pytest.mark.asyncio
async def test_the_real_dispatch_arm_refuses_the_value_instead_of_crashing_in_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real execution: no backend stub, the arm a client's call actually runs.

    The mocked tests above cannot see issue #118's crash — with a stub in
    place the float sailed through to the stub and the call *succeeded*. The
    crash lived one layer deeper, inside the real ``ensembl_plants`` arm,
    where ``organisms.resolve(3.5)`` raised ``AttributeError`` and
    ``_call_tool`` returned it as the bare text ``'float' object has no
    attribute 'strip'`` — an error result, so a test asserting only
    ``is_error`` passes on the bug.

    Sockets are blocked so this can never become a live call: a well-formed
    call that got past validation fails at the socket instead of going to
    Ensembl. That is the positive control below — the legitimate call must
    fail for a *different* reason than the malformed one, which is what
    proves validation let it through rather than refusing everything. httpx
    runs its transport inside a TaskGroup, so the guard's own message is
    wrapped by the time it reaches the wire; the load-bearing assertion is
    that the failure is not an ``InvalidArguments`` refusal.
    """

    def _no_network(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", _no_network)

    err = await _call(TOOL, {"locus": "AT1G01010", "organism": 3.5})
    assert err.is_error is True
    text = _text(err)
    assert text.startswith("[InvalidArguments] "), text
    assert "strip" not in text and "network access attempted" not in text, text

    passed = await _call(TOOL, {"locus": "AT1G01010", "organism": "oryza_sativa"})
    assert passed.is_error is True
    assert not _text(passed).startswith("[InvalidArguments] "), _text(passed)


@pytest.mark.asyncio
async def test_an_unknown_tool_still_reports_itself_as_unknown() -> None:
    """Validation must not swallow the unknown-tool path.

    There is no schema to check an unknown name against; the call has to fall
    through to ``_dispatch``, whose ``unknown tool:`` message is the contract
    tests/test_server_dispatch.py pins.
    """
    err = await _call("not_a_real_tool", {"locus": "AT1G01010"})

    assert err.is_error is True
    assert "unknown tool: not_a_real_tool" in _text(err)
