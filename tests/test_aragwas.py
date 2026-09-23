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
        url=_URL, json={"count": 1, "links": {"next": None}, "results": [_ASSOC]}
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
        url=_URL, json={"count": 2, "links": {"next": _NEXT}, "results": [_ASSOC]}
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
        url=_URL, json={"count": 2, "links": {"next": off_host}, "results": [_ASSOC]}
    )
    async with httpx.AsyncClient() as client:
        r = await aragwas.lookup_locus(client, "AT1G01060", "arabidopsis")
    assert r["found"] is True
    assert len(httpx_mock.get_requests()) == 1  # off-host next was not fetched


@pytest.mark.asyncio
async def test_lookup_non_json_200_raises_typed(httpx_mock: HTTPXMock) -> None:
    """A 200 carrying a non-JSON body surfaces as a typed PlantGenomicsError,
    not a raw JSONDecodeError (bug audit L3) — covers the ``cached``/literal-
    service _get shape shared with onekg."""
    from plant_genomics_mcp.errors import PlantGenomicsError

    # Body is non-JSON but NOT html: this is the L3 path proper. An html body
    # is now intercepted upstream in _http as an interposed page (see the
    # companion test below), so it can no longer reach the "non-JSON" branch.
    httpx_mock.add_response(url=_URL, text="upstream error, not json", status_code=200)
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
        httpx_mock.add_response(url=_URL, text="<html>upstream error</html>", status_code=200)
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamUnavailableError):
            await aragwas.lookup_locus(client, "AT1G01060", "arabidopsis")


@pytest.mark.asyncio
async def test_lookup_page_cap_truncates(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(aragwas, "MAX_PAGES", 1)
    httpx_mock.add_response(
        url=_URL, json={"count": 200, "links": {"next": _NEXT}, "results": [_ASSOC]}
    )
    async with httpx.AsyncClient() as client:
        r = await aragwas.lookup_locus(client, "AT1G01060", "arabidopsis")
    assert r["association_count"] == 200
    assert r["returned"] == 1
    assert r["truncated"] is True


@pytest.mark.asyncio
async def test_lookup_empty_is_found_true(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=_URL, json={"count": 0, "links": {"next": None}, "results": []})
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
        url=_URL, json={"count": 2, "links": {"next": None}, "results": [no_match, no_ann]}
    )
    async with httpx.AsyncClient() as client:
        r = await aragwas.lookup_locus(client, "AT1G01060", "arabidopsis")
    assert r["associations"][0]["snp"]["effect"] == "E1"  # fell back to first annotation
    assert r["associations"][1]["snp"]["effect"] is None  # no annotations at all


@pytest.mark.asyncio
async def test_lookup_malformed_raises(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=_URL, json=["unexpected", "list"])
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlantGenomicsError, match="unexpected payload"):
            await aragwas.lookup_locus(client, "AT1G01060", "arabidopsis")


@pytest.mark.asyncio
async def test_lookup_non_arabidopsis_raises() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(OrganismNotSupported):
            await aragwas.lookup_locus(client, "Os01g0100100", "rice")


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
    lower = f"{aragwas.BASE_URL}/api/genes/at1g01060/associations/"
    httpx_mock.add_response(url=lower, status_code=500, text="Server Error", is_reusable=True)
    httpx_mock.add_response(
        url=_URL, json={"count": 1, "links": {"next": None}, "results": [_ASSOC]}
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
