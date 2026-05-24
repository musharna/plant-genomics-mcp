"""BAR (Bio-Analytic Resource) backend unit tests."""

from __future__ import annotations

import os

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import bar, server
from plant_genomics_mcp.errors import (
    NotFoundError,
    OrganismNotFound,
    OrganismNotSupported,
    PlantGenomicsError,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    bar._CACHE.clear()
    yield
    bar._CACHE.clear()


# Live shape captured 2026-05-23 against bar.utoronto.ca/api.
# /thalemine/gene_information/ returns an InterMine envelope with the
# positional row under results[0]; /gaia/aliases/ returns wasSuccessful +
# a list of entries (case-variant rows for the same locus).
_THALEMINE_OK = {
    "wasSuccessful": True,
    "modelName": "genomic",
    "results": [
        [
            "AT1G01010",
            "NAC domain containing protein 1",
            "locus:2200935",
            "NAC domain containing protein 1",
            "NAC001",
            "ANAC001, NAC001, NTL10",
            "NAC domain containing protein 1;(source:Araport11)",
            "Member of the NAC domain containing family of plant specific transcriptional regulators.",
            "NAC domain containing protein 1",
        ]
    ],
}

_GAIA_OK = {
    "wasSuccessful": True,
    "data": [
        {
            "species": "Arabidopsis_thaliana",
            "locus": "At1g01010",
            "geneid": None,
            "aliases": ["NAC domain containing protein 1"],
        },
        {
            "species": "Arabidopsis_thaliana",
            "locus": "AT1G01010",
            "geneid": "839580",
            "aliases": [
                "NAC001",
                "ANAC001",
                "NAC domain containing protein 1",
                "T25K16.1",
                "NM_099983",
                "Q0WV96",
                "locus:2200935",
            ],
        },
    ],
}


@pytest.mark.asyncio
async def test_gene_summary_happy(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://bar.utoronto.ca/api/thalemine/gene_information/AT1G01010",
        json=_THALEMINE_OK,
    )
    httpx_mock.add_response(
        url="https://bar.utoronto.ca/api/gaia/aliases/AT1G01010",
        json=_GAIA_OK,
    )
    async with httpx.AsyncClient() as client:
        result = await bar.gene_summary(client, "AT1G01010")
    assert result["locus"] == "AT1G01010"
    assert result["agi"] == "AT1G01010"
    assert result["symbol"] == "NAC001"
    assert result["full_name"] == "NAC domain containing protein 1"
    assert result["tair_locus_id"] == "locus:2200935"
    assert result["synonyms"] == ["ANAC001", "NAC001", "NTL10"]
    assert "Araport11" in result["computational_description"]
    assert "transcriptional regulators" in result["curator_summary"]
    assert result["ncbi_gene_id"] == "839580"
    assert "Q0WV96" in result["aliases"]
    assert "T25K16.1" in result["aliases"]
    assert result["species"] == "arabidopsis_thaliana"
    assert (
        result["source_url"] == "https://bar.utoronto.ca/api/thalemine/gene_information/AT1G01010"
    )


@pytest.mark.asyncio
async def test_gene_summary_invalid_locus() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="invalid locus"):
            await bar.gene_summary(client, "AT1G01010<script>")


