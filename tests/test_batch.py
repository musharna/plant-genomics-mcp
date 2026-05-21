"""Tests for the batch fanout helpers.

Two tiers:
  1. Unit tests with mocked HTTP via pytest-httpx for the native POST
     batch endpoint (Ensembl /lookup/id) and gather-based fanouts.
  2. Live integration tests gated by PLANT_GENOMICS_MCP_LIVE=1, hitting
     the real upstream endpoints.

The gather-based tests use ``monkeypatch`` to stub the underlying
single-locus calls — we don't re-test the per-locus REST shape here, we
test the envelope and the success/error splitting.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import batch, ensembl_plants
from plant_genomics_mcp.errors import NotFoundError, RateLimitError

LIVE = os.environ.get("PLANT_GENOMICS_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set PLANT_GENOMICS_MCP_LIVE=1 to run")


# ---------- bounds ----------


@pytest.mark.asyncio
async def test_bound_rejects_empty_loci() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="non-empty"):
            await batch.batch_get_gene_xrefs(client, [])


@pytest.mark.asyncio
async def test_bound_rejects_oversized_loci() -> None:
    too_many = [f"AT1G{i:05d}" for i in range(batch.MAX_BATCH + 1)]
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="exceeds MAX_BATCH"):
            await batch.batch_get_gene_xrefs(client, too_many)


# ---------- native POST batch — Ensembl /lookup/id ----------


@pytest.mark.asyncio
async def test_batch_ensembl_native_post_splits_hits_and_misses(httpx_mock: HTTPXMock) -> None:
    """One POST round-trip; nulls translate to [NotFoundError] entries."""
    httpx_mock.add_response(
        url="https://rest.ensembl.org/lookup/id",
        method="POST",
        json={
            "AT1G01010": {
                "id": "AT1G01010",
                "display_name": "NAC001",
                "biotype": "protein_coding",
                "species": "arabidopsis_thaliana",
            },
            "AT1G01020": {
                "id": "AT1G01020",
                "display_name": "ARV1",
                "species": "arabidopsis_thaliana",
            },
            "AT9G99999": None,  # miss
        },
    )
    async with httpx.AsyncClient() as client:
        result = await batch.batch_ensembl_plants_lookup_locus(
            client, ["AT1G01010", "AT1G01020", "AT9G99999"]
        )
    assert result["tool"] == "ensembl_plants_lookup_locus"
    assert result["count"] == 3
    assert set(result["results"]) == {"AT1G01010", "AT1G01020"}
    assert result["results"]["AT1G01010"]["display_name"] == "NAC001"
    assert set(result["errors"]) == {"AT9G99999"}
    assert result["errors"]["AT9G99999"].startswith("[NotFoundError]")


@pytest.mark.asyncio
async def test_batch_ensembl_native_post_one_round_trip(httpx_mock: HTTPXMock) -> None:
    """A single POST handles all loci — proves we're using the batch endpoint."""
    httpx_mock.add_response(
        url="https://rest.ensembl.org/lookup/id",
        method="POST",
        json={
            f"AT1G{i:05d}": {"id": f"AT1G{i:05d}", "species": "arabidopsis_thaliana"}
            for i in range(5)
        },
    )
    async with httpx.AsyncClient() as client:
        result = await batch.batch_ensembl_plants_lookup_locus(
            client, [f"AT1G{i:05d}" for i in range(5)]
        )
    assert len(result["results"]) == 5
    assert len(result["errors"]) == 0
    # pytest-httpx asserts all registered responses were consumed; one
    # extra add_response would cause teardown failure. Inverse: no extras
    # means one POST handled the batch.


