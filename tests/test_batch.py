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
from plant_genomics_mcp.errors import (
    NotFoundError,
    OrganismNotSupported,
    PlantGenomicsError,
    RateLimitError,
)

LIVE = os.environ.get("PLANT_GENOMICS_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set PLANT_GENOMICS_MCP_LIVE=1 to run")


@pytest.mark.asyncio
async def test_gather_bounds_concurrency(monkeypatch: pytest.MonkeyPatch) -> None:
    """_gather caps concurrent per-locus work at _CONCURRENCY (audit M3)."""
    monkeypatch.setattr(batch, "_CONCURRENCY", 2)
    in_flight = 0
    max_seen = 0

    async def fn(locus: str) -> dict[str, Any]:
        nonlocal in_flight, max_seen
        in_flight += 1
        max_seen = max(max_seen, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return {"locus": locus}

    results, errors = await batch._gather([f"L{i}" for i in range(6)], fn)
    assert len(results) == 6
    assert not errors
    assert max_seen <= 2  # never more than _CONCURRENCY concurrent


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


def test_bound_deduplicates_preserving_first_seen_order() -> None:
    """Duplicate loci collapse to first-seen order so the envelope ``count``
    (len of this list) can't disagree with len(results)+len(errors) and two
    outcomes for the same locus can't silently coalesce (bug audit L5)."""
    assert batch._bound(["b", "a", "b", "a", "c"]) == ["b", "a", "c"]


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
        with pytest.raises(PlantGenomicsError, match="HTTP 500"):
            await batch.batch_ensembl_plants_lookup_locus(client, ["AT1G01010"])


@pytest.mark.asyncio
async def test_batch_ensembl_non_dict_payload_raises(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://rest.ensembl.org/lookup/id",
        method="POST",
        json=["this", "is", "not", "a", "dict"],
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlantGenomicsError, match="non-dict payload"):
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
        url="https://rest.kegg.jp/link/pathway/ath:AT1G01010",
        text="ath:AT1G01010\tpath:ath04075\n",
    )
    httpx_mock.add_response(
        url="https://rest.kegg.jp/get/path:ath04075",
        text="ENTRY       ath04075                    Pathway\nNAME        Plant hormone signal transduction\n",
    )
    httpx_mock.add_response(
        url="https://rest.kegg.jp/link/pathway/ath:ATNOPE",
        text="",
    )
    # An empty /link is checked against /list (issue #140); unknown -> 404.
    httpx_mock.add_response(url="https://rest.kegg.jp/list/ath:ATNOPE", status_code=404, text="")
    async with httpx.AsyncClient() as client:
        env = await batch.batch_kegg_pathways(client, ["AT1G01010", "ATNOPE"])
    assert env["tool"] == "kegg_pathways"
    assert env["count"] == 2
    assert "AT1G01010" in env["results"]
    assert "ATNOPE" in env["errors"]
    assert "[NotFoundError]" in env["errors"]["ATNOPE"]


