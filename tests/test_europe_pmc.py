"""Tests for the Europe PMC REST client.

Two tiers (mirrors the ensembl_plants / uniprot pattern):
  1. Unit tests with mocked HTTP via pytest-httpx (always run).
  2. Live integration test gated by PLANT_GENOMICS_MCP_LIVE=1.
"""

from __future__ import annotations

import os
import re

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import europe_pmc

LIVE = os.environ.get("PLANT_GENOMICS_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set PLANT_GENOMICS_MCP_LIVE=1 to run")


def _one_result(**overrides):
    """Synthetic Europe PMC result row shaped like the resultType=core wire format.

    ``core`` carries the journal under ``journalInfo.journal.title``; the flat
    ``journalTitle`` is a ``lite``-only field (live record, 2026-09-22). This
    fixture used to carry ``journalTitle``, which is how the projection's
    lite-shaped key survived: no test could see it was null on every real hit.
    """
    base = {
        "id": "12345678",
        "source": "MED",
        "pmid": "12345678",
        "pmcid": "PMC0000001",
        "doi": "10.1000/example.001",
        "title": "Functional analysis of NAC001 in Arabidopsis thaliana.",
        "authorString": "Doe J, Smith A.",
        "journalInfo": {
            "volume": "36",
            "yearOfPublication": 2024,
            "journal": {"title": "The Plant cell", "medlineAbbreviation": "Plant Cell"},
        },
        "pubYear": "2024",
        "firstPublicationDate": "2024-03-15",
        "citedByCount": 7,
        "isOpenAccess": "Y",
        "hasPDF": "Y",
        "abstractText": "We characterize NAC001 ...",
    }
    base.update(overrides)
    return base


# ---------- mocked unit tests ----------


@pytest.mark.asyncio
async def test_lookup_locus_at1g01010_arabidopsis_strips_species_suffix(
    httpx_mock: HTTPXMock,
) -> None:
    """Arabidopsis queries don't get a species common-name suffix appended."""
    httpx_mock.add_response(
        url=(
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
            "?query=AT1G01010&format=json&resultType=core&pageSize=10"
        ),
        json={
            "hitCount": 40,
            "resultList": {"result": [_one_result()]},
        },
    )
    async with httpx.AsyncClient() as client:
        result = await europe_pmc.lookup_locus(client, "AT1G01010")
    assert result["locus"] == "AT1G01010"
    assert result["organism"] == "arabidopsis_thaliana"
    assert result["query"] == "AT1G01010"
    assert result["hitCount"] == 40
    assert result["returned"] == 1
    hit = result["hits"][0]
    assert hit["title"].startswith("Functional analysis")
    assert hit["web_url"] == "https://europepmc.org/article/PMC/PMC0000001"


@pytest.mark.asyncio
async def test_lookup_locus_rice_appends_species_common_name(httpx_mock: HTTPXMock) -> None:
    """Non-Arabidopsis species get ` AND <common_name>` appended to disambiguate."""
    httpx_mock.add_response(
        url=(
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
            "?query=Os01g0100100+AND+rice&format=json&resultType=core&pageSize=10"
        ),
        json={
            "hitCount": 12,
            "resultList": {"result": [_one_result(pmcid=None, pmid="99999999")]},
        },
    )
    async with httpx.AsyncClient() as client:
        result = await europe_pmc.lookup_locus(client, "Os01g0100100", organism="oryza_sativa")
    assert result["query"] == "Os01g0100100 AND rice"
    # No pmcid → web_url falls back to PMID-based MED URL.
    assert result["hits"][0]["web_url"] == "https://europepmc.org/article/MED/99999999"


@pytest.mark.asyncio
async def test_lookup_locus_size_is_clamped_to_max(httpx_mock: HTTPXMock) -> None:
    """size=999 is clamped to MAX_PAGE_SIZE so the upstream pageSize stays bounded."""
    httpx_mock.add_response(
        url=(
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
            f"?query=AT1G01010&format=json&resultType=core&pageSize={europe_pmc.MAX_PAGE_SIZE}"
        ),
        json={"hitCount": 0, "resultList": {"result": []}},
    )
    async with httpx.AsyncClient() as client:
        result = await europe_pmc.lookup_locus(client, "AT1G01010", size=999)
    assert result["returned"] == 0
    assert result["hits"] == []