@pytest.mark.asyncio
async def test_gene_summary_http_400_propagates(httpx_mock: HTTPXMock) -> None:
    # Non-Arabidopsis loci 400 with wasSuccessful=false (live shape 2026-05-23
    # for LOC_Os01g01080). _get raises PlantGenomicsError on 400 before we
    # even see the envelope; asyncio.gather surfaces the thalemine error.
    httpx_mock.add_response(
        url="https://bar.utoronto.ca/api/thalemine/gene_information/LOC_Os01g01080",
        status_code=400,
        json={"wasSuccessful": False, "error": "Invalid gene id"},
    )
    httpx_mock.add_response(
        url="https://bar.utoronto.ca/api/gaia/aliases/LOC_Os01g01080",
        json={
            "wasSuccessful": True,
            "data": [
                {
                    "species": "Oryza_sativa",
                    "locus": "LOC_Os01g01080",
                    "geneid": None,
                    "aliases": ["Os01g0100900"],
                }
            ],
        },
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(Exception, match="HTTP 400"):
            await bar.gene_summary(client, "LOC_Os01g01080")


@pytest.mark.asyncio
async def test_gene_summary_empty_results(httpx_mock: HTTPXMock) -> None:
    # Unknown Arabidopsis locus: thalemine 200s with wasSuccessful=true but
    # results=[] (live shape 2026-05-23 for AT1G99999).
    httpx_mock.add_response(
        url="https://bar.utoronto.ca/api/thalemine/gene_information/AT1G99999",
        json={"wasSuccessful": True, "modelName": "genomic", "results": []},
    )
    httpx_mock.add_response(
        url="https://bar.utoronto.ca/api/gaia/aliases/AT1G99999",
        json={"wasSuccessful": False, "error": "Nothing found"},
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="no record"):
            await bar.gene_summary(client, "AT1G99999")


@pytest.mark.asyncio
async def test_gene_summary_malformed_row(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://bar.utoronto.ca/api/thalemine/gene_information/AT1G01010",
        json={"wasSuccessful": True, "results": [["AT1G01010", "short"]]},
    )
    httpx_mock.add_response(
        url="https://bar.utoronto.ca/api/gaia/aliases/AT1G01010",
        json=_GAIA_OK,
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="malformed row"):
            await bar.gene_summary(client, "AT1G01010")


@pytest.mark.asyncio
async def test_gene_summary_aliases_degrade_on_failure(httpx_mock: HTTPXMock) -> None:
    # /gaia/aliases/ failures must not block the canonical TAIR fields:
    # ncbi_gene_id is None and aliases is [] when gaia returns wasSuccessful=false.
    httpx_mock.add_response(
        url="https://bar.utoronto.ca/api/thalemine/gene_information/AT1G01010",
        json=_THALEMINE_OK,
    )
    httpx_mock.add_response(
        url="https://bar.utoronto.ca/api/gaia/aliases/AT1G01010",
        json={"wasSuccessful": False, "error": "Nothing found"},
    )
    async with httpx.AsyncClient() as client:
        result = await bar.gene_summary(client, "AT1G01010")
    assert result["symbol"] == "NAC001"
    assert result["ncbi_gene_id"] is None
    assert result["aliases"] == []


# Live shape captured 2026-05-23 against
# /microarray_gene_expression/world_efp/arabidopsis/. Each numeric key is an
# ecotype "code"; entries carry probeset + per-replicate values + lat/lng.
# Unknown valid AGI → 200 wasSuccessful=false "There are no data found...";
# invalid locus → 200 wasSuccessful=false "Invalid gene id".
_EFP_OK = {
    "wasSuccessful": True,
    "data": {
        "111": {
            "source": "http://www.arabidopsis.org/servlets/TairObject?type=bio_sample_collection&id=1008803961",
            "id": "Bay-0 (CS6608) from Bayreuth, Germany<br>Longitude/Latitude/Elevation: E11/N50 at ~300m",
            "samples": ["ATGE_111_A", "ATGE_111_B", "ATGE_111_C"],
            "ctrlSamples": ["ATGE_113_A", "ATGE_113_C", "ATGE_113_D"],
            "position": {"lat": "49.950999", "lng": "11.572323"},
            "probeset": "261585_at",
            "values": {"ATGE_111_A": 6.9, "ATGE_111_B": 6.55, "ATGE_111_C": 10.0},
            "code": "111",
        },
        "112": {
            "source": "http://www.arabidopsis.org/servlets/TairObject?type=bio_sample_collection&id=1008803961",
            "id": "C24 (CS906) from Coimbra, Portugal",
            "samples": ["ATGE_112_A", "ATGE_112_C", "ATGE_112_D"],
            "ctrlSamples": ["ATGE_113_A", "ATGE_113_C", "ATGE_113_D"],
            "position": {"lat": "40.217684", "lng": "-8.436921"},
            "probeset": "261585_at",
            "values": {"ATGE_112_A": 1.05, "ATGE_112_C": 3.65, "ATGE_112_D": 1.45},
            "code": "112",
        },
    },
}


@pytest.mark.asyncio
async def test_efp_expression_happy(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://bar.utoronto.ca/api/microarray_gene_expression/world_efp/arabidopsis/AT1G01010",
        json=_EFP_OK,
    )
    async with httpx.AsyncClient() as client:
        result = await bar.efp_expression(client, "AT1G01010")
    assert result["locus"] == "AT1G01010"
    assert result["probeset"] == "261585_at"
    assert result["ecotype_count"] == 2
    assert result["species"] == "arabidopsis_thaliana"
    by_code = {e["code"]: e for e in result["ecotypes"]}
    bay = by_code["111"]
    assert bay["name"].startswith("Bay-0")
    # HTML <br> stripped from ecotype name so clients don't need to render it.
    assert "<br>" not in bay["name"]
    assert bay["samples"] == ["ATGE_111_A", "ATGE_111_B", "ATGE_111_C"]
    assert bay["values"] == {"ATGE_111_A": 6.9, "ATGE_111_B": 6.55, "ATGE_111_C": 10.0}
    # mean = (6.9 + 6.55 + 10.0) / 3 ≈ 7.8166...
    assert abs(bay["mean"] - 7.8166666666666666) < 1e-9
    assert bay["position"] == {"lat": "49.950999", "lng": "11.572323"}
    assert (
        result["source_url"]
        == "https://bar.utoronto.ca/api/microarray_gene_expression/world_efp/arabidopsis/AT1G01010"
    )


@pytest.mark.asyncio
async def test_efp_expression_invalid_locus_format() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="invalid locus"):
            await bar.efp_expression(client, "AT1G01010<script>")


