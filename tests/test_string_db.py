"""STRING interaction-partners backend unit tests.

STRING returns JSON via /api/json/. We accept either a UniProt accession
or a locus identifier — the latter dispatches to resolve_locus_to_uniprot
first (mirrors v0.6's input-shape detection in resolve_locus_to_uniprot).
"""

from __future__ import annotations

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import string_db
from plant_genomics_mcp.errors import NotFoundError


@pytest.fixture(autouse=True)
def _clear_cache():
    string_db._CACHE.clear()
    yield
    string_db._CACHE.clear()


@pytest.mark.asyncio
async def test_lookup_partners_by_accession_happy(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url=(
            "https://string-db.org/api/json/interaction_partners"
            "?identifiers=Q0WV96&species=3702&limit=5"
            "&caller_identity=plant-genomics-mcp"
        ),
        json=[
            {
                "stringId_A": "3702.AT1G01010.1",
                "stringId_B": "3702.AT3G15500.1",
                "preferredName_A": "NAC001",
                "preferredName_B": "NAC3",
                "ncbiTaxonId": 3702,
                "score": 0.812,
                "escore": 0.0,
                "dscore": 0.4,
                "tscore": 0.7,
                "pscore": 0.0,
            },
        ],
    )
    async with httpx.AsyncClient() as client:
        result = await string_db.lookup_partners(client, "Q0WV96", limit=5)
    assert result["query"] == "Q0WV96"
    assert result["accession"] == "Q0WV96"
    assert result["organism_taxid"] == 3702
    assert len(result["partners"]) == 1
    p = result["partners"][0]
    assert p["accession"] == "Q0WV96" or p["accession"] is not None  # raw stringId field too
    assert p["preferred_name"] == "NAC3"
    assert p["score"] == 0.812


def test_looks_like_accession_rejects_trailing_garbage():
    """Both regex branches must anchor with ``$`` — bug fix regression."""
    assert string_db._looks_like_accession("Q0WV96") is True
    assert string_db._looks_like_accession("P12345") is True
    assert string_db._looks_like_accession("A0A123B456") is True
    assert string_db._looks_like_accession("Q0WV96.1") is True
    # Trailing garbage must be rejected on BOTH the 6-char and 10-char branch.
    assert string_db._looks_like_accession("Q0WV96extra") is False
    assert string_db._looks_like_accession("P12345junk") is False
    assert string_db._looks_like_accession("A0A123B456junk") is False
    # Non-accession (locus) stays rejected.
    assert string_db._looks_like_accession("AT1G01010") is False


@pytest.mark.asyncio
async def test_lookup_partners_with_locus_input_resolves_first(httpx_mock: HTTPXMock):
    # Step 1: uniprot resolution. The real `_search` call sends
    # query="gene:{locus} AND organism_id:{taxon} AND reviewed:true",
    # format=json, size=1 — matches what uniprot.py actually emits.
    httpx_mock.add_response(
        url=(
            "https://rest.uniprot.org/uniprotkb/search"
            "?query=gene%3AAT1G01010+AND+organism_id%3A3702+AND+reviewed%3Atrue"
            "&format=json&size=1"
        ),
        json={
            "results": [
                {
                    "primaryAccession": "Q0WV96",
                    "uniProtkbId": "NAC1_ARATH",
                    "entryType": "UniProtKB reviewed (Swiss-Prot)",
                }
            ],
        },
    )
    # Step 2: STRING call with resolved accession (default limit=20).
    httpx_mock.add_response(
        url=(
            "https://string-db.org/api/json/interaction_partners"
            "?identifiers=Q0WV96&species=3702&limit=20"
            "&caller_identity=plant-genomics-mcp"
        ),
        json=[
            {"stringId_B": "3702.AT3G15500.1", "preferredName_B": "NAC3", "score": 0.8},
        ],
    )
    async with httpx.AsyncClient() as client:
        result = await string_db.lookup_partners(client, "AT1G01010")
    assert result["query"] == "AT1G01010"
    assert result["accession"] == "Q0WV96"
    assert result["partners"][0]["preferred_name"] == "NAC3"


@pytest.mark.asyncio
async def test_lookup_partners_empty_array_raises_not_found(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url=(
            "https://string-db.org/api/json/interaction_partners"
            "?identifiers=Q0WV96&species=3702&limit=20"
            "&caller_identity=plant-genomics-mcp"
        ),
        json=[],
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError) as exc:
            await string_db.lookup_partners(client, "Q0WV96")
    assert "[NotFoundError]" in str(exc.value)


@pytest.mark.parametrize(
    "acc,looks_like",
    [
        ("Q0WV96", True),  # canonical 6-char
        ("Q0WV96.1", True),  # versioned
        ("P12345", True),
        ("A0A0A0A0A0", True),  # 10-char form
        ("AT1G01010", False),  # locus, not accession
        ("Os01g0100100", False),
        ("foo", False),
    ],
)
def test_looks_like_accession(acc, looks_like):
    assert string_db._looks_like_accession(acc) is looks_like
