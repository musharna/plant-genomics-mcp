"""Tests for the AlphaFold DB structure backend.

Two tiers (mirrors the quickgo / plantcyc pattern):
  1. Unit tests with mocked HTTP via pytest-httpx. ``lookup_by_uniprot`` is
     tested against a mocked ``/api/prediction`` endpoint; ``lookup_locus`` is
     tested with ``uniprot.lookup_locus`` monkeypatched, so each test exercises
     only this module's logic.
  2. Live integration tests gated by PLANT_GENOMICS_MCP_LIVE=1.
"""

from __future__ import annotations

import os

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import alphafold, uniprot
from plant_genomics_mcp.errors import NotFoundError, PlantGenomicsError

LIVE = os.environ.get("PLANT_GENOMICS_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set PLANT_GENOMICS_MCP_LIVE=1 to run")

# One real-shaped AlphaFold prediction entry (fields trimmed to what we surface,
# key names verified against the live API 2026-07-20, acc Q9SZ92).
_PREDICTION = [
    {
        "uniprotAccession": "Q9SZ92",
        "modelEntityId": "AF-Q9SZ92-F1",
        "globalMetricValue": 90.25,
        "fractionPlddtVeryLow": 0.017,
        "fractionPlddtLow": 0.04,
        "fractionPlddtConfident": 0.243,
        "fractionPlddtVeryHigh": 0.699,
        "latestVersion": 6,
        "modelCreatedDate": "2025-08-01T00:00:00Z",
        "sequenceStart": 1,
        "sequenceEnd": 346,
        "organismScientificName": "Arabidopsis thaliana",
        "gene": "At4g09760",
        "uniprotDescription": "Probable choline kinase 3",
        "cifUrl": "https://alphafold.ebi.ac.uk/files/AF-Q9SZ92-F1-model_v6.cif",
        "pdbUrl": "https://alphafold.ebi.ac.uk/files/AF-Q9SZ92-F1-model_v6.pdb",
        "paeImageUrl": "https://alphafold.ebi.ac.uk/files/AF-Q9SZ92-F1-predicted_aligned_error_v6.png",
    }
]

_PRED_URL = f"{alphafold.BASE_URL}/api/prediction/Q9SZ92"


def _fake_uniprot(acc: str | None):
    """Return a monkeypatch stand-in for uniprot.lookup_locus."""

    async def _lookup(client, locus, organism="arabidopsis"):  # noqa: ANN001, ARG001
        if acc is None:
            raise NotFoundError(f"no UniProt entry for {locus!r}")
        return {"primaryAccession": acc, "uniProtkbId": "CK3_ARATH"}

    return _lookup


# ---------- mocked unit tests: lookup_by_uniprot ----------


@pytest.mark.asyncio
async def test_lookup_by_uniprot_full(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=_PRED_URL, json=_PREDICTION)
    async with httpx.AsyncClient() as client:
        r = await alphafold.lookup_by_uniprot(client, "Q9SZ92")
    assert r["found"] is True
    assert r["accession"] == "Q9SZ92"
    assert r["model_entity_id"] == "AF-Q9SZ92-F1"
    assert r["mean_plddt"] == 90.25
    assert r["plddt_bands"] == {
        "very_low": 0.017,
        "low": 0.04,
        "confident": 0.243,
        "very_high": 0.699,
    }
    assert r["latest_version"] == 6
    assert r["residue_range"] == {"start": 1, "end": 346}
    assert r["organism"] == "Arabidopsis thaliana"
    assert r["gene"] == "At4g09760"
    assert r["cif_url"].endswith("model_v6.cif")
    assert r["pdb_url"].endswith("model_v6.pdb")
    assert r["pae_image_url"].endswith(".png")


@pytest.mark.asyncio
async def test_lookup_by_uniprot_no_model_is_graceful(httpx_mock: HTTPXMock) -> None:
    """404 = no predicted model for this (valid) accession → found=False."""
    httpx_mock.add_response(url=_PRED_URL, status_code=404, text="Not found")
    async with httpx.AsyncClient() as client:
        r = await alphafold.lookup_by_uniprot(client, "Q9SZ92")
    assert r["found"] is False
    assert r["accession"] == "Q9SZ92"
    assert r["mean_plddt"] is None
    assert r["cif_url"] is None


