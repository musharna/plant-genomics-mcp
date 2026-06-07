"""Tests for the Ensembl Plants REST client.

Two tiers (mirrors the genomics-mcp sibling pattern):
  1. Unit tests with mocked HTTP via pytest-httpx (always run).
  2. Live integration tests gated by PLANT_GENOMICS_MCP_LIVE=1, hitting
     the real rest.ensembl.org. These satisfy the real-execution-check
     doctrine.
"""

from __future__ import annotations

import os

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import _http, ensembl_plants  # noqa: F401

LIVE = os.environ.get("PLANT_GENOMICS_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set PLANT_GENOMICS_MCP_LIVE=1 to run")


# ---------- mocked unit tests ----------


@pytest.mark.asyncio
async def test_lookup_locus_at1g01010_returns_nac001(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://rest.ensembl.org/lookup/id/AT1G01010?species=arabidopsis_thaliana&expand=0",
        json={
            "id": "AT1G01010",
            "display_name": "NAC001",
            "biotype": "protein_coding",
            "species": "arabidopsis_thaliana",
            "description": "NAC domain containing protein 1 [Source:UniProtKB/Swiss-Prot;Acc:Q0WV96]",
        },
    )
    async with httpx.AsyncClient() as client:
        result = await ensembl_plants.lookup_locus(
            client, "AT1G01010", organism="arabidopsis_thaliana"
        )
    assert result["id"] == "AT1G01010"
    assert result["display_name"] == "NAC001"
    assert "NAC" in result["description"]


@pytest.mark.asyncio
async def test_lookup_locus_default_species_is_arabidopsis(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://rest.ensembl.org/lookup/id/AT1G01010?species=arabidopsis_thaliana&expand=0",
        json={"id": "AT1G01010", "display_name": "NAC001"},
    )
    async with httpx.AsyncClient() as client:
        result = await ensembl_plants.lookup_locus(client, "AT1G01010")
    assert result["id"] == "AT1G01010"


@pytest.mark.asyncio
async def test_lookup_locus_retries_on_429_then_succeeds(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://rest.ensembl.org/lookup/id/AT1G01010?species=arabidopsis_thaliana&expand=0",
        status_code=429,
        headers={"Retry-After": "0"},
    )
    httpx_mock.add_response(
        url="https://rest.ensembl.org/lookup/id/AT1G01010?species=arabidopsis_thaliana&expand=0",
        json={"id": "AT1G01010", "display_name": "NAC001"},
    )
    async with httpx.AsyncClient() as client:
        result = await ensembl_plants.lookup_locus(client, "AT1G01010")
    assert result["display_name"] == "NAC001"