@pytest.mark.asyncio
async def test_efp_expression_unknown_locus(httpx_mock: HTTPXMock) -> None:
    # Unknown valid AGI → 200 wasSuccessful=false (live shape 2026-05-23 for
    # AT1G99999). NotFoundError carries the upstream error string.
    httpx_mock.add_response(
        url="https://bar.utoronto.ca/api/microarray_gene_expression/world_efp/arabidopsis/AT1G99999",
        json={"wasSuccessful": False, "error": "There are no data found for the given gene"},
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="no data"):
            await bar.efp_expression(client, "AT1G99999")


@pytest.mark.asyncio
async def test_efp_expression_invalid_gene_id(httpx_mock: HTTPXMock) -> None:
    # Garbage but regex-passing locus → 200 wasSuccessful=false "Invalid gene id".
    httpx_mock.add_response(
        url="https://bar.utoronto.ca/api/microarray_gene_expression/world_efp/arabidopsis/INVALID",
        json={"wasSuccessful": False, "error": "Invalid gene id"},
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="Invalid gene id"):
            await bar.efp_expression(client, "INVALID")


@pytest.mark.skipif(
    not os.environ.get("PLANT_GENOMICS_MCP_LIVE"),
    reason="set PLANT_GENOMICS_MCP_LIVE=1 to hit bar.utoronto.ca/api",
)
@pytest.mark.asyncio
async def test_live_bar_at1g01010_returns_curator_summary() -> None:
    async with httpx.AsyncClient() as client:
        result = await bar.gene_summary(client, "AT1G01010")
    assert result["locus"] == "AT1G01010"
    assert result["agi"] == "AT1G01010"
    assert result["symbol"] == "NAC001"
    assert result["curator_summary"] and "NAC" in result["curator_summary"]
    assert result["ncbi_gene_id"] == "839580"
    assert "Q0WV96" in result["aliases"]


