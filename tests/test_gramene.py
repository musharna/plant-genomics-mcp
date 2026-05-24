"""Gramene compara backend unit tests."""

from __future__ import annotations

import os

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import gramene
from plant_genomics_mcp.errors import (
    NotFoundError,
    RateLimitError,
    UpstreamUnavailableError,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    gramene._CACHE.clear()
    yield
    gramene._CACHE.clear()


@pytest.mark.asyncio
async def test_lookup_homologs_happy_path(httpx_mock: HTTPXMock):
    # Live shape (probed 2026-05-21, see /tmp/p3_probes_2026-05-21.txt):
    # homology is a DICT, with gene_tree metadata + homologous_genes whose
    # KEYS are the homology categories and whose VALUES are flat lists of
    # locus-ID strings. There is no per-row taxon/identity/protein_id field.
    httpx_mock.add_response(
        url="https://data.gramene.org/v69/genes?idList=AT1G01010&fl=homology",
        json=[
            {
                "_id": "AT1G01010",
                "homology": {
                    "gene_tree": {
                        "id": "EPlGT01130000406172",
                        "root_taxon_id": 3193,
                        "root_taxon_name": "Embryophyta",
                        "duplications": [3193],
                    },
                    "homologous_genes": {
                        "ortholog_one2many": ["Os01g0100100"],
                        "within_species_paralog": ["AT3G15500"],
                    },
                },
            }
        ],
    )
    async with httpx.AsyncClient() as client:
        result = await gramene.lookup_homologs(client, "AT1G01010", homology_type="all")
    assert result["locus"] == "AT1G01010"
    assert result["total"] == 2
    assert len(result["homologs"]) == 2
    by_locus = {h["target_locus"]: h for h in result["homologs"]}
    assert by_locus["Os01g0100100"]["type"] == "ortholog_one2many"
    assert by_locus["Os01g0100100"]["gene_tree_id"] == "EPlGT01130000406172"
    assert by_locus["AT3G15500"]["type"] == "within_species_paralog"


@pytest.mark.asyncio
async def test_lookup_homologs_ortholog_only(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="https://data.gramene.org/v69/genes?idList=AT1G01010&fl=homology",
        json=[
            {
                "_id": "AT1G01010",
                "homology": {
                    "gene_tree": {"id": "EPlGT01130000406172"},
                    "homologous_genes": {
                        "ortholog_one2many": ["Os01g0100100"],
                        "within_species_paralog": ["AT3G15500"],
                    },
                },
            }
        ],
    )
    async with httpx.AsyncClient() as client:
        result = await gramene.lookup_homologs(client, "AT1G01010", homology_type="ortholog")
    assert result["total"] == 1
    assert result["homologs"][0]["target_locus"] == "Os01g0100100"


@pytest.mark.asyncio
async def test_lookup_homologs_empty_record_raises_not_found(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="https://data.gramene.org/v69/genes?idList=NOPE&fl=homology",
        json=[],
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError) as exc:
            await gramene.lookup_homologs(client, "NOPE")
    assert "[NotFoundError]" in str(exc.value)


@pytest.mark.asyncio
async def test_lookup_homologs_503_then_200(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="https://data.gramene.org/v69/genes?idList=AT1G01010&fl=homology",
        status_code=503,
        text="upstream",
    )
    httpx_mock.add_response(
        url="https://data.gramene.org/v69/genes?idList=AT1G01010&fl=homology",
        json=[{"_id": "AT1G01010", "homology": {"gene_tree": {}, "homologous_genes": {}}}],
    )
    async with httpx.AsyncClient() as client:
        result = await gramene.lookup_homologs(client, "AT1G01010")
    assert result["total"] == 0


@pytest.mark.asyncio
async def test_lookup_homologs_503_exhausts_raises(httpx_mock: HTTPXMock):
    for _ in range(3):
        httpx_mock.add_response(
            url="https://data.gramene.org/v69/genes?idList=AT1G01010&fl=homology",
            status_code=503,
            text="upstream",
        )
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamUnavailableError):
            await gramene.lookup_homologs(client, "AT1G01010")


@pytest.mark.asyncio
async def test_lookup_homologs_429_exhausts_raises(httpx_mock: HTTPXMock):
    for _ in range(3):
        httpx_mock.add_response(
            url="https://data.gramene.org/v69/genes?idList=AT1G01010&fl=homology",
            status_code=429,
            text="rate limit",
        )
    async with httpx.AsyncClient() as client:
        with pytest.raises(RateLimitError):
            await gramene.lookup_homologs(client, "AT1G01010")


