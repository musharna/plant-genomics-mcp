"""Tests for the 1001 Genomes natural-variation backend (Arabidopsis-only).

Two tiers:
  1. Unit tests with mocked HTTP via pytest-httpx (gi2coords + effects, with the
     headerless positional-column mapping and organism gating).
  2. Live integration tests gated by PLANT_GENOMICS_MCP_LIVE=1.
"""

from __future__ import annotations

import os

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import onekg
from plant_genomics_mcp.errors import NotFoundError, OrganismNotSupported, PlantGenomicsError

LIVE = os.environ.get("PLANT_GENOMICS_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set PLANT_GENOMICS_MCP_LIVE=1 to run")

_COORDS_URL = f"{onekg.BASE_URL}/api/v2/gi2coords/TAIR10/AT1G01060.1"
_EFF_URL = f"{onekg.BASE_URL}/api/v1.1/effects.json?type=snps;accs=all;gid=AT1G01060.1"

# One real-shaped effects row (14 columns, order per API docs verified 2026-07-20).
_ROW = [
    1,
    37371,
    6963,
    "splice_donor_variant",
    "HIGH",
    "N/A",
    "N/A",
    "c.1T>C",
    645,
    "LHY",
    "protein_coding",
    "CODING",
    "AT1G01060.1",
    2,
]
_COORDS = {"regions": [{"reg_str": "Chr1:33666..37840", "dir": "-"}]}


@pytest.mark.asyncio
async def test_lookup_full(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=_COORDS_URL, json=_COORDS)
    httpx_mock.add_response(url=_EFF_URL, json={"data": [_ROW]})
    async with httpx.AsyncClient() as client:
        r = await onekg.lookup_locus(client, "AT1G01060", "arabidopsis")
    assert r["found"] is True
    assert r["organism"] == "arabidopsis_thaliana"
    assert r["transcript"] == "AT1G01060.1"
    assert r["region"] == "Chr1:33666..37840"
    assert r["variant_count"] == 1
    assert r["truncated"] is False
    v = r["variants"][0]
    assert v["chr"] == 1
    assert v["position"] == 37371
    assert v["accession_id"] == 6963
    assert v["effect"] == "splice_donor_variant"
    assert v["impact"] == "HIGH"
    assert v["amino_acid_change"] == "c.1T>C"
    assert v["gene"] == "LHY"
    assert v["transcript"] == "AT1G01060.1"
    assert v["exon_rank"] == 2


@pytest.mark.asyncio
async def test_lookup_already_transcript_scoped(httpx_mock: HTTPXMock) -> None:
    """A locus that already carries a transcript suffix is used as-is."""
    httpx_mock.add_response(url=_COORDS_URL, json=_COORDS)
    httpx_mock.add_response(url=_EFF_URL, json={"data": []})
    async with httpx.AsyncClient() as client:
        r = await onekg.lookup_locus(client, "AT1G01060.1", "arabidopsis")
    assert r["transcript"] == "AT1G01060.1"
    assert r["variants"] == []


@pytest.mark.asyncio
async def test_lookup_no_regions_gives_null_region(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=_COORDS_URL, json={"regions": []})
    httpx_mock.add_response(url=_EFF_URL, json={"data": [_ROW]})
    async with httpx.AsyncClient() as client:
        r = await onekg.lookup_locus(client, "AT1G01060", "arabidopsis")
    assert r["region"] is None
    assert r["variant_count"] == 1


@pytest.mark.asyncio
async def test_lookup_short_and_nonlist_rows(httpx_mock: HTTPXMock) -> None:
    """A short row pads missing columns with None; a non-list row is skipped."""
    httpx_mock.add_response(url=_COORDS_URL, json=_COORDS)
    httpx_mock.add_response(url=_EFF_URL, json={"data": [_ROW, [2, 500], "junk-non-list"]})
    async with httpx.AsyncClient() as client:
        r = await onekg.lookup_locus(client, "AT1G01060", "arabidopsis")
    assert r["variant_count"] == 3  # raw total includes the junk row
    assert r["returned"] == 2  # junk skipped
    assert r["variants"][1]["chr"] == 2
    assert r["variants"][1]["position"] == 500
    assert r["variants"][1]["effect"] is None  # padded


@pytest.mark.asyncio
async def test_lookup_truncates(httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(onekg, "MAX_EFFECTS", 1)
    httpx_mock.add_response(url=_COORDS_URL, json=_COORDS)
    httpx_mock.add_response(url=_EFF_URL, json={"data": [_ROW, _ROW]})
    async with httpx.AsyncClient() as client:
        r = await onekg.lookup_locus(client, "AT1G01060", "arabidopsis")
    assert r["variant_count"] == 2
    assert r["returned"] == 1
    assert r["truncated"] is True


@pytest.mark.asyncio
async def test_lookup_malformed_raises(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=_COORDS_URL, json=_COORDS)
    httpx_mock.add_response(url=_EFF_URL, json=["unexpected", "list"])
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlantGenomicsError, match="unexpected payload"):
            await onekg.lookup_locus(client, "AT1G01060", "arabidopsis")


@pytest.mark.asyncio
async def test_lookup_non_arabidopsis_raises() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(OrganismNotSupported):
            await onekg.lookup_locus(client, "Os01g0100100", "rice")


@pytest.mark.asyncio
async def test_lookup_bad_agi_raises_before_network() -> None:
    """Malformed AGI raises NotFoundError before any HTTP call (no mock set)."""
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="AGI"):
            await onekg.lookup_locus(client, "AT1G0106", "arabidopsis")


@live_only
@pytest.mark.asyncio
async def test_live_arabidopsis_variation() -> None:
    """Real 1001 Genomes call — AT1G01060 has natural variation."""
    async with httpx.AsyncClient() as client:
        r = await onekg.lookup_locus(client, "AT1G01060", "arabidopsis")
    assert r["found"] is True
    assert r["region"]