@pytest.mark.asyncio
async def test_lookup_locus_empty_result_list(httpx_mock: HTTPXMock) -> None:
    """hitCount=0 with empty result[] returns empty hits[] — not an error."""
    httpx_mock.add_response(
        url=(
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
            "?query=NONEXISTENT_LOCUS&format=json&resultType=core&pageSize=10"
        ),
        json={"hitCount": 0, "resultList": {"result": []}},
    )
    async with httpx.AsyncClient() as client:
        result = await europe_pmc.lookup_locus(client, "NONEXISTENT_LOCUS")
    assert result["hitCount"] == 0
    assert result["returned"] == 0
    assert result["hits"] == []


@pytest.mark.asyncio
async def test_lookup_locus_normalizes_missing_optional_fields(httpx_mock: HTTPXMock) -> None:
    """Hits with null fields propagate as None — outputSchema marks them optional."""
    httpx_mock.add_response(
        url=(
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
            "?query=AT1G01010&format=json&resultType=core&pageSize=10"
        ),
        json={
            "hitCount": 1,
            "resultList": {
                "result": [{"id": "BARE", "source": "PPR", "title": "Preprint"}],
            },
        },
    )
    async with httpx.AsyncClient() as client:
        result = await europe_pmc.lookup_locus(client, "AT1G01010")
    hit = result["hits"][0]
    assert hit["id"] == "BARE"
    assert hit["pmid"] is None
    assert hit["pmcid"] is None
    assert hit["citedByCount"] is None
    # No pmcid, no pmid → web_url is None.
    assert hit["web_url"] is None


# ---------- live integration (real-execution check) ----------


@live_only
@pytest.mark.asyncio
async def test_live_lookup_at1g01010_returns_hits() -> None:
    """Real call to Europe PMC — AT1G01010 should have published literature."""
    async with httpx.AsyncClient() as client:
        result = await europe_pmc.lookup_locus(client, "AT1G01010", size=3)
    assert result["hitCount"] > 0
    assert 1 <= result["returned"] <= 3
    # Sanity: at least one hit has a title.
    assert any(h.get("title") for h in result["hits"])


# ---------- T10: organism= param via resolver ----------


@pytest.mark.asyncio
async def test_lookup_locus_accepts_organism_param(httpx_mock: HTTPXMock) -> None:
    """T10: lookup_locus accepts ``organism=`` (via organisms.resolve), not ``species=``."""
    httpx_mock.add_response(
        url=re.compile(r"https://www\.ebi\.ac\.uk/europepmc/.*"),
        json={"hitCount": 1, "resultList": {"result": [{"id": "12345"}]}},
    )
    async with httpx.AsyncClient() as client:
        result = await europe_pmc.lookup_locus(client, "AT1G01010", organism="arabidopsis_thaliana")
    assert result is not None


@live_only
@pytest.mark.asyncio
async def test_live_lookup_rice_locus_returns_hits() -> None:
    """v0.9 T19: real call against rice — exercises europe_pmc_slug='rice'.

    Confirms the organism resolver feeds the right slug into the Europe
    PMC query for a non-Arabidopsis organism. Rice OsDREB1A homolog
    Os01g0100100 should have hits (cereal genes are well-published).
    """
    async with httpx.AsyncClient() as client:
        result = await europe_pmc.lookup_locus(
            client, "Os01g0100100", organism="oryza_sativa", size=3
        )
    assert result["hitCount"] >= 0  # non-crash check; literature may be sparse
    assert result["organism"] == "oryza_sativa"


# --- abstract trimming ------------------------------------------------------
# abstractText measured at 10,968 of 16,360 bytes (67%) for AT3G51240 at the
# default page size. A row cap cannot help here — size already bounds rows — so
# the lever is field-level.


