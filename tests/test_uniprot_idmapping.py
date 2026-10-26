"""Tests for the UniProt ID mapping backend (batch job, every entry per ID).

Unit tests drive the run -> status -> results job through pytest-httpx with
bodies shaped like the live service's (probed 2026-10-26). Live tests
(PLANT_GENOMICS_MCP_LIVE=1) map v5 maize genes whose accessions the
2026-10-24 field key fixed from the same service.
"""

from __future__ import annotations

import os
import re

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import uniprot_idmapping as idm
from plant_genomics_mcp.errors import (
    InvalidArguments,
    PlantGenomicsError,
    UpstreamUnavailableError,
)

LIVE = os.environ.get("PLANT_GENOMICS_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set PLANT_GENOMICS_MCP_LIVE=1 to run")

JOB = "wIsfIEwriB"
RUN = f"{idm.BASE_URL}/run"
STATUS = f"{idm.BASE_URL}/status/{JOB}"
RESULTS = re.compile(re.escape(f"{idm.BASE_URL}/uniprotkb/results/{JOB}") + r"\?.*")
TREMBL = "UniProtKB unreviewed (TrEMBL)"
SWISS = "UniProtKB reviewed (Swiss-Prot)"


def _row(source: str, accession: str, entry_type: str = TREMBL) -> dict:
    return {
        "from": source,
        "to": {"entryType": entry_type, "primaryAccession": accession, "extraAttributes": {}},
    }


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _instant(_s: float) -> None:
        return None

    monkeypatch.setattr(idm, "_sleep", _instant)


def _job(httpx_mock: HTTPXMock, *, running: int = 1) -> None:
    httpx_mock.add_response(url=RUN, method="POST", json={"jobId": JOB})
    for _ in range(running):
        httpx_mock.add_response(url=STATUS, json={"jobStatus": "RUNNING"})
    httpx_mock.add_response(
        url=STATUS,
        status_code=303,
        headers={"location": f"{idm.BASE_URL}/uniprotkb/results/{JOB}"},
        json={"jobStatus": "FINISHED"},
    )


async def test_every_entry_per_id_and_failed_ids_as_unmapped(httpx_mock: HTTPXMock) -> None:
    _job(httpx_mock)
    nxt = f'<{idm.BASE_URL}/uniprotkb/results/{JOB}?cursor=abc123&size=500>; rel="next"'
    httpx_mock.add_response(
        url=RESULTS,
        json={
            "results": [
                _row("Zm00001eb181120", "A0A804NRR9"),
                _row("Zm00001eb181120", "A0A804NRS0"),
                _row("Zm00001eb326750", "P12345", SWISS),
            ],
            "failedIds": ["Zm00001eb999999"],
            "obsoleteCount": 0,
        },
        headers={"link": nxt, "x-uniprot-release": "2026_03"},
    )
    # The second page carries the rest of one ID's entries (and no failedIds).
    httpx_mock.add_response(
        url=RESULTS,
        json={
            "results": [
                _row("Zm00001eb181120", "A0A804NRS2"),
                _row("Zm00001eb181120", "A0A804NRS0"),
            ]
        },
    )
    async with httpx.AsyncClient() as client:
        r = await idm.map_ids(
            client, ["Zm00001eb181120", "Zm00001eb326750", "Zm00001eb999999", "Zm00001eb181120"]
        )
    by = {row["query"]: row for row in r["results"]}
    assert [a["accession"] for a in by["Zm00001eb181120"]["accessions"]] == [
        "A0A804NRR9",
        "A0A804NRS0",
        "A0A804NRS2",
    ]
    assert by["Zm00001eb326750"]["accessions"] == [
        {"accession": "P12345", "entry_type": SWISS, "reviewed": True}
    ]
    assert by["Zm00001eb181120"]["accessions"][0]["reviewed"] is False
    assert by["Zm00001eb999999"] == {
        "query": "Zm00001eb999999",
        "status": "unmapped",
        "accessions": [],
    }
    assert (r["count"], r["mapped"], r["unmapped"]) == (3, 2, 1)
    assert r["upstream_version"] == "2026_03" and r["obsolete_count"] == 0
    sent = httpx_mock.get_requests()
    assert dict(httpx.QueryParams(sent[0].content.decode())) == {
        "from": "Ensembl_Genomes",
        "to": "UniProtKB",
        "ids": "Zm00001eb181120,Zm00001eb326750,Zm00001eb999999",
    }
    pages = [q for q in sent if "/uniprotkb/results/" in str(q.url)]
    # The paged endpoint, never results/stream (which drops failedIds), and
    # the second page asks for the cursor the first one named.
    assert all("/stream/" not in str(q.url) for q in pages)
    assert [q.url.params.get("cursor") for q in pages] == [None, "abc123"]


