"""Every response body is read through ``_http.json_body``.

An HTML page on a 200 is already retried by ``request_with_retry``; any other
body that is not JSON (empty, truncated, plain text) reaches the parse.
``json_body`` is where that becomes a :class:`PlantGenomicsError` naming the
service. A module that calls ``resp.json()`` itself leaks ``JSONDecodeError``
instead, which reaches the caller as "Expecting value: line 1 column 1
(char 0)" with no service and no error class. PANTHER sent such a 200 live on
2026-09-27.
"""

from __future__ import annotations

import ast
import importlib
import json
import pathlib
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest

from plant_genomics_mcp import _http
from plant_genomics_mcp.errors import UpstreamUnavailableError

_SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "plant_genomics_mcp"


def _json_reads(source: str) -> list[int]:
    """Lines of ``source`` that decode JSON: ``<expr>.json(...)`` with any
    arguments, and ``json.loads(...)`` under any import name (``import json as
    j``, ``from json import loads as f``).

    It cannot see a call whose name is computed at run time, such as
    ``getattr(resp, "json")()``.
    """
    tree = ast.parse(source)
    modules, functions = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {a.asname or a.name for a in node.names if a.name == "json"}
        elif isinstance(node, ast.ImportFrom) and node.module == "json":
            functions |= {a.asname or a.name for a in node.names if a.name == "loads"}
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if (
            (isinstance(f, ast.Attribute) and f.attr == "json")
            or (
                isinstance(f, ast.Attribute)
                and f.attr == "loads"
                and isinstance(f.value, ast.Name)
                and f.value.id in modules
            )
            or (isinstance(f, ast.Name) and f.id in functions)
        ):
            lines.append(node.lineno)
    return sorted(lines)


@pytest.mark.parametrize(
    "source",
    [
        "resp.json()",
        "resp.json(strict=False)",
        "r.json(parse_float=str)",
        "import json\njson.loads(resp.text)",
        "import json as j\nj.loads(resp.content)",
        "from json import loads\nloads(resp.text)",
        "from json import loads as f\nf(resp.text)",
    ],
)
def test_the_scan_sees_each_way_of_decoding_a_body(source: str) -> None:
    assert _json_reads(source) == [source.count("\n") + 1]
    # Positive control: the same module without the decode is clean.
    assert _json_reads("import json\nfrom json import loads\nresp.text") == []


def test_no_module_but_http_decodes_json() -> None:
    sites = [
        f"{p.name}:{line}"
        for p in sorted(_SRC.glob("*.py"))
        if p.name != "_http.py"
        for line in _json_reads(p.read_text(encoding="utf-8"))
    ]
    assert sites == []
    # Positive control: in _http the scan finds json_body's read and the
    # cursor decode, and nothing else.
    text = (_SRC / "_http.py").read_text(encoding="utf-8")
    found = [text.splitlines()[n - 1].strip() for n in _json_reads(text)]
    assert len(found) == 2
    assert found[0].startswith("state = json.loads(base64.urlsafe_b64decode(")
    assert found[1] == "return resp.json()"


_Call = Callable[[Any, httpx.AsyncClient], Awaitable[Any]]

# module, the call that reaches its read of the body, and the smallest body it
# answers (the positive control).
_CASES: dict[str, tuple[str, _Call, Any]] = {
    "alphafold": ("alphafold", lambda m, c: m.lookup_by_uniprot(c, "Q9SZ92"), []),
    "batch": (
        "batch",
        lambda m, c: m.batch_ensembl_plants_lookup_locus(c, ["AT1G01010"], "arabidopsis_thaliana"),
        {},
    ),
    "gprofiler": (
        "gprofiler",
        lambda m, c: m.go_enrichment(c, ["AT1G01010"], "arabidopsis_thaliana"),
        {"result": []},
    ),
    "jaspar": (
        "jaspar",
        lambda m, c: m._get_json(c, "/matrix/MA0001.1/", None, shape=_http.expect_object),
        {},
    ),
    "panther": (
        "panther",
        lambda m, c: m.lookup_locus(c, "AT1G01010", "arabidopsis_thaliana"),
        {"search": {"unmapped_list": {"unmapped": "AT1G01010"}}},
    ),
    "pdbe": ("pdbe", lambda m, c: m.lookup_by_uniprot(c, "Q9SZ92"), {}),
    "thalemine": ("thalemine", lambda m, c: m._rows(c, "<query/>"), {"results": []}),
    "uniprot-search": ("uniprot", lambda m, c: m._search(c, "gene:X"), {"results": []}),
    "uniprot-accession": ("uniprot", lambda m, c: m._fetch_by_accession(c, "Q9SZ92"), {}),
    "uniprot-entry-members": (
        "uniprot",
        lambda m, c: m.entry_members(c, "PF00069", "arabidopsis_thaliana"),
        {"results": []},
    ),
}


@pytest.mark.parametrize("case", sorted(_CASES))
@pytest.mark.asyncio
async def test_a_body_that_is_not_json_names_the_service(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    name, call, valid = _CASES[case]
    mod = importlib.import_module(f"plant_genomics_mcp.{name}")
    store = getattr(mod, "_CACHE", None)
    if store is not None:
        store.clear()
    bodies = [b"<not json>", json.dumps(valid).encode()]
    services: list[str] = []

    async def upstream(client: Any, method: str, url: str, **kw: Any) -> httpx.Response:
        services.append(kw["service"])
        return httpx.Response(
            200,
            content=bodies.pop(0),
            headers={"x-total-results": "0"},
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(_http, "request_with_retry", upstream)
    async with httpx.AsyncClient() as c:
        # A JSON API's unparseable 200 is the upstream failing, not the caller:
        # a plain PlantGenomicsError read as a regression in the live check.
        with pytest.raises(UpstreamUnavailableError) as err:
            await call(mod, c)
        assert not isinstance(err.value, ValueError)
        assert str(err.value) == (
            f"[UpstreamUnavailableError] {services[0]} returned non-JSON: <not json>"
        )
        # Positive control, same request: the failure was not stored, the
        # request goes upstream again, and a JSON body is answered.
        assert await call(mod, c) is not None
    assert bodies == []
    assert len(services) == 2
    if store is not None:
        store.clear()