@pytest.mark.skipif(
    not os.environ.get("PLANT_GENOMICS_MCP_LIVE"),
    reason="set PLANT_GENOMICS_MCP_LIVE=1 to hit data.gramene.org",
)
@pytest.mark.asyncio
async def test_live_gramene_at1g01010_has_homologs():
    """Smoke against real Gramene v69 — regression for upstream schema drift."""
    async with httpx.AsyncClient() as client:
        result = await gramene.lookup_homologs(client, "AT1G01010", homology_type="all")
    assert result["locus"] == "AT1G01010"
    assert result["release"] == "v69"
    assert result["total"] > 0, "AT1G01010 should have at least one homolog in v69"
    sample = result["homologs"][0]
    assert sample["type"], "homology_type field should populate"


# ---------- Wave B6: shared locus validator at the URL boundary ----------


@pytest.mark.asyncio
async def test_lookup_homologs_rejects_malformed_locus_before_http() -> None:
    """Gramene passes the locus as the ``idList`` query parameter — httpx
    encodes it, but we want defense-in-depth: malformed input never
    reaches the upstream. Test exercises the pre-HTTP guard with no
    ``httpx_mock`` configured.
    """
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="invalid locus"):
            await gramene.lookup_homologs(client, "AT1G01010<x>")


# ---------- v1.2.0: batch UniProt-acc + species enrichment for consensus_homologs ----------


@pytest.mark.asyncio
async def test_fetch_homolog_enrichment_batch_prefers_swissprot(httpx_mock: HTTPXMock):
    """SWISSPROT (Swiss-Prot, curated) wins over SPTREMBL (TrEMBL, unreviewed)
    when both are present. Mirrors UniProt's own reviewed-first heuristic
    in ``uniprot.lookup_locus``.
    """
    httpx_mock.add_response(
        url="https://data.gramene.org/v69/genes?idList=AT1G01010&fl=_id%2Cxrefs%2Csystem_name",
        json=[
            {
                "_id": "AT1G01010",
                "system_name": "arabidopsis_thaliana",
                "xrefs": [
                    {"db": "Uniprot/SPTREMBL", "ids": ["A0A178WAE4"]},
                    {"db": "Uniprot/SWISSPROT", "ids": ["Q0WV96"]},
                ],
            }
        ],
    )
    async with httpx.AsyncClient() as client:
        result = await gramene.fetch_homolog_enrichment_batch(client, ["AT1G01010"])
    assert result == {"AT1G01010": {"uniprot_acc": "Q0WV96", "system_name": "arabidopsis_thaliana"}}


@pytest.mark.asyncio
async def test_fetch_homolog_enrichment_batch_sptrembl_fallback(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="https://data.gramene.org/v69/genes?idList=Mp4g11910&fl=_id%2Cxrefs%2Csystem_name",
        json=[
            {
                "_id": "Mp4g11910",
                "system_name": "marchantia_polymorpha",
                "xrefs": [{"db": "Uniprot/SPTREMBL", "ids": ["A0A2R6XKC8"]}],
            }
        ],
    )
    async with httpx.AsyncClient() as client:
        result = await gramene.fetch_homolog_enrichment_batch(client, ["Mp4g11910"])
    assert result == {
        "Mp4g11910": {
            "uniprot_acc": "A0A2R6XKC8",
            "system_name": "marchantia_polymorpha",
        }
    }


@pytest.mark.asyncio
async def test_fetch_homolog_enrichment_batch_no_uniprot_xref_returns_none(
    httpx_mock: HTTPXMock,
):
    """Cucurbits (and a long tail of other plant genomes in Gramene v69) have
    no Swiss-Prot or TrEMBL entry. The system_name still resolves — we
    surface it so downstream callers can attribute the Gramene-only homolog
    to a species even when no canonical protein record exists.
    """
    httpx_mock.add_response(
        url="https://data.gramene.org/v69/genes?idList=Cla97C03G067000&fl=_id%2Cxrefs%2Csystem_name",
        json=[
            {
                "_id": "Cla97C03G067000",
                "system_name": "citrullus_lanatus",
                "xrefs": [{"db": "EntrezGene", "ids": ["111796305"]}],
            }
        ],
    )
    async with httpx.AsyncClient() as client:
        result = await gramene.fetch_homolog_enrichment_batch(client, ["Cla97C03G067000"])
    assert result == {"Cla97C03G067000": {"uniprot_acc": None, "system_name": "citrullus_lanatus"}}


