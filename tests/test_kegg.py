"""Tests for the KEGG REST client.

Two tiers:
  1. Mocked unit tests + a rate-limiter timing test (always run).
  2. Live integration test gated by GENOMICS_MCP_LIVE=1.
"""

from __future__ import annotations

import asyncio
import os
import time

import httpx
import pytest
from pytest_httpx import HTTPXMock

from genomics_mcp import kegg

LIVE = os.environ.get("GENOMICS_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set GENOMICS_MCP_LIVE=1 to run")


# ---------- mocked unit tests ----------


@pytest.mark.asyncio
async def test_find_parses_tsv(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://rest.kegg.jp/find/genes/BRCA2",
        text="hsa:675\tBRCA2; BRCA2 DNA repair associated\nhsa:7157\tTP53; tumor protein p53\n",
    )
    async with httpx.AsyncClient() as client:
        rows = await kegg.find(client, "genes", "BRCA2")
    assert len(rows) == 2
    assert rows[0] == {"id": "hsa:675", "value": "BRCA2; BRCA2 DNA repair associated"}


@pytest.mark.asyncio
async def test_get_returns_raw_text(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://rest.kegg.jp/get/hsa:675",
        text="ENTRY       675               CDS       T01001\nNAME        BRCA2\n",
    )
    async with httpx.AsyncClient() as client:
        text = await kegg.get(client, "hsa:675")
    assert "BRCA2" in text


@pytest.mark.asyncio
async def test_link_parses_tsv(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://rest.kegg.jp/link/pathway/hsa:675",
        text="hsa:675\tpath:hsa03440\nhsa:675\tpath:hsa05224\n",
    )
    async with httpx.AsyncClient() as client:
        rows = await kegg.link(client, "pathway", "hsa:675")
    assert {r["value"] for r in rows} == {"path:hsa03440", "path:hsa05224"}


@pytest.mark.asyncio
async def test_conv_handles_external_ids(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://rest.kegg.jp/conv/hsa/ncbi-geneid:675",
        text="ncbi-geneid:675\thsa:675\n",
    )
    async with httpx.AsyncClient() as client:
        rows = await kegg.conv(client, "hsa", "ncbi-geneid:675")
    assert rows[0]["value"] == "hsa:675"


@pytest.mark.asyncio
async def test_404_returns_empty_string(httpx_mock: HTTPXMock) -> None:
    """KEGG returns 404 to mean 'no result' — that's not an error."""
    httpx_mock.add_response(
        url="https://rest.kegg.jp/find/genes/NOMATCH",
        status_code=404,
        text="",
    )
    async with httpx.AsyncClient() as client:
        rows = await kegg.find(client, "genes", "NOMATCH")
    assert rows == []


@pytest.mark.asyncio
async def test_500_raises(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://rest.kegg.jp/get/hsa:675",
        status_code=500,
        text="server error",
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(kegg.KeggError, match="HTTP 500"):
            await kegg.get(client, "hsa:675")


@pytest.mark.asyncio
async def test_rate_limiter_enforces_3_per_sec(httpx_mock: HTTPXMock) -> None:
    """Five sequential requests must take ≥ (5-1)/3 ≈ 1.33 s under the cap.

    First request is free (bucket starts ready); subsequent four wait
    1/3 s each, totalling ~1.33 s minimum.
    """
    for _ in range(5):
        httpx_mock.add_response(
            url="https://rest.kegg.jp/find/genes/X",
            text="hsa:1\tdummy\n",
        )
    # reset shared limiter so prior tests don't bias the timing
    kegg._LIMITER = kegg._RateLimiter()

    async with httpx.AsyncClient() as client:
        start = time.monotonic()
        await asyncio.gather(*(kegg.find(client, "genes", "X") for _ in range(5)))
        elapsed = time.monotonic() - start
    assert elapsed >= 4.0 / kegg.RATE_LIMIT_PER_SEC, f"limiter too loose: {elapsed:.2f}s"


# ---------- live integration (real-execution check) ----------


@live_only
@pytest.mark.asyncio
async def test_live_find_brca2() -> None:
    """Real call to rest.kegg.jp — verifies wire format hasn't drifted."""
    async with httpx.AsyncClient() as client:
        rows = await kegg.find(client, "genes", "BRCA2")
    assert any("BRCA2" in r["value"].upper() for r in rows)
