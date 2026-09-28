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
from plant_genomics_mcp.errors import PlantGenomicsError

_SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "plant_genomics_mcp"


def _json_calls(path: pathlib.Path) -> list[str]:
    """``file:line`` of every bare ``<expr>.json()`` call in ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        f"{path.name}:{node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "json"
        and not node.args
        and not node.keywords
    ]


def test_no_module_but_http_reads_a_body_as_json() -> None:
    sites = [s for p in sorted(_SRC.glob("*.py")) if p.name != "_http.py" for s in _json_calls(p)]
    assert sites == []
    # Positive control: the scan does see a .json() call, the one in json_body.
    http_sites = _json_calls(_SRC / "_http.py")
    line = int(http_sites[0].split(":")[1])
    assert len(http_sites) == 1
    assert "return resp.json()" in (_SRC / "_http.py").read_text().splitlines()[line - 1]


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
    "jaspar": ("jaspar", lambda m, c: m._get_json(c, "/matrix/MA0001.1/", None), {}),
    "panther": (
        "panther",
        lambda m, c: m.lookup_locus(c, "AT1G01010", "arabidopsis_thaliana"),
        {},
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
        with pytest.raises(PlantGenomicsError) as err:
            await call(mod, c)
        assert not isinstance(err.value, ValueError)
        assert str(err.value) == f"{services[0]} returned non-JSON: <not json>"
        # Positive control, same request: the failure was not stored, the
        # request goes upstream again, and a JSON body is answered.
        assert await call(mod, c) is not None
    assert bodies == []
    assert len(services) == 2
    if store is not None:
        store.clear()