@pytest.mark.asyncio
async def test_fetch_homolog_enrichment_batch_missing_record_returns_none(
    httpx_mock: HTTPXMock,
):
    """If a locus is in the input list but missing from Gramene's response
    (404 or filtered upstream), the dict still carries the key with both
    fields None, so the caller's join is total over the input list.
    """
    httpx_mock.add_response(
        url="https://data.gramene.org/v69/genes?idList=AT1G01010%2CNOPE&fl=_id%2Cxrefs%2Csystem_name",
        json=[
            {
                "_id": "AT1G01010",
                "system_name": "arabidopsis_thaliana",
                "xrefs": [{"db": "Uniprot/SWISSPROT", "ids": ["Q0WV96"]}],
            }
        ],
    )
    async with httpx.AsyncClient() as client:
        result = await gramene.fetch_homolog_enrichment_batch(client, ["AT1G01010", "NOPE"])
    assert result == {
        "AT1G01010": {"uniprot_acc": "Q0WV96", "system_name": "arabidopsis_thaliana"},
        "NOPE": {"uniprot_acc": None, "system_name": None},
    }


@pytest.mark.asyncio
async def test_fetch_homolog_enrichment_batch_chunks_by_size(httpx_mock: HTTPXMock):
    """URL-length safety — large idList chunked to ``chunk_size`` loci/call.
    Two chunks of 2 loci verified by both endpoint URLs receiving a response.
    """
    httpx_mock.add_response(
        url="https://data.gramene.org/v69/genes?idList=AT1G01010%2CAT3G15500&fl=_id%2Cxrefs%2Csystem_name",
        json=[
            {
                "_id": "AT1G01010",
                "system_name": "arabidopsis_thaliana",
                "xrefs": [{"db": "Uniprot/SWISSPROT", "ids": ["Q0WV96"]}],
            },
            {
                "_id": "AT3G15500",
                "system_name": "arabidopsis_thaliana",
                "xrefs": [{"db": "Uniprot/SWISSPROT", "ids": ["Q9LV28"]}],
            },
        ],
    )
    httpx_mock.add_response(
        url="https://data.gramene.org/v69/genes?idList=Os01g0100100%2CMp4g11910&fl=_id%2Cxrefs%2Csystem_name",
        json=[
            {
                "_id": "Os01g0100100",
                "system_name": "oryza_sativa",
                "xrefs": [{"db": "Uniprot/SPTREMBL", "ids": ["A0A0P0UX28"]}],
            },
            {
                "_id": "Mp4g11910",
                "system_name": "marchantia_polymorpha",
                "xrefs": [{"db": "Uniprot/SPTREMBL", "ids": ["A0A2R6XKC8"]}],
            },
        ],
    )
    async with httpx.AsyncClient() as client:
        result = await gramene.fetch_homolog_enrichment_batch(
            client,
            ["AT1G01010", "AT3G15500", "Os01g0100100", "Mp4g11910"],
            chunk_size=2,
        )
    assert result == {
        "AT1G01010": {"uniprot_acc": "Q0WV96", "system_name": "arabidopsis_thaliana"},
        "AT3G15500": {"uniprot_acc": "Q9LV28", "system_name": "arabidopsis_thaliana"},
        "Os01g0100100": {"uniprot_acc": "A0A0P0UX28", "system_name": "oryza_sativa"},
        "Mp4g11910": {
            "uniprot_acc": "A0A2R6XKC8",
            "system_name": "marchantia_polymorpha",
        },
    }


@pytest.mark.asyncio
async def test_fetch_homolog_enrichment_batch_empty_input_no_http(
    httpx_mock: HTTPXMock,
):
    """No HTTP call when the input list is empty — important so the caller
    can pass through ``gramene_payload['homologs']`` unconditionally even
    when Gramene returned zero homologs.
    """
    async with httpx.AsyncClient() as client:
        result = await gramene.fetch_homolog_enrichment_batch(client, [])
    assert result == {}
    # pytest_httpx asserts no unmatched mocks on teardown; reaching this
    # line with no responses configured proves no HTTP was issued.
