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
    # Retry helper makes max_retries=3 attempts; register one response per attempt.
    for _ in range(3):
        httpx_mock.add_response(
            url="https://rest.ensembl.org/lookup/id",
            method="POST",
            status_code=500,
            text="upstream broke",
        )
    async with httpx.AsyncClient() as client:
        with pytest.raises(Exception, match="500"):
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
        client: httpx.AsyncClient, locus: str, organism: str | int = "arabidopsis_thaliana"
    ) -> dict[str, Any]:
        if locus == "AT1G01010":
            return {"locus": locus, "organism": organism, "count": 1, "xrefs": [], "by_db": {}}
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
        client: httpx.AsyncClient, locus: str, organism: str | int = "arabidopsis_thaliana"
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
        client: httpx.AsyncClient, locus: str, organism: str | int = "arabidopsis_thaliana"
    ) -> dict[str, Any]:
        await asyncio.sleep(0.2)
        return {"locus": locus, "organism": organism, "count": 0, "xrefs": [], "by_db": {}}

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


@pytest.mark.asyncio
async def test_batch_gramene_homologs_mixed(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="https://data.gramene.org/v69/genes?idList=AT1G01010&fl=homology",
        json=[
            {
                "_id": "AT1G01010",
                "homology": {
                    "gene_tree": {"id": "EPlGT01130000406172"},
                    "homologous_genes": {"ortholog_one2many": ["Os01g0100100"]},
                },
            }
        ],
    )
    httpx_mock.add_response(
        url="https://data.gramene.org/v69/genes?idList=NOPE&fl=homology",
        json=[],
    )
    async with httpx.AsyncClient() as client:
        env = await batch.batch_gramene_homologs(client, ["AT1G01010", "NOPE"])
    assert env["tool"] == "gramene_homologs"
    assert env["count"] == 2
    assert "AT1G01010" in env["results"]
    assert env["results"]["AT1G01010"]["total"] == 1
    assert "NOPE" in env["errors"]
    assert "[NotFoundError]" in env["errors"]["NOPE"]


@pytest.mark.asyncio
async def test_batch_kegg_pathways_mixed(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="https://rest.kegg.jp/link/pathway/ath:at1g01010",
        text="ath:at1g01010\tpath:ath04075\n",
    )
    httpx_mock.add_response(
        url="https://rest.kegg.jp/get/path:ath04075",
        text="ENTRY       ath04075                    Pathway\nNAME        Plant hormone signal transduction\n",
    )
    httpx_mock.add_response(
        url="https://rest.kegg.jp/link/pathway/ath:atnope",
        text="",
    )
    async with httpx.AsyncClient() as client:
        env = await batch.batch_kegg_pathways(client, ["AT1G01010", "ATNOPE"])
    assert env["tool"] == "kegg_pathways"
    assert env["count"] == 2
    assert "AT1G01010" in env["results"]
    assert "ATNOPE" in env["errors"]
    assert "[NotFoundError]" in env["errors"]["ATNOPE"]


@pytest.mark.asyncio
async def test_batch_string_interactions_mixed(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url=(
            "https://string-db.org/api/json/interaction_partners"
            "?identifiers=Q0WV96&species=3702&limit=20"
            "&caller_identity=plant-genomics-mcp"
        ),
        json=[{"stringId_B": "3702.AT3G15500.1", "preferredName_B": "NAC3", "score": 0.8}],
    )
    httpx_mock.add_response(
        url=(
            "https://string-db.org/api/json/interaction_partners"
            "?identifiers=Q9LXQ5&species=3702&limit=20"
            "&caller_identity=plant-genomics-mcp"
        ),
        json=[],
    )
    async with httpx.AsyncClient() as client:
        env = await batch.batch_string_interactions(client, ["Q0WV96", "Q9LXQ5"])
    assert env["count"] == 2
    assert "Q0WV96" in env["results"]
    assert "Q9LXQ5" in env["errors"]


