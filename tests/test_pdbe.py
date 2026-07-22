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
    # Changed 2026-07-22 (audit L1): the count is now of *structures*, so the
    # junk row is excluded. It previously reported the raw upstream length (3),
    # which overstated what the caller actually received.
    assert r["structure_count"] == 2
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


# ---------- negative caching + count hygiene (audit 2026-07-22, M2 / L1) ----------


@pytest.mark.asyncio
async def test_404_is_cached_so_a_repeat_lookup_stays_off_the_wire(
    httpx_mock: HTTPXMock,
) -> None:
    """The 404 path is the COMMON one for plant proteins, so it must be cached.

    Exactly one mock is registered and the helper is called twice: if the second
    call re-issued the request, pytest-httpx would fail it as unexpected. The
    request count is asserted too, so the mechanism — not just the result — is
    pinned.
    """
    httpx_mock.add_response(url=_URL, status_code=404, text="Not Found")
    async with httpx.AsyncClient() as client:
        first = await pdbe.lookup_by_uniprot(client, "Q9SZ92")
        second = await pdbe.lookup_by_uniprot(client, "Q9SZ92")
    assert first == second
    assert second["found"] is False
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.asyncio
async def test_non_dict_rows_are_excluded_from_the_count_not_just_the_output(
    httpx_mock: HTTPXMock,
) -> None:
    """``structure_count`` must match what's returned, even on a junk row.

    Counting before filtering used to report 3 structures while returning 2.
    """
    httpx_mock.add_response(
        url=_URL, json={"Q9SZ92": [_ENTRY, "junk", {**_ENTRY, "pdb_id": "1rcx"}]}
    )
    async with httpx.AsyncClient() as client:
        r = await pdbe.lookup_by_uniprot(client, "Q9SZ92")
    assert r["structure_count"] == 2
    assert len(r["structures"]) == 2
    assert r["truncated"] is False


@pytest.mark.asyncio
async def test_a_list_of_only_junk_rows_is_found_false(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=_URL, json={"Q9SZ92": ["junk", 7]})
    async with httpx.AsyncClient() as client:
        r = await pdbe.lookup_by_uniprot(client, "Q9SZ92")
    assert r["found"] is False
    assert r["structure_count"] == 0


@pytest.mark.asyncio
async def test_truncation_is_computed_after_filtering(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two real rows + one junk row at cap=2 is NOT truncated."""
    monkeypatch.setattr(pdbe, "MAX_STRUCTURES", 2)
    httpx_mock.add_response(
        url=_URL, json={"Q9SZ92": [_ENTRY, "junk", {**_ENTRY, "pdb_id": "1rcx"}]}
    )
    async with httpx.AsyncClient() as client:
        r = await pdbe.lookup_by_uniprot(client, "Q9SZ92")
    assert r["structure_count"] == 2
    assert r["truncated"] is False


@pytest.mark.asyncio
async def test_payload_without_the_accession_key_is_found_false(httpx_mock: HTTPXMock) -> None:
    """PDBe echoing a different key (or none) is 'no structure', not a crash."""
    httpx_mock.add_response(url=_URL, json={"SOMETHING-ELSE": [_ENTRY]})
    async with httpx.AsyncClient() as client:
        r = await pdbe.lookup_by_uniprot(client, "Q9SZ92")
    assert r["found"] is False
    assert r["structure_count"] == 0
