"""Tests for the MaizeGDB B73 v4 <-> v5 gene-model translation backend.

Unit tests serve a real-shaped table (lines copied from the 2025-12-02 file)
through pytest-httpx. Live tests (PLANT_GENOMICS_MCP_LIVE=1) read the real
table and check IDs whose answers the benchmark key fixed from it.
"""

from __future__ import annotations

import hashlib
import os

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import maizegdb
from plant_genomics_mcp.errors import InvalidArguments, UpstreamUnavailableError

LIVE = os.environ.get("PLANT_GENOMICS_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set PLANT_GENOMICS_MCP_LIVE=1 to run")

V4_URL = f"{maizegdb.BASE_URL}/B73v4_to_B73v5.tsv"
V5_URL = f"{maizegdb.BASE_URL}/B73v5_to_B73v4.tsv"
LAST_MODIFIED = "Tue, 02 Dec 2025 16:46:38 GMT"
# One model to one, one split into two, one with no v5 counterpart (the
# empty second column, as the real file writes it).
V4_TABLE = (
    b"Zm00001d022051\tZm00001eb326750\n"
    b"Zm00001d050612\tZm00001eb181120,Zm00001eb181130\n"
    b"Zm00001d035507\t\n"
)
V5_TABLE = b"Zm00001eb326750\tZm00001d022051\nZm00001eb999990\t\n"


def _serve(httpx_mock: HTTPXMock, url: str = V4_URL, body: bytes = V4_TABLE) -> None:
    httpx_mock.add_response(
        url=url,
        content=body,
        headers={"content-type": "text/tab-separated-values", "last-modified": LAST_MODIFIED},
    )


async def test_each_status_comes_from_the_table_row(httpx_mock: HTTPXMock) -> None:
    _serve(httpx_mock)
    async with httpx.AsyncClient() as client:
        r = await maizegdb.translate_gene_models(
            client, ["Zm00001d022051", "Zm00001d050612", "Zm00001d035507", "Zm00001d999999"]
        )
    by = {row["query"]: row for row in r["results"]}
    assert by["Zm00001d022051"] == {
        "query": "Zm00001d022051",
        "status": "mapped",
        "targets": ["Zm00001eb326750"],
    }
    assert by["Zm00001d050612"]["targets"] == ["Zm00001eb181120", "Zm00001eb181130"]
    # In the table with an empty target column: no v5 model, a real answer.
    assert by["Zm00001d035507"] == {
        "query": "Zm00001d035507",
        "status": "no_counterpart",
        "targets": [],
    }
    # Absent from the table: the source says nothing, which is not the same.
    assert by["Zm00001d999999"]["status"] == "not_in_table"
    assert (r["mapped"], r["no_counterpart"], r["not_in_table"], r["count"]) == (2, 1, 1, 4)
    assert r["from_assembly"] == "RefGen_v4" and r["to_assembly"] == "RefGen_v5"


async def test_the_answer_names_the_file_that_gave_it(httpx_mock: HTTPXMock) -> None:
    _serve(httpx_mock)
    async with httpx.AsyncClient() as client:
        r = await maizegdb.translate_gene_models(client, ["Zm00001d022051"])
    assert r["source"] == {
        "url": V4_URL,
        "last_modified": LAST_MODIFIED,
        "sha256": hashlib.sha256(V4_TABLE).hexdigest(),
        "rows": 3,
        "rows_without_counterpart": 1,
    }
    assert r["upstream_version"] == LAST_MODIFIED


async def test_v5_to_v4_reads_the_other_table(httpx_mock: HTTPXMock) -> None:
    _serve(httpx_mock, V5_URL, V5_TABLE)
    async with httpx.AsyncClient() as client:
        r = await maizegdb.translate_gene_models(
            client, ["Zm00001eb326750", "Zm00001eb999990"], direction="v5_to_v4"
        )
    assert [row["targets"] for row in r["results"]] == [["Zm00001d022051"], []]
    assert [str(req.url) for req in httpx_mock.get_requests()] == [V5_URL]


async def test_letter_case_and_duplicates_collapse_to_one_query(httpx_mock: HTTPXMock) -> None:
    _serve(httpx_mock)
    async with httpx.AsyncClient() as client:
        r = await maizegdb.translate_gene_models(
            client, ["zm00001D022051", " Zm00001d022051 ", "ZM00001D022051"]
        )
    assert r["count"] == 1
    assert r["results"][0]["status"] == "mapped"


