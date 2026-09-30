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
from plant_genomics_mcp.errors import (
    InvalidArguments,
    NotFoundError,
    PlantGenomicsError,
    UpstreamUnavailableError,
)

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
            "&format=json&size=25"
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
            "&format=json&size=25"
        ),
        json={"results": []},
    )
    # Pass 2: no reviewed filter → 1 TrEMBL hit
    httpx_mock.add_response(
        url=(
            "https://rest.uniprot.org/uniprotkb/search"
            "?query=%28gene%3AOs01g0100100+OR+xref%3Aensemblplants-Os01g0100100%29+AND+organism_id%3A39947"
            "&format=json&size=25"
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
            "&format=json&size=25"
        ),
        json={"results": []},
    )
    httpx_mock.add_response(
        url=(
            "https://rest.uniprot.org/uniprotkb/search"
            "?query=%28gene%3ANOTREAL+OR+xref%3Aensemblplants-NOTREAL%29+AND+organism_id%3A3702"
            "&format=json&size=25"
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
        "&format=json&size=25"
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
@pytest.mark.parametrize(
    "injected",
    [
        "AT1G01010) OR (reviewed:true",  # rewrites the Lucene query
        "AT1G01010 OR gene:ARF5",
        "",
    ],
)
async def test_lookup_locus_refuses_a_query_fragment_before_any_request(
    httpx_mock: HTTPXMock, injected: str
) -> None:
    """Audit 2026-09-22 L8: lookup_locus spliced the raw locus into
    ``(gene:{locus} OR xref:ensemblplants-{locus}) AND organism_id:...``.
    No mock is registered for the injected form, so a request would fail."""
    httpx_mock.add_response(
        url=(
            "https://rest.uniprot.org/uniprotkb/search"
            "?query=%28gene%3AAT1G01010+OR+xref%3Aensemblplants-AT1G01010%29+AND+organism_id%3A3702+AND+reviewed%3Atrue"
            "&format=json&size=25"
        ),
        json=_one_hit(),
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="UniProt: invalid locus"):
            await uniprot.lookup_locus(client, injected)
        # Positive control: the legitimate locus — even lowercase — still resolves.
        result = await uniprot.lookup_locus(client, "at1g01010")
    assert result["primaryAccession"] == "Q0WV96"
    assert len(httpx_mock.get_requests()) == 1


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
    """P0XXX0 is accession-shaped but was never issued: a 404 on the live
    service (2026-09-30). Q9XXX9, which this test used as "synthetic", is a
    real deleted entry, answered 200 (see the tests of inactive entries)."""
    httpx_mock.add_response(
        url="https://rest.uniprot.org/uniprotkb/P0XXX0.json",
        status_code=404,
        # UniProt's answer for an accession never issued (live, 2026-09-30).
        json={
            "url": "http://rest.uniprot.org/uniprotkb/P0XXX0",
            "messages": ["Resource not found"],
        },
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="no entry for accession='P0XXX0'"):
            await uniprot.lookup_locus(client, "P0XXX0")


# An inactive entry is a 200 that names the accession, so it passed as an
# entry and resolve_locus_to_uniprot answered it with no name, gene or
# organism. MERGED is built from UniProt's schema (EntryInactiveReason:
# inactiveReasonType DELETED/MERGED/DEMERGED, mergeDemergeTos), not seen live.
_GONE = {
    "deleted (live)": (
        "Q9XXX9",
        None,
        "the entry is inactive (DELETED: Not part of a reference proteome)",
    ),
    "merged (schema)": (
        "P0XXX1",
        {"inactiveReasonType": "MERGED", "mergeDemergeTos": ["Q0WV96"]},
        "the entry is inactive (MERGED into Q0WV96)",
    ),
    "no reason given": ("P0XXX2", {}, "the entry is inactive (no reason given)"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(_GONE))
async def test_an_inactive_entry_is_not_found_with_uniprots_reason(
    httpx_mock: HTTPXMock, case: str
) -> None:
    """UniProt's own answer, so it is stored like any "no record": asked
    once. Positive control: an active entry beside it answers."""
    acc, reason, said = _GONE[case]
    body = (
        _INACTIVE
        if reason is None
        else {
            "entryType": "Inactive",
            "primaryAccession": acc,
            "inactiveReason": reason,
        }
    )
    httpx_mock.add_response(url=_ENTRY_URL.format(acc), json=body)
    httpx_mock.add_response(url=_ENTRY_URL.format("Q0WV96"), json=_one_hit()["results"][0])
    async with httpx.AsyncClient() as client:
        for _ in range(2):
            with pytest.raises(NotFoundError) as err:
                await uniprot.lookup_locus(client, acc)
            assert str(err.value) == (
                f"[NotFoundError] UniProt has no entry for accession={acc!r}: {said}"
            )
        active = await uniprot.lookup_locus(client, "Q0WV96")
    assert active["primaryAccession"] == "Q0WV96"
    assert len(httpx_mock.get_requests()) == 2


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
        text="Error messages\nResource not found\n",  # live, 2026-09-29
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


# Bodies of the accession endpoint that name no entry (#96). The dict check
# passed ``{}``, it was stored, and the tool answered ``primaryAccession: ''``
# for the whole TTL without asking again.
_NOT_AN_ENTRY: dict[str, tuple[object, str]] = {
    "empty object": ({}, "no str 'primaryAccession' in {}"),
    "array": ([], "list, not an object"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(_NOT_AN_ENTRY))
async def test_an_accession_answer_naming_no_entry_is_refused_and_not_stored(
    httpx_mock: HTTPXMock, case: str
) -> None:
    """Checked before the store. Positive control, same cache: a real entry
    is then asked for and answered."""
    body, problem = _NOT_AN_ENTRY[case]
    url = "https://rest.uniprot.org/uniprotkb/Q0WV96.json"
    httpx_mock.add_response(url=url, json=body)
    httpx_mock.add_response(url=url, json=_one_hit()["results"][0])
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlantGenomicsError) as err:
            await uniprot.lookup_locus(client, "Q0WV96")
        assert not isinstance(err.value, NotFoundError)
        assert str(err.value) == f"UniProt accession fetch returned unexpected payload: {problem}"
        result = await uniprot.lookup_locus(client, "Q0WV96")
    assert (result["primaryAccession"], result["uniProtkbId"]) == ("Q0WV96", "NAC1_ARATH")
    assert len(httpx_mock.get_requests()) == 2


# Bodies of the FASTA endpoint that are not a FASTA record. Each was returned
# as the sequence (``"{}"``, ``""``, ``"[1]"``), stored, and handed to BLAST
# by the synthesis tools for the whole TTL without asking again.
_NOT_FASTA = {
    "json object": "{}",
    "json row": "[1]",
    "header only": ">sp|Q0WV96|NAC1_ARATH NAC domain-containing protein 1\n",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(_NOT_FASTA))
async def test_a_body_that_is_not_fasta_is_asked_again_and_not_stored(
    httpx_mock: HTTPXMock, case: str
) -> None:
    """Asked once more, then the entry is asked whether it is inactive, then
    a typed upstream error, never a sequence. Positive control, same cache: a
    real record is then asked for and read."""
    url = "https://rest.uniprot.org/uniprotkb/Q0WV96.fasta"
    httpx_mock.add_response(url=url, text=_NOT_FASTA[case])
    httpx_mock.add_response(url=url, text=_NOT_FASTA[case])
    httpx_mock.add_response(url=_ENTRY_URL.format("Q0WV96"), json=_one_hit()["results"][0])
    httpx_mock.add_response(url=url, text=">sp|Q0WV96|NAC1_ARATH\nMEDQ\nVGF\n")
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamUnavailableError) as err:
            await uniprot.fetch_sequence(client, "Q0WV96")
        assert "UniProt FASTA answered 200 twice without a readable result" in str(err.value)
        assert await uniprot.fetch_sequence(client, "Q0WV96") == "MEDQVGF"
    assert len(httpx_mock.get_requests()) == 4


_FASTA_URL = "https://rest.uniprot.org/uniprotkb/{}.fasta"
_ENTRY_URL = "https://rest.uniprot.org/uniprotkb/{}.json"
# UniProt's .json for a deleted entry (live 2026-09-30, Q9XXX9, verbatim).
_INACTIVE = {
    "entryType": "Inactive",
    "primaryAccession": "Q9XXX9",
    "uniProtkbId": "Q9XXX9_PLAFA",
    "annotationScore": 0.0,
    "inactiveReason": {
        "inactiveReasonType": "DELETED",
        "deletedReason": "Not part of a reference proteome",
    },
    "extraAttributes": {"uniParcId": "UPI000007B4F7"},
}


@pytest.mark.asyncio
async def test_an_empty_fasta_body_is_asked_again(httpx_mock: HTTPXMock) -> None:
    """A body alone cannot tell a deleted entry from a transient empty
    answer (#215 review), so an empty body is asked for again like any other
    unreadable one, and a record that follows is the answer."""
    url = _FASTA_URL.format("Q0WV96")
    httpx_mock.add_response(url=url, text="")
    httpx_mock.add_response(url=url, text=">sp|Q0WV96|X\nMEDQ\n")
    async with httpx.AsyncClient() as client:
        assert await uniprot.fetch_sequence(client, "Q0WV96") == "MEDQ"
    assert len(httpx_mock.get_requests()) == 2


@pytest.mark.asyncio
async def test_a_fasta_that_stays_empty_is_not_found_when_the_entry_says_inactive(
    httpx_mock: HTTPXMock,
) -> None:
    """UniProt's FASTA for a deleted entry is a 200 with no bytes (live, see
    below); the entry's own ``entryType`` is what says it has no sequence."""
    httpx_mock.add_response(url=_FASTA_URL.format("Q9XXX9"), text="", is_reusable=True)
    httpx_mock.add_response(url=_ENTRY_URL.format("Q9XXX9"), json=_INACTIVE)
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError) as err:
            await uniprot.fetch_sequence(client, "Q9XXX9")
    assert str(err.value) == (
        "[NotFoundError] UniProt has no FASTA for accession='Q9XXX9': "
        "the entry is inactive (DELETED: Not part of a reference proteome)"
    )
    assert len(httpx_mock.get_requests()) == 3


@pytest.mark.asyncio
async def test_a_fasta_that_stays_empty_for_an_active_entry_is_an_outage(
    httpx_mock: HTTPXMock,
) -> None:
    """Negative control of the test above: the same empty FASTA for an entry
    that is active is the upstream failing, not "no sequence"."""
    httpx_mock.add_response(url=_FASTA_URL.format("Q0WV96"), text="", is_reusable=True)
    httpx_mock.add_response(url=_ENTRY_URL.format("Q0WV96"), json=_one_hit()["results"][0])
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamUnavailableError) as err:
            await uniprot.fetch_sequence(client, "Q0WV96")
    assert "UniProt FASTA answered 200 twice without a readable result" in str(err.value)
    assert len(httpx_mock.get_requests()) == 3


