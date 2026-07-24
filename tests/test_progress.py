"""Tests for the MCP progress-notification shim.

Three layers:
  1. Reporter unit tests — step counter, no-op without reporter, set/reset
     contextvar lifecycle.
  2. Retry-loop integration — register a 429 then a 200 in pytest-httpx,
     install a recording reporter, assert the retry actually emitted a
     ``Ensembl/QuickGO/Europe PMC/UniProt …: retrying`` message. This is
     the real-execution check at the boundary — if the notify call were
     wrong, no recorded message would land.
  3. BioMart checkpoint — phytozome _post emits "submitting query" +
     "query complete" around the POST.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import (
    ensembl_plants,
    europe_pmc,
    phytozome,
    progress,
    quickgo,
    uniprot,
)


def _recording_reporter() -> tuple[progress.Reporter, list[tuple[float, float | None, str | None]]]:
    """A Reporter that appends every emission to a list for assertion."""
    log: list[tuple[float, float | None, str | None]] = []

    async def _send(p: float, t: float | None, m: str | None) -> None:
        log.append((p, t, m))

    return progress.Reporter(_send), log


# ---------- Reporter unit tests ----------


def test_no_reporter_means_notify_is_noop() -> None:
    """notify() must not blow up when nothing is installed."""
    assert progress.get_reporter() is None
    asyncio.run(progress.notify("nobody listening"))  # must not raise


@pytest.mark.asyncio
async def test_reporter_records_each_notification() -> None:
    reporter, log = _recording_reporter()
    token = progress.set_reporter(reporter)
    try:
        await progress.notify("a")
        await progress.notify("b")
        await progress.notify("c")
    finally:
        progress.reset_reporter(token)
    assert [m for _, _, m in log] == ["a", "b", "c"]
    # Step counter monotonically increases by default step=1.
    assert [p for p, _, _ in log] == [1.0, 2.0, 3.0]


@pytest.mark.asyncio
async def test_reset_restores_no_reporter() -> None:
    reporter, _ = _recording_reporter()
    token = progress.set_reporter(reporter)
    assert progress.get_reporter() is reporter
    progress.reset_reporter(token)
    assert progress.get_reporter() is None


@pytest.mark.asyncio
async def test_reporter_step_param_advances_counter() -> None:
    reporter, log = _recording_reporter()
    token = progress.set_reporter(reporter)
    try:
        await progress.notify("a", step=2.5)
        await progress.notify("b", step=0.5)
    finally:
        progress.reset_reporter(token)
    assert [p for p, _, _ in log] == [2.5, 3.0]


# ---------- retry-loop integration ----------


@pytest.mark.asyncio
async def test_ensembl_429_then_200_emits_retry_notification(httpx_mock: HTTPXMock) -> None:
    url = "https://rest.ensembl.org/lookup/id/AT1G01010?species=arabidopsis_thaliana&expand=0"
    httpx_mock.add_response(url=url, status_code=429, headers={"Retry-After": "0"})
    httpx_mock.add_response(url=url, json={"id": "AT1G01010"})
    reporter, log = _recording_reporter()
    token = progress.set_reporter(reporter)
    try:
        async with httpx.AsyncClient() as client:
            await ensembl_plants.lookup_locus(client, "AT1G01010")
    finally:
        progress.reset_reporter(token)
    messages = [m for _, _, m in log if m is not None]
    assert any("Ensembl Plants" in m and "429" in m and "retrying" in m for m in messages), messages


@pytest.mark.asyncio
async def test_europe_pmc_503_then_200_emits_retry_notification(httpx_mock: HTTPXMock) -> None:
    url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=AT1G01010&format=json&resultType=core&pageSize=10"
    httpx_mock.add_response(url=url, status_code=503, headers={"Retry-After": "0"})
    httpx_mock.add_response(url=url, json={"hitCount": 0, "resultList": {"result": []}})
    reporter, log = _recording_reporter()
    token = progress.set_reporter(reporter)
    try:
        async with httpx.AsyncClient() as client:
            await europe_pmc.lookup_locus(client, "AT1G01010")
    finally:
        progress.reset_reporter(token)
    messages = [m for _, _, m in log if m is not None]
    assert any("Europe PMC" in m and "503" in m for m in messages), messages


@pytest.mark.asyncio
async def test_quickgo_500_then_200_emits_retry_notification(httpx_mock: HTTPXMock) -> None:
    url = "https://www.ebi.ac.uk/QuickGO/services/annotation/search?geneProductId=Q0WV96&limit=50&includeFields=goName%2CtaxonName"
    httpx_mock.add_response(url=url, status_code=500, headers={"Retry-After": "0"})
    httpx_mock.add_response(url=url, json={"numberOfHits": 0, "results": []})
    reporter, log = _recording_reporter()
    token = progress.set_reporter(reporter)
    try:
        async with httpx.AsyncClient() as client:
            await quickgo.lookup_by_uniprot(client, "Q0WV96")
    finally:
        progress.reset_reporter(token)
    messages = [m for _, _, m in log if m is not None]
    assert any("QuickGO" in m and "500" in m for m in messages), messages


@pytest.mark.asyncio
async def test_uniprot_502_then_200_emits_retry_notification(httpx_mock: HTTPXMock) -> None:
    import re

    url_re = re.compile(r"https://rest\.uniprot\.org/uniprotkb/search.*")
    httpx_mock.add_response(url=url_re, status_code=502, headers={"Retry-After": "0"})
    httpx_mock.add_response(
        url=url_re,
        json={
            "results": [
                {"primaryAccession": "Q0WV96", "entryType": "UniProtKB reviewed (Swiss-Prot)"}
            ]
        },
    )
    reporter, log = _recording_reporter()
    token = progress.set_reporter(reporter)
    try:
        async with httpx.AsyncClient() as client:
            await uniprot.lookup_locus(client, "AT1G01010")
    finally:
        progress.reset_reporter(token)
    messages = [m for _, _, m in log if m is not None]
    assert any("UniProt" in m and "502" in m for m in messages), messages


# ---------- BioMart checkpoint ----------


@pytest.mark.asyncio
async def test_phytozome_post_emits_submit_and_complete(httpx_mock: HTTPXMock) -> None:
    """BioMart POST should bracket the wire call with submit + complete pings."""
    body = (
        "organism_name\tgene_name1\tchr_name1\tgene_chrom_start\tgene_chrom_end\tgene_chrom_strand\tgene_description\n"
        "Athaliana\tAT1G01010\t1\t3631\t5899\t1\tNAC domain protein\n"
    )
    httpx_mock.add_response(url=phytozome.BASE_URL, text=body)
    reporter, log = _recording_reporter()
    token = progress.set_reporter(reporter)
    try:
        async with httpx.AsyncClient() as client:
            await phytozome.lookup_locus(client, "AT1G01010")
    finally:
        progress.reset_reporter(token)
    messages = [m for _, _, m in log if m is not None]
    assert any("submitting query" in m for m in messages), messages
    assert any("query complete" in m for m in messages), messages


# ---------- without a reporter, helpers do not raise ----------


@pytest.mark.asyncio
async def test_retry_with_no_reporter_does_not_raise(httpx_mock: HTTPXMock) -> None:
    """The no-reporter branch is the common case; it must not blow up."""
    url = "https://rest.ensembl.org/lookup/id/AT1G01010?species=arabidopsis_thaliana&expand=0"
    httpx_mock.add_response(url=url, status_code=429, headers={"Retry-After": "0"})
    httpx_mock.add_response(url=url, json={"id": "AT1G01010"})
    assert progress.get_reporter() is None
    async with httpx.AsyncClient() as client:
        out: Any = await ensembl_plants.lookup_locus(client, "AT1G01010")
    assert out["id"] == "AT1G01010"
