"""Tests for the ThaleMine backend (experimental interactions + GeneRIFs).

The behaviours worth locking here are the ones that cost probing to discover:

* ThaleMine has **no 404** — an unknown locus and a real gene with no data both
  answer ``HTTP 200`` with rows. The OUTER join makes them distinguishable, and
  that distinction has to survive into ``NotFoundError`` vs ``found=False``.
* Interaction rows arrive one per *evidence record*, so the same partner recurs;
  aggregation must union the evidence rather than emit duplicate partners.
* The locus reaches the wire inside query XML, so validation is a security
  boundary, not a nicety.
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import thalemine
from plant_genomics_mcp.errors import (
    NotFoundError,
    OrganismNotFound,
    OrganismNotSupported,
    PlantGenomicsError,
    UpstreamUnavailableError,
)

LOCUS = "AT5G11260"
QUERY_URL = f"{thalemine.BASE_URL}{thalemine.QUERY_PATH}"

# Column order mirrors thalemine._INTERACTION_VIEW.
#   0 gene id | 1 gene symbol | 2 partner id | 3 partner symbol | 4 type
#   5 relationship | 6 detection method | 7 pubmed | 8 source
INT_ROWS = [
    [
        LOCUS,
        "HY5",
        "AT2G32950",
        "COP1",
        "physical",
        "direct interaction",
        "two hybrid",
        "9755158",
        "BioGRID interaction data set",
    ],
    [
        LOCUS,
        "HY5",
        "AT2G32950",
        "COP1",
        "physical",
        "physical association",
        "pull down",
        "10990463",
        "IntAct molecular interactions",
    ],
    [
        LOCUS,
        "HY5",
        "AT2G32950",
        "COP1",
        "genetic",
        "synthetic rescue",
        "genetic interference",
        "9755158",
        "BioGRID interaction data set",
    ],
    [
        LOCUS,
        "HY5",
        "AT1G02340",
        "HFR1",
        "physical",
        "direct interaction",
        "two hybrid",
        "23503597",
        "BioGRID interaction data set",
    ],
]

# Column order mirrors thalemine._RIF_VIEW.
RIF_ROWS = [
    [LOCUS, "HY5", "HY5 binds the G-box.", "17001643", "2010-01-01"],
    [LOCUS, "HY5", "HY5 is degraded by COP1 in the dark.", "10990463", None],
]

# The OUTER-join shape for a gene that exists but has nothing in the collection.
EMPTY_INT_ROW = [[LOCUS, "HY5", None, None, None, None, None, None, None]]
EMPTY_RIF_ROW = [["AT1G01010", "NAC001", None, None, None]]


def _payload(rows: list[list[Any]]) -> dict[str, Any]:
    return {"results": rows, "views": [], "executionTime": "0.1"}


@pytest.fixture
async def client() -> Any:
    async with httpx.AsyncClient() as c:
        yield c


# --------------------------------------------------------------------------
# query construction
# --------------------------------------------------------------------------


def test_query_xml_uses_outer_join_and_constraint() -> None:
    """The OUTER join is what separates 'no such gene' from 'no data'."""
    xml = thalemine._query_xml("Gene.primaryIdentifier", "Gene.geneRifs", LOCUS)
    assert '<join path="Gene.geneRifs" style="OUTER"/>' in xml
    assert f'<constraint path="Gene.primaryIdentifier" op="=" value="{LOCUS}"/>' in xml
    assert xml.startswith('<query model="genomic"') and xml.endswith("</query>")


def test_report_url_points_at_the_gene_portal() -> None:
    assert thalemine._report_url(LOCUS) == (
        f"{thalemine.BASE_URL}/portal.do?externalids={LOCUS}&class=Gene"
    )


@pytest.mark.parametrize("bad", ["AT1G01010<x>", 'AT1"G', "AT1G01010&amp;", "AT1G/01010"])
async def test_invalid_locus_never_reaches_the_wire(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock, bad: str
) -> None:
    """Validation is an XML-injection boundary: the locus is interpolated into
    the query document, so metacharacters must be rejected before any request."""
    with pytest.raises(NotFoundError, match="invalid locus"):
        await thalemine.lookup_interactions(client, bad)
    with pytest.raises(NotFoundError, match="invalid locus"):
        await thalemine.lookup_gene_rifs(client, bad)
    assert not httpx_mock.get_requests()


@pytest.mark.parametrize("fn", ["lookup_interactions", "lookup_gene_rifs"])
async def test_non_arabidopsis_raises_organism_not_supported(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock, fn: str
) -> None:
    with pytest.raises(OrganismNotSupported) as exc:
        await getattr(thalemine, fn)(client, LOCUS, organism="oryza_sativa")
    assert exc.value.backend == "thalemine"
    assert exc.value.organism == "oryza_sativa"
    assert not httpx_mock.get_requests()


async def test_unknown_organism_raises_organism_not_found(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    with pytest.raises(OrganismNotFound):
        await thalemine.lookup_interactions(client, LOCUS, organism="not_a_species")
    assert not httpx_mock.get_requests()


async def test_organism_accepts_taxid_and_scientific_name(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(json=_payload(RIF_ROWS))
    out = await thalemine.lookup_gene_rifs(client, LOCUS, organism=3702)
    assert out["organism"] == "arabidopsis_thaliana"


# --------------------------------------------------------------------------
# interactions
# --------------------------------------------------------------------------


async def test_interactions_aggregate_evidence_per_partner(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    """Three COP1 rows are one partner with the union of their evidence."""
    httpx_mock.add_response(json=_payload(INT_ROWS))
    out = await thalemine.lookup_interactions(client, LOCUS)

    assert out["found"] is True
    assert out["gene_symbol"] == "HY5"
    assert out["organism"] == "arabidopsis_thaliana"
    assert out["partner_count"] == 2
    assert out["evidence_count"] == 4
    assert out["truncated"] is False

    cop1 = out["partners"][0]
    assert cop1["partner_locus"] == "AT2G32950"
    assert cop1["partner_symbol"] == "COP1"
    assert cop1["evidence_count"] == 3
    assert cop1["interaction_types"] == ["genetic", "physical"]
    assert cop1["relationship_types"] == [
        "direct interaction",
        "physical association",
        "synthetic rescue",
    ]
    assert cop1["detection_methods"] == ["genetic interference", "pull down", "two hybrid"]
    assert cop1["pubmed_ids"] == ["10990463", "9755158"]
    assert cop1["sources"] == ["BioGRID interaction data set", "IntAct molecular interactions"]


async def test_interactions_sort_by_evidence_then_locus(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    """Ties break on locus so the ordering is stable across runs."""
    rows = [
        [LOCUS, "HY5", "AT3G00003", "C", "physical", "r", "m", "1", "s"],
        [LOCUS, "HY5", "AT1G00001", "A", "physical", "r", "m", "2", "s"],
        [LOCUS, "HY5", "AT2G00002", "B", "physical", "r", "m", "3", "s"],
        [LOCUS, "HY5", "AT2G00002", "B", "physical", "r", "m2", "4", "s"],
    ]
    httpx_mock.add_response(json=_payload(rows))
    out = await thalemine.lookup_interactions(client, LOCUS)
    assert [p["partner_locus"] for p in out["partners"]] == [
        "AT2G00002",  # 2 evidence rows
        "AT1G00001",  # 1, ties broken alphabetically
        "AT3G00003",
    ]


async def test_interactions_truncate_reports_true_total(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(thalemine, "MAX_PARTNERS", 2)
    rows = [[LOCUS, "HY5", f"AT1G0000{i}", None, "physical", "r", "m", "1", "s"] for i in range(5)]
    httpx_mock.add_response(json=_payload(rows))
    out = await thalemine.lookup_interactions(client, LOCUS)
    assert out["partner_count"] == 5
    assert out["truncated"] is True
    assert len(out["partners"]) == 2


async def test_interactions_existing_gene_with_no_data_is_not_an_error(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    """The OUTER null row means 'real gene, nothing curated' — found=False."""
    httpx_mock.add_response(json=_payload(EMPTY_INT_ROW))
    out = await thalemine.lookup_interactions(client, LOCUS)
    assert out["found"] is False
    assert out["partner_count"] == 0
    assert out["evidence_count"] == 0
    assert out["partners"] == []
    assert out["gene_symbol"] == "HY5"


async def test_interactions_unknown_locus_raises_not_found(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    """Zero rows — not a 404 — is how ThaleMine says 'no such gene'."""
    httpx_mock.add_response(json=_payload([]))
    with pytest.raises(NotFoundError, match="no gene with primaryIdentifier"):
        await thalemine.lookup_interactions(client, "AT9G99999")


async def test_interactions_skips_rows_with_non_string_partner(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    """A malformed partner cell drops that row rather than inventing a partner."""
    rows = [
        [LOCUS, "HY5", 12345, None, "physical", "r", "m", "1", "s"],
        [LOCUS, "HY5", "AT2G32950", "COP1", "physical", "r", "m", "1", "s"],
    ]
    httpx_mock.add_response(json=_payload(rows))
    out = await thalemine.lookup_interactions(client, LOCUS)
    assert out["partner_count"] == 1
    assert out["partners"][0]["partner_locus"] == "AT2G32950"


async def test_interactions_drop_short_rows(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    """A row missing trailing columns can't be indexed safely — skip it."""
    httpx_mock.add_response(json=_payload([[LOCUS, "HY5", "AT2G32950"], *INT_ROWS]))
    out = await thalemine.lookup_interactions(client, LOCUS)
    assert out["partner_count"] == 2
    assert out["evidence_count"] == 4


