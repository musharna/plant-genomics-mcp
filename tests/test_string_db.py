"""STRING interaction-partners backend unit tests.

STRING returns JSON via /api/json/. We accept either a UniProt accession
or a locus identifier — both are passed through to STRING unchanged
(v1.1.1 removed the UniProt pre-resolution step; STRING's own resolver
handles loci, and pre-resolving caused accession-choice mismatches when a
locus has multiple valid UniProt accessions).
"""

from __future__ import annotations

import os
import re

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
    assert p["string_id"] == "3702.AT3G15500.1"  # stringId_B, as STRING spells it
    # Removed in 1.24.0 (#133): a partner's "accession" was only ever string_id
    # again, a STRING id where every other tool means a UniProt accession.
    assert "accession" not in p, p
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


@pytest.mark.asyncio
async def test_lookup_partners_non_json_200_raises_typed(httpx_mock: HTTPXMock):
    """A 200 carrying a non-JSON body (e.g. an HTML error page) surfaces as a
    typed PlantGenomicsError, not a raw JSONDecodeError (bug audit L3)."""
    from plant_genomics_mcp.errors import PlantGenomicsError

    httpx_mock.add_response(
        url=(
            "https://string-db.org/api/json/interaction_partners"
            "?identifiers=Q0WV96&species=3702&limit=20"
            "&caller_identity=plant-genomics-mcp"
        ),
        text="upstream error, not json",
        status_code=200,
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlantGenomicsError, match="non-JSON"):
            await string_db.lookup_partners(client, "Q0WV96")


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


@pytest.mark.asyncio
async def test_lookup_partners_rejects_separator_identifier() -> None:
    """audit P6: lookup_partners now validates the identifier (parity with the
    other locus-accepting backends), so one carrying cache-key separators is
    rejected before it can reach make_key unescaped."""
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="invalid locus"):
            await string_db.lookup_partners(client, "AT1G01010&species=9606")


def test_normalize_rejects_non_string_string_id():
    """#95: the normaliser owns the ``string_id: str`` invariant that
    synthesis._string_partner_locus consumes. A non-string ``stringId_B``
    must raise the typed error here; a string passes through unchanged
    (positive control).
    """
    from plant_genomics_mcp.errors import PlantGenomicsError

    with pytest.raises(PlantGenomicsError, match="stringId_B"):
        string_db._normalize({"stringId_B": True, "score": 0.5}, "Q0WV96")
    with pytest.raises(PlantGenomicsError, match="stringId_B"):
        string_db._normalize({"stringId_B": 3702, "score": 0.5}, "Q0WV96")

    ok = string_db._normalize({"stringId_B": "3702.AT3G15500.1", "score": 0.5}, "Q0WV96")
    assert ok["string_id"] == "3702.AT3G15500.1"
    # Missing field stays None (STRING rows without a B side are tolerated downstream).
    assert string_db._normalize({"score": 0.5}, "Q0WV96")["string_id"] is None


# ---------- H1 (audit 2026-09-22): the STRING query id is derived, never truncated ----------

_PARTNERS = "https://string-db.org/api/json/interaction_partners"


def _partner_row(a: str, b: str) -> dict[str, object]:
    return {"stringId_A": a, "stringId_B": b, "preferredName_B": "P", "score": 0.9}


@pytest.mark.parametrize(
    "given,organism,sent",
    [
        # Soybean: STRING knows the UniProt ORF name GLYMA_04G220900 (-> K7KLM4,
        # live 2026-09-22) and nothing called Glyma.04G220900. The old
        # split('.')[0] sent "Glyma", which STRING resolved to an unrelated
        # protein (A0A0R0I6K5 = GLYMA_09G103300) and answered with confidence.
        ("Glyma.04G220900", "glycine_max", "GLYMA_04G220900"),
        ("Glyma.04G220900.1", "glycine_max", "GLYMA_04G220900"),
        # Sorghum: SORBI_3001G000100 -> C5WR12 (live); Sobic.001G000100 -> none.
        ("Sobic.001G000100", "sorghum_bicolor", "SORBI_3001G000100"),
        # Brachypodium: BRADI_1g00200v3 -> I1GKD6 (live); Bradi1g00200 -> none.
        ("Bradi1g00200", "brachypodium_distachyon", "BRADI_1g00200v3"),
        # Arabidopsis: AT1G01010 -> Q0WV96 (live); the transcript AT1G01010.1
        # resolves to nothing, so a transcript suffix is dropped.
        ("AT1G01010.1", "arabidopsis_thaliana", "AT1G01010"),
        # A UniProt accession's .N version is dropped (BLAST text reports).
        ("Q0WV96.2", "arabidopsis_thaliana", "Q0WV96"),
        # Anything else goes through whole — a dotted id is never cut to its prefix.
        ("Potri.001G399000", "populus_trichocarpa", "Potri.001G399000"),
        ("Os01g0100100", "oryza_sativa", "Os01g0100100"),
    ],
)
def test_query_id_is_the_whole_id_in_strings_spelling(given: str, organism: str, sent: str):
    assert string_db.string_query_id(given, organism) == sent