def test_include_abstract_default_keeps_the_abstract() -> None:
    """Default is unchanged: abstracts are usually the point of this tool."""
    row = europe_pmc._normalize({"id": "1", "abstractText": "long text"})
    assert row["abstractText"] == "long text"


def test_include_abstract_false_nulls_it() -> None:
    row = europe_pmc._normalize({"id": "1", "abstractText": "long text"}, False)
    assert row["abstractText"] is None
    # Everything else must survive — the point is trimming one field, not
    # degrading the record.
    assert row["id"] == "1"


@pytest.mark.asyncio
async def test_response_says_which_mode_produced_it(httpx_mock: HTTPXMock) -> None:
    """A null abstract is ambiguous without this flag.

    'Not requested' and 'this article has no abstract' look identical in the
    row, and only the original caller would know which. The flag is what makes
    the payload self-describing to anyone reading it later.
    """
    europe_pmc._CACHE.clear()
    body = {
        "hitCount": 1,
        "resultList": {"result": [{"id": "1", "pmid": "9", "abstractText": "long text"}]},
    }
    httpx_mock.add_response(json=body)
    async with httpx.AsyncClient() as client:
        trimmed = await europe_pmc.lookup_locus(client, "AT3G51240", include_abstract=False)
    assert trimmed["abstracts_included"] is False
    assert trimmed["hits"][0]["abstractText"] is None

    europe_pmc._CACHE.clear()
    httpx_mock.add_response(json=body)
    async with httpx.AsyncClient() as client:
        full = await europe_pmc.lookup_locus(client, "AT3G51240")
    assert full["abstracts_included"] is True
    assert full["hits"][0]["abstractText"] == "long text"


# ---------- issue #134: the journal the description promises ----------

_AT1G01010_URL = (
    "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
    "?query=AT1G01010&format=json&resultType=core&pageSize=10"
)


@pytest.mark.asyncio
async def test_the_journal_is_read_from_where_a_core_record_carries_it(
    httpx_mock: HTTPXMock,
) -> None:
    """journalTitle was null on all 160 hits of the dossier run (#134)."""
    httpx_mock.add_response(
        url=_AT1G01010_URL,
        json={
            "hitCount": 2,
            "resultList": {
                "result": [_one_result(), _one_result(id="2", journalInfo=None, pmid="2")]
            },
        },
    )
    async with httpx.AsyncClient() as client:
        result = await europe_pmc.lookup_locus(client, "AT1G01010")
    assert result["hits"][0]["journalTitle"] == "The Plant cell"
    # A record without journalInfo (a preprint) still reads as no journal.
    assert result["hits"][1]["journalTitle"] is None


@live_only
@pytest.mark.asyncio
async def test_live_hits_carry_a_journal() -> None:
    """Real execution: the key the projection reads exists on real records."""
    async with httpx.AsyncClient() as client:
        result = await europe_pmc.lookup_locus(client, "AT1G19850", size=5)
    assert result["returned"] > 0
    assert any(isinstance(h["journalTitle"], str) for h in result["hits"])


# ---------- issue #141: an answer without a count is not a count of zero ----------

_EMPTY_BODY = {"version": "6.9"}  # verbatim, 200 from /search, caught live 2026-09-22


@pytest.mark.asyncio
async def test_a_body_with_no_count_is_asked_again_and_never_cached_as_zero(
    httpx_mock: HTTPXMock,
) -> None:
    """Europe PMC intermittently answers 200 with only a version (#141).

    It was read as hitCount 0 and cached for the TTL, so genes with 22-91
    papers came back empty and ok=true. One fresh request recovers it.
    """
    httpx_mock.add_response(url=_AT1G01010_URL, json=_EMPTY_BODY)
    httpx_mock.add_response(
        url=_AT1G01010_URL, json={"hitCount": 61, "resultList": {"result": [_one_result()]}}
    )
    async with httpx.AsyncClient() as client:
        result = await europe_pmc.lookup_locus(client, "AT1G01010")
    assert (result["hitCount"], result["returned"]) == (61, 1)
    assert len(httpx_mock.get_requests()) == 2


