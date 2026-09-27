"""Tests for the AraGWAS association backend (Arabidopsis-only).

Two tiers:
  1. Unit tests with mocked HTTP via pytest-httpx (pagination + projection +
     organism gating).
  2. Live integration tests gated by PLANT_GENOMICS_MCP_LIVE=1.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import aragwas
from plant_genomics_mcp.errors import NotFoundError, OrganismNotSupported, PlantGenomicsError

LIVE = os.environ.get("PLANT_GENOMICS_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set PLANT_GENOMICS_MCP_LIVE=1 to run")

_URL = f"{aragwas.BASE_URL}/api/genes/AT1G01060/associations/"
# The first page asks for the default limit (one 25-row page).
_FIRST = f"{_URL}?limit=25"
_NEXT = f"{_URL}?limit=25&offset=25"

# Real-shaped association (key names verified live 2026-07-20, AT1G01060).
_ASSOC: dict[str, Any] = {
    "score": 30.386,
    "maf": 0.00199,
    "mac": 1,
    "overBonferroni": True,
    "overFDR": True,
    "overPermutation": True,
    "snp": {
        "chr": "chr1",
        "position": 35574,
        "ref": "G",
        "anc": "G",
        "alt": "A",
        "coding": True,
        "geneName": "AT1G01060",
        "annotations": [
            {
                "function": "MISSENSE",
                "geneName": "AT9G00000",  # non-matching first entry
                "impact": "LOW",
                "transcriptId": "AT9G00000.1",
                "aminoAcidChange": "X0X",
                "effect": "SOMETHING_ELSE",
            },
            {
                "function": "MISSENSE",
                "geneName": "AT1G01060",  # matching entry — should win
                "impact": "MODERATE",
                "transcriptId": "AT1G01060.5",
                "aminoAcidChange": "P172L",
                "effect": "NON_SYNONYMOUS_CODING",
            },
        ],
    },
    "study": {
        "name": "clim-pet12_raw_amm",
        "method": "amm",
        "phenotype": {
            "name": "clim-pet12",
            "description": "Potential evapotranspiration of December (mm)",
        },
    },
}


@pytest.mark.asyncio
async def test_lookup_full_matches_gene_annotation(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=_FIRST, json={"count": 1, "links": {"next": None}, "results": [_ASSOC]}
    )
    async with httpx.AsyncClient() as client:
        r = await aragwas.lookup_locus(client, "AT1G01060", "arabidopsis")
    assert r["found"] is True
    assert r["organism"] == "arabidopsis_thaliana"
    assert r["association_count"] == 1
    assert r["truncated"] is False
    a = r["associations"][0]
    assert a["score"] == 30.386
    assert a["over_bonferroni"] is True
    assert a["snp"]["gene"] == "AT1G01060"
    assert a["snp"]["position"] == 35574
    # the annotation matching this gene wins over the first (non-matching) entry
    assert a["snp"]["effect"] == "NON_SYNONYMOUS_CODING"
    assert a["snp"]["amino_acid_change"] == "P172L"
    assert a["study"]["phenotype"] == "clim-pet12"


@pytest.mark.asyncio
async def test_lookup_paginates(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=_FIRST, json={"count": 2, "links": {"next": _NEXT}, "results": [_ASSOC]}
    )
    httpx_mock.add_response(
        url=_NEXT, json={"count": 2, "links": {"next": None}, "results": [_ASSOC]}
    )
    async with httpx.AsyncClient() as client:
        r = await aragwas.lookup_locus(client, "AT1G01060", "arabidopsis")
    assert r["association_count"] == 2
    assert r["returned"] == 2
    assert r["truncated"] is False


@pytest.mark.asyncio
async def test_lookup_does_not_follow_off_host_next(httpx_mock: HTTPXMock) -> None:
    """A next link on a look-alike host (``…1001genomes.org.evil.example``) must
    NOT be followed — the same-host guard is a host match, not a bare prefix
    (bug audit L2). Only page 1 is registered, so a followed off-host link would
    fail as an unexpected request; the request count pins the mechanism."""
    off_host = f"{aragwas.BASE_URL}.evil.example/api/genes/x/associations/?offset=25"
    httpx_mock.add_response(
        url=_FIRST, json={"count": 2, "links": {"next": off_host}, "results": [_ASSOC]}
    )
    async with httpx.AsyncClient() as client:
        r = await aragwas.lookup_locus(client, "AT1G01060", "arabidopsis")
    assert r["found"] is True
    assert len(httpx_mock.get_requests()) == 1  # off-host next was not fetched
    # The count still says a second row exists, so the answer says so too.
    assert r["truncated"] is True and r["next_cursor"] is not None


@pytest.mark.asyncio
async def test_lookup_non_json_200_raises_typed(httpx_mock: HTTPXMock) -> None:
    """A 200 carrying a non-JSON body surfaces as a typed PlantGenomicsError,
    not a raw JSONDecodeError (bug audit L3) — covers the ``cached``/literal-
    service _get shape shared with onekg."""
    from plant_genomics_mcp.errors import PlantGenomicsError

    # Body is non-JSON but NOT html: this is the L3 path proper. An html body
    # is now intercepted upstream in _http as an interposed page (see the
    # companion test below), so it can no longer reach the "non-JSON" branch.
    httpx_mock.add_response(url=_FIRST, text="upstream error, not json", status_code=200)
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlantGenomicsError, match="non-JSON"):
            await aragwas.lookup_locus(client, "AT1G01060", "arabidopsis")


@pytest.mark.asyncio
async def test_lookup_html_200_is_interposed_page(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An HTML body on a 200 is a challenge/error page, not this backend's data.

    It is retried and typed as UpstreamUnavailableError rather than reported as
    a parse failure, which would blame the upstream's data for a blocked or
    intercepted request.
    """
    from plant_genomics_mcp import _http
    from plant_genomics_mcp.errors import UpstreamUnavailableError

    async def _no_sleep(_seconds: float) -> None:
        pass

    monkeypatch.setattr(_http.asyncio, "sleep", _no_sleep)
    for _ in range(3):
        httpx_mock.add_response(url=_FIRST, text="<html>upstream error</html>", status_code=200)
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamUnavailableError):
            await aragwas.lookup_locus(client, "AT1G01060", "arabidopsis")


