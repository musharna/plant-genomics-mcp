"""A test that reaches an upstream goes through ``_http``.

The nightly (``scripts/classify_live_failures.py``) reads a failure as the
upstream's only when its message carries ``[UpstreamUnavailableError]`` or
``[RateLimitError]``, the tags ``_http`` puts on an outage after retrying it.
Seven live calls used ``client.get`` directly: Ensembl's 500 failed
``test_live_the_no_release_claims_still_hold`` as ``assert 500 == 200`` and a
STRING hiccup as ``JSONDecodeError``, both counted as our regressions. This
scans every test module for a raw HTTP call, so the next one is named here.
"""

from __future__ import annotations

import ast
from pathlib import Path

_TESTS = Path(__file__).parent
_VERBS = {"get", "post", "put", "patch", "delete", "head", "request", "stream", "send"}

# Raw on purpose, each for a stated reason. Keyed on the path under tests/, not
# the file name: the scan recurses, and a same-named file elsewhere must not
# inherit an exemption (#208 review).
_ALLOWED = {
    # Talks to this server on 127.0.0.1, not an upstream.
    ("test_http_transport.py", "*"),
    # The outage probes themselves: they decide "down" from a raw answer.
    ("_live_outage.py", "probe"),
    ("test_phytozome.py", "_biomart_down"),
    # This file's own fixture strings.
    ("test_live_calls_go_through_http.py", "*"),
}


def _raw_calls(source: str) -> list[tuple[str, int]]:
    """(enclosing function, line) of each raw HTTP call in ``source``."""
    found: list[tuple[str, int]] = []

    def visit(node: ast.AST, func: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func = node.name
        # An awaited .get(...) is an async client's; a dict's .get is never awaited.
        if (
            isinstance(node, ast.Await)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr in _VERBS
        ):
            found.append((func, node.lineno))
        # ``async with client.stream(...)`` opens a request without an await.
        if isinstance(node, ast.AsyncWith):
            for item in node.items:
                call = item.context_expr
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "stream"
                ):
                    found.append((func, node.lineno))
        # httpx.get(...) and a sync httpx.Client(...).
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "httpx"
            and (node.func.attr in _VERBS or node.func.attr == "Client")
        ):
            found.append((func, node.lineno))
        for child in ast.iter_child_nodes(node):
            visit(child, func)

    visit(ast.parse(source), "<module>")
    return found


def test_the_scan_sees_each_raw_form_and_not_a_routed_call() -> None:
    """Positive and negative control for the scan itself."""
    raw = (
        "async def a(client):\n    await client.get('https://x')\n"
        "def b():\n    httpx.post('https://x')\n"
        "def c():\n    with httpx.Client() as s:\n        pass\n"
        "async def d(client):\n    async with client.stream('GET', 'https://x') as r:\n        pass\n"
    )
    routed = (
        "async def e(client):\n"
        "    await _http.request_with_retry(client, 'GET', 'https://x', service='x')\n"
        "    row = {'a': 1}.get('a')\n"
        "    async with httpx.AsyncClient() as c:\n        pass\n"
    )
    assert [f for f, _ in _raw_calls(raw)] == ["a", "b", "c", "d"]
    assert _raw_calls(routed) == []


def _allowed(rel: str, func: str) -> bool:
    return (rel, "*") in _ALLOWED or (rel, func) in _ALLOWED


def test_an_exemption_names_one_file_not_every_file_of_that_name() -> None:
    assert _allowed("test_http_transport.py", "anything")
    assert _allowed("_live_outage.py", "probe")
    assert not _allowed("live/test_http_transport.py", "anything")
    assert not _allowed("live/_live_outage.py", "probe")
    assert not _allowed("_live_outage.py", "outage")


def test_no_test_reaches_an_upstream_around_http() -> None:
    offenders = []
    for path in sorted(_TESTS.rglob("*.py")):
        rel = path.relative_to(_TESTS).as_posix()
        for func, line in _raw_calls(path.read_text(encoding="utf-8")):
            if not _allowed(rel, func):
                offenders.append(f"{rel}:{line} in {func}")
    assert not offenders, (
        "raw HTTP call in a test: route it through _http.request_with_retry so an "
        f"outage is tagged for the nightly, or add a stated reason to _ALLOWED: {offenders}"
    )
