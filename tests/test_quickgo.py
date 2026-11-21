"""Tests for the QuickGO REST client.

Two tiers (mirrors the europe_pmc / uniprot pattern):
  1. Unit tests with mocked HTTP via pytest-httpx.
  2. Live integration test gated by PLANT_GENOMICS_MCP_LIVE=1.
"""

from __future__ import annotations

import os

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import quickgo
from plant_genomics_mcp.errors import RateLimitError, UpstreamUnavailableError

LIVE = os.environ.get("PLANT_GENOMICS_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set PLANT_GENOMICS_MCP_LIVE=1 to run")


def _ann(go_id: str, aspect: str, *, go_name: str | None = None, **overrides):
    """Synthetic QuickGO annotation row shaped like the wire format."""
    base = {
        "geneProductId": "UniProtKB:Q0WV96",
        "symbol": "NAC001",
        "qualifier": "enables",
        "goId": go_id,
        "goName": go_name or f"term {go_id}",
        "goAspect": aspect,
        "goEvidence": "IPI",
        "evidenceCode": "ECO:0000353",
        "reference": "PMID:30356219",
        "assignedBy": "TAIR",
        "taxonId": 3702,
        "taxonName": "Arabidopsis thaliana",
        "date": "20201218",
        "withFrom": None,
    }
    base.update(overrides)
    return base


# ---------- mocked unit tests ----------


@pytest.mark.asyncio
async def test_lookup_by_uniprot_basic(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=(
            "https://www.ebi.ac.uk/QuickGO/services/annotation/search"
            "?geneProductId=Q0WV96&limit=50&includeFields=goName%2CtaxonName"
        ),
        json={
            "numberOfHits": 9,
            "results": [
                _ann("GO:0000976", "molecular_function", go_name="DNA binding"),
                _ann("GO:0006355", "biological_process", go_name="regulation of transcription"),
            ],
        },
    )
    async with httpx.AsyncClient() as client:
        result = await quickgo.lookup_by_uniprot(client, "Q0WV96")
    assert result["uniprot_accession"] == "Q0WV96"
    assert result["numberOfHits"] == 9
    assert result["returned"] == 2
    assert result["annotations"][0]["goId"] == "GO:0000976"
    assert result["annotations"][0]["goName"] == "DNA binding"
    # by_aspect rollup groups by goAspect.
    assert result["by_aspect"]["molecular_function"] == [
        {"goId": "GO:0000976", "goName": "DNA binding"},
    ]
    assert result["by_aspect"]["biological_process"] == [
        {"goId": "GO:0006355", "goName": "regulation of transcription"},
    ]


@pytest.mark.asyncio
async def test_lookup_by_uniprot_dedupes_repeated_go_id_in_rollup(httpx_mock: HTTPXMock) -> None:
    """Same goId with different evidence codes → one rollup entry per goId."""
    httpx_mock.add_response(
        url=(
            "https://www.ebi.ac.uk/QuickGO/services/annotation/search"
            "?geneProductId=Q0WV96&limit=50&includeFields=goName%2CtaxonName"
        ),
        json={
            "numberOfHits": 3,
            "results": [
                _ann("GO:0006355", "biological_process", go_name="reg of transcription"),
                _ann(
                    "GO:0006355",
                    "biological_process",
                    go_name="reg of transcription",
                    goEvidence="IDA",
                    reference="PMID:99999999",
                ),
                _ann("GO:0005634", "cellular_component", go_name="nucleus"),
            ],
        },
    )
    async with httpx.AsyncClient() as client:
        result = await quickgo.lookup_by_uniprot(client, "Q0WV96")
    # 3 raw annotations, 2 distinct goIds.
    assert result["returned"] == 3
    bp = result["by_aspect"]["biological_process"]
    assert len(bp) == 1, f"expected dedup on goId, got {bp}"
    assert bp[0]["goId"] == "GO:0006355"
    assert result["by_aspect"]["cellular_component"][0]["goId"] == "GO:0005634"


