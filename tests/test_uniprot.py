"""Tests for the UniProt REST client.

Two tiers (mirrors the ensembl_plants test layout):
  1. Unit tests with mocked HTTP via pytest-httpx (always run).
  2. Live integration test gated by PLANT_GENOMICS_MCP_LIVE=1, hitting
     the real rest.uniprot.org. Satisfies the real-execution-check doctrine.
"""

from __future__ import annotations

import os

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import uniprot
from plant_genomics_mcp.errors import NotFoundError

LIVE = os.environ.get("PLANT_GENOMICS_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set PLANT_GENOMICS_MCP_LIVE=1 to run")


# Helper: build a plausible UniProtKB search response with one hit.
def _one_hit(
    *,
    accession: str = "Q0WV96",
    uniprotkb_id: str = "NAC1_ARATH",
    entry_type: str = "UniProtKB reviewed (Swiss-Prot)",
    name: str = "NAC domain-containing protein 1",
    gene: str = "NAC001",
    organism: str = "Arabidopsis thaliana",
    taxon: int = 3702,
    length: int = 429,
) -> dict:
    return {
        "results": [
            {
                "primaryAccession": accession,
                "uniProtkbId": uniprotkb_id,
                "entryType": entry_type,
                "proteinDescription": {"recommendedName": {"fullName": {"value": name}}},
                "genes": [{"geneName": {"value": gene}}],
                "organism": {"scientificName": organism, "taxonId": taxon},
                "sequence": {"length": length},
            }
        ]
    }


# ---------- mocked unit tests ----------


@pytest.mark.asyncio
async def test_lookup_locus_at1g01010_returns_q0wv96(httpx_mock: HTTPXMock) -> None:
    """Default Arabidopsis path — single reviewed hit, normalized shape."""
    httpx_mock.add_response(
        url=(
            "https://rest.uniprot.org/uniprotkb/search"
            "?query=%28gene%3AAT1G01010+OR+xref%3Aensemblplants-AT1G01010%29+AND+organism_id%3A3702+AND+reviewed%3Atrue"
            "&format=json&size=1"
        ),
        json=_one_hit(),
    )
    async with httpx.AsyncClient() as client:
        result = await uniprot.lookup_locus(client, "AT1G01010")
    assert result["primaryAccession"] == "Q0WV96"
    assert result["uniProtkbId"] == "NAC1_ARATH"
    assert result["reviewed"] is True
    assert result["recommendedName"] == "NAC domain-containing protein 1"
    assert result["geneNames"] == ["NAC001"]
    assert result["organism"] == "Arabidopsis thaliana"
    assert result["taxonId"] == 3702
    assert result["sequenceLength"] == 429
    assert result["web_url"] == "https://www.uniprot.org/uniprotkb/Q0WV96"
    assert result["locus_query"] == "AT1G01010"


@pytest.mark.asyncio
async def test_lookup_locus_falls_back_to_unreviewed(httpx_mock: HTTPXMock) -> None:
    """Rice path — no Swiss-Prot hit, falls back to TrEMBL."""
    # Pass 1: reviewed=true → 0 hits
    httpx_mock.add_response(
        url=(
            "https://rest.uniprot.org/uniprotkb/search"
            "?query=%28gene%3AOs01g0100100+OR+xref%3Aensemblplants-Os01g0100100%29+AND+organism_id%3A39947+AND+reviewed%3Atrue"
            "&format=json&size=1"
        ),
        json={"results": []},
    )
    # Pass 2: no reviewed filter → 1 TrEMBL hit
    httpx_mock.add_response(
        url=(
            "https://rest.uniprot.org/uniprotkb/search"
            "?query=%28gene%3AOs01g0100100+OR+xref%3Aensemblplants-Os01g0100100%29+AND+organism_id%3A39947"
            "&format=json&size=1"
        ),
        json=_one_hit(
            accession="Q0JRI1",
            uniprotkb_id="Q0JRI1_ORYSJ",
            entry_type="UniProtKB unreviewed (TrEMBL)",
            name="Os01g0100100 protein",
            gene="Os01g0100100",
            organism="Oryza sativa subsp. japonica",
            taxon=39947,
            length=200,
        ),
    )
    async with httpx.AsyncClient() as client:
        result = await uniprot.lookup_locus(client, "Os01g0100100", organism=39947)
    assert result["primaryAccession"] == "Q0JRI1"
    assert result["reviewed"] is False
    assert "TrEMBL" in result["entryType"]


@pytest.mark.asyncio
async def test_lookup_locus_raises_not_found_when_both_passes_empty(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        url=(
            "https://rest.uniprot.org/uniprotkb/search"
            "?query=%28gene%3ANOTREAL+OR+xref%3Aensemblplants-NOTREAL%29+AND+organism_id%3A3702+AND+reviewed%3Atrue"
            "&format=json&size=1"
        ),
        json={"results": []},
    )
    httpx_mock.add_response(
        url=(
            "https://rest.uniprot.org/uniprotkb/search"
            "?query=%28gene%3ANOTREAL+OR+xref%3Aensemblplants-NOTREAL%29+AND+organism_id%3A3702"
            "&format=json&size=1"
        ),
        json={"results": []},
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="no entry for gene=NOTREAL"):
            await uniprot.lookup_locus(client, "NOTREAL")