@pytest.mark.asyncio
async def test_lookup_page_cap_truncates(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(aragwas, "MAX_PAGES", 1)
    httpx_mock.add_response(
        url=_FIRST, json={"count": 200, "links": {"next": _NEXT}, "results": [_ASSOC]}
    )
    async with httpx.AsyncClient() as client:
        r = await aragwas.lookup_locus(client, "AT1G01060", "arabidopsis")
    assert r["association_count"] == 200
    assert r["returned"] == 1
    assert r["truncated"] is True


@pytest.mark.asyncio
async def test_lookup_empty_is_found_true(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=_FIRST, json={"count": 0, "links": {"next": None}, "results": []})
    async with httpx.AsyncClient() as client:
        r = await aragwas.lookup_locus(client, "AT1G01060", "arabidopsis")
    assert r["found"] is True
    assert r["associations"] == []
    assert r["association_count"] == 0


@pytest.mark.asyncio
async def test_lookup_annotation_fallback_and_empty(httpx_mock: HTTPXMock) -> None:
    """No gene-matching annotation → first; no annotations → null effect fields."""
    no_match = {**_ASSOC, "snp": {**_ASSOC["snp"], "geneName": "AT1G01060"}}
    no_match["snp"] = {**no_match["snp"], "annotations": [{"geneName": "OTHER", "effect": "E1"}]}
    no_ann = {**_ASSOC, "snp": {"chr": "chr1", "position": 1, "annotations": []}}
    httpx_mock.add_response(
        url=_FIRST, json={"count": 2, "links": {"next": None}, "results": [no_match, no_ann]}
    )
    async with httpx.AsyncClient() as client:
        r = await aragwas.lookup_locus(client, "AT1G01060", "arabidopsis")
    assert r["associations"][0]["snp"]["effect"] == "E1"  # fell back to first annotation
    assert r["associations"][1]["snp"]["effect"] is None  # no annotations at all


@pytest.mark.asyncio
async def test_lookup_malformed_raises(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=_FIRST, json=["unexpected", "list"])
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlantGenomicsError, match="unexpected payload"):
            await aragwas.lookup_locus(client, "AT1G01060", "arabidopsis")


@pytest.mark.asyncio
async def test_lookup_non_arabidopsis_raises() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(OrganismNotSupported) as exc:
            await aragwas.lookup_locus(client, "Os01g0100100", "rice")
    # The error names what the caller can do instead, not just that it failed.
    assert (exc.value.backend, exc.value.organism, exc.value.supported) == (
        "aragwas",
        "oryza_sativa",
        ["arabidopsis_thaliana"],
    )