@pytest.mark.asyncio
async def test_a_dotted_locus_is_never_sent_as_its_prefix(httpx_mock: HTTPXMock):
    """The finding, end to end: soybean Glyma.04G220900 reaches STRING whole."""
    httpx_mock.add_response(
        url=(
            f"{_PARTNERS}?identifiers=GLYMA_04G220900&species=3847&limit=20"
            "&caller_identity=plant-genomics-mcp"
        ),
        json=[_partner_row("3847.K7KLM4", "3847.I1K9R8")],
    )
    async with httpx.AsyncClient() as client:
        result = await string_db.lookup_partners(client, "Glyma.04G220900", organism="glycine_max")
    sent = httpx_mock.get_requests()[0].url.params["identifiers"]
    assert sent == "GLYMA_04G220900", sent
    # `query` is what the caller passed (its schema says so), not the wire form.
    assert result["query"] == "Glyma.04G220900"
    assert result["accession"] == "K7KLM4"


@pytest.mark.asyncio
async def test_an_id_string_cannot_resolve_is_not_found_not_a_neighbour(httpx_mock: HTTPXMock):
    """STRING's real answer for a name it does not know: HTTP 404 with an
    Error object (live 2026-09-22, identifiers=Glyma.04G220900). Poplar loci
    have no STRING alias, so the honest answer is NotFound."""
    httpx_mock.add_response(
        url=(
            f"{_PARTNERS}?identifiers=Potri.001G399000&species=3694&limit=20"
            "&caller_identity=plant-genomics-mcp"
        ),
        status_code=404,
        json=[{"Error": "not found", "ErrorMessage": "Sorry, STRING did not find a protein"}],
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError):
            await string_db.lookup_partners(
                client, "Potri.001G399000", organism="populus_trichocarpa"
            )


@pytest.mark.skipif(
    os.environ.get("PLANT_GENOMICS_MCP_LIVE") != "1",
    reason="set PLANT_GENOMICS_MCP_LIVE=1 to hit string-db.org",
)
@pytest.mark.asyncio
async def test_live_soybean_dotted_locus_resolves_to_its_own_protein():
    """Real execution for H1: the old code answered for 'Glyma' (A0A0R0I6K5)."""
    async with httpx.AsyncClient() as client:
        result = await string_db.lookup_partners(
            client, "Glyma.04G220900", organism="glycine_max", limit=3
        )
    assert result["accession"] == "K7KLM4", result["accession"]


# ---------- #155: STRING indexes wheat by UniProt accession only ----------


def _uniprot_hit(accession: str) -> dict[str, object]:
    return {
        "results": [
            {
                "primaryAccession": accession,
                "uniProtkbId": f"{accession}_WHEAT",
                "entryType": "UniProtKB unreviewed (TrEMBL)",
                "organism": {"scientificName": "Triticum aestivum", "taxonId": 4565},
            }
        ]
    }


