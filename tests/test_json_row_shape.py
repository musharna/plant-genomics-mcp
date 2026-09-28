"""A row that is not a JSON object is a typed error, not a false answer.

Narrowing a body to ``list`` leaves its rows ``Any``, which the ``object``
typing of #193 cannot reach. Each tool here used to skip or pass through a
row of the wrong shape: ``vep_annotate`` answered ``found: false`` for a
variant it had annotated, ``string_interactions`` answered zero partners,
and ``get_gene_xrefs`` (and its batch form) and ``ensembl_region_query`` put
the row into an answer that broke their schema. No live row of these
endpoints is anything but an object (probed 2026-09-28), so such a row now
fails ``_http.object_rows`` inside ``cached_get``: the body is asked for once
more, never stored, and a second bad one is ``UpstreamUnavailableError``.

Each case runs the tool through the real dispatch arm twice: with a bad row
(the typed error, message exact, upstream asked twice) and with a body copied
verbatim from the live endpoint (answered, and asked for again, because the
bad body was never stored).
"""

from __future__ import annotations

import json
import socket
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from plant_genomics_mcp import _http, server
from plant_genomics_mcp.errors import UpstreamUnavailableError
from tests.test_ensembl_variation import _LIVE_VEP

# Verbatim first rows, live 2026-09-28.
_XREF = {
    "info_type": "DIRECT",
    "description": "NAC domain containing protein 1",
    "primary_id": "AT1G01010",
    "display_id": "AT1G01010-TAIR-G",
    "version": "0",
    "dbname": "NASC_GENE_ID",
    "synonyms": [],
    "db_display_name": "NASC Gene ID",
    "info_text": "",
}
_GENE = {
    "biotype": "protein_coding",
    "end": 9130,
    "start": 6788,
    "gene_id": "AT1G01020",
    "logic_name": "araport11",
    "external_name": "ARV1",
    "assembly_name": "TAIR10",
    "strand": -1,
    "id": "AT1G01020",
    "feature_type": "gene",
    "description": "ARV1 family protein [Source:NCBI gene (formerly Entrezgene);Acc:839569]",
    "source": "araport11",
    "seq_region_name": "1",
    "canonical_transcript": "AT1G01020.1.",
}
_PARTNER = {
    "stringId_A": "3702.Q0WV96",
    "stringId_B": "3702.Q5MK24",
    "preferredName_A": "NAC001",
    "preferredName_B": "ARV1",
    "ncbiTaxonId": 3702,
    "score": 0.957,
    "nscore": 0,
    "fscore": 0,
    "pscore": 0,
    "ascore": 0,
    "escore": 0,
    "dscore": 0,
    "tscore": 0.957,
}
_VEP_ROW: dict[str, Any] = _LIVE_VEP[0]
_VEP_BAD_CONSEQUENCE = [
    {**_VEP_ROW, "transcript_consequences": [_VEP_ROW["transcript_consequences"][0], 7]}
]

_VEP = "Ensembl variation /vep/arabidopsis_thaliana/region/1:3767-3767:1/G"
_ROW0 = "row 0 is int, not an object"


def _twice(service: str, problem: str) -> str:
    return (
        f"[UpstreamUnavailableError] {service} answered 200 twice without a readable "
        f"result ({problem}); this is not a count of zero"
    )


# tool, arguments, URL fragment the tool must ask, bad body, error, good body
_CASES: dict[str, tuple[str, dict[str, Any], str, Any, str, Any]] = {
    "vep-row": (
        "vep_annotate",
        {"region": "1:3767-3767:1", "allele": "G"},
        "/vep/",
        [1],
        _twice(_VEP, _ROW0),
        _LIVE_VEP,
    ),
    "vep-consequence": (
        "vep_annotate",
        {"region": "1:3767-3767:1", "allele": "G"},
        "/vep/",
        _VEP_BAD_CONSEQUENCE,
        _twice(_VEP, "transcript_consequences row 1 is int, not an object"),
        _LIVE_VEP,
    ),
    "xrefs": (
        "get_gene_xrefs",
        {"locus": "AT1G01010"},
        "/xrefs/id/",
        [_XREF, 1],
        _twice("Ensembl Plants /xrefs/id/AT1G01010", "row 1 is int, not an object"),
        [_XREF],
    ),
    "region": (
        "ensembl_region_query",
        {"region": "1", "start": 3000, "end": 10000},
        "/overlap/region/",
        [1, _GENE],
        _twice("Ensembl Plants /overlap/region/arabidopsis_thaliana/1:3000-10000", _ROW0),
        [_GENE],
    ),
    "string": (
        "string_interactions",
        {"locus": "AT1G01010"},
        "interaction_partners",
        [1],
        _twice("STRING /api/json/interaction_partners", _ROW0),
        [_PARTNER],
    ),
}