@pytest.mark.asyncio
async def test_batch_ensembl_http_error_raises(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://rest.ensembl.org/lookup/id",
        method="POST",
        status_code=500,
        text="upstream broke",
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(Exception, match="HTTP 500"):
            await batch.batch_ensembl_plants_lookup_locus(client, ["AT1G01010"])


@pytest.mark.asyncio
async def test_batch_ensembl_non_dict_payload_raises(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://rest.ensembl.org/lookup/id",
        method="POST",
        json=["this", "is", "not", "a", "dict"],
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(Exception, match="non-dict payload"):
            await batch.batch_ensembl_plants_lookup_locus(client, ["AT1G01010"])


# ---------- gather-based fanout — error splitting ----------


@pytest.mark.asyncio
async def test_gather_splits_plant_genomics_errors_to_errors_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PlantGenomicsError subclasses land in errors[] with the typed prefix.

    We stub ensembl_plants.lookup_xrefs so we don't depend on HTTP, and
    exercise batch_get_gene_xrefs which uses the shared ``_gather`` path.
    """

    async def fake_xrefs(
        client: httpx.AsyncClient, locus: str, species: str = "arabidopsis_thaliana"
    ) -> dict[str, Any]:
        if locus == "AT1G01010":
            return {"locus": locus, "species": species, "count": 1, "xrefs": [], "by_db": {}}
        if locus == "AT9G99999":
            raise NotFoundError(f"Ensembl /xrefs/id: no record for {locus}")
        if locus == "AT8G88888":
            raise RateLimitError("rate limit at upstream")
        raise AssertionError(f"unexpected locus {locus}")

    monkeypatch.setattr(ensembl_plants, "lookup_xrefs", fake_xrefs)

    async with httpx.AsyncClient() as client:
        result = await batch.batch_get_gene_xrefs(client, ["AT1G01010", "AT9G99999", "AT8G88888"])
    assert result["tool"] == "get_gene_xrefs"
    assert result["count"] == 3
    assert set(result["results"]) == {"AT1G01010"}
    assert set(result["errors"]) == {"AT9G99999", "AT8G88888"}
    assert result["errors"]["AT9G99999"].startswith("[NotFoundError]")
    assert result["errors"]["AT8G88888"].startswith("[RateLimitError]")


@pytest.mark.asyncio
async def test_gather_reraises_non_plant_genomics_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plain RuntimeError is not the typed-error wire shape — propagate."""

    async def boom(
        client: httpx.AsyncClient, locus: str, species: str = "arabidopsis_thaliana"
    ) -> dict[str, Any]:
        raise RuntimeError("something else broke")

    monkeypatch.setattr(ensembl_plants, "lookup_xrefs", boom)

    async with httpx.AsyncClient() as client:
        with pytest.raises(RuntimeError, match="something else broke"):
            await batch.batch_get_gene_xrefs(client, ["AT1G01010"])


@pytest.mark.asyncio
async def test_gather_runs_concurrently(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two slow per-locus calls should overlap, not serialize.

    Each call sleeps 0.2s. Five calls in parallel must finish well under
    1.0s (serial would be ~1.0s). We're generous on the bound to avoid
    CI flakiness while still catching a fully-serial regression.
    """

    async def slow(
        client: httpx.AsyncClient, locus: str, species: str = "arabidopsis_thaliana"
    ) -> dict[str, Any]:
        await asyncio.sleep(0.2)
        return {"locus": locus, "species": species, "count": 0, "xrefs": [], "by_db": {}}

    monkeypatch.setattr(ensembl_plants, "lookup_xrefs", slow)

    loop = asyncio.get_event_loop()
    t0 = loop.time()
    async with httpx.AsyncClient() as client:
        result = await batch.batch_get_gene_xrefs(client, [f"AT1G{i:05d}" for i in range(5)])
    elapsed = loop.time() - t0
    assert len(result["results"]) == 5
    assert elapsed < 0.8, f"calls did not overlap (elapsed={elapsed:.2f}s)"


# ---------- live integration (real-execution check) ----------


@live_only
@pytest.mark.asyncio
async def test_live_batch_ensembl_at1g01010_plus_miss() -> None:
    """Real POST to Ensembl /lookup/id with one hit + one miss."""
    async with httpx.AsyncClient() as client:
        result = await batch.batch_ensembl_plants_lookup_locus(client, ["AT1G01010", "AT9G99999"])
    assert result["count"] == 2
    assert "AT1G01010" in result["results"]
    assert result["results"]["AT1G01010"]["display_name"] == "NAC001"
    assert "AT9G99999" in result["errors"]
    assert result["errors"]["AT9G99999"].startswith("[NotFoundError]")
