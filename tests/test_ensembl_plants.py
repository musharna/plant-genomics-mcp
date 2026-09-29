"""Tests for the Ensembl Plants REST client.

Two tiers (mirrors the genomics-mcp sibling pattern):
  1. Unit tests with mocked HTTP via pytest-httpx (always run).
  2. Live integration tests gated by PLANT_GENOMICS_MCP_LIVE=1, hitting
     the real rest.ensembl.org. These satisfy the real-execution-check
     doctrine.
"""

from __future__ import annotations

import os
import re

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import _http, ensembl_plants, organisms  # noqa: F401
from plant_genomics_mcp.errors import UpstreamUnavailableError

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
        json={"id": "AT1G01010", "display_name": "NAC001", "species": "arabidopsis_thaliana"},
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
        json={"id": "AT1G01010", "display_name": "NAC001", "species": "arabidopsis_thaliana"},
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
        json={"id": "AT1G01010", "display_name": "NAC001", "species": "arabidopsis_thaliana"},
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
        is_reusable=True,
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(
            ensembl_plants.UpstreamUnavailableError,
            match=r"answered 200 twice without a readable result \(dict, not a list\)",
        ):
            await ensembl_plants.lookup_xrefs(client, "AT1G01010")
    assert len(httpx_mock.get_requests(url=re.compile(".*/xrefs/id/"))) == 2


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
        json={"id": "AT1G01010", "biotype": "protein_coding", "species": "arabidopsis_thaliana"},
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


# ---------- get_sequence unit tests ----------


_AT_LOOKUP = "https://rest.ensembl.org/lookup/id/{}?species=arabidopsis_thaliana&expand=0"


def _gene(gene_id: str, transcript: str) -> dict[str, object]:
    """A /lookup/id gene record as served live: version None, so Ensembl's
    canonical_transcript ends in a bare '.' (see the #137 tests below)."""
    return {
        "id": gene_id,
        "object_type": "Gene",
        "species": "arabidopsis_thaliana",
        "version": None,
        "canonical_transcript": f"{transcript}.",
    }


@pytest.mark.asyncio
async def test_get_sequence_default_type_is_protein(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=_AT_LOOKUP.format("AT1G01010"), json=_gene("AT1G01010", "AT1G01010.1")
    )
    httpx_mock.add_response(
        url="https://rest.ensembl.org/sequence/id/AT1G01010.1?species=arabidopsis_thaliana&type=protein",
        json={
            "id": "AT1G01010.1",
            "query": "AT1G01010.1",
            "molecule": "protein",
            "seq": "MEDQVGFGFRPNDEELVGHYL",
            "version": 1,
            "desc": None,
        },
    )
    async with httpx.AsyncClient() as client:
        result = await ensembl_plants.get_sequence(client, "AT1G01010")
    assert result["locus"] == "AT1G01010"
    assert result["organism"] == "arabidopsis_thaliana"
    assert result["type"] == "protein"
    assert result["molecule"] == "protein"
    assert result["sequence"].startswith("MEDQ")
    assert result["length"] == len("MEDQVGFGFRPNDEELVGHYL")
    assert result["ensembl_id"] == "AT1G01010.1"


_AT1G01010_PROTEIN_URL = (
    "https://rest.ensembl.org/sequence/id/AT1G01010.1?species=arabidopsis_thaliana&type=protein"
)
# The first 60 residues of AT1G01010.1's live CDS (1290 nt, 2026-09-29)
# translated one frame off; in full that is 429 aa, as long as the real protein,
# with 14 stops, like the wrong-frame protein /sequence served for another gene
# during that night's Ensembl incident.
_WRONG_FRAME = "WRIKLGLGSVRTTRSSLVTISVTKSKETLAATLK*PSARSTSVATILGTCASSQSTNREM"
# The real protein's opening, and the same with a stop on the end: its CDS is
# 430 codons with the stop, and /sequence answered 430 aa that night.
_REAL = "MEDQVGFGFRPNDEELVGHYL"