@pytest.mark.skipif(
    not os.environ.get("PLANT_GENOMICS_MCP_LIVE"),
    reason="set PLANT_GENOMICS_MCP_LIVE=1 to hit bar.utoronto.ca/api",
)
@pytest.mark.asyncio
async def test_live_bar_efp_expression_at1g01010() -> None:
    async with httpx.AsyncClient() as client:
        result = await bar.efp_expression(client, "AT1G01010")
    assert result["locus"] == "AT1G01010"
    assert result["species"] == "arabidopsis_thaliana"
    assert result["probeset"]  # uniform across ecotypes
    assert result["ecotype_count"] >= 30  # live shape 2026-05-23 ≈ 36
    sample = result["ecotypes"][0]
    assert sample["code"]
    assert sample["name"]
    assert sample["samples"]
    assert sample["mean"] is not None


# Live shape captured 2026-05-23 against /interactions/get_paper_by_agi/.
# Arabidopsis AIV returns curated GRN paper refs. Failures come back at HTTP
# 400 (not 200 wasSuccessful=false) — so the unknown/invalid paths exercise
# the underlying _get error surface, not a body-level branch.
_AIV_ARABIDOPSIS_OK = {
    "wasSuccessful": True,
    "data": [
        {
            "source_id": 20,
            "grn_title": "Ikeuchi et al. (Plant Cell Physiol., 2018) Wound Response Network",
            "image_url": "https://bar.utoronto.ca/GRN_Images/29462363.jpg",
            "source_name": "29462363",
            "comments": "Used enhanced yeast one-hybrid (Y1H)...",
            "cyjs_layout": '{"name": "breadthfirst", "animate" : "true"}',
            "tags": "auxin:Misc|cytokinin:Misc|ESR1:Gene|HSFB1:Gene|PLT3:Gene",
        },
        {
            "source_id": 42,
            "grn_title": "Other et al. (2020) Stress Network",
            "image_url": "https://bar.utoronto.ca/GRN_Images/12345678.jpg",
            "source_name": "12345678",
            "comments": "Different study.",
            "cyjs_layout": '{"name": "circle"}',
            "tags": "ABA:Misc|stress:Condition",
        },
    ],
}

# Live shape captured 2026-05-23 against /interactions/rice/. Rice AIV is
# predicted PPIs (not curated). protein_1 == queried locus, protein_2 is the
# partner. pcc is Pearson correlation across co-expression evidence.
_AIV_RICE_OK = {
    "wasSuccessful": True,
    "data": [
        {
            "protein_1": "LOC_Os01g01080",
            "protein_2": "LOC_Os01g52560",
            "total_hits": 1,
            "Num_species": 1,
            "Quality": 1,
            "pcc": 0.65,
        },
        {
            "protein_1": "LOC_Os01g01080",
            "protein_2": "LOC_Os08g39140",
            "total_hits": 1,
            "Num_species": 1,
            "Quality": 1,
            "pcc": 0.077,
        },
    ],
}


@pytest.mark.asyncio
async def test_aiv_interactions_arabidopsis_happy(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://bar.utoronto.ca/api/interactions/get_paper_by_agi/AT1G01010",
        json=_AIV_ARABIDOPSIS_OK,
    )
    async with httpx.AsyncClient() as client:
        result = await bar.aiv_interactions(client, "AT1G01010", organism="arabidopsis_thaliana")
    assert result["locus"] == "AT1G01010"
    assert result["organism"] == "arabidopsis_thaliana"
    assert result["kind"] == "grn_papers"
    assert result["count"] == 2
    first = result["papers"][0]
    assert first["pmid"] == "29462363"
    assert first["title"].startswith("Ikeuchi")
    assert first["image_url"].endswith("29462363.jpg")
    # tags pipe-split into list so clients don't reparse.
    assert "auxin:Misc" in first["tags"]
    assert "PLT3:Gene" in first["tags"]
    assert (
        result["source_url"]
        == "https://bar.utoronto.ca/api/interactions/get_paper_by_agi/AT1G01010"
    )


