"""No tool leaks a raw exception when upstream sends JSON of the wrong shape.

A 200 can carry any JSON value. ``_http.json_body`` turns a body that is not
JSON into a typed error (tests/test_json_body_everywhere.py), but a JSON array
or scalar where an object was expected used to reach ``.get`` and leak
``AttributeError: 'list' object has no attribute 'get'``, with no service and
no error class: through ``uniprot._search`` (behind eight tools),
``uniprot.entry_members``, and ``ensembl_plants.lookup_locus`` (behind
``get_sequence``, ``locus_variants`` and the two synthesis tools, whose step
row then failed pydantic validation).

The mechanism was the type: ``json_body``, ``cached_get`` and nine backend
``_get`` wrappers returned ``Any``, so mypy accepted a read of a shape nobody
had checked. They now return ``object``, and mypy refuses the read until the
caller narrows; a scan below keeps a new helper from annotating the body
``Any`` again. The main test drives every tool through the real dispatch arm
with each wrong-shaped body. The type sees only the top level: a list
narrowed by ``isinstance`` has ``Any`` elements, so a wrong-shaped row
(``[1]``) is checked by the shape a backend states to ``cached_get``
(tests/test_json_row_shape.py) and is among the bodies here.

A failed call must also not be stored: the same call made again asks
upstream, with every body, for every tool. ``{}`` is an object that states
none of the fields a backend reads, the shape of InterPro's overload answer
``{"Error":1040}``: a check of such a field made after the store failed the
same call for the whole TTL (AraGWAS, and Ensembl's lookup behind
``get_sequence`` and ``locus_variants``), and a lookup that passed it through
answered ``{}``, which the tool's schema rejects.
"""

from __future__ import annotations

import ast
import importlib
import json
import pathlib
import pkgutil
import socket
from typing import Any

import httpx
import pytest

import plant_genomics_mcp
from plant_genomics_mcp import _http, cache, server
from plant_genomics_mcp.errors import NotFoundError, PlantGenomicsError
from tests.test_server_dispatch import DISPATCH_SPECS

_MODULES = [
    importlib.import_module(f"plant_genomics_mcp.{m.name}")
    for m in pkgutil.iter_modules(plant_genomics_mcp.__path__)
]

_BODIES = [[], [1], "x", 7, None, {}]
_SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "plant_genomics_mcp"


def _any_body_helpers(sources: dict[str, str]) -> list[str]:
    """Functions annotated ``-> Any`` that return a parsed body unnarrowed.

    A function reads a body when it calls ``json_body`` or ``cached_get``, or
    names (calls, or passes as ``parse=``) a function that does and is itself
    annotated ``Any`` or ``object``; a concrete return type means mypy made it
    narrow. Calls are matched by bare name across modules. It cannot see a
    helper reached through a variable or ``getattr``, or a return annotation
    spelled other than the bare name ``Any`` (``typing.Any``, a string).
    """
    fns = []
    for name, text in sources.items():
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                named = set()
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
                        named.add(sub.func.attr)
                    elif isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
                        named.add(sub.func.id)
                    elif isinstance(sub, ast.keyword) and isinstance(sub.value, ast.Name):
                        named.add(sub.value.id)
                returns = node.returns.id if isinstance(node.returns, ast.Name) else None
                fns.append((f"{name}:{node.name}", node.name, named, returns))
    readers = {"json_body", "cached_get"}
    while (
        new := {fn for _, fn, named, ret in fns if named & readers and ret in ("Any", "object")}
        - readers
    ):
        readers |= new
    return sorted(q for q, fn, _, ret in fns if fn in readers and ret == "Any")


def test_the_scan_sees_a_helper_that_hands_on_the_body_as_any() -> None:
    direct = "def f(r) -> Any:\n    return _http.json_body(r, 's')\n"
    via_parse = (
        "def p(r) -> object:\n    return json_body(r, 's')\n"
        "async def g(c) -> Any:\n    return await _http.cached_get(c, parse=p)\n"
    )
    chained = direct + "def h(r) -> Any:\n    return f(r)\n"
    assert _any_body_helpers({"m": direct}) == ["m:f"]
    assert _any_body_helpers({"m": via_parse}) == ["m:g"]
    assert _any_body_helpers({"m": chained}) == ["m:f", "m:h"]
    # Positive control: a helper that narrows to a concrete type ends the
    # chain, so its caller may be Any without holding a raw body.
    narrowed = (
        "def f(r) -> dict:\n    return json_body(r, 's')\ndef h(r) -> Any:\n    return f(r)\n"
    )
    assert _any_body_helpers({"m": narrowed}) == []