def _protein(seq: object) -> dict[str, object]:
    return {"id": "AT1G01010.1", "query": "AT1G01010.1", "molecule": "protein", "seq": seq}


@pytest.mark.asyncio
async def test_a_protein_with_a_stop_symbol_is_not_an_answer(httpx_mock: HTTPXMock) -> None:
    """No Ensembl Plants protein holds a ``*`` (0 of 694,618, all 12
    organisms); get_sequence passed a wrong-frame one on as the answer."""
    httpx_mock.add_response(
        url=_AT_LOOKUP.format("AT1G01010"), json=_gene("AT1G01010", "AT1G01010.1")
    )
    # The second pair names no molecule: the check follows the type asked for.
    unnamed = {"id": "AT1G01010.1", "seq": _REAL + "*"}
    for bad in (_protein(_WRONG_FRAME), _protein(_WRONG_FRAME), unnamed, unnamed):
        httpx_mock.add_response(url=_AT1G01010_PROTEIN_URL, json=bad)
    httpx_mock.add_response(url=_AT1G01010_PROTEIN_URL, json=_protein(_REAL))
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamUnavailableError, match=r"stop symbol at residue 35 of 60"):
            await ensembl_plants.get_sequence(client, "AT1G01010")
        with pytest.raises(UpstreamUnavailableError, match=r"stop symbol at residue 22 of 22"):
            await ensembl_plants.get_sequence(client, "AT1G01010")
        # Positive control, same cache: nothing bad was stored, and a protein
        # without a stop is the answer.
        result = await ensembl_plants.get_sequence(client, "AT1G01010")
    assert (result["sequence"], result["length"]) == (_REAL, len(_REAL))
    assert len(httpx_mock.get_requests(url=_AT1G01010_PROTEIN_URL)) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("seq_type", ["protein", "cds"])
async def test_an_empty_or_non_string_seq_is_not_an_answer(
    httpx_mock: HTTPXMock, seq_type: str
) -> None:
    """``{"seq": ""}`` was relayed as a 0-length answer, and a null or a list
    passed the shape as well."""
    url = _AT1G01010_PROTEIN_URL.replace("type=protein", f"type={seq_type}")
    httpx_mock.add_response(
        url=_AT_LOOKUP.format("AT1G01010"), json=_gene("AT1G01010", "AT1G01010.1")
    )
    for bad in ("", None, ["M"]):
        for _ in range(2):
            httpx_mock.add_response(url=url, json=_protein(bad))
    good = _REAL if seq_type == "protein" else "ATGGAGGATCAAGTTGGG"
    httpx_mock.add_response(url=url, json=_protein(good))
    async with httpx.AsyncClient() as client:
        for bad in ("", None, ["M"]):
            with pytest.raises(UpstreamUnavailableError, match=re.escape(f"seq is {bad!r}")):
                await ensembl_plants.get_sequence(client, "AT1G01010", seq_type=seq_type)
        # Positive control, same cache: a sequence is the answer.
        result = await ensembl_plants.get_sequence(client, "AT1G01010", seq_type=seq_type)
    assert (result["sequence"], result["length"]) == (good, len(good))
    assert len(httpx_mock.get_requests(url=url)) == 7