@pytest.mark.asyncio
async def test_aiv_interactions_arabidopsis_default_organism(httpx_mock: HTTPXMock) -> None:
    # organism kwarg defaults to arabidopsis_thaliana — match gene_summary/efp_expression UX.
    httpx_mock.add_response(
        url="https://bar.utoronto.ca/api/interactions/get_paper_by_agi/AT1G01010",
        json=_AIV_ARABIDOPSIS_OK,
    )
    async with httpx.AsyncClient() as client:
        result = await bar.aiv_interactions(client, "AT1G01010")
    assert result["organism"] == "arabidopsis_thaliana"
    assert result["kind"] == "grn_papers"


@pytest.mark.asyncio
async def test_aiv_interactions_rice_happy(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://bar.utoronto.ca/api/interactions/rice/LOC_Os01g01080",
        json=_AIV_RICE_OK,
    )
    async with httpx.AsyncClient() as client:
        result = await bar.aiv_interactions(client, "LOC_Os01g01080", organism="oryza_sativa")
    assert result["locus"] == "LOC_Os01g01080"
    assert result["organism"] == "oryza_sativa"
    assert result["kind"] == "ppi_predictions"
    assert result["count"] == 2
    first = result["partners"][0]
    # protein_1 == queried locus; partner_locus derived as the non-queried side.
    assert first["partner_locus"] == "LOC_Os01g52560"
    assert first["protein_1"] == "LOC_Os01g01080"
    assert first["protein_2"] == "LOC_Os01g52560"
    assert first["pcc"] == 0.65
    assert first["total_hits"] == 1
    assert first["num_species"] == 1
    assert first["quality"] == 1
    assert result["source_url"] == "https://bar.utoronto.ca/api/interactions/rice/LOC_Os01g01080"


@pytest.mark.asyncio
async def test_aiv_interactions_rice_aliases_common_name(httpx_mock: HTTPXMock) -> None:
    # organism="rice" must resolve via the registry alias index to oryza_sativa.
    httpx_mock.add_response(
        url="https://bar.utoronto.ca/api/interactions/rice/LOC_Os01g01080",
        json=_AIV_RICE_OK,
    )
    async with httpx.AsyncClient() as client:
        result = await bar.aiv_interactions(client, "LOC_Os01g01080", organism="rice")
    assert result["organism"] == "oryza_sativa"
    assert result["kind"] == "ppi_predictions"


@pytest.mark.asyncio
async def test_aiv_interactions_invalid_locus_format() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="invalid locus"):
            await bar.aiv_interactions(client, "AT1G01010<script>")


@pytest.mark.asyncio
async def test_aiv_interactions_arabidopsis_unknown_400(httpx_mock: HTTPXMock) -> None:
    # BAR returns HTTP 400 (not 200 wasSuccessful=false) for unknown AGI.
    # _get propagates this as PlantGenomicsError with the body included.
    httpx_mock.add_response(
        url="https://bar.utoronto.ca/api/interactions/get_paper_by_agi/AT1G99999",
        status_code=400,
        json={"wasSuccessful": False, "error": "Invalid AGI"},
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlantGenomicsError, match="HTTP 400"):
            await bar.aiv_interactions(client, "AT1G99999")


@pytest.mark.asyncio
async def test_aiv_interactions_rice_unknown_400(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://bar.utoronto.ca/api/interactions/rice/LOC_Os01g99999",
        status_code=400,
        json={
            "wasSuccessful": False,
            "error": "There are no data found for the given gene",
        },
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlantGenomicsError, match="HTTP 400"):
            await bar.aiv_interactions(client, "LOC_Os01g99999", organism="oryza_sativa")


@pytest.mark.asyncio
async def test_aiv_interactions_rice_rapdb_format_400(httpx_mock: HTTPXMock) -> None:
    # BAR rice endpoint rejects RAP-DB format (Os01g0100100) — only MSU
    # (LOC_Os01g01080) works. Surfaces as PlantGenomicsError HTTP 400.
    httpx_mock.add_response(
        url="https://bar.utoronto.ca/api/interactions/rice/Os01g0100100",
        status_code=400,
        json={"wasSuccessful": False, "error": "Invalid species or gene ID"},
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlantGenomicsError, match="HTTP 400"):
            await bar.aiv_interactions(client, "Os01g0100100", organism="oryza_sativa")