async def test_interactions_null_symbol_survives(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    """Unnamed genes are common; a null symbol must not become the string 'None'."""
    rows = [[LOCUS, None, "AT2G32950", None, "physical", None, None, None, None]]
    httpx_mock.add_response(json=_payload(rows))
    out = await thalemine.lookup_interactions(client, LOCUS)
    assert out["gene_symbol"] is None
    p = out["partners"][0]
    assert p["partner_symbol"] is None
    assert p["relationship_types"] == []
    assert p["detection_methods"] == []
    assert p["pubmed_ids"] == []
    assert p["interaction_types"] == ["physical"]


# --------------------------------------------------------------------------
# GeneRIFs
# --------------------------------------------------------------------------


async def test_gene_rifs_project_statements_and_pubmed(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(json=_payload(RIF_ROWS))
    out = await thalemine.lookup_gene_rifs(client, LOCUS)
    assert out["found"] is True
    assert out["rif_count"] == 2
    assert out["truncated"] is False
    assert out["gene_symbol"] == "HY5"
    assert out["source_url"].endswith(f"externalids={LOCUS}&class=Gene")
    assert out["gene_rifs"][0] == {
        "annotation": "HY5 binds the G-box.",
        "pubmed_id": "17001643",
        "time_stamp": "2010-01-01",
    }
    # A missing timestamp stays null rather than being dropped or stringified.
    assert out["gene_rifs"][1]["time_stamp"] is None


async def test_gene_rifs_preserve_upstream_order(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    """Documented contract: no reordering, because there is no ranking to apply."""
    httpx_mock.add_response(json=_payload(RIF_ROWS))
    out = await thalemine.lookup_gene_rifs(client, LOCUS)
    assert [r["annotation"] for r in out["gene_rifs"]] == [r[2] for r in RIF_ROWS]


async def test_gene_rifs_truncate_reports_true_total(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(thalemine, "MAX_RIFS", 1)
    httpx_mock.add_response(json=_payload(RIF_ROWS))
    out = await thalemine.lookup_gene_rifs(client, LOCUS)
    assert out["rif_count"] == 2
    assert out["truncated"] is True
    assert len(out["gene_rifs"]) == 1


async def test_gene_rifs_existing_gene_with_none_is_not_an_error(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(json=_payload(EMPTY_RIF_ROW))
    out = await thalemine.lookup_gene_rifs(client, "AT1G01010")
    assert out["found"] is False
    assert out["rif_count"] == 0
    assert out["gene_rifs"] == []
    assert out["gene_symbol"] == "NAC001"


async def test_gene_rifs_unknown_locus_raises_not_found(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(json=_payload([]))
    with pytest.raises(NotFoundError):
        await thalemine.lookup_gene_rifs(client, "AT9G99999")


# --------------------------------------------------------------------------
# transport, payload shape, caching
# --------------------------------------------------------------------------


async def test_request_carries_query_xml_and_json_format(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(json=_payload(RIF_ROWS))
    await thalemine.lookup_gene_rifs(client, LOCUS)
    req = httpx_mock.get_requests()[0]
    qs = parse_qs(urlparse(str(req.url)).query)
    assert qs["format"] == ["json"]
    assert qs["size"] == [str(thalemine.MAX_ROWS)]
    assert LOCUS in qs["query"][0]
    assert "Gene.geneRifs.annotation" in qs["query"][0]
    assert req.headers["Accept"] == "application/json"


async def test_results_are_cached_across_calls(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(json=_payload(RIF_ROWS))
    first = await thalemine.lookup_gene_rifs(client, LOCUS)
    second = await thalemine.lookup_gene_rifs(client, LOCUS)
    assert first == second
    assert len(httpx_mock.get_requests()) == 1


async def test_interactions_and_rifs_do_not_share_a_cache_entry(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    """Different query XML must key differently or one would serve the other."""
    httpx_mock.add_response(json=_payload(RIF_ROWS), is_reusable=True)
    await thalemine.lookup_gene_rifs(client, LOCUS)
    await thalemine.lookup_interactions(client, LOCUS)
    assert len(httpx_mock.get_requests()) == 2


@pytest.mark.parametrize("payload", [[], "text", 42])
async def test_non_dict_payload_raises_typed_error(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock, payload: Any
) -> None:
    httpx_mock.add_response(json=payload)
    with pytest.raises(PlantGenomicsError, match="unexpected payload"):
        await thalemine.lookup_gene_rifs(client, LOCUS)


async def test_in_band_error_field_raises(client: httpx.AsyncClient, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json={"error": "Path Gene.bogus is not in the model"})
    with pytest.raises(PlantGenomicsError, match="not in the model"):
        await thalemine.lookup_gene_rifs(client, LOCUS)


async def test_missing_results_list_raises(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(json={"views": []})
    with pytest.raises(PlantGenomicsError, match="no 'results' list"):
        await thalemine.lookup_gene_rifs(client, LOCUS)


async def test_non_list_rows_are_filtered_out(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(json={"results": ["junk", {"a": 1}, *RIF_ROWS]})
    out = await thalemine.lookup_gene_rifs(client, LOCUS)
    assert out["rif_count"] == 2


async def test_upstream_500_surfaces_as_upstream_unavailable(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    # Retried, so the response must be reusable; Retry-After keeps it instant.
    httpx_mock.add_response(status_code=500, headers={"Retry-After": "0"}, is_reusable=True)
    with pytest.raises(UpstreamUnavailableError):
        await thalemine.lookup_gene_rifs(client, LOCUS)


async def test_upstream_400_surfaces_with_its_message(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    """A bad view path is a coding bug — it must not masquerade as 'no data'."""
    httpx_mock.add_response(status_code=400, text="[ERROR] 400 Path not in the model")
    with pytest.raises(PlantGenomicsError):
        await thalemine.lookup_gene_rifs(client, LOCUS)


# --------------------------------------------------------------------------
# live tests — opt-in, pin the biology rather than the plumbing
# --------------------------------------------------------------------------

live = pytest.mark.skipif(
    not os.environ.get("PLANT_GENOMICS_MCP_LIVE"),
    reason="set PLANT_GENOMICS_MCP_LIVE=1 to run live ThaleMine tests",
)


@live
async def test_live_hy5_interacts_with_cop1(client: httpx.AsyncClient) -> None:
    """HY5–COP1 is the canonical photomorphogenesis interaction; it must be the
    best-supported partner and carry real method + publication provenance."""
    out = await thalemine.lookup_interactions(client, "AT5G11260")
    assert out["found"] is True
    assert out["gene_symbol"] == "HY5"
    cop1 = out["partners"][0]
    assert cop1["partner_locus"] == "AT2G32950"
    assert cop1["partner_symbol"] == "COP1"
    assert cop1["evidence_count"] > 5
    assert "two hybrid" in cop1["detection_methods"]
    assert len(cop1["pubmed_ids"]) > 3
    # Both curated sources should be represented for a pair this well studied.
    assert any("BioGRID" in s for s in cop1["sources"])
    assert any("IntAct" in s for s in cop1["sources"])


@live
async def test_live_hy5_has_many_gene_rifs(client: httpx.AsyncClient) -> None:
    out = await thalemine.lookup_gene_rifs(client, "AT5G11260")
    assert out["found"] is True
    assert out["rif_count"] > 50  # 114 as of release 5.1.0-20250704
    assert out["truncated"] is True
    assert len(out["gene_rifs"]) == thalemine.MAX_RIFS
    assert all(r["annotation"] for r in out["gene_rifs"])
    assert any(r["pubmed_id"] for r in out["gene_rifs"])


@live
async def test_live_real_gene_without_rifs_is_found_false_not_error(
    client: httpx.AsyncClient,
) -> None:
    """AT1G01010 exists (NAC001) but has no GeneRIF — the case that would look
    identical to a missing gene without the OUTER join."""
    out = await thalemine.lookup_gene_rifs(client, "AT1G01010")
    assert out["found"] is False
    assert out["rif_count"] == 0
    assert out["gene_symbol"] == "NAC001"


@live
async def test_live_unknown_locus_is_not_found(client: httpx.AsyncClient) -> None:
    with pytest.raises(NotFoundError):
        await thalemine.lookup_interactions(client, "AT9G99999")


@live
async def test_live_allele_and_strain_classes_are_still_empty(
    client: httpx.AsyncClient,
) -> None:
    """Guard on the finding that re-scoped this backend (2026-07-21).

    ThaleMine's Allele / Strain classes exist in the data model but hold zero
    rows, which is why this module exposes interactions and GeneRIFs instead of
    the alleles / germplasm the model appears to promise. If this ever starts
    failing, ThaleMine has loaded that data and an allele tool becomes buildable.
    """
    for cls in ("Allele", "Strain"):
        resp = await client.get(
            f"{thalemine.BASE_URL}{thalemine.QUERY_PATH}",
            params={
                "query": f'<query model="genomic" view="{cls}.primaryIdentifier"></query>',
                "format": "count",
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        assert resp.text.strip() == "0", f"{cls} is now populated — revisit the design"


@live
async def test_live_report_url_resolves(client: httpx.AsyncClient) -> None:
    resp = await client.get(thalemine._report_url("AT5G11260"), follow_redirects=True, timeout=30.0)
    assert resp.status_code == 200


@live
async def test_live_output_matches_the_published_schema(client: httpx.AsyncClient) -> None:
    """Real payloads must satisfy the outputSchema we advertise to clients."""
    from plant_genomics_mcp.models import ExperimentalInteractions, GeneRifs

    ints = await thalemine.lookup_interactions(client, "AT5G11260")
    rifs = await thalemine.lookup_gene_rifs(client, "AT5G11260")
    ExperimentalInteractions.model_validate(ints)
    GeneRifs.model_validate(rifs)
    # Round-trips through MCP as JSON.
    assert json.loads(json.dumps(ints))["found"] is True