@pytest.mark.asyncio
@pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
@pytest.mark.parametrize("seq_type", ["protein", "cds", "cdna"])
async def test_get_sequence_of_a_multi_transcript_gene_is_its_canonical_product(
    httpx_mock: HTTPXMock, seq_type: str
) -> None:
    """Audit 2026-09-22 M3: /sequence/id on a GENE with type protein/cds/cdna
    is a 400 on any gene with more than one transcript (live, AT2G33860/ETT:
    "2 sequences detected ... specify the multiple_sequences parameter"), so
    the tool failed on most real genes. The product belongs to a transcript:
    the tool asks for the canonical one, as its description always said."""
    httpx_mock.add_response(  # the real answer, served to the gene-level request
        url=(
            "https://rest.ensembl.org/sequence/id/AT2G33860"
            f"?species=arabidopsis_thaliana&type={seq_type}"
        ),
        status_code=400,
        json={
            "error": 'Requesting a gene and type not equal to "genomic" can result in '
            "multiple sequences. 2 sequences detected. Please rerun your request and "
            "specify the multiple_sequences parameter"
        },
    )
    httpx_mock.add_response(
        url=_AT_LOOKUP.format("AT2G33860"), json=_gene("AT2G33860", "AT2G33860.1")
    )
    httpx_mock.add_response(
        url=(
            "https://rest.ensembl.org/sequence/id/AT2G33860.1"
            f"?species=arabidopsis_thaliana&type={seq_type}"
        ),
        json={"id": "AT2G33860.1", "query": "AT2G33860.1", "molecule": "x", "seq": "MGGLIDLNV"},
    )
    async with httpx.AsyncClient() as client:
        result = await ensembl_plants.get_sequence(client, "AT2G33860", seq_type=seq_type)
    assert result["sequence"] == "MGGLIDLNV"
    assert result["ensembl_id"] == "AT2G33860.1"
    assert result["locus"] == "AT2G33860"


@pytest.mark.asyncio
async def test_get_sequence_gene_without_a_canonical_transcript_is_not_found(
    httpx_mock: HTTPXMock,
) -> None:
    """A gene with no transcript (e.g. a non-coding locus record) has no product."""
    from plant_genomics_mcp.errors import NotFoundError

    httpx_mock.add_response(
        url=_AT_LOOKUP.format("AT1G01010"),
        json={"id": "AT1G01010", "object_type": "Gene", "species": "arabidopsis_thaliana"},
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="no canonical transcript"):
            await ensembl_plants.get_sequence(client, "AT1G01010")


@pytest.mark.asyncio
async def test_get_sequence_of_a_transcript_id_is_that_transcripts_product(
    httpx_mock: HTTPXMock,
) -> None:
    """Positive control for the canonical hop: a transcript id (object_type
    Transcript, live shape for AT2G33860.1) is fetched as itself."""
    httpx_mock.add_response(
        url=_AT_LOOKUP.format("AT2G33860.2"),
        json={"id": "AT2G33860.2", "object_type": "Transcript", "species": "arabidopsis_thaliana"},
    )
    httpx_mock.add_response(
        url="https://rest.ensembl.org/sequence/id/AT2G33860.2?species=arabidopsis_thaliana&type=protein",
        json={"id": "AT2G33860.2", "molecule": "protein", "seq": "MKK"},
    )
    async with httpx.AsyncClient() as client:
        result = await ensembl_plants.get_sequence(client, "AT2G33860.2")
    assert result["ensembl_id"] == "AT2G33860.2" and result["sequence"] == "MKK"


@pytest.mark.asyncio
async def test_get_sequence_genomic_type_routes_type_param(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://rest.ensembl.org/sequence/id/AT1G01010?species=arabidopsis_thaliana&type=genomic",
        json={"id": "AT1G01010", "molecule": "dna", "seq": "ACGTACGTAC", "query": "AT1G01010"},
    )
    async with httpx.AsyncClient() as client:
        result = await ensembl_plants.get_sequence(client, "AT1G01010", seq_type="genomic")
    assert result["type"] == "genomic"
    assert result["molecule"] == "dna"
    assert result["length"] == 10


@pytest.mark.asyncio
async def test_get_sequence_rejects_invalid_seq_type() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="seq_type"):
            await ensembl_plants.get_sequence(client, "AT1G01010", seq_type="bogus")


