"""The ``upstream_release`` tool (gap provenance-null-rate).

A backend whose answers state no release keeps ``upstream_version`` null; this
tool reads the release its own endpoint calls current, labelled as such.
Payload shapes are the ones probed live 2026-09-26; the live tests re-drive them.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import releases, server, string_db
from plant_genomics_mcp.errors import InvalidArguments, PlantGenomicsError
from plant_genomics_mcp.models import upstream_version_field

LIVE = os.environ.get("PLANT_GENOMICS_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set PLANT_GENOMICS_MCP_LIVE=1 to run")

ENSEMBL = "https://rest.ensembl.org/info/eg_version"
STRING = "https://string-db.org/api/json/version"
QUICKGO_ANN = "https://www.ebi.ac.uk/QuickGO/services/annotation/about"
QUICKGO_GO = "https://www.ebi.ac.uk/QuickGO/services/ontology/go/about"
JASPAR = "https://jaspar.elixir.no/api/v1/releases/?format=json&page_size=100"
KEGG = "https://rest.kegg.jp/info/kegg"

_KEGG_INFO = (
    "kegg\tKEGG (Kyoto Encyclopedia of Genes and Genomes)\n"
    "\tpathway        587  2026/09/25\n"
    "\tbrite          205  2026/09/25\n"
    "\tgenes   68,166,738  2026/09/26\n"
    "\t  vg       709,014  2026/09/17\n"
)


def _jaspar(rows: list[dict], next_page: str | None = None) -> dict:
    return {"count": len(rows), "next": next_page, "previous": None, "results": rows}


def _jaspar_row(number: int, active: str, year: str = "2026") -> dict:
    return {"year": year, "release_number": number, "active": active, "pubmed_id": "1"}


# backend → (responses to mock, expected release, expected components)
Responses = list[tuple[str, object]]
GOOD: dict[str, tuple[Responses, str, dict[str, str] | None]] = {
    "ensembl_plants": ([(ENSEMBL, {"version": 63})], "63", {"eg_version": "63"}),
    "string": (
        [(STRING, [{"string_version": "12.0", "stable_address": "https://x"}])],
        "12.0",
        {"string_version": "12.0"},
    ),
    "quickgo": (
        [
            (QUICKGO_ANN, {"annotation": {"timestamp": "2026-07-28 10:08"}}),
            (QUICKGO_GO, {"go": {"version": "http://purl/2026-09-22", "timestamp": "2026-09-22"}}),
        ],
        "annotation 2026-07-28 10:08; go 2026-09-22",
        {"annotation": "2026-07-28 10:08", "go": "2026-09-22"},
    ),
    "jaspar": (
        # Newest ACTIVE, not first listed: 12 is inactive, 11 comes after 10.
        [
            (
                JASPAR,
                _jaspar(
                    [
                        _jaspar_row(12, "No"),
                        _jaspar_row(10, "Yes", "2024"),
                        _jaspar_row(11, "Yes"),
                        _jaspar_row(3, "No", "2008"),
                    ]
                ),
            )
        ],
        "11",
        {"release_number": "11", "year": "2026", "active_releases": "2"},
    ),
    "kegg": ([(KEGG, _KEGG_INFO)], "pathway 2026/09/25; genes 2026/09/26", None),
}

# backend → a changed shape its fetcher must refuse
BROKEN: dict[str, Responses] = {
    "ensembl_plants": [(ENSEMBL, {"release": 63})],
    "string": [(STRING, {"string_version": "12.0"})],
    "quickgo": [
        (QUICKGO_ANN, {"annotation": {"timestamp": "2026-07-28 10:08"}}),
        (QUICKGO_GO, {"go": {"version": "http://purl/2026-09-22"}}),
    ],
    "jaspar": [(JASPAR, _jaspar([_jaspar_row(11, "Yes")], next_page="https://jaspar/?page=2"))],
    "kegg": [(KEGG, _KEGG_INFO.replace("\tgenes", "\tgenez"))],
}


def _mock(httpx_mock: HTTPXMock, responses: Responses) -> None:
    for url, body in responses:
        if isinstance(body, str):
            httpx_mock.add_response(url=url, text=body)
        else:
            httpx_mock.add_response(url=url, json=body)


async def _read(backend: str) -> dict:
    async with httpx.AsyncClient() as client:
        return await releases.upstream_release(client, backend)


@pytest.mark.parametrize("backend", list(GOOD))
@pytest.mark.asyncio
async def test_a_publishing_backend_reports_what_its_endpoint_states(
    httpx_mock: HTTPXMock, backend: str
) -> None:
    responses, release, components = GOOD[backend]
    _mock(httpx_mock, responses)
    r = await _read(backend)
    assert (r["backend"], r["release"]) == (backend, release), r
    if components is not None:
        assert r["components"] == components, r
    # Provenance is the URL each read actually requested, query included.
    assert r["endpoints"] == [url for url, _ in responses], r
    assert datetime.fromisoformat(r["observed_at"]).utcoffset() == timedelta(0), r
    assert r["reason"], r


@pytest.mark.parametrize("backend", list(BROKEN))
@pytest.mark.asyncio
async def test_a_changed_release_shape_is_refused_not_guessed(
    httpx_mock: HTTPXMock, backend: str
) -> None:
    _mock(httpx_mock, BROKEN[backend])
    with pytest.raises(PlantGenomicsError, match=r"unusable|unexpected shape|lists no"):
        await _read(backend)
    # Positive control: the well-formed answer is accepted by the same fetcher.
    _mock(httpx_mock, GOOD[backend][0])
    assert (await _read(backend))["release"] == GOOD[backend][1]


@pytest.mark.parametrize(
    ("url", "body", "backend"),
    [
        (ENSEMBL, {"version": True}, "ensembl_plants"),
        (ENSEMBL, {"version": " "}, "ensembl_plants"),
        (ENSEMBL, {"version": 6.3}, "ensembl_plants"),
        (JASPAR, _jaspar([_jaspar_row(11, "No")]), "jaspar"),
    ],
    ids=["bool", "blank", "float", "no-active-jaspar"],
)
@pytest.mark.asyncio
async def test_an_unusable_release_value_is_refused(
    httpx_mock: HTTPXMock, url: str, body: dict, backend: str
) -> None:
    httpx_mock.add_response(url=url, json=body)
    with pytest.raises(PlantGenomicsError, match=r"unusable|lists no active"):
        await _read(backend)
    _mock(httpx_mock, GOOD[backend][0])
    assert (await _read(backend))["release"] == GOOD[backend][1]


@pytest.mark.parametrize("backend", ["pdbe", "aragwas", "europe_pmc"])
@pytest.mark.asyncio
async def test_a_backend_that_publishes_no_release_says_why_and_fetches_nothing(
    httpx_mock: HTTPXMock, backend: str
) -> None:
    r = await _read(backend)
    assert (r["release"], r["components"], r["endpoints"]) == (None, {}, []), r
    assert "2026-09-26" in r["reason"] or "continuously" in r["reason"], r
    assert httpx_mock.get_requests() == []
    # Positive control: a publishing backend in the same test does fetch.
    _mock(httpx_mock, GOOD["ensembl_plants"][0])
    assert (await _read("ensembl_plants"))["release"] == "63"


@pytest.mark.asyncio
async def test_every_read_goes_upstream_so_a_before_and_after_pair_can_differ(
    httpx_mock: HTTPXMock,
) -> None:
    """Never cached: a cached read would compare a value with itself."""
    httpx_mock.add_response(url=ENSEMBL, json={"version": 63})
    httpx_mock.add_response(url=ENSEMBL, json={"version": 64})
    before, after = await _read("ensembl_plants"), await _read("ensembl_plants")
    assert (before["release"], after["release"]) == ("63", "64")
    assert len(httpx_mock.get_requests()) == 2


@pytest.mark.asyncio
async def test_an_unknown_backend_is_refused() -> None:
    with pytest.raises(InvalidArguments, match="backend must be one of"):
        await _read("uniprot")
    assert (await _read("pdbe"))["backend"] == "pdbe"


def test_the_tool_offers_exactly_the_backends_the_module_serves() -> None:
    (tool,) = [t for t in server.TOOLS if t.name == "upstream_release"]
    assert tool.input_schema["properties"]["backend"]["enum"] == list(releases.BACKENDS)
    assert tool.input_schema["required"] == ["backend"]


def test_every_always_null_upstream_version_names_its_upstream_release() -> None:
    """A null field points somewhere real, and a new one cannot forget to."""
    pointer = re.compile(r"upstream_release\(backend='([a-z_]+)'\)")
    named: dict[str, str] = {}
    unnamed = []
    for tool in server.TOOLS:
        schema = tool.output_schema or {}
        for model in [schema, *(schema.get("$defs") or {}).values()]:
            field = (model.get("properties") or {}).get("upstream_version")
            if not field or "always null" not in field.get("description", ""):
                continue
            m = pointer.search(field["description"])
            if m:
                named[str(tool.name)] = m.group(1)
            else:
                unnamed.append(str(tool.name))
    assert not unnamed, unnamed
    assert set(named.values()) <= set(releases.BACKENDS), named
    # Positive control: the walk finds the known null tools, and not a stated one.
    assert {"kegg_pathways", "string_interactions", "ensembl_plants_lookup_locus"} <= set(named)
    assert "panther_family" not in named and "orthodb_orthologs" not in named
    with pytest.raises(TypeError, match="must name its upstream_release"):
        upstream_version_field("Somewhere", None)


# --- live -------------------------------------------------------------------

_SHAPES = {
    "ensembl_plants": r"\d+",
    "string": r"\d+\.\d+",
    "quickgo": r"annotation \d{4}-\d{2}-\d{2} \d{2}:\d{2}; go \d{4}-\d{2}-\d{2}",
    "jaspar": r"\d+",
    "kegg": r"pathway \d{4}/\d{2}/\d{2}; genes \d{4}/\d{2}/\d{2}",
}


@live_only
@pytest.mark.asyncio
async def test_live_every_publishing_backend_answers_in_the_probed_shape() -> None:
    async with httpx.AsyncClient() as client:
        for backend, shape in _SHAPES.items():
            r = await releases.upstream_release(client, backend)
            assert r["release"] and re.fullmatch(shape, r["release"]), r


@live_only
@pytest.mark.asyncio
async def test_live_the_no_release_claims_still_hold() -> None:
    """The endpoints the PDBe/AraGWAS reasons name still state nothing.

    Positive control first, through the same client: Ensembl's release endpoint
    answers, so a 404 below is PDBe's, not a broken probe.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        control = await client.get(ENSEMBL, headers={"Accept": "application/json"})
        assert control.status_code == 200 and "version" in control.json()
        for path in ("/pdbe/api/status", "/pdbe/api/pdb/release", "/pdbe/api/v2/status"):
            resp = await client.get(f"https://www.ebi.ac.uk{path}")
            assert resp.status_code == 404, (path, resp.status_code)
        root = (await client.get("https://aragwas.1001genomes.org/api/")).json()
        assert "genes" in root and not [k for k in root if re.search("version|release", k)], root


@live_only
@pytest.mark.asyncio
async def test_live_string_answers_stay_null_while_its_release_is_one_call_away() -> None:
    """A STRING host pin is not evidence: an unknown version-99-0 host is served
    12.0 (probed 2026-09-26), so interaction answers keep upstream_version null
    and the release comes only from upstream_release."""
    async with httpx.AsyncClient() as client:
        answer = await string_db.lookup_partners(client, "AT1G01060", limit=1)
        release = await releases.upstream_release(client, "string")
        bogus = await client.get("https://version-99-0.string-db.org/api/json/version")
    assert answer["upstream_version"] is None, answer
    assert release["release"] == bogus.json()[0]["string_version"], (release, bogus.text)