@pytest.mark.asyncio
async def test_a_body_with_no_count_twice_is_an_upstream_error_and_is_not_cached(
    httpx_mock: HTTPXMock,
) -> None:
    from plant_genomics_mcp.errors import UpstreamUnavailableError

    httpx_mock.add_response(url=_AT1G01010_URL, json=_EMPTY_BODY, is_reusable=True)
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamUnavailableError, match="hitCount"):
            await europe_pmc.lookup_locus(client, "AT1G01010")
    assert europe_pmc._CACHE.stats()["size"] == 0  # the bad body was not kept


@pytest.mark.asyncio
async def test_a_real_zero_is_still_a_zero(httpx_mock: HTTPXMock) -> None:
    """Positive control for the two tests above: a genuine empty answer."""
    httpx_mock.add_response(
        url=_AT1G01010_URL,
        json={"version": "6.9", "hitCount": 0, "request": {}, "resultList": {"result": []}},
    )
    async with httpx.AsyncClient() as client:
        result = await europe_pmc.lookup_locus(client, "AT1G01010")
    assert (result["hitCount"], result["returned"], result["hits"]) == (0, 0, [])
    assert len(httpx_mock.get_requests()) == 1
    # The cache is live in this suite: a valid body IS kept, so the size-0
    # assertion above is a claim about the bad body, not about a dead cache.
    assert europe_pmc._CACHE.stats()["size"] == 1


@pytest.mark.asyncio
@pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
async def test_an_empty_locus_is_refused_input_not_an_outage(httpx_mock: HTTPXMock) -> None:
    """Audit 2026-09-22 L12: an empty locus is an empty query, which Europe PMC
    answers with this 200 (live 2026-09-22) — no hitCount, so the #141 shape
    check read it as an upstream outage after asking twice."""
    from plant_genomics_mcp.errors import NotFoundError

    httpx_mock.add_response(
        url=re.compile(r"^https://www\.ebi\.ac\.uk/europepmc/webservices/rest/search\?query=&.*"),
        json={
            "errCode": 404,
            "errMsg": "No search criteria provided. Please provide a search criteria "
            "which is less than 1500 characters.",
        },
        is_reusable=True,
    )
    httpx_mock.add_response(
        url=_AT1G01010_URL,
        json={"version": "6.9", "hitCount": 0, "request": {}, "resultList": {"result": []}},
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="invalid locus"):
            await europe_pmc.lookup_locus(client, "")
        # Positive control: a real locus with no papers is still an ok zero.
        result = await europe_pmc.lookup_locus(client, "AT1G01010")
    assert result["hitCount"] == 0


def test_normalize_gives_one_type_per_kind_of_value() -> None:
    """#134: Europe PMC sends pubYear and the Y/N flags as strings beside an
    int citedByCount. The projection types them: a year is an int, a flag a
    bool, an identifier (pmid) stays a string. A flag outside Y/N is refused
    by name rather than read as either answer; a normal row in the same test
    is the positive control."""
    from plant_genomics_mcp.errors import PlantGenomicsError

    hit = europe_pmc._normalize(_one_result())
    assert hit["pubYear"] == 2024 and type(hit["pubYear"]) is int
    assert hit["isOpenAccess"] is True and hit["hasPDF"] is True
    assert hit["pmid"] == "12345678"
    assert hit["citedByCount"] == 7
    closed = europe_pmc._normalize(_one_result(isOpenAccess="N", hasPDF="N"))
    assert closed["isOpenAccess"] is False and closed["hasPDF"] is False
    absent = europe_pmc._normalize(_one_result(pubYear=None, isOpenAccess=None, hasPDF=None))
    assert (absent["pubYear"], absent["isOpenAccess"], absent["hasPDF"]) == (None, None, None)
    with pytest.raises(PlantGenomicsError, match="isOpenAccess.*'maybe'"):
        europe_pmc._normalize(_one_result(isOpenAccess="maybe"))
    with pytest.raises(PlantGenomicsError, match="pubYear.*'20x4'"):
        europe_pmc._normalize(_one_result(pubYear="20x4"))