@pytest.mark.asyncio
async def test_get_sequence_raises_on_unexpected_payload(httpx_mock: HTTPXMock) -> None:
    from plant_genomics_mcp.errors import UpstreamUnavailableError

    httpx_mock.add_response(
        url=_AT_LOOKUP.format("AT1G01010"), json=_gene("AT1G01010", "AT1G01010.1")
    )
    httpx_mock.add_response(
        url="https://rest.ensembl.org/sequence/id/AT1G01010.1?species=arabidopsis_thaliana&type=protein",
        json=[{"seq": "X"}],  # Ensembl should hand back a dict, not a list.
        is_reusable=True,
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(
            UpstreamUnavailableError,
            match=r"answered 200 twice without a readable result \(list, not an object\)",
        ):
            await ensembl_plants.get_sequence(client, "AT1G01010")
    assert len(httpx_mock.get_requests(url=re.compile(".*/sequence/id/"))) == 2


@pytest.mark.asyncio
async def test_get_sequence_rejects_malformed_locus_before_http() -> None:
    from plant_genomics_mcp.errors import NotFoundError

    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="invalid locus"):
            await ensembl_plants.get_sequence(client, "AT1G01010/extra")


# ---------- region_query unit tests ----------


@pytest.mark.asyncio
async def test_region_query_returns_overlapping_genes(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://rest.ensembl.org/overlap/region/arabidopsis_thaliana/1:3000-10000?feature=gene",
        json=[
            {
                "id": "AT1G01020",
                "feature_type": "gene",
                "external_name": "ARV1",
                "biotype": "protein_coding",
                "seq_region_name": "1",
                "start": 6788,
                "end": 9130,
                "strand": -1,
            }
        ],
    )
    async with httpx.AsyncClient() as client:
        result = await ensembl_plants.region_query(client, "1", 3000, 10000)
    assert result["organism"] == "arabidopsis_thaliana"
    assert result["region"] == "1:3000-10000"
    assert result["feature"] == "gene"
    assert result["count"] == 1
    assert result["features"][0]["id"] == "AT1G01020"
    assert result["features"][0]["external_name"] == "ARV1"


# A tomato gene row as Ensembl sends it (live, 2026-09-29: none of the 13 genes
# in CM001064.4:100000-200000 carries external_name). The row is copied into
# the answer, so a key the record lacks stays absent.
_TOMATO_GENE = {
    "source": "Community",
    "start": 119339,
    "end": 122563,
    "id": "gene-Solyc01g005120.3",
    "strand": -1,
    "feature_type": "gene",
    "gene_id": "gene-Solyc01g005120.3",
    "biotype": "protein_coding",
    "logic_name": "gff3_genes",
    "assembly_name": "SL4.0",
    "canonical_transcript": "mRNA-Solyc01g005120.3.1.",
    "seq_region_name": "CM001064.4",
    "description": None,
}
# An Arabidopsis CDS row as sent (live, 2026-09-29): exon and cds rows of every
# organism probed carry no biotype, external_name or description.
_ARABIDOPSIS_CDS = {
    "protein_id": "AT1G01020.1",
    "source": "araport11",
    "end": 8666,
    "start": 8571,
    "seq_region_name": "1",
    "id": "AT1G01020.1",
    "feature_type": "cds",
    "assembly_name": "TAIR10",
    "version": None,
    "Parent": "AT1G01020.1",
    "strand": -1,
    "phase": 0,
}