@pytest.mark.asyncio
async def test_lookup_by_uniprot_empty_array_is_graceful(httpx_mock: HTTPXMock) -> None:
    """A 200 with an empty array (no entries) is also found=False."""
    httpx_mock.add_response(url=_PRED_URL, json=[])
    async with httpx.AsyncClient() as client:
        r = await alphafold.lookup_by_uniprot(client, "Q9SZ92")
    assert r["found"] is False


@pytest.mark.asyncio
async def test_lookup_by_uniprot_malformed_raises(httpx_mock: HTTPXMock) -> None:
    """A 200 whose body is not a JSON list → typed PlantGenomicsError."""
    httpx_mock.add_response(url=_PRED_URL, json={"unexpected": "object"})
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlantGenomicsError, match="unexpected payload"):
            await alphafold.lookup_by_uniprot(client, "Q9SZ92")


@pytest.mark.asyncio
async def test_lookup_by_uniprot_no_residue_range(httpx_mock: HTTPXMock) -> None:
    """An entry without sequenceStart yields residue_range=None (L11)."""
    entry = {k: v for k, v in _PREDICTION[0].items() if k != "sequenceStart"}
    httpx_mock.add_response(url=_PRED_URL, json=[entry])
    async with httpx.AsyncClient() as client:
        r = await alphafold.lookup_by_uniprot(client, "Q9SZ92")
    assert r["found"] is True
    assert r["residue_range"] is None


# ---------- mocked unit tests: lookup_locus (uniprot monkeypatched) ----------


@pytest.mark.asyncio
async def test_lookup_locus_wraps_with_locus(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(uniprot, "lookup_locus", _fake_uniprot("Q9SZ92"))
    httpx_mock.add_response(url=_PRED_URL, json=_PREDICTION)
    async with httpx.AsyncClient() as client:
        r = await alphafold.lookup_locus(client, "AT4G09760", "arabidopsis")
    assert r["locus"] == "AT4G09760"
    assert r["accession"] == "Q9SZ92"
    assert r["found"] is True
    assert r["mean_plddt"] == 90.25


@pytest.mark.asyncio
async def test_lookup_locus_unresolvable_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """A locus with no UniProt entry → NotFoundError propagates (typed)."""
    monkeypatch.setattr(uniprot, "lookup_locus", _fake_uniprot(None))
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError):
            await alphafold.lookup_locus(client, "NOSUCHLOCUS", "arabidopsis")


# ---------- live integration (real-execution check) ----------


@live_only
@pytest.mark.asyncio
async def test_live_arabidopsis_has_structure() -> None:
    """Real AlphaFold call — AT4G09760 (Q9SZ92) has a predicted model."""
    async with httpx.AsyncClient() as client:
        r = await alphafold.lookup_locus(client, "AT4G09760", "arabidopsis")
    assert r["found"] is True
    assert r["model_entity_id"].startswith("AF-")
    assert isinstance(r["mean_plddt"], (int, float))
    assert r["cif_url"].startswith("https://alphafold.ebi.ac.uk/")


# ---------- negative caching (audit 2026-07-22, M2) ----------


@pytest.mark.asyncio
async def test_404_is_cached_so_a_repeat_lookup_stays_off_the_wire(
    httpx_mock: HTTPXMock,
) -> None:
    """One mock, two calls: a second request would fail as unexpected."""
    httpx_mock.add_response(url=_PRED_URL, status_code=404, text="Not found")
    async with httpx.AsyncClient() as client:
        first = await alphafold.lookup_by_uniprot(client, "Q9SZ92")
        second = await alphafold.lookup_by_uniprot(client, "Q9SZ92")
    assert first == second
    assert second["found"] is False
    assert len(httpx_mock.get_requests()) == 1