async def test_an_id_reported_neither_way_is_an_error(httpx_mock: HTTPXMock) -> None:
    """A gap in UniProt's answer is raised, not served as unmapped.

    Positive control in the same test: when every ID is accounted for, the
    same flow answers.
    """
    for failed in ([], ["Zm00001eb999999"]):
        _job(httpx_mock, running=0)
        httpx_mock.add_response(
            url=RESULTS,
            json={"results": [_row("Zm00001eb326750", "A0A804QG68")], "failedIds": failed},
        )
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlantGenomicsError, match="neither mapped nor failed"):
            await idm.map_ids(client, ["Zm00001eb326750", "Zm00001eb999999"])
        idm._CACHE.clear()
        r = await idm.map_ids(client, ["Zm00001eb326750", "Zm00001eb999999"])
    assert [row["status"] for row in r["results"]] == ["mapped", "unmapped"]


async def test_a_job_that_errors_is_not_an_empty_mapping(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=RUN, method="POST", json={"jobId": JOB})
    httpx_mock.add_response(url=STATUS, json={"jobStatus": "ERROR", "errors": [{"message": "bad"}]})
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamUnavailableError, match="ended 'ERROR'"):
            await idm.map_ids(client, ["Zm00001eb326750"])
    assert not any("/results/" in str(q.url) for q in httpx_mock.get_requests())


async def test_a_job_still_running_at_the_deadline_is_an_outage(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(idm, "POLL_BUDGET_S", 0.0)
    httpx_mock.add_response(url=RUN, method="POST", json={"jobId": JOB})
    httpx_mock.add_response(url=STATUS, json={"jobStatus": "RUNNING"})
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamUnavailableError, match="still RUNNING"):
            await idm.map_ids(client, ["Zm00001eb326750"])


async def test_a_finished_mapping_is_cached(httpx_mock: HTTPXMock) -> None:
    _job(httpx_mock, running=0)
    httpx_mock.add_response(url=RESULTS, json={"results": [_row("Zm00001eb326750", "A0A804QG68")]})
    async with httpx.AsyncClient() as client:
        await idm.map_ids(client, ["Zm00001eb326750"])
        r = await idm.map_ids(client, ["Zm00001eb326750"])
    assert r["results"][0]["accessions"][0]["accession"] == "A0A804QG68"
    assert len(httpx_mock.get_requests()) == 3  # run, status, one results page


@pytest.mark.parametrize(
    ("ids", "from_db", "match"),
    [
        (["Zm00001eb326750"], "UniProtKB_AC-ID", "from_db must be one of"),
        (["Zm00001eb326750,Zm00001eb181120"], "Ensembl_Genomes", "no spaces or commas"),
        (["a b"], "Ensembl_Genomes", "no spaces or commas"),
        ([], "Ensembl_Genomes", "non-empty list"),
        (["x"] * (idm.MAX_IDS + 1), "Ensembl_Genomes", "at most"),
    ],
)
async def test_bad_input_is_refused_before_any_request(
    httpx_mock: HTTPXMock, ids: list[str], from_db: str, match: str
) -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(InvalidArguments, match=match):
            await idm.map_ids(client, ids, from_db=from_db)
        assert httpx_mock.get_requests() == []
        # Positive control: one valid ID goes through the same call.
        _job(httpx_mock, running=0)
        httpx_mock.add_response(
            url=RESULTS, json={"results": [_row("Zm00001eb326750", "A0A804QG68")]}
        )
        r = await idm.map_ids(client, ["Zm00001eb326750"])
    assert r["mapped"] == 1


@live_only
async def test_live_maize_v5_genes_map_as_the_benchmark_key_says() -> None:
    """Accessions the 2026-10-24 field key took from this service (release 2026_03).

    Compared as sets: a new UniProt release can add entries, so a mismatch
    names the release it ran on.
    """
    want = {
        "Zm00001eb181120": {"A0A804NRR9", "A0A804NRS0", "A0A804NRS2", "A0A804NRS3"},
        "Zm00001eb181130": {"A0A804MI33"},
        "Zm00001eb034970": {"A0A1D6KIR0", "A0A804LT98", "A0A804LT99"},
        "Zm00001eb326750": {"A0A804QG68"},
    }
    async with httpx.AsyncClient() as client:
        r = await idm.map_ids(client, [*want, "Zm00001eb999999"])
    got = {row["query"]: {a["accession"] for a in row["accessions"]} for row in r["results"]}
    assert {k: got[k] for k in want} == want, r["upstream_version"]
    assert got["Zm00001eb999999"] == set()
    assert r["results"][-1]["status"] == "unmapped"
    assert r["upstream_version"]