def _upstream(fragment: str, bodies: list[Any], asked: list[str]) -> Any:
    async def upstream(client: Any, method: str, url: str, **kw: Any) -> httpx.Response:
        # Only the endpoint under test may be asked; anything else is a call
        # this test did not plan for, not a body to invent.
        assert fragment in url, url
        asked.append(url)
        return httpx.Response(
            200,
            content=json.dumps(bodies[0]).encode(),
            headers={"content-type": "application/json"},
            request=httpx.Request(method, url),
        )

    return upstream


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.mark.parametrize("case", sorted(_CASES))
async def test_a_row_that_is_not_an_object_is_a_typed_error(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool, args, fragment, bad, message, good = _CASES[case]
    bodies: list[Any] = [bad]
    asked: list[str] = []
    monkeypatch.setattr(_http, "request_with_retry", _upstream(fragment, bodies, asked))
    with pytest.raises(UpstreamUnavailableError) as err:
        await server._dispatch(tool, dict(args))
    assert str(err.value) == message
    assert len(asked) == 2
    # Positive control, same tool: the live body is answered. The bad body
    # was not stored, so the endpoint is asked again.
    bodies[0] = good
    answer = await server._dispatch(tool, dict(args))
    assert len(asked) == 3
    assert answer


async def test_a_batch_carries_the_row_error_per_locus(monkeypatch: pytest.MonkeyPatch) -> None:
    loci = ["AT1G01010", "AT1G01020"]
    bodies: list[Any] = [[1]]
    asked: list[str] = []
    monkeypatch.setattr(_http, "request_with_retry", _upstream("/xrefs/id/", bodies, asked))
    env = await server._dispatch("batch_get_gene_xrefs", {"loci": loci})
    assert env["results"] == {}
    assert env["errors"] == {
        locus: _twice(f"Ensembl Plants /xrefs/id/{locus}", _ROW0) for locus in loci
    }
    # Positive control: a live row answers every locus, asked for again.
    bodies[0] = [_XREF]
    env = await server._dispatch("batch_get_gene_xrefs", {"loci": loci})
    assert env["errors"] == {}
    assert sorted(env["results"]) == loci
    assert len(asked) == 6


async def test_an_empty_answer_is_still_not_found_or_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check is on rows, not on count: an empty list keeps its meaning."""
    asked: list[str] = []
    monkeypatch.setattr(_http, "request_with_retry", _upstream("/vep/", [[]], asked))
    vep = await server._dispatch("vep_annotate", {"region": "1:3767-3767:1", "allele": "G"})
    assert vep["found"] is False
    monkeypatch.setattr(_http, "request_with_retry", _upstream("/overlap/region/", [[]], asked))
    region = await server._dispatch(
        "ensembl_region_query", {"region": "1", "start": 3000, "end": 10000}
    )
    assert (region["count"], region["features"]) == (0, [])
    assert len(asked) == 2


def test_the_shape_helpers_name_the_shape_they_refuse() -> None:
    assert _http.object_rows([{"a": 1}, {}]) == [{"a": 1}, {}]
    assert _http.object_rows([]) == []
    assert _http.expect_object({"a": 1}) == {"a": 1}
    cases: list[tuple[Callable[[object], object], object, str]] = [
        (_http.object_rows, {"a": 1}, "dict, not a list"),
        (_http.object_rows, None, "NoneType, not a list"),
        (_http.object_rows, [{}, "x"], "row 1 is str, not an object"),
        (_http.object_rows, [[{}]], "row 0 is list, not an object"),
        (_http.expect_object, [], "list, not an object"),
        (_http.expect_object, "x", "str, not an object"),
    ]
    for check, value, detail in cases:
        with pytest.raises(_http.UnreadableBody) as err:
            check(value)
        assert err.value.args == (detail,)