@pytest.mark.asyncio
async def test_retry_after_capped_at_60s(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hostile upstream returning ``Retry-After: 3600`` (one hour) must
    not pin the agent for an hour. Cap the honoured sleep at 60s — a
    deliberate ceiling shared across all 10 backend modules (Wave B2).

    This is the canonical test for the cap behavior. The same one-line
    cap lands at every Retry-After site in the codebase; the full suite
    is the regression check that none of those edits broke other paths.
    """
    sleeps: list[float] = []

    async def _record(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(_http.asyncio, "sleep", _record)

    httpx_mock.add_response(
        url="https://rest.ensembl.org/lookup/id/AT1G01010?species=arabidopsis_thaliana&expand=0",
        status_code=429,
        headers={"Retry-After": "3600"},
    )
    httpx_mock.add_response(
        url="https://rest.ensembl.org/lookup/id/AT1G01010?species=arabidopsis_thaliana&expand=0",
        json={"id": "AT1G01010", "display_name": "NAC001"},
    )
    async with httpx.AsyncClient() as client:
        result = await ensembl_plants.lookup_locus(client, "AT1G01010")
    assert result["display_name"] == "NAC001"
    assert sleeps, "retry path never slept"
    assert max(sleeps) <= 60.0, f"sleep {max(sleeps)} exceeded 60s cap"


@pytest.mark.asyncio
async def test_lookup_locus_raises_on_404(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://rest.ensembl.org/lookup/id/NOTREAL?species=arabidopsis_thaliana&expand=0",
        status_code=404,
        text="not found",
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(ensembl_plants.PlantGenomicsError, match="HTTP 404"):
            await ensembl_plants.lookup_locus(client, "NOTREAL")


# ---------- xrefs unit tests ----------


@pytest.mark.asyncio
async def test_lookup_xrefs_wraps_array_and_rolls_up_by_db(httpx_mock: HTTPXMock) -> None:
    """Ensembl returns a top-level array; we wrap with metadata + by_db rollup."""
    httpx_mock.add_response(
        url="https://rest.ensembl.org/xrefs/id/AT1G01010?species=arabidopsis_thaliana",
        json=[
            {
                "dbname": "Uniprot_gn",
                "primary_id": "Q0WV96",
                "display_id": "Q0WV96",
                "info_type": "DEPENDENT",
            },
            {
                "dbname": "EntrezGene",
                "primary_id": "839580",
                "display_id": "NAC001",
                "info_type": "DEPENDENT",
            },
            {
                "dbname": "TAIR_LOCUS",
                "primary_id": "AT1G01010",
                "display_id": "AT1G01010",
                "info_type": "DIRECT",
            },
        ],
    )
    async with httpx.AsyncClient() as client:
        result = await ensembl_plants.lookup_xrefs(client, "AT1G01010")
    assert result["locus"] == "AT1G01010"
    assert result["organism"] == "arabidopsis_thaliana"
    assert result["count"] == 3
    assert len(result["xrefs"]) == 3
    assert result["by_db"]["Uniprot_gn"] == ["Q0WV96"]
    assert result["by_db"]["EntrezGene"] == ["839580"]
    assert result["by_db"]["TAIR_LOCUS"] == ["AT1G01010"]


@pytest.mark.asyncio
async def test_lookup_xrefs_groups_duplicate_dbname_into_list(httpx_mock: HTTPXMock) -> None:
    """Two xrefs with the same dbname both land in by_db[dbname]."""
    httpx_mock.add_response(
        url="https://rest.ensembl.org/xrefs/id/AT1G01010?species=arabidopsis_thaliana",
        json=[
            {"dbname": "GO", "primary_id": "GO:0003700"},
            {"dbname": "GO", "primary_id": "GO:0006355"},
        ],
    )
    async with httpx.AsyncClient() as client:
        result = await ensembl_plants.lookup_xrefs(client, "AT1G01010")
    assert result["by_db"]["GO"] == ["GO:0003700", "GO:0006355"]


@pytest.mark.asyncio
async def test_lookup_xrefs_raises_on_non_list_payload(httpx_mock: HTTPXMock) -> None:
    """Ensembl /xrefs/id is documented as returning an array; raise loud if not."""
    httpx_mock.add_response(
        url="https://rest.ensembl.org/xrefs/id/AT1G01010?species=arabidopsis_thaliana",
        json={"error": "unexpected object shape"},
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(ensembl_plants.PlantGenomicsError, match="non-list payload"):
            await ensembl_plants.lookup_xrefs(client, "AT1G01010")


# ---------- live integration (real-execution check) ----------


@live_only
@pytest.mark.asyncio
async def test_live_lookup_at1g01010() -> None:
    """Real call to rest.ensembl.org — verifies wire format hasn't drifted."""
    async with httpx.AsyncClient() as client:
        result = await ensembl_plants.lookup_locus(client, "AT1G01010")
    assert result["id"] == "AT1G01010"
    # NAC001 is the canonical display name; description should mention NAC.
    assert "NAC" in (result.get("display_name", "") + result.get("description", ""))


@live_only
@pytest.mark.asyncio
async def test_live_lookup_xrefs_at1g01010_includes_uniprot() -> None:
    """Real call to /xrefs/id — verifies wire format + that UniProt link exists."""
    async with httpx.AsyncClient() as client:
        result = await ensembl_plants.lookup_xrefs(client, "AT1G01010")
    assert result["locus"] == "AT1G01010"
    assert result["count"] > 0
    # Q0WV96 is AT1G01010's canonical UniProt accession; cross-validates against
    # the direct UniProt query in tests/test_uniprot.py.
    uniprot_ids = result["by_db"].get("Uniprot_gn", [])
    assert "Q0WV96" in uniprot_ids, f"expected Q0WV96 in Uniprot_gn, got {result['by_db']}"


def test_lookup_locus_accepts_organism_alias(httpx_mock: HTTPXMock) -> None:
    """The new organism= param accepts common names + taxids."""
    httpx_mock.add_response(
        url="https://rest.ensembl.org/lookup/id/AT1G01010?species=arabidopsis_thaliana&expand=0",
        json={"id": "AT1G01010", "biotype": "protein_coding"},
    )
    import asyncio

    import httpx as _httpx

    async def run():
        async with _httpx.AsyncClient() as client:
            return await ensembl_plants.lookup_locus(client, "AT1G01010", organism="thale cress")

    result = asyncio.run(run())
    assert result["id"] == "AT1G01010"


def test_lookup_locus_rejects_unknown_organism() -> None:
    import asyncio

    import httpx as _httpx

    from plant_genomics_mcp.errors import OrganismNotFound

    async def run():
        async with _httpx.AsyncClient() as client:
            return await ensembl_plants.lookup_locus(client, "AT1G01010", organism="zucchini")

    with pytest.raises(OrganismNotFound):
        asyncio.run(run())


def test_ensembl_plants_locus_model_field_renamed_to_organism() -> None:
    """v0.9 contract: EnsemblPlantsLocus exposes `organism`, not `species`."""
    from plant_genomics_mcp.models import EnsemblPlantsLocus

    sample = EnsemblPlantsLocus(
        id="AT1G01010",
        organism="arabidopsis_thaliana",
        biotype="protein_coding",
    )
    assert sample.organism == "arabidopsis_thaliana"
    schema = EnsemblPlantsLocus.model_json_schema()
    assert "organism" in schema["properties"]
    assert "species" not in schema["properties"]


# ---------- Wave B6: shared locus validator at the URL boundary ----------


@pytest.mark.asyncio
async def test_lookup_locus_rejects_malformed_locus_before_http() -> None:
    """Ensembl ``/lookup/id/{locus}`` splices the locus into the path —
    a stray slash, space, or NUL would forge a different request than the
    caller intended. Validation must fire before any HTTP call, so no
    ``httpx_mock`` is configured here.
    """
    from plant_genomics_mcp.errors import NotFoundError

    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="invalid locus"):
            await ensembl_plants.lookup_locus(client, "AT1G01010/extra")


@pytest.mark.asyncio
async def test_lookup_xrefs_rejects_malformed_locus_before_http() -> None:
    """Same validation at the ``/xrefs/id/{locus}`` boundary."""
    from plant_genomics_mcp.errors import NotFoundError

    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="invalid locus"):
            await ensembl_plants.lookup_xrefs(client, "AT1G01010<x>")


@live_only
@pytest.mark.asyncio
async def test_live_lookup_rice_locus() -> None:
    """v0.9 T19: real call against a non-Arabidopsis organism (rice).

    Confirms the organisms.resolve → ensembl_slug_for → REST URL chain
    reaches Ensembl Plants in the right shape for a rice locus.
    """
    async with httpx.AsyncClient() as client:
        result = await ensembl_plants.lookup_locus(client, "Os01g0100100", organism="oryza_sativa")
    assert result["id"] == "Os01g0100100"
    # Translated species → organism via T8 wire-format adapter.
    assert result.get("organism") == "oryza_sativa" or result.get("species") == "oryza_sativa"