@pytest.mark.asyncio
async def test_batch_atted_coexpression_mixed(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="https://atted.jp/api5/?gene=AT1G01010&topN=25&db=Ath-u.c4-0",
        json={
            "request": {"query_id": "AT1G01010", "topN": 25},
            "result_set": [
                {
                    "entrez_gene_id": 839580,
                    "type": "z",
                    "results": [{"gene": 842367, "other_id": ["At4g36990"], "z": 4.58}],
                    "other_id": "At1g01010",
                }
            ],
        },
    )
    httpx_mock.add_response(
        url="https://atted.jp/api5/?gene=ATNOPE&topN=25&db=Ath-u.c4-0",
        json={
            "request": {"query_id": "ATNOPE"},
            "result_set": [{"entrez_gene_id": 0, "type": "z", "results": [], "other_id": "ATNOPE"}],
        },
    )
    async with httpx.AsyncClient() as client:
        env = await batch.batch_atted_coexpression(client, ["AT1G01010", "ATNOPE"])
    assert env["count"] == 2
    assert "AT1G01010" in env["results"]
    assert "ATNOPE" in env["errors"]


# ---------- v0.9 resolver-driven organism= (T13) ----------


@pytest.mark.asyncio
async def test_batch_ensembl_lookup_accepts_organism_alias(httpx_mock: HTTPXMock) -> None:
    """Common-name 'thale cress' resolves to Ensembl slug 'arabidopsis_thaliana'."""
    httpx_mock.add_response(
        url="https://rest.ensembl.org/lookup/id",
        method="POST",
        json={"AT1G01010": {"id": "AT1G01010", "species": "arabidopsis_thaliana"}},
    )
    async with httpx.AsyncClient() as client:
        result = await batch.batch_ensembl_plants_lookup_locus(
            client, ["AT1G01010"], organism="thale cress"
        )
    assert result["count"] == 1
    assert "AT1G01010" in result["results"]
    # Inspect the POST body to confirm the resolved slug was sent on the wire.
    posts = httpx_mock.get_requests()
    assert any("arabidopsis_thaliana" in r.content.decode() for r in posts)


@pytest.mark.asyncio
async def test_batch_phytozome_accepts_organism_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """organism='arabidopsis' resolves to phytozome_int=167 and reaches the backend."""
    seen: dict[str, Any] = {}

    async def fake_phytozome(
        client: httpx.AsyncClient,
        locus: str,
        organism: str | int = "arabidopsis_thaliana",
    ) -> dict[str, Any]:
        seen["organism"] = organism
        return {"gene_name": locus, "organism_name": "Athaliana_TAIR10"}

    from plant_genomics_mcp import phytozome as _phyto

    monkeypatch.setattr(_phyto, "lookup_locus", fake_phytozome)

    async with httpx.AsyncClient() as client:
        result = await batch.batch_phytozome_lookup_locus(
            client, ["AT1G01010"], organism="arabidopsis"
        )
    assert result["count"] == 1
    # The resolver should have routed "arabidopsis" → canonical slug or taxid
    # through to phytozome.lookup_locus unchanged (backend resolves itself).
    assert seen["organism"] == "arabidopsis"


@pytest.mark.asyncio
async def test_batch_ensembl_plants_lookup_locus_retries_on_503(
    httpx_mock: HTTPXMock,
) -> None:
    """Batch POST adopts the _http retry helper (closes audit C7 / batch.py:107-114 gap)."""
    url = "https://rest.ensembl.org/lookup/id"
    # First call: transient 503. Second call: success.
    httpx_mock.add_response(url=url, method="POST", status_code=503, headers={"Retry-After": "0"})
    httpx_mock.add_response(
        url=url,
        method="POST",
        status_code=200,
        json={"AT1G01010": {"id": "AT1G01010", "biotype": "protein_coding"}},
    )
    async with httpx.AsyncClient() as client:
        envelope = await batch.batch_ensembl_plants_lookup_locus(
            client, ["AT1G01010"], organism="arabidopsis_thaliana"
        )
    assert envelope["count"] == 1
    assert envelope["results"]["AT1G01010"]["id"] == "AT1G01010"
    assert envelope["errors"] == {}


@pytest.mark.asyncio
async def test_batch_locus_literature_accepts_organism_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """organism='rice' passes through to europe_pmc.lookup_locus unchanged."""
    seen: dict[str, Any] = {}

    async def fake_europepmc(
        client: httpx.AsyncClient,
        locus: str,
        organism: str | int = "arabidopsis_thaliana",
        size: int = 25,
    ) -> dict[str, Any]:
        seen["organism"] = organism
        return {"locus": locus, "size": size, "results": []}

    from plant_genomics_mcp import europe_pmc as _epmc

    monkeypatch.setattr(_epmc, "lookup_locus", fake_europepmc)

    async with httpx.AsyncClient() as client:
        result = await batch.batch_locus_literature(client, ["Os01g0100100"], organism="rice")
    assert result["count"] == 1
    assert seen["organism"] == "rice"
