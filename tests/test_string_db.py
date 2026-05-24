"""STRING interaction-partners backend unit tests.

STRING returns JSON via /api/json/. We accept either a UniProt accession
or a locus identifier — both are passed through to STRING unchanged
(v1.1.1 removed the UniProt pre-resolution step; STRING's own resolver
handles loci, and pre-resolving caused accession-choice mismatches when a
locus has multiple valid UniProt accessions).
"""

from __future__ import annotations

import os

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
    # v1.1.1: accession is STRING's canonical pick from stringId_A (taxid-stripped),
    # not the input query. STRING canonicalizes arabidopsis on locus IDs.
    assert result["accession"] == "AT1G01010.1"
    assert result["organism"] == "arabidopsis_thaliana"
    assert len(result["partners"]) == 1
    p = result["partners"][0]
    assert p["accession"] is not None  # raw stringId_B field
    assert p["preferred_name"] == "NAC3"
    assert p["score"] == 0.812


@pytest.mark.asyncio
async def test_lookup_partners_with_locus_passes_through(httpx_mock: HTTPXMock):
    """v1.1.1: loci pass through to STRING unchanged; no UniProt pre-resolve.

    STRING's own resolver canonicalizes the locus. The species-canonical
    accession appears taxid-prefixed in ``stringId_A``; we surface the bare
    accession on ``result["accession"]``.
    """
    httpx_mock.add_response(
        url=(
            "https://string-db.org/api/json/interaction_partners"
            "?identifiers=AT1G01010&species=3702&limit=20"
            "&caller_identity=plant-genomics-mcp"
        ),
        json=[
            {
                "stringId_A": "3702.AT1G01010.1",
                "stringId_B": "3702.AT3G15500.1",
                "preferredName_B": "NAC3",
                "score": 0.8,
            },
        ],
    )
    async with httpx.AsyncClient() as client:
        result = await string_db.lookup_partners(client, "AT1G01010")
    assert result["query"] == "AT1G01010"
    assert result["accession"] == "AT1G01010.1"
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


@pytest.mark.skipif(
    not os.environ.get("PLANT_GENOMICS_MCP_LIVE"),
    reason="set PLANT_GENOMICS_MCP_LIVE=1 to hit string-db.org",
)
@pytest.mark.asyncio
async def test_live_string_q0wv96_has_partners():
    async with httpx.AsyncClient() as client:
        result = await string_db.lookup_partners(client, "Q0WV96", limit=5)
    assert result["accession"] == "Q0WV96"
    assert len(result["partners"]) > 0
    assert result["partners"][0]["score"] is not None


@pytest.mark.skipif(
    not os.environ.get("PLANT_GENOMICS_MCP_LIVE"),
    reason="set PLANT_GENOMICS_MCP_LIVE=1 to hit string-db.org",
)
@pytest.mark.asyncio
async def test_live_string_rice_locus_resolves_and_returns_partners():
    """v1.1.1: rice locus passes through to STRING's own resolver.

    v1.1.0 pre-resolved Os01g0100100 → UniProt Q0JRI1 and asked STRING for
    that accession, which 404'd because STRING canonicalizes that locus on
    A0A0P0UX28. v1.1.1 drops the pre-resolution; STRING handles the locus
    and we surface whichever species-canonical accession it picks.
    """
    async with httpx.AsyncClient() as client:
        result = await string_db.lookup_partners(
            client, "Os01g0100100", limit=5, organism="oryza_sativa"
        )
    assert result["organism"] == "oryza_sativa"
    assert result["accession"]  # STRING's species-canonical pick
    # Partner list may be empty for some loci; tolerate but require structure if present.
    if result["partners"]:
        assert result["partners"][0]["score"] is not None


@pytest.mark.asyncio
async def test_lookup_partners_accepts_organism_param(httpx_mock: HTTPXMock):
    """Resolver-driven organism kwarg accepts a slug; STRING wire-format species=3702 preserved."""
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
        result = await string_db.lookup_partners(client, "Q0WV96", organism="arabidopsis_thaliana")
    assert result["accession"] == "Q0WV96"
    assert result["organism"] == "arabidopsis_thaliana"
    assert result["partners"][0]["preferred_name"] == "NAC3"


@pytest.mark.asyncio
async def test_lookup_partners_unsupported_organism_raises():
    """Resolving an organism with string_taxid=None raises OrganismNotSupported.

    Note: all 12 records in organisms.ORGANISMS currently have a non-None
    string_taxid (STRING covers every plant in our matrix). To exercise the
    error path we use a non-existent organism slug, which raises
    OrganismNotFound. If a future record drops STRING coverage, swap the
    assertion to OrganismNotSupported.
    """
    from plant_genomics_mcp.errors import OrganismNotFound

    async with httpx.AsyncClient() as client:
        with pytest.raises(OrganismNotFound):
            await string_db.lookup_partners(client, "Q0WV96", organism="not_a_real_plant_42")
