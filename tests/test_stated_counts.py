"""A total the upstream did not state is not a total of zero (issue #141's class).

Europe PMC's false zeros came from ``int(raw.get("hitCount", 0))`` over a
body that carried no count. The same default sat in four more backends: a
body without its count became ``numberOfHits`` / ``domain_count`` /
``association_count`` 0, and ``truncated`` false beside a non-empty list.
Each case feeds the backend the body with the count removed (must raise
``UpstreamUnavailableError`` naming the key) and the same body with it
(must answer), in one test.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from plant_genomics_mcp import _http, aragwas, interpro, planteome, quickgo
from plant_genomics_mcp.errors import UpstreamUnavailableError

CASES = [
    pytest.param(
        quickgo,
        lambda c: quickgo.lookup_by_uniprot(c, "Q0WV96"),
        {"numberOfHits": 1, "results": [{"goId": "GO:1", "goAspect": "biological_process"}]},
        ("numberOfHits",),
        id="quickgo",
    ),
    pytest.param(
        planteome,
        lambda c: planteome.lookup_locus(c, "AT1G01010"),
        {"response": {"numFound": 0, "docs": []}},
        ("response", "numFound"),
        id="planteome",
    ),
    pytest.param(
        interpro,
        lambda c: interpro.lookup_by_uniprot(c, "Q0WV96"),
        {"count": 0, "results": [], "next": None},
        ("count",),
        id="interpro",
    ),
    pytest.param(
        aragwas,
        lambda c: aragwas.lookup_locus(c, "AT1G01010"),
        {"count": 0, "results": [], "links": {}},
        ("count",),
        id="aragwas",
    ),
]


def _without(body: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any]:
    body = {**body}
    if len(path) == 1:
        del body[path[0]]
    else:
        body[path[0]] = {k: v for k, v in body[path[0]].items() if k != path[1]}
    return body


@pytest.mark.asyncio
@pytest.mark.parametrize(("module", "call", "body", "count_path"), CASES)
async def test_a_body_without_its_count_raises_and_one_with_it_answers(
    module: Any, call: Any, body: dict[str, Any], count_path: tuple[str, ...], monkeypatch
) -> None:
    served: list[dict[str, Any]] = []

    async def _fake_get(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return served[-1]

    monkeypatch.setattr(module, "_get", _fake_get)
    async with httpx.AsyncClient() as client:
        served.append(body)
        answered = await call(client)  # positive control: the stated count is read
        assert isinstance(answered, dict)

        served.append(_without(body, count_path))
        with pytest.raises(UpstreamUnavailableError, match=count_path[-1]):
            await call(client)


def test_counted_is_one_definition_of_total_returned_truncated() -> None:
    """#123: every list tool spreads these three keys; a null total is unknown."""
    assert _http.counted(3, [1, 2]) == {"total": 3, "returned": 2, "truncated": True}
    assert _http.counted(2, [1, 2]) == {"total": 2, "returned": 2, "truncated": False}
    assert _http.counted(None, [1, 2]) == {"total": None, "returned": 2, "truncated": None}