@pytest.mark.asyncio
async def test_lookup_by_uniprot_limit_clamps_to_max(httpx_mock: HTTPXMock) -> None:
    """limit=999 is clamped to MAX_LIMIT."""
    httpx_mock.add_response(
        url=(
            "https://www.ebi.ac.uk/QuickGO/services/annotation/search"
            f"?geneProductId=Q0WV96&limit={quickgo.MAX_LIMIT}"
            "&includeFields=goName%2CtaxonName"
        ),
        json={"numberOfHits": 0, "results": []},
    )
    async with httpx.AsyncClient() as client:
        result = await quickgo.lookup_by_uniprot(client, "Q0WV96", limit=999)
    assert result["returned"] == 0


@pytest.mark.asyncio
async def test_lookup_by_uniprot_skips_rows_missing_aspect_or_id(httpx_mock: HTTPXMock) -> None:
    """Rows with missing goAspect or goId don't contribute to by_aspect."""
    httpx_mock.add_response(
        url=(
            "https://www.ebi.ac.uk/QuickGO/services/annotation/search"
            "?geneProductId=Q0WV96&limit=50&includeFields=goName%2CtaxonName"
        ),
        json={
            "numberOfHits": 3,
            "results": [
                _ann("GO:0000976", "molecular_function"),
                {**_ann("GO:0000000", "molecular_function"), "goAspect": None},  # bad aspect
                {**_ann("GO:0000000", "molecular_function"), "goId": None},  # bad goId
            ],
        },
    )
    async with httpx.AsyncClient() as client:
        result = await quickgo.lookup_by_uniprot(client, "Q0WV96")
    # All 3 rows still surface in annotations[]; only the well-formed one rolls up.
    assert result["returned"] == 3
    assert result["by_aspect"]["molecular_function"] == [
        {"goId": "GO:0000976", "goName": "term GO:0000976"},
    ]


# ---------- live integration (real-execution check) ----------


@live_only
@pytest.mark.asyncio
async def test_live_lookup_q0wv96_returns_annotations() -> None:
    """Real call to QuickGO — Q0WV96 (NAC001) should have GO annotations."""
    async with httpx.AsyncClient() as client:
        result = await quickgo.lookup_by_uniprot(client, "Q0WV96", limit=20)
    assert result["numberOfHits"] > 0
    assert result["returned"] > 0
    aspects = set(result["by_aspect"].keys())
    # NAC001 is a transcription factor — molecular_function and
    # biological_process are both expected.
    assert "molecular_function" in aspects or "biological_process" in aspects
    # QuickGO cannot always supply taxonName; the answer says which kind it is.
    named = [a for a in result["annotations"] if a["taxonName"]]
    if result["taxon_name_included"]:
        assert named, "the answer claims taxonName and no row has one"
        assert result["taxon_name_missing_because"] is None
    else:
        assert not named, named[:1]
        assert "taxonName" in result["taxon_name_missing_because"]
    assert all(a["taxonId"] == 3702 for a in result["annotations"])


# ---------- issue #132: the payload says what it cut and what it collapsed ----------

_SEARCH_URL = (
    "https://www.ebi.ac.uk/QuickGO/services/annotation/search"
    "?geneProductId=Q0WV96&limit=50&includeFields=goName%2CtaxonName"
)
_SEARCH_URL_NO_TAXON_NAME = (
    "https://www.ebi.ac.uk/QuickGO/services/annotation/search"
    "?geneProductId=Q0WV96&limit=50&includeFields=goName"
)


@pytest.mark.asyncio
async def test_a_capped_answer_is_flagged_truncated_and_a_whole_one_is_not(
    httpx_mock: HTTPXMock,
) -> None:
    """numberOfHits 51 / returned 50 used to ship with no flag (issue #132)."""
    rows = [_ann("GO:0006355", "biological_process"), _ann("GO:0005634", "cellular_component")]
    httpx_mock.add_response(url=_SEARCH_URL, json={"numberOfHits": 3, "results": rows})
    httpx_mock.add_response(url=_SEARCH_URL, json={"numberOfHits": 2, "results": rows})
    async with httpx.AsyncClient() as client:
        capped = await quickgo.lookup_by_uniprot(client, "Q0WV96")
        quickgo._CACHE.clear()
        whole = await quickgo.lookup_by_uniprot(client, "Q0WV96")
    assert (capped["numberOfHits"], capped["returned"], capped["truncated"]) == (3, 2, True)
    # Positive control: everything upstream has was returned.
    assert (whole["numberOfHits"], whole["returned"], whole["truncated"]) == (2, 2, False)