@pytest.mark.asyncio
async def test_wheat_locus_goes_to_string_as_its_uniprot_accession(httpx_mock: HTTPXMock):
    """STRING's alias table carries no IWGSC id for wheat, in any form tried
    (gene, transcript, RefSeq LOC name: all [] on get_string_ids, live
    2026-09-25), while the UniProt accession resolves to itself (48 of the
    dossier's 66 wheat ARFs). So a wheat locus is sent as its accession."""
    httpx_mock.add_response(
        url=re.compile(r"^https://rest\.uniprot\.org/uniprotkb/search.*TraesCS3A02G449300.*"),
        json=_uniprot_hit("A0A3B6EQF8"),
        is_reusable=True,
    )
    httpx_mock.add_response(
        url=(
            f"{_PARTNERS}?identifiers=A0A3B6EQF8&species=4565&limit=20"
            "&caller_identity=plant-genomics-mcp"
        ),
        json=[_partner_row("4565.A0A3B6EQF8", "4565.A0A3B6PMC1")],
    )
    async with httpx.AsyncClient() as client:
        result = await string_db.lookup_partners(
            client, "TraesCS3A02G449300", organism="triticum_aestivum"
        )
    string_calls = [r for r in httpx_mock.get_requests() if r.url.host == "string-db.org"]
    assert [r.url.params["identifiers"] for r in string_calls] == ["A0A3B6EQF8"]
    assert result["query"] == "TraesCS3A02G449300"
    assert result["accession"] == "A0A3B6EQF8"
    assert result["partners"][0]["string_id"] == "4565.A0A3B6PMC1"


@pytest.mark.asyncio
async def test_wheat_locus_string_lacks_names_the_locus_and_accession(httpx_mock: HTTPXMock):
    """18 of the 66 wheat ARF accessions are not in STRING (live 2026-09-25).
    The error must say what was asked and as what, and a UniProt miss must be
    told apart from a STRING miss. Positive control: a wheat locus STRING does
    carry succeeds in the same test."""
    for locus, acc in (("TraesCS1A02G000100", "A0A3B6B034"), ("TraesCS3A02G449300", "A0A3B6EQF8")):
        httpx_mock.add_response(
            url=re.compile(rf"^https://rest\.uniprot\.org/uniprotkb/search.*{locus}.*"),
            json=_uniprot_hit(acc),
            is_reusable=True,
        )
    httpx_mock.add_response(
        url=re.compile(r"^https://rest\.uniprot\.org/uniprotkb/search.*TraesCS9Z99G999999.*"),
        json={"results": []},
        is_reusable=True,
    )
    httpx_mock.add_response(
        url=(
            f"{_PARTNERS}?identifiers=A0A3B6B034&species=4565&limit=20"
            "&caller_identity=plant-genomics-mcp"
        ),
        status_code=404,
        json=[{"Error": "not found", "ErrorMessage": "Sorry, STRING did not find a protein"}],
    )
    httpx_mock.add_response(
        url=(
            f"{_PARTNERS}?identifiers=A0A3B6EQF8&species=4565&limit=20"
            "&caller_identity=plant-genomics-mcp"
        ),
        json=[_partner_row("4565.A0A3B6EQF8", "4565.A0A3B6PMC1")],
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError) as string_miss:
            await string_db.lookup_partners(
                client, "TraesCS1A02G000100", organism="triticum_aestivum"
            )
        with pytest.raises(NotFoundError) as uniprot_miss:
            await string_db.lookup_partners(
                client, "TraesCS9Z99G999999", organism="triticum_aestivum"
            )
        ok = await string_db.lookup_partners(
            client, "TraesCS3A02G449300", organism="triticum_aestivum"
        )
    assert "TraesCS1A02G000100" in str(string_miss.value)
    assert "queried as A0A3B6B034" in str(string_miss.value)
    assert "UniProt" in str(uniprot_miss.value) and "TraesCS9Z99G999999" in str(uniprot_miss.value)
    assert ok["accession"] == "A0A3B6EQF8"


@pytest.mark.skipif(
    os.environ.get("PLANT_GENOMICS_MCP_LIVE") != "1",
    reason="set PLANT_GENOMICS_MCP_LIVE=1 to hit string-db.org",
)
@pytest.mark.asyncio
async def test_live_wheat_locus_resolves_through_its_uniprot_accession():
    """Real execution for #155: every IWGSC id was a 404 before."""
    async with httpx.AsyncClient() as client:
        result = await string_db.lookup_partners(
            client, "TraesCS3A02G449300", organism="triticum_aestivum", limit=3
        )
    assert result["accession"] == "A0A3B6EQF8", result["accession"]
    assert result["partners"]