@pytest.mark.asyncio
async def test_lookup_bad_agi_raises_before_network() -> None:
    """A malformed AGI (the typo that used to hit an upstream 500) raises
    NotFoundError before any HTTP call — no mock is registered, so a stray
    request would fail the test."""
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="AGI"):
            await aragwas.lookup_locus(client, "AT1G0106", "arabidopsis")


@pytest.mark.asyncio
@pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
async def test_a_lowercase_agi_is_asked_for_in_its_canonical_spelling(
    httpx_mock: HTTPXMock,
) -> None:
    """Audit 2026-09-22 L7: AGI_RE is case-insensitive, so 'at1g01060' passed
    validation and went out as typed; AraGWAS answers a lowercase AGI with
    HTTP 500, which the retry layer reports as an outage."""
    lower = f"{aragwas.BASE_URL}/api/genes/at1g01060/associations/?limit=25"
    httpx_mock.add_response(url=lower, status_code=500, text="Server Error", is_reusable=True)
    httpx_mock.add_response(
        url=_FIRST, json={"count": 1, "links": {"next": None}, "results": [_ASSOC]}
    )
    async with httpx.AsyncClient() as client:
        r = await aragwas.lookup_locus(client, "at1g01060", "arabidopsis")
    assert r["locus"] == "AT1G01060"
    assert r["association_count"] == 1


@live_only
@pytest.mark.asyncio
async def test_live_arabidopsis_associations() -> None:
    """Real AraGWAS call — AT1G01060 has GWAS associations."""
    async with httpx.AsyncClient() as client:
        r = await aragwas.lookup_locus(client, "AT1G01060", "arabidopsis")
    assert r["found"] is True
    assert r["association_count"] > 0
    assert r["associations"][0]["snp"]["position"]


# ---------- issue #137: study name and the thresholds behind the booleans ----------

# Verbatim from the live association (AT1G19850, 2026-09-22), trimmed to study.
_LIVE_STUDY = {
    "id": 283,
    "name": "('As75_raw_Full imputed genotype_amm',)",
    "transformation": "raw",
    "method": "amm",
    "phenotype": {"id": 283, "name": "As75", "description": "Arsenic concentrations in leaves"},
    "thresholds": [
        {"name": "bonferroni_threshold05", "value": 8.023918991285525},
        {"name": "bonferroni_threshold01", "value": 8.722888995621544},
        {"name": "bh_threshold", "value": 4.665903379135818},
        {"name": "total_associations", "value": 5283102},
        {"name": "permutation_threshold", "value": 15.954589770191001},
    ],
}


def test_the_study_name_is_unwrapped_and_its_thresholds_surface() -> None:
    row = aragwas._project({"score": 33.07, "study": _LIVE_STUDY}, "AT1G19850")
    assert row["study"]["name"] == "As75_raw_Full imputed genotype_amm"
    assert row["study"]["thresholds"] == {
        "bonferroni_threshold05": 8.023918991285525,
        "bonferroni_threshold01": 8.722888995621544,
        "bh_threshold": 4.665903379135818,
        "total_associations": 5283102,
        "permutation_threshold": 15.954589770191001,
    }
    # Positive control: a name that is not a tuple repr comes back as sent,
    # and a study without thresholds says so rather than inventing any.
    plain = aragwas._project({"study": {"name": "As75_raw"}}, "AT1G19850")
    assert plain["study"]["name"] == "As75_raw" and plain["study"]["thresholds"] == {}


@live_only
@pytest.mark.asyncio
async def test_live_score_is_minus_log10_p_on_the_thresholds_scale() -> None:
    """The Bonferroni 0.05 threshold is -log10(0.05 / total): score's scale."""
    import math

    async with httpx.AsyncClient() as client:
        r = await aragwas.lookup_locus(client, "AT1G19850")
    study = r["associations"][0]["study"]
    t = study["thresholds"]
    assert math.isclose(
        t["bonferroni_threshold05"], -math.log10(0.05 / t["total_associations"]), rel_tol=1e-6
    )
    assert "(" not in study["name"]


# --- default page size (2026-09-27) -------------------------------------------
# At the old default (every row up to 4 x 25-row pages) each Arabidopsis answer
# in the ARF dossier was 36-40k tokens, over Claude Code's 25k default cap on
# its own: 22 of 22 loci. The default is now one upstream page, strongest first.