@pytest.mark.asyncio
async def test_the_payload_names_the_key_by_aspect_is_deduplicated_on(
    httpx_mock: HTTPXMock,
) -> None:
    """16 rollup terms beside 50 annotations read as a cut, not a dedup (#132)."""
    httpx_mock.add_response(
        url=_SEARCH_URL,
        json={
            "numberOfHits": 2,
            "results": [
                _ann("GO:0006355", "biological_process"),
                _ann("GO:0006355", "biological_process", goEvidence="IDA"),
            ],
        },
    )
    async with httpx.AsyncClient() as client:
        result = await quickgo.lookup_by_uniprot(client, "Q0WV96")
    assert result["by_aspect_deduped_on"] == "goId"
    # The dedup it names is the one it did: two rows, one term.
    assert result["returned"] == 2 and len(result["by_aspect"]["biological_process"]) == 1


@pytest.mark.asyncio
async def test_both_tool_forms_ship_every_field_the_output_schema_declares(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The single and batch wrappers used to re-list QuickGO's keys by hand.

    Each copy dropped whatever was added to QuickGO's answer after it was
    written — ``upstream_version`` (issue #121) never reached either form's
    payload although the output schema declares it. Driven through the real
    projection, with only UniProt resolution stubbed and QuickGO's HTTP mocked.
    """
    from plant_genomics_mcp import batch, server, uniprot
    from plant_genomics_mcp.models import LocusGoAnnotations

    async def _resolved(client, locus, organism="arabidopsis_thaliana"):
        return {"primaryAccession": "Q0WV96"}

    monkeypatch.setattr(uniprot, "lookup_locus", _resolved)
    rows = [_ann("GO:0006355", "biological_process")]
    httpx_mock.add_response(
        url=_SEARCH_URL, json={"numberOfHits": 2, "results": rows}, is_reusable=True
    )
    declared = set(LocusGoAnnotations.model_fields)

    async with httpx.AsyncClient() as client:
        single = await server._resolve_then_go_annotations(
            client, "AT1G01010", "arabidopsis_thaliana", 50
        )
        env = await batch.batch_locus_go_annotations(client, ["AT1G01010"])

    for form, payload in (("single", single), ("batch", env["results"]["AT1G01010"])):
        assert set(payload) == declared, (form, declared ^ set(payload))
        LocusGoAnnotations.model_validate(payload)  # extra="forbid": nothing undeclared
        assert payload["truncated"] is True and payload["upstream_version"] is None, form
    assert env["errors"] == {}  # positive control: the batch call itself succeeded


@pytest.mark.asyncio
async def test_with_from_can_be_left_out_and_the_payload_says_so(httpx_mock: HTTPXMock) -> None:
    """gene_report's Markdown never reads withFrom, and on an IPI-heavy gene it
    was about two thirds of every row (2026-09-27): the report leaves it out,
    and a null withFrom must not read as "no interaction partner"."""
    partner = [{"connectedXrefs": [{"db": "AGI_LocusCode", "id": "AT1G19220"}]}]
    rows = [_ann("GO:0005515", "molecular_function", withFrom=partner)]
    httpx_mock.add_response(url=_SEARCH_URL, json={"numberOfHits": 1, "results": rows})
    async with httpx.AsyncClient() as client:
        lean = await quickgo.lookup_by_uniprot(client, "Q0WV96", include_with_from=False)
        full = await quickgo.lookup_by_uniprot(client, "Q0WV96")  # the same cached page
    assert lean["with_from_included"] is False
    assert [a["withFrom"] for a in lean["annotations"]] == [None]
    # Positive control: by default withFrom is there, and leaving it out of one
    # answer did not strip it from the cached upstream page.
    assert full["with_from_included"] is True
    assert [a["withFrom"] for a in full["annotations"]] == [partner]
    assert lean["annotations"][0]["goId"] == full["annotations"][0]["goId"] == "GO:0005515"


@pytest.mark.parametrize(
    ("bad", "problem"),
    [
        pytest.param({"results": []}, "no integer 'numberOfHits'", id="no count"),
        pytest.param({"numberOfHits": 2}, "results is not a list", id="no results"),
    ],
)
@pytest.mark.asyncio
async def test_a_body_without_its_count_or_rows_is_refused_before_the_store(
    httpx_mock: HTTPXMock, bad: dict, problem: str
) -> None:
    """The count was read after the store, so a body without one failed every
    call for the TTL without asking again, and a body without ``results``
    read as no annotations. Every live answer carries both, zero included
    (2026-09-28). Positive control, same cache: the answer that follows."""
    rows = [_ann("GO:0006355", "biological_process")]
    # Asked twice with taxonName and twice without: the search without it is
    # tried before the failure is raised, and it is refused the same way.
    for url in (_SEARCH_URL, _SEARCH_URL, _SEARCH_URL_NO_TAXON_NAME, _SEARCH_URL_NO_TAXON_NAME):
        httpx_mock.add_response(url=url, json=bad)
    httpx_mock.add_response(url=_SEARCH_URL, json={"numberOfHits": 1, "results": rows})
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamUnavailableError, match=problem):
            await quickgo.lookup_by_uniprot(client, "Q0WV96")
        good = await quickgo.lookup_by_uniprot(client, "Q0WV96")
    assert (good["numberOfHits"], good["returned"]) == (1, 1)
    assert good["taxon_name_included"] is True
    assert len(httpx_mock.get_requests()) == 5


# ---------- QuickGO fails the search that asks for taxonName ----------


@pytest.mark.asyncio
async def test_a_failed_taxon_name_search_is_answered_without_the_name_and_says_so(
    httpx_mock: HTTPXMock,
) -> None:
    """QuickGO answered every search that asked for taxonName with a 500 after
    16-20 s and the same search without it in under a second (2026-11-19), so
    `locus_go_annotations` failed for every caller over one optional label.
    Positive control, same test: an accession whose search with the name
    works is answered with it, in one request."""
    from plant_genomics_mcp.models import LocusGoAnnotations

    base = "https://www.ebi.ac.uk/QuickGO/services/annotation/search"
    nameless = _ann("GO:0006355", "biological_process", taxonName=None)
    httpx_mock.add_response(url=_SEARCH_URL, status_code=500)
    httpx_mock.add_response(
        url=_SEARCH_URL_NO_TAXON_NAME, json={"numberOfHits": 1, "results": [nameless]}
    )
    healthy = f"{base}?geneProductId=P93024&limit=50&includeFields=goName%2CtaxonName"
    httpx_mock.add_response(
        url=healthy,
        json={"numberOfHits": 1, "results": [_ann("GO:0003700", "molecular_function")]},
    )
    async with httpx.AsyncClient() as client:
        degraded = await quickgo.lookup_by_uniprot(client, "Q0WV96")
        full = await quickgo.lookup_by_uniprot(client, "P93024")

    assert degraded["taxon_name_included"] is False
    assert "taxonName" in degraded["taxon_name_missing_because"]
    assert "HTTP 500" in degraded["taxon_name_missing_because"]
    assert [(a["goId"], a["taxonId"], a["taxonName"]) for a in degraded["annotations"]] == [
        ("GO:0006355", 3702, None)
    ]
    assert (degraded["numberOfHits"], degraded["returned"]) == (1, 1)
    LocusGoAnnotations.model_validate({"locus": "AT1G01010", **degraded})  # extra="forbid"

    assert full["taxon_name_included"] is True
    assert full["taxon_name_missing_because"] is None
    assert [a["taxonName"] for a in full["annotations"]] == ["Arabidopsis thaliana"]
    LocusGoAnnotations.model_validate({"locus": "AT1G19850", **full})

    # One try with the name, one without, one for the healthy accession: the
    # failing search is not retried, which is what kept the answer under a minute.
    assert [str(r.url) for r in httpx_mock.get_requests()] == [
        _SEARCH_URL,
        _SEARCH_URL_NO_TAXON_NAME,
        healthy,
    ]


@pytest.mark.asyncio
async def test_quickgo_down_with_and_without_the_name_is_raised_not_degraded(
    httpx_mock: HTTPXMock,
) -> None:
    """A failure of the search without taxonName means QuickGO is down, not
    that the name is the broken part. Positive control: a failure with the
    name alone is answered."""
    httpx_mock.add_response(url=_SEARCH_URL, status_code=500)
    for _ in range(quickgo.MAX_RETRIES):
        httpx_mock.add_response(
            url=_SEARCH_URL_NO_TAXON_NAME, status_code=500, headers={"Retry-After": "0"}
        )
    other = "https://www.ebi.ac.uk/QuickGO/services/annotation/search?geneProductId=P93024&limit=50"
    httpx_mock.add_response(url=f"{other}&includeFields=goName%2CtaxonName", status_code=500)
    httpx_mock.add_response(
        url=f"{other}&includeFields=goName", json={"numberOfHits": 0, "results": []}
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(
            UpstreamUnavailableError, match=r"exhausted 3 retries \(last HTTP 500\)"
        ):
            await quickgo.lookup_by_uniprot(client, "Q0WV96")
        answered = await quickgo.lookup_by_uniprot(client, "P93024")
    assert answered["taxon_name_included"] is False
    assert len(httpx_mock.get_requests()) == 1 + quickgo.MAX_RETRIES + 2


@pytest.mark.asyncio
async def test_a_rate_limited_taxon_name_search_is_answered_and_a_lasting_limit_is_raised(
    httpx_mock: HTTPXMock,
) -> None:
    """The search with taxonName gets one try, so a 429 on it is not retried
    there, and the retry helper raises a 429 as RateLimitError, not
    UpstreamUnavailableError: caught by neither, one refused request failed the
    call. The search without the name keeps its retries, so the call is
    answered, and a limit that outlasts those retries is still raised as a
    rate limit for the caller to back off on."""
    nameless = _ann("GO:0006355", "biological_process", taxonName=None)
    httpx_mock.add_response(url=_SEARCH_URL, status_code=429)
    httpx_mock.add_response(
        url=_SEARCH_URL_NO_TAXON_NAME, status_code=429, headers={"Retry-After": "0"}
    )
    httpx_mock.add_response(
        url=_SEARCH_URL_NO_TAXON_NAME, json={"numberOfHits": 1, "results": [nameless]}
    )
    other = "https://www.ebi.ac.uk/QuickGO/services/annotation/search?geneProductId=P93024&limit=50"
    httpx_mock.add_response(url=f"{other}&includeFields=goName%2CtaxonName", status_code=429)
    for _ in range(quickgo.MAX_RETRIES):
        httpx_mock.add_response(
            url=f"{other}&includeFields=goName", status_code=429, headers={"Retry-After": "0"}
        )
    async with httpx.AsyncClient() as client:
        answered = await quickgo.lookup_by_uniprot(client, "Q0WV96")
        with pytest.raises(RateLimitError, match=r"exhausted 3 retries \(HTTP 429\)"):
            await quickgo.lookup_by_uniprot(client, "P93024")

    assert answered["taxon_name_included"] is False
    assert "HTTP 429" in answered["taxon_name_missing_because"]
    assert [a["goId"] for a in answered["annotations"]] == ["GO:0006355"]
    assert [str(r.url) for r in httpx_mock.get_requests()] == [
        _SEARCH_URL,
        _SEARCH_URL_NO_TAXON_NAME,
        _SEARCH_URL_NO_TAXON_NAME,
        f"{other}&includeFields=goName%2CtaxonName",
        *[f"{other}&includeFields=goName"] * quickgo.MAX_RETRIES,
    ]
