"""Tests for the PDBe experimental-structure backend.

Two tiers (mirrors the alphafold / interpro pattern):
  1. Unit tests with mocked HTTP via pytest-httpx. ``lookup_by_uniprot`` is
     tested against a mocked ``best_structures`` endpoint; ``lookup_locus`` with
     ``uniprot.lookup_locus`` monkeypatched.
  2. Live integration tests gated by PLANT_GENOMICS_MCP_LIVE=1.
"""

from __future__ import annotations

import os

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import pdbe, uniprot
from plant_genomics_mcp.errors import NotFoundError, PlantGenomicsError

LIVE = os.environ.get("PLANT_GENOMICS_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set PLANT_GENOMICS_MCP_LIVE=1 to run")

# Real-shaped best_structures entries (field names verified live 2026-07-21).
_ENTRY = {
    "pdb_id": "8ruc",
    "chain_id": "A",
    "experimental_method": "X-ray diffraction",
    "resolution": 1.6,
    "tax_id": 3562,
    "unp_start": 1,
    "unp_end": 475,
    "start": 1,
    "end": 475,
    "coverage": 1.0,
}
_URL = f"{pdbe.BASE_URL}/pdbe/api/mappings/best_structures/Q9SZ92"


def _fake_uniprot(acc: str | None):
    async def _lookup(client, locus, organism="arabidopsis"):  # noqa: ANN001, ARG001
        if acc is None:
            raise NotFoundError(f"no UniProt entry for {locus!r}")
        return {"primaryAccession": acc, "uniProtkbId": "RBL_ARATH"}

    return _lookup


# ---------- lookup_by_uniprot ----------


@pytest.mark.asyncio
async def test_lookup_by_uniprot_full(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=_URL, json={"Q9SZ92": [_ENTRY, {**_ENTRY, "pdb_id": "1rcx"}]})
    async with httpx.AsyncClient() as client:
        r = await pdbe.lookup_by_uniprot(client, "Q9SZ92")
    assert r["found"] is True
    assert r["accession"] == "Q9SZ92"
    assert r["structure_count"] == 2
    assert r["truncated"] is False
    s = r["structures"][0]
    assert s["pdb_id"] == "8ruc"
    assert s["chain_id"] == "A"
    assert s["experimental_method"] == "X-ray diffraction"
    assert s["resolution"] == 1.6
    assert s["coverage"] == 1.0
    assert s["residue_range"] == {"start": 1, "end": 475}


@pytest.mark.asyncio
async def test_lookup_by_uniprot_no_structure_404_is_graceful(httpx_mock: HTTPXMock) -> None:
    """404 = no deposited structure (the common plant case) → found=False."""
    httpx_mock.add_response(url=_URL, status_code=404, text="Not Found")
    async with httpx.AsyncClient() as client:
        r = await pdbe.lookup_by_uniprot(client, "Q9SZ92")
    assert r["found"] is False
    assert r["structures"] == []
    assert r["structure_count"] == 0


@pytest.mark.asyncio
async def test_lookup_by_uniprot_empty_mapping_is_graceful(httpx_mock: HTTPXMock) -> None:
    """A 200 with an empty structure list is also found=False."""
    httpx_mock.add_response(url=_URL, json={"Q9SZ92": []})
    async with httpx.AsyncClient() as client:
        r = await pdbe.lookup_by_uniprot(client, "Q9SZ92")
    assert r["found"] is False


@pytest.mark.asyncio
async def test_lookup_by_uniprot_truncates(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pdbe, "MAX_STRUCTURES", 1)
    httpx_mock.add_response(
        url=_URL, json={"Q9SZ92": [_ENTRY, {**_ENTRY, "pdb_id": "1rcx"}, "junk-non-dict"]}
    )
    async with httpx.AsyncClient() as client:
        r = await pdbe.lookup_by_uniprot(client, "Q9SZ92")
    assert r["structure_count"] == 3  # raw total incl. the junk row
    assert r["truncated"] is True
    assert len(r["structures"]) == 1


@pytest.mark.asyncio
async def test_lookup_by_uniprot_no_residue_range(httpx_mock: HTTPXMock) -> None:
    """An entry without unp_start yields residue_range=None."""
    entry = {k: v for k, v in _ENTRY.items() if k != "unp_start"}
    httpx_mock.add_response(url=_URL, json={"Q9SZ92": [entry]})
    async with httpx.AsyncClient() as client:
        r = await pdbe.lookup_by_uniprot(client, "Q9SZ92")
    assert r["structures"][0]["residue_range"] is None


@pytest.mark.asyncio
async def test_lookup_by_uniprot_malformed_raises(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=_URL, json=["unexpected", "list"])
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlantGenomicsError, match="unexpected payload"):
            await pdbe.lookup_by_uniprot(client, "Q9SZ92")


# ---------- lookup_locus (uniprot monkeypatched) ----------


@pytest.mark.asyncio
async def test_lookup_locus_wraps_with_locus(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(uniprot, "lookup_locus", _fake_uniprot("Q9SZ92"))
    httpx_mock.add_response(url=_URL, json={"Q9SZ92": [_ENTRY]})
    async with httpx.AsyncClient() as client:
        r = await pdbe.lookup_locus(client, "AT1G01010", "arabidopsis")
    assert r["locus"] == "AT1G01010"
    assert r["accession"] == "Q9SZ92"
    assert r["found"] is True
    assert r["structure_count"] == 1


@pytest.mark.asyncio
async def test_lookup_locus_bad_locus_raises_before_network() -> None:
    """An invalid locus raises NotFoundError before any HTTP call (no mock set)."""
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="invalid locus"):
            await pdbe.lookup_locus(client, "AT1G01010/x", "arabidopsis")


@pytest.mark.asyncio
async def test_lookup_locus_unresolvable_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(uniprot, "lookup_locus", _fake_uniprot(None))
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError):
            await pdbe.lookup_locus(client, "NOSUCHLOCUS", "arabidopsis")


# ---------- live integration (real-execution check) ----------


@live_only
@pytest.mark.asyncio
async def test_live_uniprot_with_structures() -> None:
    """Real PDBe call — P00875 (a plant RuBisCO) has deposited X-ray structures."""
    async with httpx.AsyncClient() as client:
        r = await pdbe.lookup_by_uniprot(client, "P00875")
    assert r["found"] is True
    assert r["structure_count"] > 0
    assert r["structures"][0]["pdb_id"]


@live_only
@pytest.mark.asyncio
async def test_live_locus_no_structure_is_graceful() -> None:
    """A plant locus with no crystal structure resolves to found=False, not error."""
    async with httpx.AsyncClient() as client:
        r = await pdbe.lookup_locus(client, "AT1G01010", "arabidopsis")
    assert isinstance(r["found"], bool)