@pytest.mark.asyncio
async def test_region_query_passes_rows_missing_declared_fields_through(
    httpx_mock: HTTPXMock,
) -> None:
    """A gene with no symbol and a CDS with no biotype are answered as Ensembl
    sent them, not refused by the output contract; the Arabidopsis gene with
    a symbol keeps it."""
    httpx_mock.add_response(
        url=re.compile(
            r"^https://rest\.ensembl\.org/overlap/region/"
            r"solanum_lycopersicum_gca000188115v5cm/CM001064\.4:100000-200000\?feature=gene$"
        ),
        json=[_TOMATO_GENE],
    )
    httpx_mock.add_response(
        url="https://rest.ensembl.org/overlap/region/arabidopsis_thaliana/1:3000-10000?feature=cds",
        json=[_ARABIDOPSIS_CDS],
    )
    httpx_mock.add_response(
        url="https://rest.ensembl.org/overlap/region/arabidopsis_thaliana/1:3000-10000?feature=gene",
        json=[{"id": "AT1G01020", "feature_type": "gene", "external_name": "ARV1"}],
    )
    async with httpx.AsyncClient() as client:
        tomato = await ensembl_plants.region_query(
            client, "CM001064.4", 100000, 200000, organism="solanum_lycopersicum"
        )
        cds = await ensembl_plants.region_query(client, "1", 3000, 10000, feature="cds")
        arabidopsis = await ensembl_plants.region_query(client, "1", 3000, 10000)
    assert tomato["features"] == [_TOMATO_GENE]
    assert "external_name" not in tomato["features"][0]
    assert cds["features"] == [_ARABIDOPSIS_CDS]
    assert arabidopsis["features"][0]["external_name"] == "ARV1"


@pytest.mark.asyncio
async def test_region_query_empty_region_returns_zero(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://rest.ensembl.org/overlap/region/arabidopsis_thaliana/2:1-2?feature=gene",
        json=[],
    )
    async with httpx.AsyncClient() as client:
        result = await ensembl_plants.region_query(client, "2", 1, 2)
    assert result["count"] == 0
    assert result["features"] == []


@pytest.mark.asyncio
async def test_region_query_rejects_invalid_feature() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="feature"):
            await ensembl_plants.region_query(client, "1", 3000, 10000, feature="banana")


@pytest.mark.asyncio
@pytest.mark.parametrize("region", ["1?feature=exon", "1/../x", "1 2", "1#f", "a&b", "1%2f"])
async def test_region_query_rejects_path_metachars(region: str) -> None:
    """``region`` is spliced into the URL path, so path/query metacharacters must
    be rejected before any network call (bug audit M1) — the same guard
    ``vep_annotate`` applies to its own path-templated ``region``."""
    from plant_genomics_mcp.errors import NotFoundError

    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError):
            await ensembl_plants.region_query(client, region, 1, 100)


@pytest.mark.asyncio
async def test_region_query_rejects_start_below_one() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="start"):
            await ensembl_plants.region_query(client, "1", 0, 100)


@pytest.mark.asyncio
async def test_region_query_rejects_end_before_start() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="end"):
            await ensembl_plants.region_query(client, "1", 100, 50)