@live_only
@pytest.mark.asyncio
async def test_live_a_deleted_entry_has_no_fasta_and_a_live_one_has_residues() -> None:
    """Q9XXX9 is a deleted entry (``entryType: Inactive``, 2026-09-30); its
    FASTA is a 200 with no bytes. Positive control: AT1G01010's protein."""
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="no FASTA for accession='Q9XXX9'"):
            await uniprot.fetch_sequence(client, "Q9XXX9")
        seq = await uniprot.fetch_sequence(client, "Q0WV96")
    assert seq.startswith("MEDQVGFGFRPNDEELVGHY") and seq.isalpha(), seq[:40]


# Search pages with a row that is not an object (#96). Row 0 leaked a raw
# ``TypeError: 'int' object does not support item assignment``; a later one
# was stored and handed to every reader of the page.
_BAD_SEARCH_PAGES = {
    "row 0": ([1], "row 0 is int"),
    "a later row": ([_one_hit()["results"][0], 7], "row 1 is int"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(_BAD_SEARCH_PAGES))
async def test_a_search_row_that_is_not_an_object_is_refused_and_not_stored(
    httpx_mock: HTTPXMock, case: str
) -> None:
    """Checked before the store, through the tool's own call. Positive
    control, same cache: a readable page is then asked for and answered."""
    rows, problem = _BAD_SEARCH_PAGES[case]
    httpx_mock.add_response(json={"results": rows})
    httpx_mock.add_response(json=_one_hit())
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlantGenomicsError) as err:
            await uniprot.lookup_locus(client, "AT1G01010")
        assert str(err.value) == (
            f"UniProt search returned unexpected payload: {problem}, not an object"
        )
        result = await uniprot.lookup_locus(client, "AT1G01010")
    assert (result["primaryAccession"], result["uniProtkbId"]) == ("Q0WV96", "NAC1_ARATH")
    assert len(httpx_mock.get_requests()) == 2