@pytest.mark.asyncio
async def test_batch_locus_go_annotations_mixed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two-stage fanout (UniProt resolve → QuickGO), audit I4.

    Covers the all-success row, a stage-1 resolve failure, and — the audit's
    key case — a stage-2 QuickGO failure for a locus whose resolve SUCCEEDED,
    proving the second-stage error still lands in errors[] with the typed
    prefix (batch.py:215-231, previously fully uncovered).
    """
    from plant_genomics_mcp import quickgo as _quickgo
    from plant_genomics_mcp import uniprot as _uniprot

    async def fake_uniprot(
        client: httpx.AsyncClient, locus: str, organism: str | int = "arabidopsis_thaliana"
    ) -> dict[str, Any]:
        if locus == "AT1G01010":
            return {"primaryAccession": "Q0WV96"}
        if locus == "AT2G02010":  # resolves fine; QuickGO will 404 on this accession
            return {"primaryAccession": "Q9ZZZ9"}
        raise NotFoundError(f"UniProt: no accession for {locus}")

    async def fake_quickgo(
        client: httpx.AsyncClient, accession: str, limit: int = 25
    ) -> dict[str, Any]:
        if accession == "Q0WV96":
            return {
                "uniprot_accession": accession,  # the real function always returns it
                "numberOfHits": 1,
                "returned": 1,
                "annotations": [{"goId": "GO:0003677"}],
                "by_aspect": {"F": 1},
            }
        raise NotFoundError(f"QuickGO: no annotations for {accession}")

    monkeypatch.setattr(_uniprot, "lookup_locus", fake_uniprot)
    monkeypatch.setattr(_quickgo, "lookup_by_uniprot", fake_quickgo)

    async with httpx.AsyncClient() as client:
        env = await batch.batch_locus_go_annotations(client, ["AT1G01010", "AT2G02010", "ATNOPE"])
    assert env["tool"] == "locus_go_annotations"
    assert env["count"] == 3
    # Both stages succeed → success row with the merged two-stage shape.
    assert set(env["results"]) == {"AT1G01010"}
    row = env["results"]["AT1G01010"]
    assert row["uniprot_accession"] == "Q0WV96"
    assert row["numberOfHits"] == 1
    assert row["by_aspect"] == {"F": 1}
    # Stage-2 failure (resolve ok, QuickGO 404) AND stage-1 failure both land
    # in errors with the typed prefix.
    assert set(env["errors"]) == {"AT2G02010", "ATNOPE"}
    assert env["errors"]["AT2G02010"].startswith("[NotFoundError]")  # the two-stage case
    assert env["errors"]["ATNOPE"].startswith("[NotFoundError]")


@pytest.mark.asyncio
async def test_batch_bar_gene_summary_mixed(monkeypatch: pytest.MonkeyPatch) -> None:
    """audit I4 — batch_bar_gene_summary envelope + success/error split."""
    from plant_genomics_mcp import bar as _bar

    async def fake_summary(client: httpx.AsyncClient, locus: str) -> dict[str, Any]:
        if locus == "AT1G01010":
            return {"locus": locus, "symbol": "NAC001"}
        raise NotFoundError(f"BAR ThaleMine: nothing for {locus}")

    monkeypatch.setattr(_bar, "gene_summary", fake_summary)

    async with httpx.AsyncClient() as client:
        env = await batch.batch_bar_gene_summary(client, ["AT1G01010", "AT9G99999"])
    assert env["tool"] == "bar_gene_summary"
    assert env["count"] == 2
    assert set(env["results"]) == {"AT1G01010"}
    assert set(env["errors"]) == {"AT9G99999"}
    assert env["errors"]["AT9G99999"].startswith("[NotFoundError]")


@pytest.mark.asyncio
async def test_batch_bar_aiv_interactions_mixed(monkeypatch: pytest.MonkeyPatch) -> None:
    """audit I4 — batch_bar_aiv_interactions split + organism forwarding."""
    from plant_genomics_mcp import bar as _bar

    seen: dict[str, Any] = {}

    async def fake_aiv(
        client: httpx.AsyncClient, locus: str, organism: str | int = "arabidopsis_thaliana"
    ) -> dict[str, Any]:
        seen[locus] = organism
        if locus == "AT1G01010":
            return {"locus": locus, "organism": organism, "interactions": []}
        raise NotFoundError(f"BAR AIV: nothing for {locus}")

    monkeypatch.setattr(_bar, "aiv_interactions", fake_aiv)

    async with httpx.AsyncClient() as client:
        env = await batch.batch_bar_aiv_interactions(
            client, ["AT1G01010", "AT9G99999"], organism="rice"
        )
    assert env["tool"] == "bar_aiv_interactions"
    assert env["count"] == 2
    assert set(env["results"]) == {"AT1G01010"}
    assert set(env["errors"]) == {"AT9G99999"}
    assert env["errors"]["AT9G99999"].startswith("[NotFoundError]")
    # The organism arg reaches the backend unchanged (batch resolves nothing itself).
    assert seen["AT1G01010"] == "rice"


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
    assert len(httpx_mock.get_requests()) == 2


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


# --- Gaps named by the nightly mutation run (2026-09-16: 48 logic survivors in batch) ---


def test_bound_accepts_exactly_max_batch_and_rejects_one_more() -> None:
    """Survivor: ``len(loci) > MAX_BATCH`` -> ``>=``. The documented cap is
    inclusive: a 50-locus batch is the largest legal one."""
    exactly = [f"AT1G{i:05d}" for i in range(batch.MAX_BATCH)]
    assert batch._bound(exactly) == exactly
    with pytest.raises(ValueError, match=f"exceeds MAX_BATCH={batch.MAX_BATCH}"):
        batch._bound(exactly + ["AT1G99999"])


@pytest.mark.asyncio
async def test_batch_ensembl_post_is_the_documented_request(httpx_mock: HTTPXMock) -> None:
    """The one round-trip test proved ONE request; nothing proved WHICH request.

    Survivors: payload keys/values (``expand: 0`` -> 1), the JSON headers ->
    None, ``timeout=ensembl_plants.DEFAULT_TIMEOUT`` -> None, and a non-dict
    record landing in ``errors`` as ``None`` instead of a typed message.
    """
    url = f"{ensembl_plants.BASE_URL}/lookup/id"
    httpx_mock.add_response(
        url=url,
        method="POST",
        json={"AT1G01010": {"id": "AT1G01010"}, "AT1G01020": ["not", "a", "record"]},
    )
    async with httpx.AsyncClient() as client:
        env = await batch.batch_ensembl_plants_lookup_locus(
            client, ["AT1G01010", "AT1G01020", "AT1G01030"], organism="arabidopsis_thaliana"
        )
    req = httpx_mock.get_request(url=url, method="POST")
    assert req is not None
    assert req.headers["accept"] == "application/json"
    assert req.headers["content-type"] == "application/json"
    assert req.read() == (
        b'{"ids": ["AT1G01010", "AT1G01020", "AT1G01030"], '
        b'"species": "arabidopsis_thaliana", "expand": 0}'
    )
    assert req.extensions["timeout"]["read"] == ensembl_plants.DEFAULT_TIMEOUT
    assert env["tool"] == "ensembl_plants_lookup_locus" and env["count"] == 3
    # Projected exactly as the single form projects it (issue #137).
    assert env["results"] == {"AT1G01010": {"id": "AT1G01010", "upstream_version": None}}
    assert env["errors"]["AT1G01020"] == (
        "[PlantGenomicsError] Ensembl Plants returned non-dict for AT1G01020: list"
    )
    assert env["errors"]["AT1G01030"].startswith("[NotFoundError]")


@pytest.mark.asyncio
async def test_batch_ensembl_400_not_found_body_is_a_typed_not_found(
    httpx_mock: HTTPXMock,
) -> None:
    """Survivor: ``not_found_400_pattern=ensembl_plants.NOT_FOUND_400_RE`` -> None.
    Ensembl signals an unknown id with 400 + "not found" in the body; the batch
    call must classify that the way the single-locus call does. Positive
    control: a 400 WITHOUT the marker stays a generic PlantGenomicsError."""
    url = f"{ensembl_plants.BASE_URL}/lookup/id"
    httpx_mock.add_response(
        url=url, method="POST", status_code=400, json={"error": "ID 'x' not found"}
    )
    httpx_mock.add_response(
        url=url, method="POST", status_code=400, json={"error": "region too large"}
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError):
            await batch.batch_ensembl_plants_lookup_locus(client, ["x"])
        with pytest.raises(PlantGenomicsError) as exc:
            await batch.batch_ensembl_plants_lookup_locus(client, ["x"])
    assert not isinstance(exc.value, NotFoundError)


@pytest.mark.parametrize(
    ("tool", "module", "fn_name", "call_kwargs"),
    [
        (batch.batch_get_gene_xrefs, "ensembl_plants", "lookup_xrefs", {"organism": "rice"}),
        (batch.batch_phytozome_lookup_locus, "phytozome", "lookup_locus", {"organism": "rice"}),
        (batch.batch_resolve_locus_to_uniprot, "uniprot", "lookup_locus", {"organism": "rice"}),
        (
            batch.batch_locus_literature,
            "europe_pmc",
            "lookup_locus",
            {"organism": "rice", "size": 7},
        ),
        (
            batch.batch_gramene_homologs,
            "gramene",
            "lookup_homologs",
            {"homology_type": "paralog", "target_organism": "rice", "with_organism": True},
        ),
        (batch.batch_kegg_pathways, "kegg", "lookup_pathways", {"organism": "rice"}),
        (batch.batch_bar_gene_summary, "bar", "gene_summary", {}),
        (batch.batch_bar_aiv_interactions, "bar", "aiv_interactions", {"organism": "rice"}),
        (
            batch.batch_string_interactions,
            "string_db",
            "lookup_partners",
            {"organism": "rice", "limit": 3},
        ),
        (
            batch.batch_atted_coexpression,
            "atted",
            "lookup_coexpression",
            {"organism": "rice", "top_n": 4},
        ),
    ],
)
@pytest.mark.asyncio
async def test_fanout_wrappers_forward_client_organism_and_kwargs(
    monkeypatch: pytest.MonkeyPatch,
    tool: Any,
    module: str,
    fn_name: str,
    call_kwargs: dict[str, Any],
) -> None:
    """Survivors: ``fn(None, locus, ...)``, ``organism=None`` / dropped, ``size`` /
    ``limit`` / ``top_n`` / ``homology_type`` dropped, and ``_envelope(None, ...)``
    / ``results=None`` / ``errors=None``. The existing "mixed" tests checked the
    success/error split but never that the caller's client and keyword
    arguments reach the backend. One row per wrapper asserts the whole call
    and the whole envelope.
    """
    import importlib

    mod = importlib.import_module(f"plant_genomics_mcp.{module}")
    seen: list[tuple[Any, str, dict[str, Any]]] = []

    async def fake(client: httpx.AsyncClient, locus: str, **kwargs: Any) -> dict[str, Any]:
        seen.append((client, locus, kwargs))
        if locus == "AT9G99999":
            raise NotFoundError(f"nothing for {locus}")
        return {"locus": locus}

    monkeypatch.setattr(mod, fn_name, fake)
    async with httpx.AsyncClient() as client:
        env = await tool(client, ["AT1G01010", "AT9G99999"], **call_kwargs)
    assert env == {
        "tool": tool.__name__.removeprefix("batch_"),
        "count": 2,
        "results": {"AT1G01010": {"locus": "AT1G01010"}},
        "errors": {"AT9G99999": "[NotFoundError] nothing for AT9G99999"},
    }
    assert sorted(locus for _, locus, _ in seen) == ["AT1G01010", "AT9G99999"]
    for seen_client, _, kwargs in seen:
        assert seen_client is client, "the wrapper must pass the caller's client through"
        assert kwargs == call_kwargs


@pytest.mark.asyncio
async def test_batch_locus_go_annotations_forwards_both_stages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Survivors in the two-stage wrapper: ``uniprot.lookup_locus(None, ...)``,
    ``organism=None`` / dropped, ``quickgo.lookup_by_uniprot(None, ...)``,
    ``limit=None`` / dropped. The accession from stage one must be what stage
    two receives, with the caller's client and limit."""
    from plant_genomics_mcp import quickgo as _quickgo
    from plant_genomics_mcp import uniprot as _uniprot

    seen: dict[str, Any] = {}

    async def fake_uniprot(client: httpx.AsyncClient, locus: str, **kwargs: Any) -> dict[str, Any]:
        seen["uniprot"] = (client, locus, kwargs)
        return {"primaryAccession": "Q9LFT1"}

    async def fake_quickgo(
        client: httpx.AsyncClient, accession: str, **kwargs: Any
    ) -> dict[str, Any]:
        seen["quickgo"] = (client, accession, kwargs)
        return {
            "uniprot_accession": accession,  # the real function always returns it
            "numberOfHits": 1,
            "returned": 1,
            "annotations": [{"goId": "GO:1"}],
            "by_aspect": {},
        }

    monkeypatch.setattr(_uniprot, "lookup_locus", fake_uniprot)
    monkeypatch.setattr(_quickgo, "lookup_by_uniprot", fake_quickgo)
    async with httpx.AsyncClient() as client:
        env = await batch.batch_locus_go_annotations(
            client, ["AT1G01010"], organism="rice", limit=5
        )
    assert seen["uniprot"] == (client, "AT1G01010", {"organism": "rice"})
    assert seen["quickgo"] == (client, "Q9LFT1", {"limit": 5})
    assert env["tool"] == "locus_go_annotations"
    assert env["results"]["AT1G01010"]["uniprot_accession"] == "Q9LFT1"
    assert env["results"]["AT1G01010"]["annotations"] == [{"goId": "GO:1"}]