def _serve(total: int, urls: list[str], *, honour_limit: bool = True, page: int = 25) -> Any:
    """A fake `_get` paging like AraGWAS: `?limit=` (default 25) and `?offset=`,
    rows strongest first, a `next` link while rows remain. With
    ``honour_limit=False`` it sends ``page`` rows whatever `?limit=` says."""

    async def fake(client: Any, url: str) -> dict[str, Any]:
        urls.append(url)
        params = httpx.URL(url).params
        limit = int(params.get("limit", 25)) if honour_limit else page
        offset = int(params.get("offset", 0))
        end = min(offset + limit, total)
        nxt = f"{_URL}?limit={limit}&offset={end}" if end < total else None
        rows = [{**_ASSOC, "score": float(total - i)} for i in range(offset, end)]
        return {"count": total, "links": {"next": nxt}, "results": rows}

    return fake


@pytest.mark.asyncio
async def test_the_default_answer_is_one_page_of_the_strongest_25(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    urls: list[str] = []
    monkeypatch.setattr(aragwas, "_get", _serve(126, urls))
    async with httpx.AsyncClient() as client:
        first = await aragwas.lookup_locus(client, "AT1G01060")
        rest = await aragwas.lookup_locus(client, "AT1G01060", cursor=first["next_cursor"])
    assert len(urls) == 2  # one upstream request per answer
    assert [a["score"] for a in first["associations"]] == [float(126 - i) for i in range(25)]
    assert (first["association_count"], first["returned"], first["truncated"]) == (126, 25, True)
    # Positive control: the cursor carries on at row 26, nothing skipped.
    assert rest["associations"][0]["score"] == 101.0 and rest["returned"] == 25


@pytest.mark.asyncio
async def test_pages_of_any_limit_concatenate_to_the_whole_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(aragwas, "_get", _serve(60, []))
    async with httpx.AsyncClient() as client:
        whole = await aragwas.lookup_locus(client, "AT1G01060", limit=100)
        walked: list[dict[str, Any]] = []
        cursor = None
        while True:
            page = await aragwas.lookup_locus(client, "AT1G01060", limit=25, cursor=cursor)
            walked += page["associations"]
            cursor = page["next_cursor"]
            if cursor is None:
                break
    assert (whole["returned"], whole["truncated"]) == (60, False)
    assert walked == whole["associations"]


@pytest.mark.asyncio
async def test_a_page_longer_than_the_limit_is_cut_and_resumed_exactly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If AraGWAS ignored `?limit=` and sent its 25 rows, the answer must still
    hold `limit` rows and the cursor resume at the first row not returned."""
    monkeypatch.setattr(aragwas, "_get", _serve(40, [], honour_limit=False))
    async with httpx.AsyncClient() as client:
        first = await aragwas.lookup_locus(client, "AT1G01060", limit=10)
        second = await aragwas.lookup_locus(
            client, "AT1G01060", limit=10, cursor=first["next_cursor"]
        )
    assert [a["score"] for a in first["associations"]] == [float(40 - i) for i in range(10)]
    assert [a["score"] for a in second["associations"]] == [float(30 - i) for i in range(10)]


def test_a_limit_out_of_range_is_clamped_not_obeyed() -> None:
    assert aragwas._resolve_limit(None) == aragwas.DEFAULT_LIMIT == 25
    assert aragwas._resolve_limit(0) == 1
    assert aragwas._resolve_limit(-3) == 1
    assert aragwas._resolve_limit(10_000) == aragwas.MAX_LIMIT == 100
    assert aragwas._resolve_limit(40) == 40  # an in-range limit is kept


@pytest.mark.asyncio
async def test_the_tool_takes_limit_and_passes_it_through(monkeypatch: pytest.MonkeyPatch) -> None:
    from plant_genomics_mcp import server

    urls: list[str] = []
    monkeypatch.setattr(aragwas, "_get", _serve(126, urls))
    args = {"locus": "AT1G01060", "limit": 60}
    server._validate_arguments("aragwas_associations", args)  # the schema accepts it
    r = await server._dispatch("aragwas_associations", args)
    assert r["returned"] == 60 and "limit=60" in urls[0]
    # Positive control: without limit the tool answers with the default page.
    r = await server._dispatch("aragwas_associations", {"locus": "AT1G01060"})
    assert r["returned"] == aragwas.DEFAULT_LIMIT


# --- every projected field, from a verbatim live row (#96 mutation triage) ----
# A test that reads three fields of a row cannot see the other fifteen: 51
# mutants renaming an output key or the upstream key it reads survived here.

# Row 7 of AT1G01060's associations (?limit=100, live 2026-09-27), verbatim
# except that annotations are cut to the first two and study.genotype and the
# "suggest" lists are dropped (none is read). Every field _project reads is on
# every live row at AT1G01060 and AT1G19850 (100/100 each; snp.geneName 96 and
# 88 of 100), and this row's three significance flags are not all equal.
_LIVE_ROW: dict[str, Any] = {
    "mac": 1,
    "maf": 0.00199600798403194,
    "score": 12.850055994675705,
    "created": "2019-09-25T15:32:11.098609",
    "study": {
        "id": 591,
        "name": "('clim-gs_tmin_raw_Full imputed genotype_amm',)",
        "transformation": "raw",
        "method": "amm",
        "phenotype": {
            "id": 591,
            "name": "clim-gs tmin",
            "studyName": "Lifetime fitness in Germany and Spain under rainfall manipulation",
            "description": "Minimum temperature average within growing season (_C)",
            "date": "2019-03-12T19:08:54.452000Z",
        },
        "nHitsBonf": 4,
        "nHitsPerm": 1,
        "nHitsThr": 1836835,
        "thresholds": [
            {"name": "bonferroni_threshold05", "value": 8.250514951149334},
            {"name": "bonferroni_threshold01", "value": 8.949484955485353},
            {"name": "bh_threshold", "value": 4.553475160679959},
            {"name": "total_associations", "value": 8901946},
            {"name": "permutation_threshold", "value": 12.923181305939378},
        ],
    },
    "overFDR": True,
    "overBonferroni": True,
    "overPermutation": False,
    "snp": {
        "coding": True,
        "alt": "T",
        "chr": "chr1",
        "position": 35368,
        "anc": "A",
        "annotations": [
            {
                "function": "MISSENSE",
                "rank": 8,
                "geneName": "AT1G01060",
                "impact": "MODERATE",
                "transcriptId": "AT1G01060.5",
                "codonChange": "gTg/gAg",
                "aminoAcidChange": "V210E",
                "effect": "NON_SYNONYMOUS_CODING",
            },
            {
                "function": "MISSENSE",
                "rank": 6,
                "geneName": "Exon_1_36810_36836",
                "impact": "MODERATE",
                "transcriptId": "AT1G01060.5-Protein",
                "codonChange": "gTg/gAg",
                "aminoAcidChange": "V210E",
                "effect": "NON_SYNONYMOUS_CODING",
            },
        ],
        "ref": "A",
        "geneName": "AT1G01060",
    },
}

# Written out by hand from the row above, not produced by the code under test.
_LIVE_ROW_PROJECTED: dict[str, Any] = {
    "score": 12.850055994675705,
    "maf": 0.00199600798403194,
    "mac": 1,
    "over_bonferroni": True,
    "over_fdr": True,
    "over_permutation": False,
    "snp": {
        "chr": "chr1",
        "position": 35368,
        "ref": "A",
        "alt": "T",
        "coding": True,
        "gene": "AT1G01060",
        "effect": "NON_SYNONYMOUS_CODING",
        "impact": "MODERATE",
        "amino_acid_change": "V210E",
        "transcript": "AT1G01060.5",
    },
    "study": {
        "name": "clim-gs_tmin_raw_Full imputed genotype_amm",
        "method": "amm",
        "phenotype": "clim-gs tmin",
        "phenotype_description": "Minimum temperature average within growing season (_C)",
        "thresholds": {
            "bonferroni_threshold05": 8.250514951149334,
            "bonferroni_threshold01": 8.949484955485353,
            "bh_threshold": 4.553475160679959,
            "total_associations": 8901946,
            "permutation_threshold": 12.923181305939378,
        },
    },
}


def _leaves(value: Any) -> list[Any]:
    if isinstance(value, dict):
        return [leaf for v in value.values() for leaf in _leaves(v)]
    return [value]


@pytest.mark.asyncio
async def test_a_live_row_is_projected_field_for_field(httpx_mock: HTTPXMock) -> None:
    # match_headers: a request without the JSON Accept header gets no response.
    httpx_mock.add_response(
        url=_FIRST,
        match_headers={"Accept": "application/json"},
        json={"count": 1, "links": {"next": None}, "results": [_LIVE_ROW]},
    )
    async with httpx.AsyncClient() as client:
        r = await aragwas.lookup_locus(client, "AT1G01060")
    assert r["associations"] == [_LIVE_ROW_PROJECTED]
    # The request is bounded by the backend's own timeout, not left unbounded.
    (request,) = httpx_mock.get_requests()
    assert request.extensions["timeout"]["read"] == aragwas.DEFAULT_TIMEOUT
    # The fixture can fail: no expected value is null, so a mutant that reads
    # a key the row lacks (null) cannot match the expected row by accident.
    assert None not in _leaves(_LIVE_ROW_PROJECTED)


def test_thresholds_keep_named_entries_and_skip_the_rest() -> None:
    study = {
        "thresholds": [
            {"name": "bh_threshold", "value": 4.55},
            "not a threshold",
            {"name": 7, "value": 1.0},
            {"value": 2.0},
        ]
    }
    assert aragwas._thresholds(study) == {"bh_threshold": 4.55}


@pytest.mark.asyncio
async def test_an_upstream_paging_short_is_followed_for_max_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If AraGWAS paged at 20 whatever `?limit=` said, a 100-row ask stops at
    MAX_PAGES (4) upstream pages, 80 rows, and the cursor resumes at row 81."""
    urls: list[str] = []
    monkeypatch.setattr(aragwas, "_get", _serve(200, urls, honour_limit=False, page=20))
    async with httpx.AsyncClient() as client:
        r = await aragwas.lookup_locus(client, "AT1G01060", limit=100)
        pages = len(urls)
        rest = await aragwas.lookup_locus(client, "AT1G01060", cursor=r["next_cursor"])
    assert pages == aragwas.MAX_PAGES == 4
    assert [a["score"] for a in r["associations"]] == [float(200 - i) for i in range(80)]
    assert (r["returned"], r["truncated"]) == (80, True)
    assert rest["associations"][0]["score"] == 120.0  # nothing skipped


@pytest.mark.asyncio
async def test_a_next_link_past_the_stated_count_still_means_more(httpx_mock: HTTPXMock) -> None:
    """When the row cap stops the walk, a same-host next link still set says
    more rows exist even if the stated count says none are left."""
    one = f"{aragwas.BASE_URL}/api/genes/AT1G01060/associations/?limit=1"
    httpx_mock.add_response(
        url=one,
        json={"count": 1, "links": {"next": f"{one}&offset=1"}, "results": [_ASSOC]},
    )
    # Positive control: count and link agree nothing is left.
    done = f"{aragwas.BASE_URL}/api/genes/AT1G01070/associations/?limit=1"
    httpx_mock.add_response(
        url=done, json={"count": 1, "links": {"next": None}, "results": [_ASSOC]}
    )
    async with httpx.AsyncClient() as client:
        more = await aragwas.lookup_locus(client, "AT1G01060", limit=1)
        last = await aragwas.lookup_locus(client, "AT1G01070", limit=1)
    assert (more["truncated"], more["next_cursor"] is not None) == (True, True)
    assert (last["truncated"], last["next_cursor"]) == (False, None)


# Fields on every live row probed (100/100 at AT1G01060 and at AT1G19850,
# 2026-09-27). The annotation-derived fields and snp.geneName are not on
# every row, so they are left to the verbatim-row test above.
_ALWAYS_SENT = (
    ("score",),
    ("maf",),
    ("mac",),
    ("over_bonferroni",),
    ("over_fdr",),
    ("over_permutation",),
    ("snp", "chr"),
    ("snp", "position"),
    ("snp", "ref"),
    ("snp", "alt"),
    ("snp", "coding"),
    ("study", "name"),
    ("study", "method"),
    ("study", "phenotype"),
    ("study", "phenotype_description"),
    ("study", "thresholds"),
)


@live_only
@pytest.mark.asyncio
async def test_live_rows_fill_every_field_upstream_always_sends() -> None:
    """The fixture above is one row as AraGWAS sent it on one day; this is
    what notices AraGWAS renaming a field later (projected as null)."""
    async with httpx.AsyncClient() as client:
        r = await aragwas.lookup_locus(client, "AT1G01060")
    assert r["returned"] == aragwas.DEFAULT_LIMIT  # a full page was checked
    for row in r["associations"]:
        for path in _ALWAYS_SENT:
            value: Any = row
            for key in path:
                value = value[key]
            assert value not in (None, {}), (path, row)