def test_no_helper_hands_on_a_parsed_body_as_any() -> None:
    sources = {
        p.name: p.read_text(encoding="utf-8")
        for p in sorted(_SRC.glob("*.py"))
        if p.name != "_http.py"
    }
    assert _any_body_helpers(sources) == []


def _clear_caches() -> None:
    for mod in _MODULES:
        for value in vars(mod).values():
            if isinstance(value, cache.TTLCache):
                value.clear()


async def _outcomes(
    tool: str, args: dict[str, Any], body: Any, mp: pytest.MonkeyPatch, n: int
) -> list[tuple[list[str], BaseException | None, Any]]:
    """The same call ``n`` times over one cache, starting cold: per call, the
    upstream URLs it asked, the exception it raised or None, and its answer."""
    calls: list[str] = []

    async def upstream(client: Any, method: str, url: str, **kw: Any) -> httpx.Response:
        calls.append(url)
        return httpx.Response(
            200,
            content=json.dumps(body).encode(),
            headers={"content-type": "application/json", "x-total-results": "0"},
            request=httpx.Request(method, url),
        )

    mp.setattr(_http, "request_with_retry", upstream)
    _clear_caches()
    seen: list[tuple[list[str], BaseException | None, Any]] = []
    try:
        for _ in range(n):
            before, err, answer = len(calls), None, None
            try:
                answer = await server._dispatch(tool, dict(args))
            except Exception as e:  # noqa: BLE001 - the class is what is under test
                err = e
            seen.append((calls[before:], err, answer))
    finally:
        _clear_caches()
    return seen


async def _outcome(tool: str, args: dict[str, Any], body: Any, mp: pytest.MonkeyPatch) -> tuple:
    """(upstream calls, the exception the call raised or None)."""
    [(urls, err, _)] = await _outcomes(tool, args, body, mp, 1)
    return len(urls), err


def _reported_failures(answer: Any) -> list[str]:
    """The failures an answer carries instead of raising them: a batch's
    ``errors``, a pathway lookup's ``errors`` list, a synthesis step whose
    status is ``error``."""
    if not isinstance(answer, dict):
        return []
    errors = answer.get("errors")
    out = [str(e) for e in (errors.values() if isinstance(errors, dict) else errors or [])]
    for step in answer.get("steps") or []:
        if isinstance(step, dict) and step.get("status") == "error":
            out.append(str(step.get("error")))
    return out


async def _stuck(tool: str, args: dict[str, Any], mp: pytest.MonkeyPatch) -> list[str]:
    """Bodies whose failure the next identical call served, in whole or in
    part, without asking again.

    A failure that is not a "no record" answer says nothing lasting about the
    record, so it must not be stored: the same call made again asks every URL
    the failed one asked. ``NotFoundError`` is exempt, because a real empty
    answer is cached. A failure counts whether it was raised or reported in
    the answer (a batch, a synthesis step); a call stuck on one hop that asks
    a later hop afresh is still stuck (``kegg_pathways``: a stored /link body
    fell through to a fresh /list).
    """
    stuck = []
    for body in _BODIES:
        (asked, err, answer), (again, _, _) = await _outcomes(tool, args, body, mp, 2)
        if err is None:
            failed = any("[NotFoundError]" not in f for f in _reported_failures(answer))
        else:
            failed = isinstance(err, PlantGenomicsError) and not isinstance(err, NotFoundError)
        unasked = sorted(set(asked) - set(again))
        if failed and unasked:
            stuck.append(f"{json.dumps(body)} -> {err or 'reported'}; not asked again: {unasked}")
    return stuck


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def _refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", _refuse)