async def test_the_table_is_fetched_once(httpx_mock: HTTPXMock) -> None:
    _serve(httpx_mock)
    async with httpx.AsyncClient() as client:
        await maizegdb.translate_gene_models(client, ["Zm00001d022051"])
        r = await maizegdb.translate_gene_models(client, ["Zm00001d035507"])
    assert r["results"][0]["status"] == "no_counterpart"
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.parametrize(
    "body",
    [
        V4_TABLE + b"<html>maintenance</html>\n",  # an interposed page appended
        V4_TABLE + b"Zm00001d022051\tZm00001eb000001\n",  # a source row twice
        V4_TABLE.replace(b"Zm00001eb326750", b"Zm00001d326750"),  # a v4 ID as target
        b"",
    ],
)
async def test_a_table_with_one_bad_line_is_never_served(
    httpx_mock: HTTPXMock, body: bytes
) -> None:
    """A malformed table is asked for once more and then refused, not trimmed.

    Positive control in the same test: once the good table comes back, the same
    call answers, so the refusal is about the body and not the harness.
    """
    _serve(httpx_mock, body=body)
    _serve(httpx_mock, body=body)
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamUnavailableError, match="without a readable result"):
            await maizegdb.translate_gene_models(client, ["Zm00001d022051"])
        assert len(httpx_mock.get_requests()) == 2
        _serve(httpx_mock)
        r = await maizegdb.translate_gene_models(client, ["Zm00001d022051"])
    assert r["results"][0]["targets"] == ["Zm00001eb326750"]


@pytest.mark.parametrize(
    ("ids", "direction", "match"),
    [
        (["Zm00001d022051_T001"], "v4_to_v5", "no transcript suffix"),
        (["Zm00001eb326750"], "v4_to_v5", "Zm00001d \\+ 6 digits"),
        (["Zm00001d022051"], "v5_to_v4", "Zm00001eb \\+ 6 digits"),
        (["Zm00001d022051"], "v3_to_v4", "direction must be one of"),
        ([], "v4_to_v5", "non-empty list"),
        (["Zm00001d022051"] * (maizegdb.MAX_IDS + 1), "v4_to_v5", "at most"),
    ],
)
async def test_bad_input_is_refused_before_any_request(
    httpx_mock: HTTPXMock, ids: list[str], direction: str, match: str
) -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(InvalidArguments, match=match):
            await maizegdb.translate_gene_models(client, ids, direction=direction)
        assert httpx_mock.get_requests() == []
        # Positive control: a valid ID in the same direction goes through.
        if direction in maizegdb.DIRECTIONS:
            good = "Zm00001d022051" if direction == "v4_to_v5" else "Zm00001eb326750"
            _serve(
                httpx_mock, *((V4_URL, V4_TABLE) if direction == "v4_to_v5" else (V5_URL, V5_TABLE))
            )
            r = await maizegdb.translate_gene_models(client, [good], direction=direction)
            assert r["results"][0]["status"] == "mapped"


@live_only
async def test_live_v4_ids_translate_as_the_benchmark_key_says() -> None:
    """IDs whose v5 answers the 2026-10-24 field key fixed from this table.

    Zm00001d999999 is absent from the 2025-12-02 file (checked by grep). An
    earlier pick, Zm00001d000001, is a real model the table maps: a fixture
    cannot tell an ID that looks made up from one that is.
    """
    async with httpx.AsyncClient() as client:
        r = await maizegdb.translate_gene_models(
            client, ["Zm00001d022051", "Zm00001d050612", "Zm00001d035507", "Zm00001d999999"]
        )
    by = {row["query"]: row for row in r["results"]}
    assert by["Zm00001d022051"]["targets"] == ["Zm00001eb326750"]
    assert sorted(by["Zm00001d050612"]["targets"]) == ["Zm00001eb181120", "Zm00001eb181130"]
    assert by["Zm00001d035507"]["status"] == "no_counterpart"
    assert by["Zm00001d999999"]["status"] == "not_in_table"
    # The whole real file parsed under the strict line rule.
    assert r["source"]["rows"] > 30_000
    assert 0 < r["source"]["rows_without_counterpart"] < r["source"]["rows"]
    assert r["source"]["last_modified"]


@live_only
async def test_live_v5_to_v4_agrees_with_v4_to_v5_on_a_one_to_one_model() -> None:
    async with httpx.AsyncClient() as client:
        back = await maizegdb.translate_gene_models(
            client, ["Zm00001eb326750"], direction="v5_to_v4"
        )
    assert back["results"][0]["targets"] == ["Zm00001d022051"]