# ---------- issue #139: one refusal shape for the two forms of one tool ----------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("batch_fn", "single_gate", "field"),
    [
        (batch.batch_kegg_pathways, "kegg_org_code_for", "kegg_org_code"),
        (batch.batch_atted_coexpression, "atted_release_for", "atted_release"),
    ],
    ids=["kegg", "atted"],
)
async def test_a_batch_refuses_an_uncovered_organism_before_any_request(
    batch_fn: Any, single_gate: str, field: str, httpx_mock: HTTPXMock
) -> None:
    """The single tool raised OrganismNotSupported; its batch form answered
    ok=true with the refusal copied into errors per locus, although its
    description promised a raise before any HTTP fan-out."""
    from plant_genomics_mcp import organisms

    uncovered = next(c for c, r in organisms.ORGANISMS.items() if getattr(r, field) is None)
    covered = next(c for c, r in organisms.ORGANISMS.items() if getattr(r, field) is not None)
    # Positive control: the same gate the single form uses accepts `covered`.
    getattr(organisms, single_gate)(covered)
    async with httpx.AsyncClient() as client:
        with pytest.raises(OrganismNotSupported):
            await batch_fn(client, ["AT1G01010", "AT1G01020"], organism=uncovered)
    assert httpx_mock.get_requests() == []


@pytest.mark.parametrize(
    ("tools", "field"),
    [
        (("kegg_pathways", "batch_kegg_pathways"), "kegg_org_code"),
        (("atted_coexpression", "batch_atted_coexpression"), "atted_release"),
    ],
    ids=["kegg", "atted"],
)
def test_both_forms_state_the_organisms_the_registry_covers(tools: tuple, field: str) -> None:
    """kegg_pathways said 'only arabidopsis_thaliana resolves' while the
    refusal it raises listed seven organisms and rice answered (#139)."""
    from plant_genomics_mcp import organisms, server

    covered = sorted(organisms._supported_for(field))
    assert len(covered) > 1  # positive control: a real list, not one default
    by_name = {t.name: t for t in server.TOOLS}
    for name in tools:
        assert f"Covers: {', '.join(covered)}." in (by_name[name].description or ""), name