@pytest.mark.parametrize("spec", DISPATCH_SPECS, ids=lambda s: s.tool)
@pytest.mark.usefixtures("no_network")
async def test_a_wrong_shaped_body_is_a_typed_error_or_an_answer(
    spec: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    leaks = []
    asked = 0
    for body in _BODIES:
        calls, err = await _outcome(spec.tool, spec.args, body, monkeypatch)
        asked += calls
        if err is not None and not isinstance(err, PlantGenomicsError):
            leaks.append(f"{json.dumps(body)} -> {type(err).__name__}: {err}")
    assert leaks == []
    # Positive control: every body reached the tool. A tool that failed before
    # asking upstream would pass above without its parse ever being tried.
    assert asked >= len(_BODIES), asked


def _folds_calls(tool: str) -> bool:
    """A tool that folds many calls into one envelope reports a failed call
    inside its answer, per item or per step, instead of raising."""
    return (
        tool.startswith("batch_")
        or tool.endswith("_synth")
        or tool in {"gene_report", "consensus_homologs"}
    )


@pytest.mark.parametrize(
    "spec", [s for s in DISPATCH_SPECS if not _folds_calls(s.tool)], ids=lambda s: s.tool
)
@pytest.mark.usefixtures("no_network")
async def test_an_object_that_states_none_of_its_fields_is_neither_an_answer_nor_a_miss(
    spec: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``{}`` states nothing, so it is not "no record" and not an empty result.

    Every upstream here says "no record" in a form of its own, probed live
    (2026-09-28): UniProt ``{"results": []}``, ATTED an ``error`` string, BAR
    ``wasSuccessful: false``, OrthoDB ``status: ok`` with no data, PANTHER an
    ``unmapped_list``, Phytozome its header line. ``{}`` was read as each of
    them: "UniProt has no entry" behind seven tools, a JASPAR matrix of nulls,
    PANTHER ``found: false``, no orthologs, no variants, no pathways.
    """
    calls, err = await _outcome(spec.tool, spec.args, {}, monkeypatch)
    assert calls >= 1
    what = "an answer" if err is None else f"{type(err).__name__}: {err}"
    assert isinstance(err, PlantGenomicsError) and not isinstance(err, NotFoundError), what


@pytest.mark.usefixtures("no_network")
async def test_the_harness_reports_a_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    """The check above can fail: a backend that reads ``.get`` off the body
    unchecked is reported, and the same tool with a checked read is not."""
    from plant_genomics_mcp import uniprot

    async def unchecked(client: httpx.AsyncClient, query: str, size: int = 5) -> list:
        resp = await _http.request_with_retry(client, "GET", "https://x.test", service="S")
        body: Any = _http.json_body(resp, "S")
        return list(body.get("results", []))

    real = uniprot._search
    monkeypatch.setattr(uniprot, "_search", unchecked)
    calls, err = await _outcome("resolve_locus_to_uniprot", {"locus": "AT1G01010"}, [], monkeypatch)
    assert calls == 1
    assert isinstance(err, AttributeError)
    assert "'list' object has no attribute 'get'" in str(err)

    monkeypatch.setattr(uniprot, "_search", real)
    calls, err = await _outcome("resolve_locus_to_uniprot", {"locus": "AT1G01010"}, [], monkeypatch)
    assert calls == 1
    assert isinstance(err, PlantGenomicsError)
    assert str(err) == "UniProt search returned unexpected payload: list"


@pytest.mark.parametrize("spec", DISPATCH_SPECS, ids=lambda s: s.tool)
@pytest.mark.usefixtures("no_network")
async def test_a_failed_call_is_followed_by_a_fresh_upstream_request(
    spec: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shape check that ran after the store served one bad body back as the
    same error for the whole TTL (24 of 56 tools, #96)."""
    assert await _stuck(spec.tool, spec.args, monkeypatch) == []


@pytest.mark.usefixtures("no_network")
async def test_the_harness_reports_a_stuck_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """The check above can fail: ``cached_get`` made to store a body before
    its shape is checked, the old order, is reported, and the real one is not.
    Reported too: a batch, which folds the failure into its answer and raised
    nothing, and ``kegg_pathways``, whose stored /link body fell through to a
    fresh /list request; both passed the check when it read only raised
    errors and whether any request was made."""
    real = _http.cached_get

    async def store_then_check(*args: Any, shape: Any = None, **kw: Any) -> Any:
        value = await real(*args, **kw)
        return shape(value) if shape else value

    locus: dict[str, Any] = {"locus": "AT1G01010"}
    cases: list[tuple[str, dict[str, Any], str]] = [
        ("string_interactions", locus, "[1] -> [UnreadableBody] row 0 is int, not an object;"),
        ("batch_string_interactions", {"loci": ["AT1G01010", "AT2G02010"]}, "[1] -> reported;"),
        ("kegg_pathways", locus, "[] -> [UnreadableBody] not a /link pathway line"),
    ]
    monkeypatch.setattr(_http, "cached_get", store_then_check)
    for tool, args, report in cases:
        stuck = await _stuck(tool, args, monkeypatch)
        assert any(s.startswith(report) for s in stuck), (tool, stuck)

    monkeypatch.setattr(_http, "cached_get", real)
    for tool, args, _ in cases:
        assert await _stuck(tool, args, monkeypatch) == [], tool
