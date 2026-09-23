"""#131: one batch form, ``batch_locus_call``, for every locus-keyed tool.

Driven through ``server._call_tool``, the handler a client's request reaches,
so schema validation, the deprecated-argument rename and dispatch are the
real ones. The per-locus backend is recorded, not the dispatcher.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from plant_genomics_mcp import ensembl_plants, server
from plant_genomics_mcp.errors import NotFoundError
from tests.test_tool_argument_validation import _call, _text

LIVE = os.environ.get("PLANT_GENOMICS_MCP_LIVE") == "1"


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def lookup(client: Any, locus: str, **kwargs: Any) -> dict[str, Any]:
        calls.append((locus, kwargs))
        if locus == "AT9G99999":
            raise NotFoundError(f"no {locus}")
        return {"locus": locus, **kwargs}

    monkeypatch.setattr(ensembl_plants, "lookup_locus", lookup)
    return calls


def test_every_tool_keyed_by_one_locus_is_batchable_and_nothing_else() -> None:
    enum = server._TOOL_SCHEMAS["batch_locus_call"]["properties"]["tool"]["enum"]
    # The eight #131 found with no batch form.
    assert {
        "interpro_domains",
        "alphafold_structure",
        "experimental_structures",
        "tf_binding_motifs",
        "panther_family",
        "orthodb_orthologs",
        "aragwas_associations",
        "gene_report",
    } <= set(enum)
    for name, schema in server._TOOL_SCHEMAS.items():
        keyed = schema["required"] == ["locus"] and not name.startswith("batch_")
        assert (name in enum) == keyed, name


@pytest.mark.asyncio
async def test_each_locus_gets_what_the_single_tool_returns(
    backend: list[tuple[str, dict[str, Any]]],
) -> None:
    args = {"organism": "oryza_sativa"}
    res = await _call(
        "batch_locus_call",
        {
            "tool": "ensembl_plants_lookup_locus",
            "loci": ["Os01g0100100", "AT9G99999", "Os01g0100100"],
            "args": args,
        },
    )
    assert not res.is_error, _text(res)
    env = res.structured_content
    assert env is not None
    single = await _call("ensembl_plants_lookup_locus", {"locus": "Os01g0100100", **args})
    assert env["results"] == {"Os01g0100100": single.structured_content}
    assert env["errors"] == {"AT9G99999": "[NotFoundError] no AT9G99999"}
    assert (env["tool"], env["count"]) == ("ensembl_plants_lookup_locus", 2)
    # The shared args reached every locus; the duplicate ran once.
    assert sorted(backend) == sorted(
        [("Os01g0100100", args), ("AT9G99999", args), ("Os01g0100100", args)]
    )


@pytest.mark.parametrize(
    ("call", "says"),
    [
        # A typo in a shared argument: refused once against the inner schema.
        ({"tool": "ensembl_plants_lookup_locus", "args": {"organsim": "x"}}, "organsim"),
        ({"tool": "ensembl_plants_lookup_locus", "args": {"locus": "AT1G01010"}}, "locus"),
        ({"tool": "gramene_homologs", "args": {"cursor": "abc"}}, "cursor"),
        # Not locus-keyed: refused by the outer schema's enum.
        ({"tool": "jaspar_motif"}, "jaspar_motif"),
        ({"tool": "batch_kegg_pathways"}, "batch_kegg_pathways"),
    ],
)
@pytest.mark.asyncio
async def test_a_bad_call_is_refused_before_any_locus_runs(
    call: dict[str, Any], says: str, backend: list[tuple[str, dict[str, Any]]]
) -> None:
    err = await _call("batch_locus_call", {"loci": ["AT1G01010", "AT1G01020"], **call})
    assert err.is_error is True
    assert _text(err).startswith("[InvalidArguments] ") and says in _text(err), _text(err)
    assert backend == []
    # Positive control: the same batch with valid args runs every locus.
    ok = await _call(
        "batch_locus_call",
        {"tool": "ensembl_plants_lookup_locus", "loci": ["AT1G01010", "AT1G01020"]},
    )
    assert not ok.is_error, _text(ok)
    assert [locus for locus, _ in backend] == ["AT1G01010", "AT1G01020"]


@pytest.mark.skipif(not LIVE, reason="set PLANT_GENOMICS_MCP_LIVE=1 to run")
@pytest.mark.asyncio
async def test_live_batch_matches_single_calls_on_a_tool_with_no_batch_form() -> None:
    loci = ["AT1G19850", "AT2G33860"]  # ARF5, ARF3
    res = await _call("batch_locus_call", {"tool": "panther_family", "loci": loci})
    assert not res.is_error, _text(res)
    env = res.structured_content
    assert env is not None and env["errors"] == {}, env
    for locus in loci:
        single = await _call("panther_family", {"locus": locus})
        assert not single.is_error, _text(single)
        assert env["results"][locus] == single.structured_content, locus