@pytest.mark.asyncio
async def test_region_query_raises_on_non_list_payload(httpx_mock: HTTPXMock) -> None:
    from plant_genomics_mcp.errors import UpstreamUnavailableError

    httpx_mock.add_response(
        url="https://rest.ensembl.org/overlap/region/arabidopsis_thaliana/1:3000-10000?feature=gene",
        json={"error": "something"},  # Ensembl overlap returns an array on success.
        is_reusable=True,
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(
            UpstreamUnavailableError,
            match=r"answered 200 twice without a readable result \(dict, not a list\)",
        ):
            await ensembl_plants.region_query(client, "1", 3000, 10000)
    assert len(httpx_mock.get_requests(url=re.compile(".*/overlap/region/"))) == 2


@live_only
@pytest.mark.asyncio
async def test_live_get_sequence_at1g01010_protein() -> None:
    """Real /sequence/id call — NAC001 protein is 429 aa."""
    async with httpx.AsyncClient() as client:
        result = await ensembl_plants.get_sequence(client, "AT1G01010", seq_type="protein")
    assert result["molecule"] == "protein"
    assert result["length"] == 429
    assert result["sequence"].startswith("M")


@live_only
@pytest.mark.asyncio
async def test_live_get_sequence_multi_transcript_gene_protein() -> None:
    """Real execution for M3: ETT (AT2G33860) has 2 transcripts; the gene-level
    request is a 400, the canonical transcript's protein is the answer."""
    async with httpx.AsyncClient() as client:
        result = await ensembl_plants.get_sequence(client, "AT2G33860", seq_type="protein")
    assert result["molecule"] == "protein"
    assert result["sequence"].startswith("MGGLIDLNV")


# One gene per organism, from the first record of its Ensembl Plants release 63
# pep.all FASTA (2026-09-29); rice's first record, a plastid gene, and
# sorghum's, on an unplaced scaffold, are swapped for chromosome genes, and
# tomato is given without the ``gene-`` prefix it is filed under.
_PROTEIN_PROBES: dict[str, str] = {
    "arabidopsis_thaliana": "AT5G16970",
    "oryza_sativa": "Os01g0100100",
    "zea_mays": "Zm00001eb096110",
    "triticum_aestivum": "TraesCS4A02G403700",
    "solanum_lycopersicum": "Solyc04g011850.1",
    "glycine_max": "GLYMA_01G141900",
    "sorghum_bicolor": "SORBI_3001G000100",
    "hordeum_vulgare": "HORVU.MOREX.r3.7HG0737000",
    "vitis_vinifera": "Vitis15g00095",
    "populus_trichocarpa": "Potri.005G200100.v4.1",
    "medicago_truncatula": "gene36912",
    "brachypodium_distachyon": "BRADI_41430s00200v3",
}


def test_protein_probes_cover_every_organism() -> None:
    assert set(_PROTEIN_PROBES) == set(organisms.ORGANISMS)


# What the sequence shapes say when they refuse a body; a refusal of a real
# protein is ours, not an outage, so it must not reach the nightly as one.
_BASES = "TCAG"
_AMINO = "FFLLSSSSYY**CC*WLLLLPPPPHHQQRRRRIIIMTTTTNNKKSSRRVVVVAAAADDEEGGGG"
# The standard genetic code, in TCAG order.
_CODON = {
    a + b + c: _AMINO[16 * i + 4 * j + k]
    for i, a in enumerate(_BASES)
    for j, b in enumerate(_BASES)
    for k, c in enumerate(_BASES)
}


def _translate(cds: str) -> str:
    return "".join(_CODON.get(cds[i : i + 3], "X") for i in range(0, len(cds) - 2, 3))


async def _protein_or_our_failure(
    client: httpx.AsyncClient, gene: str, organism: str
) -> dict[str, object]:
    """The protein, or an outage as it came. A stop-symbol refusal is ours
    only if the protein really holds a stop: then its CDS, read in frame 0,
    encodes one before its end, and the failure carries no upstream tag, so
    the nightly classes it a regression. A CDS without one means Ensembl
    served a protein its own CDS does not encode (the 2026-09-29 incident):
    the outage is raised as it came."""
    try:
        return await ensembl_plants.get_sequence(client, gene, organism=organism)
    except UpstreamUnavailableError as exc:
        if ensembl_plants.STOP_SYMBOL_REFUSAL not in str(exc):
            raise
        cds = await ensembl_plants.get_sequence(client, gene, organism=organism, seq_type="cds")
        if "*" not in _translate(cds["sequence"]).rstrip("*"):
            raise exc
        raise AssertionError(
            f"{organism} {gene}: its CDS encodes a stop, and its protein was refused for one"
        ) from exc


def test_the_translation_reads_the_live_cds() -> None:
    """The codon table against AT1G01010.1's live protein (2026-09-29): its
    CDS in frame 0 is that protein plus the stop."""
    assert _translate("ATGGAGGATCAAGTTGGGTGA") == "MEDQVG*"
    assert _translate("ATGTAAGGG") == "M*G"


@pytest.mark.asyncio
async def test_a_refusal_is_ours_only_when_the_cds_encodes_the_stop(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _no_sleep(_seconds: float) -> None:
        pass

    monkeypatch.setattr(_http.asyncio, "sleep", _no_sleep)
    cds_url = _AT1G01010_PROTEIN_URL.replace("type=protein", "type=cds")

    def lookup() -> None:
        httpx_mock.add_response(
            url=_AT_LOOKUP.format("AT1G01010"), json=_gene("AT1G01010", "AT1G01010.1")
        )

    args = ("AT1G01010", "arabidopsis_thaliana")
    async with httpx.AsyncClient() as client:
        # Its CDS encodes a stop inside: our rule refused a real protein.
        lookup()
        for _ in range(2):
            httpx_mock.add_response(url=_AT1G01010_PROTEIN_URL, json=_protein(_REAL + "*"))
        httpx_mock.add_response(url=cds_url, json=_protein("ATGTAAGGG"))
        with pytest.raises(AssertionError, match="its CDS encodes a stop") as ours:
            await _protein_or_our_failure(client, *args)
        assert "[UpstreamUnavailableError]" not in str(ours.value)
        # Its CDS encodes none: Ensembl served a protein it does not encode.
        ensembl_plants._CACHE.clear()
        lookup()
        for _ in range(2):
            httpx_mock.add_response(url=_AT1G01010_PROTEIN_URL, json=_protein(_REAL + "*"))
        httpx_mock.add_response(url=cds_url, json=_protein("ATGGAGGATCAAGTTGGGTGA"))
        with pytest.raises(UpstreamUnavailableError, match=ensembl_plants.STOP_SYMBOL_REFUSAL):
            await _protein_or_our_failure(client, *args)
        # Positive controls: an outage stays one, and an answer is passed on.
        for _ in range(3):
            httpx_mock.add_response(url=_AT1G01010_PROTEIN_URL, status_code=500)
        with pytest.raises(UpstreamUnavailableError, match="HTTP 500"):
            await _protein_or_our_failure(client, *args)
        httpx_mock.add_response(url=_AT1G01010_PROTEIN_URL, json=_protein(_REAL))
        result = await _protein_or_our_failure(client, *args)
    assert result["sequence"] == _REAL


@live_only
@pytest.mark.asyncio
@pytest.mark.parametrize(("organism", "gene"), list(_PROTEIN_PROBES.items()))
async def test_live_every_organisms_protein_holds_no_stop(organism: str, gene: str) -> None:
    """Positive control for the stop-symbol refusal: a real protein of every
    organism is still the answer. One case per organism, so an Ensembl 500 on
    one is that organism's outage, not all twelve's."""
    async with httpx.AsyncClient() as client:
        result = await _protein_or_our_failure(client, gene, organism)
    assert isinstance(result["sequence"], str) and result["sequence"]
    assert "*" not in result["sequence"]


@live_only
@pytest.mark.asyncio
async def test_live_region_query_arabidopsis_chr1_finds_arv1() -> None:
    """Real /overlap/region call — AT1G01020 (ARV1) overlaps 1:3000-10000."""
    async with httpx.AsyncClient() as client:
        result = await ensembl_plants.region_query(client, "1", 3000, 10000, feature="gene")
    ids = {f.get("id") for f in result["features"]}
    assert "AT1G01020" in ids, f"expected AT1G01020 in region, got {ids}"


@live_only
@pytest.mark.asyncio
async def test_live_region_query_cds_rows_without_biotype_are_answered() -> None:
    """Real /overlap/region CDS call: the rows carry no biotype or
    external_name, and the output contract (conftest) accepts them."""
    async with httpx.AsyncClient() as client:
        result = await ensembl_plants.region_query(client, "1", 3000, 10000, feature="cds")
    assert result["count"] > 0, result
    assert all(f["feature_type"] == "cds" for f in result["features"])
    assert not any("biotype" in f for f in result["features"])


# ---------- issue #137: one projection, and no empty-version dot ----------

# Verbatim fields of the live record (rest.ensembl.org, 2026-09-22): plant
# genes carry version None, and Ensembl builds canonical_transcript as
# "<id>.<version>" regardless, so the id ends in a bare '.'.
_LIVE_RECORD = {
    "id": "AT1G19850",
    "canonical_transcript": "AT1G19850.1.",
    "version": None,
    "species": "arabidopsis_thaliana",
    "db_type": "core",
}


@pytest.mark.asyncio
async def test_single_and_batch_lookups_project_a_record_the_same_way(
    httpx_mock: HTTPXMock,
) -> None:
    """The batch form passed Ensembl's record through raw; the single one did not."""
    from plant_genomics_mcp import batch

    maize = {**_LIVE_RECORD, "id": "Zm1", "canonical_transcript": "Zm00001eb000010_T001"}
    httpx_mock.add_response(
        url="https://rest.ensembl.org/lookup/id/AT1G19850?species=arabidopsis_thaliana&expand=0",
        json=_LIVE_RECORD,
    )
    httpx_mock.add_response(
        url="https://rest.ensembl.org/lookup/id",
        method="POST",
        json={"AT1G19850": _LIVE_RECORD, "Zm1": maize},
    )
    async with httpx.AsyncClient() as client:
        single = await ensembl_plants.lookup_locus(client, "AT1G19850")
        env = await batch.batch_ensembl_plants_lookup_locus(client, ["AT1G19850", "Zm1"])

    assert single == env["results"]["AT1G19850"]
    assert single["canonical_transcript"] == "AT1G19850.1"  # aragwas spells it this way
    assert single["organism"] == "arabidopsis_thaliana" and "species" not in single
    assert "upstream_version" in single
    # Positive control: an id with no empty-version dot is left as it came.
    assert env["results"]["Zm1"]["canonical_transcript"] == "Zm00001eb000010_T001"


@pytest.mark.asyncio
async def test_a_batch_record_that_does_not_name_itself_is_an_error_for_that_locus(
    httpx_mock: HTTPXMock,
) -> None:
    """The batch POST projected any dict it got per id: ``{}`` landed in
    ``results`` as ``{"upstream_version": None}``, a silent bad answer."""
    from plant_genomics_mcp import batch

    httpx_mock.add_response(
        url="https://rest.ensembl.org/lookup/id",
        method="POST",
        json={"AT1G19850": _LIVE_RECORD, "AT1G01010": {}},
    )
    async with httpx.AsyncClient() as client:
        env = await batch.batch_ensembl_plants_lookup_locus(client, ["AT1G19850", "AT1G01010"])
    assert "AT1G01010" not in env["results"]
    assert env["errors"]["AT1G01010"] == (
        "[PlantGenomicsError] Ensembl Plants returned an unreadable record for "
        "AT1G01010: no string 'id' in {}"
    )
    # Positive control, same batch: the real record is projected.
    assert env["results"]["AT1G19850"]["organism"] == "arabidopsis_thaliana"


@pytest.mark.asyncio
async def test_a_lookup_record_that_does_not_name_itself_is_asked_again_and_never_stored(
    httpx_mock: HTTPXMock,
) -> None:
    """``{}`` was passed through as the answer (schema-breaking), and
    get_sequence / locus_variants then failed on it from the cache."""
    from plant_genomics_mcp.errors import UpstreamUnavailableError

    url = "https://rest.ensembl.org/lookup/id/AT1G19850?species=arabidopsis_thaliana&expand=0"
    httpx_mock.add_response(url=url, json={})
    httpx_mock.add_response(url=url, json={"id": "AT1G19850"})  # no species
    httpx_mock.add_response(url=url, json=_LIVE_RECORD)
    async with httpx.AsyncClient() as client:
        with pytest.raises(
            UpstreamUnavailableError,
            match=r"answered 200 twice without a readable result "
            r"\(no string 'species' in \{'id': 'AT1G19850'\}\)",
        ):
            await ensembl_plants.lookup_locus(client, "AT1G19850")
        # Positive control, same cache: the next real record is fetched and projected.
        record = await ensembl_plants.lookup_locus(client, "AT1G19850")
    assert (record["id"], record["organism"]) == ("AT1G19850", "arabidopsis_thaliana")
    assert len(httpx_mock.get_requests(url=url)) == 3