@pytest.mark.asyncio
async def test_lookup_locus_retries_on_429_then_succeeds(httpx_mock: HTTPXMock) -> None:
    url = (
        "https://rest.uniprot.org/uniprotkb/search"
        "?query=%28gene%3AAT1G01010+OR+xref%3Aensemblplants-AT1G01010%29+AND+organism_id%3A3702+AND+reviewed%3Atrue"
        "&format=json&size=1"
    )
    httpx_mock.add_response(url=url, status_code=429, headers={"Retry-After": "0"})
    httpx_mock.add_response(url=url, json=_one_hit())
    async with httpx.AsyncClient() as client:
        result = await uniprot.lookup_locus(client, "AT1G01010")
    assert result["primaryAccession"] == "Q0WV96"


# ---------- accession-shaped input dispatch ----------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("Q9LIV2", True),  # 6-char legacy
        ("P12345", True),
        ("Q9FLJ2.1", True),  # with version suffix (BLAST shape)
        ("A0A1B2C3D4", True),  # 10-char extended
        ("AT1G01010", False),  # TAIR locus
        ("Os01g0100100", False),  # rice locus
        ("NP_001185207.1", False),  # NCBI RefSeq
        ("", False),
        ("GARBAGE", False),
        # Only a numeric .N is a version; cutting at the first dot made any
        # "<accession>.<anything>" an accession (the H1 prefix-truncation class).
        ("Q9FLJ2.x", False),
        ("Q9FLJ2.1.2", False),
    ],
)
def test_looks_like_uniprot_accession_regex(value: str, expected: bool) -> None:
    assert uniprot._looks_like_uniprot_accession(value) is expected


@pytest.mark.asyncio
async def test_lookup_locus_with_accession_input_uses_direct_fetch(
    httpx_mock: HTTPXMock,
) -> None:
    """UniProt-shaped input routes to /uniprotkb/{accession}.json — no search."""
    httpx_mock.add_response(
        url="https://rest.uniprot.org/uniprotkb/Q9FLJ2.json",
        json={
            "primaryAccession": "Q9FLJ2",
            "uniProtkbId": "NC100_ARATH",
            "entryType": "UniProtKB reviewed (Swiss-Prot)",
            "proteinDescription": {
                "recommendedName": {"fullName": {"value": "NAC domain-containing protein 100"}}
            },
            "genes": [{"geneName": {"value": "NAC100"}}],
            "organism": {"scientificName": "Arabidopsis thaliana", "taxonId": 3702},
            "sequence": {"length": 336},
        },
    )
    async with httpx.AsyncClient() as client:
        # Pass the BLAST-style versioned accession; strip happens internally.
        result = await uniprot.lookup_locus(client, "Q9FLJ2.1")
    assert result["primaryAccession"] == "Q9FLJ2"
    assert result["uniProtkbId"] == "NC100_ARATH"
    assert result["reviewed"] is True
    # locus_query preserves the original (versioned) input for client traceability.
    assert result["locus_query"] == "Q9FLJ2.1"
    # No /uniprotkb/search request should have been issued.
    for req in httpx_mock.get_requests():
        assert "/uniprotkb/search" not in str(req.url)


@pytest.mark.asyncio
async def test_lookup_locus_with_accession_404_raises_not_found(
    httpx_mock: HTTPXMock,
) -> None:
    """Q9XXX9 is accession-shaped (last char must be a digit) but synthetic."""
    httpx_mock.add_response(
        url="https://rest.uniprot.org/uniprotkb/Q9XXX9.json",
        status_code=404,
        text="not found",
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="no entry for accession='Q9XXX9'"):
            await uniprot.lookup_locus(client, "Q9XXX9")


# ---------- live integration (real-execution check) ----------


@pytest.mark.asyncio
async def test_fetch_sequence_happy_path(httpx_mock):
    fasta = (
        ">sp|Q0WV96|Y1010_ARATH Probable inactive receptor kinase OS=Arabidopsis thaliana\n"
        "MEDQVGFGFRPNDEELVGHYLRNKIEGNTSRDVEVAISEVNICSY\n"
        "PFQPRADRAA\n"
    )
    httpx_mock.add_response(
        url="https://rest.uniprot.org/uniprotkb/Q0WV96.fasta",
        text=fasta,
    )
    async with httpx.AsyncClient() as client:
        seq = await uniprot.fetch_sequence(client, "Q0WV96")
    assert seq == "MEDQVGFGFRPNDEELVGHYLRNKIEGNTSRDVEVAISEVNICSYPFQPRADRAA"