@live_only
@pytest.mark.asyncio
async def test_live_a_deleted_accession_is_not_found_and_a_never_issued_one_too() -> None:
    """Q9XXX9 is deleted (200, ``entryType: Inactive``); P0XXX0 was never
    issued (404). Positive control: AT1G01010's protein by accession."""
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match=r"Q9XXX9'.*inactive \(DELETED: "):
            await uniprot.lookup_locus(client, "Q9XXX9")
        with pytest.raises(NotFoundError, match="no entry for accession='P0XXX0'$"):
            await uniprot.lookup_locus(client, "P0XXX0")
        result = await uniprot.lookup_locus(client, "Q0WV96")
    assert (result["primaryAccession"], result["uniProtkbId"]) == ("Q0WV96", "NAC1_ARATH")


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


# ---------- issue #128: a symbol shared by several loci ----------


def _hit(accession: str, *loci: str) -> dict:
    return {
        "primaryAccession": accession,
        "entryType": "UniProtKB reviewed (Swiss-Prot)",
        "genes": [{"geneName": {"value": "ARF1"}}],
        "organism": {"scientificName": "Arabidopsis thaliana", "taxonId": 3702},
        "uniProtKBCrossReferences": [{"database": "Araport", "id": x} for x in loci],
    }


@pytest.mark.parametrize(
    ("query", "hits", "answer"),
    [
        # The #128 case: two reviewed genes share the name ARF1.
        ("ARF1", [_hit("Q8L7G0", "AT1G59750"), _hit("P36397", "AT2G47170")], None),
        # Positive controls: a symbol whose entries are all one gene, and a
        # locus query whose first entry also lists a tandem duplicate.
        ("ARF5", [_hit("P93024", "AT1G19850"), _hit("A0A1P8AQ60", "AT1G19850")], "P93024"),
        ("at1g19850", [_hit("X1", "AT1G19850", "AT1G19860"), _hit("X2", "AT1G19860")], "X1"),
    ],
)
@pytest.mark.asyncio
async def test_a_symbol_naming_several_loci_is_refused_with_the_loci(
    query: str, hits: list[dict], answer: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake(client: object, q: str, *, size: int = 1) -> list[dict]:
        return hits

    monkeypatch.setattr(uniprot, "_search", fake)
    async with httpx.AsyncClient() as client:
        if answer is None:
            with pytest.raises(InvalidArguments, match=r"2 loci .*AT1G59750, AT2G47170"):
                await uniprot.lookup_locus(client, query)
        else:
            assert (await uniprot.lookup_locus(client, query))["primaryAccession"] == answer


@live_only
@pytest.mark.asyncio
async def test_live_arf1_is_refused_and_arf5_still_answers() -> None:
    """The dossier's own probe (#128): ARF1 is two Arabidopsis genes."""
    async with httpx.AsyncClient() as client:
        with pytest.raises(InvalidArguments) as refused:
            await uniprot.lookup_locus(client, "ARF1")
        assert "AT1G59750" in str(refused.value) and "AT2G47170" in str(refused.value)
        assert (await uniprot.lookup_locus(client, "ARF5"))["primaryAccession"] == "P93024"
        assert (await uniprot.lookup_locus(client, "AT2G47170"))["primaryAccession"] == "P36397"