@pytest.mark.asyncio
async def test_aiv_interactions_defensive_was_successful_false(httpx_mock: HTTPXMock) -> None:
    # Defensive: if BAR ever 200s with wasSuccessful=false (not currently
    # observed for AIV, but matches the contract used by efp_expression),
    # surface as NotFoundError carrying the upstream error.
    httpx_mock.add_response(
        url="https://bar.utoronto.ca/api/interactions/get_paper_by_agi/AT1G01010",
        json={"wasSuccessful": False, "error": "Invalid AGI"},
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="Invalid AGI"):
            await bar.aiv_interactions(client, "AT1G01010")


@pytest.mark.asyncio
async def test_aiv_interactions_unsupported_organism() -> None:
    # zea_mays is in the registry but BAR AIV has no maize lane → OrganismNotSupported.
    async with httpx.AsyncClient() as client:
        with pytest.raises(OrganismNotSupported, match="bar_aiv"):
            await bar.aiv_interactions(client, "GRMZM2G123456", organism="zea_mays")


@pytest.mark.asyncio
async def test_aiv_interactions_unknown_organism() -> None:
    # "klingon" is not in the registry at all → OrganismNotFound.
    async with httpx.AsyncClient() as client:
        with pytest.raises(OrganismNotFound):
            await bar.aiv_interactions(client, "AT1G01010", organism="klingon")


@pytest.mark.skipif(
    not os.environ.get("PLANT_GENOMICS_MCP_LIVE"),
    reason="set PLANT_GENOMICS_MCP_LIVE=1 to hit bar.utoronto.ca/api",
)
@pytest.mark.asyncio
async def test_live_bar_aiv_interactions_arabidopsis() -> None:
    async with httpx.AsyncClient() as client:
        result = await bar.aiv_interactions(client, "AT1G01010")
    assert result["organism"] == "arabidopsis_thaliana"
    assert result["kind"] == "grn_papers"
    assert result["count"] >= 1
    paper = result["papers"][0]
    assert paper["pmid"]
    assert paper["title"]


@pytest.mark.skipif(
    not os.environ.get("PLANT_GENOMICS_MCP_LIVE"),
    reason="set PLANT_GENOMICS_MCP_LIVE=1 to hit bar.utoronto.ca/api",
)
@pytest.mark.asyncio
async def test_live_bar_aiv_interactions_rice() -> None:
    async with httpx.AsyncClient() as client:
        result = await bar.aiv_interactions(client, "LOC_Os01g01080", organism="rice")
    assert result["organism"] == "oryza_sativa"
    assert result["kind"] == "ppi_predictions"
    assert result["count"] >= 1
    partner = result["partners"][0]
    assert partner["partner_locus"]
    assert partner["pcc"] is not None


# v1.0.0 + v1.0.1 shipped server.py with `bar` missing from the `from
# plant_genomics_mcp import (...)` block (lines 63-81). Direct-call unit
# tests above never exercise the MCP dispatch path, so the NameError only
# fires when a client actually calls the tool over stdio/HTTP. Regression
# test below routes through server._dispatch so any future drop of the
# `bar,` import fails CI loudly.
@pytest.mark.asyncio
async def test_dispatch_bar_gene_summary_resolves_bar_module(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(
        url="https://bar.utoronto.ca/api/thalemine/gene_information/AT1G01010",
        json=_THALEMINE_OK,
    )
    httpx_mock.add_response(
        url="https://bar.utoronto.ca/api/gaia/aliases/AT1G01010",
        json=_GAIA_OK,
    )
    result = await server._dispatch("bar_gene_summary", {"locus": "AT1G01010"})
    assert result["locus"] == "AT1G01010"
    assert result["symbol"] == "NAC001"