@pytest.mark.asyncio
async def test_fetch_sequence_404_raises_not_found(httpx_mock):
    httpx_mock.add_response(
        url="https://rest.uniprot.org/uniprotkb/NOSUCH.fasta",
        status_code=404,
        text="",
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(uniprot.NotFoundError):
            await uniprot.fetch_sequence(client, "NOSUCH")


@pytest.mark.asyncio
async def test_fetch_sequence_caches_result(httpx_mock):
    uniprot._CACHE.clear()
    fasta = ">sp|Q0WV96|X\nMEDQ\n"
    httpx_mock.add_response(
        url="https://rest.uniprot.org/uniprotkb/Q0WV96.fasta",
        text=fasta,
    )
    async with httpx.AsyncClient() as client:
        a = await uniprot.fetch_sequence(client, "Q0WV96")
        b = await uniprot.fetch_sequence(client, "Q0WV96")
    assert a == b == "MEDQ"


@live_only
@pytest.mark.asyncio
async def test_live_lookup_at1g01010() -> None:
    """Real call to rest.uniprot.org — verifies wire format hasn't drifted."""
    async with httpx.AsyncClient() as client:
        result = await uniprot.lookup_locus(client, "AT1G01010")
    assert result["primaryAccession"] == "Q0WV96"
    assert result["uniProtkbId"] == "NAC1_ARATH"
    assert result["reviewed"] is True
    assert result["taxonId"] == 3702


@live_only
@pytest.mark.asyncio
async def test_live_lookup_rice_falls_back_to_trembl() -> None:
    """Real call for rice locus — should fall back to TrEMBL (no Swiss-Prot)."""
    async with httpx.AsyncClient() as client:
        result = await uniprot.lookup_locus(client, "Os01g0100100", organism=39947)
    assert result["primaryAccession"]  # any non-empty accession
    # If UniProt later curates this locus into Swiss-Prot, this test will need
    # updating — for now, asserting TrEMBL doubles as a wire-format guard.
    assert "TrEMBL" in result["entryType"]


# ---------- v0.9 resolver migration ----------


def test_lookup_locus_accepts_organism_param(httpx_mock: HTTPXMock) -> None:
    """v0.9 T9: lookup_locus accepts organism= (slug/name/taxid) via resolver."""
    import asyncio
    import re

    import httpx as _httpx

    httpx_mock.add_response(
        url=re.compile(r"https://rest\.uniprot\.org/.*"),
        json=_one_hit(),
    )

    async def run() -> dict:
        async with _httpx.AsyncClient() as client:
            return await uniprot.lookup_locus(client, "AT1G01010", organism="arabidopsis_thaliana")

    result = asyncio.run(run())
    assert result is not None
    assert result["primaryAccession"] == "Q0WV96"


def test_normalize_rejects_non_string_gene_name():
    """#95: the normaliser owns the ``geneNames: list[str]`` invariant that
    synthesis._reconcile_analyze consumes. A non-string ``geneName.value``
    must raise the typed error; a string value lands in the list and a
    missing value is skipped (positive controls).
    """
    from plant_genomics_mcp.errors import PlantGenomicsError

    base = {"primaryAccession": "Q0WV96", "entryType": "UniProtKB reviewed (Swiss-Prot)"}
    with pytest.raises(PlantGenomicsError, match="geneName"):
        uniprot._normalize({**base, "genes": [{"geneName": {"value": ["NAC001"]}}]}, "AT1G01010")
    with pytest.raises(PlantGenomicsError, match="geneName"):
        uniprot._normalize({**base, "genes": [{"geneName": {"value": 7}}]}, "AT1G01010")

    ok = uniprot._normalize(
        {**base, "genes": [{"geneName": {"value": "NAC001"}}, {"geneName": {}}]}, "AT1G01010"
    )
    assert ok["geneNames"] == ["NAC001"]


# ---------- issue #138: wheat IWGSC ids are UniProt cross-references ----------


@live_only
@pytest.mark.asyncio
async def test_live_wheat_iwgsc_locus_resolves_and_the_other_organisms_still_do() -> None:
    """Every wheat IWGSC locus failed: UniProt carries TraesCS… ids only as
    EnsemblPlants cross-references, never as gene names (live, 2026-09-22:
    gene: 0 hits, xref:ensemblplants- 1 hit, A0A3B6EER4, taxon 4565)."""
    async with httpx.AsyncClient() as client:
        wheat = await uniprot.lookup_locus(
            client, "TraesCS3A02G159200", organism="triticum_aestivum"
        )
        # Positive controls: the gene-name path still answers as before.
        ath = await uniprot.lookup_locus(client, "AT1G19850")
        rice = await uniprot.lookup_locus(client, "Os01g0236300", organism="oryza_sativa")
    assert (wheat["primaryAccession"], wheat["taxonId"]) == ("A0A3B6EER4", 4565)
    assert ath["primaryAccession"] == "P93024"
    assert rice["primaryAccession"] == "Q5NB85"
