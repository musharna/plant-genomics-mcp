"""ensembl_plants_paralogs: a locus -> its Ensembl Compara (plants) paralogues.

`gramene_homologs` carries Gramene v69's projection of Compara, which keeps
`within_species_paralog` and drops `other_paralog` entirely, so AT1G19850 had
no paralog there at all (gap row `paralog-closure-empty`). Ensembl REST serves
both at `/homology/id/<species>/<gene>?compara=plants;type=paralogues` (live,
2026-09-25: AT1G19850 -> 24 other_paralog; AT2G28350 -> 3 within + 21 other).
The fixtures below are that response's shape with `sequence=none`, cut short.

Compara answers `{"data": []}` both for an id Ensembl does not know and for a
real gene it keeps no homology for (AT4G13495, an ncRNA gene, live), so the
empty answer alone cannot say which; the tool asks /lookup/id to tell them
apart.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import ensembl_plants
from plant_genomics_mcp.errors import NotFoundError, PlantGenomicsError


def _homology_url(species: str, gene: str) -> re.Pattern[str]:
    return re.compile(rf"^https://rest\.ensembl\.org/homology/id/{species}/{re.escape(gene)}\?.*")


def _lookup_url(gene: str) -> re.Pattern[str]:
    return re.compile(rf"^https://rest\.ensembl\.org/lookup/id/{re.escape(gene)}\?.*")


def _paralog(
    gene: str,
    kind: str,
    level: str,
    perc_id: float,
    species: str = "arabidopsis_thaliana",
    taxon_id: int = 3702,
) -> dict:
    return {
        "type": kind,
        "taxonomy_level": level,
        "method_link_type": "ENSEMBL_PARALOGUES",
        "dn_ds": None,
        "source": {
            "id": "AT2G28350",
            "species": "arabidopsis_thaliana",
            "taxon_id": 3702,
            "protein_id": "AT2G28350.1",
            "perc_id": 40.0,
            "perc_pos": 50.0,
            "cigar_line": "10M",
        },
        "target": {
            "id": gene,
            "species": species,
            "taxon_id": taxon_id,
            "protein_id": f"{gene}.1",
            "perc_id": perc_id,
            "perc_pos": perc_id + 10,
            "cigar_line": "10M",
        },
    }


def _answer(gene: str, homologies: list[dict]) -> dict:
    return {"data": [{"id": gene, "homologies": homologies}]}


@pytest.fixture(autouse=True)
def _clear_cache():
    ensembl_plants._CACHE.clear()
    yield
    ensembl_plants._CACHE.clear()


@pytest.mark.asyncio
async def test_paralogs_carry_both_categories_closest_first(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=_homology_url("arabidopsis_thaliana", "AT2G28350"),
        json=_answer(
            "AT2G28350",
            [
                _paralog("AT1G19850", "other_paralog", "Viridiplantae", 36.7),
                _paralog("AT1G77850", "within_species_paralog", "Magnoliopsida", 61.2),
                _paralog("AT4G30080", "within_species_paralog", "Pentapetalae", 70.5),
            ],
        ),
    )
    async with httpx.AsyncClient() as client:
        out = await ensembl_plants.paralogs(client, "AT2G28350")

    (request,) = httpx_mock.get_requests()
    assert request.url.params["compara"] == "plants"
    assert request.url.params["type"] == "paralogues"
    assert request.url.params["sequence"] == "none"

    assert out["found"] is True
    assert out["locus"] == "AT2G28350"
    assert out["organism"] == "arabidopsis_thaliana"
    assert (out["total"], out["returned"], out["truncated"]) == (3, 3, False)
    assert out["counts_by_type"] == {"other_paralog": 1, "within_species_paralog": 2}
    assert [p["locus"] for p in out["paralogs"]] == ["AT4G30080", "AT1G77850", "AT1G19850"]
    assert out["paralogs"][0] == {
        "locus": "AT4G30080",
        "type": "within_species_paralog",
        "taxonomy_level": "Pentapetalae",
        "perc_id": 70.5,
        "perc_pos": 80.5,
        "protein_id": "AT4G30080.1",
    }
    assert out["upstream_version"] is None


@pytest.mark.asyncio
async def test_empty_answer_is_read_three_ways(httpx_mock: HTTPXMock) -> None:
    """Compara's empty answer is not one thing, and the three must not blur.

    A gene Compara holds with no paralogue is found with an empty list; a real
    gene Compara holds nothing for is found=false; an id Ensembl does not know
    is a not-found error. The first case is the positive control: the empty
    list is an answer, not a failure.
    """
    httpx_mock.add_response(
        url=_homology_url("arabidopsis_thaliana", "AT5G61850"), json=_answer("AT5G61850", [])
    )
    httpx_mock.add_response(
        url=_homology_url("arabidopsis_thaliana", "AT4G13495"), json={"data": []}
    )
    httpx_mock.add_response(
        url=_lookup_url("AT4G13495"),
        json={"id": "AT4G13495", "species": "arabidopsis_thaliana", "biotype": "ncRNA"},
    )
    httpx_mock.add_response(
        url=_homology_url("arabidopsis_thaliana", "AT9G99999"), json={"data": []}
    )
    httpx_mock.add_response(
        url=_lookup_url("AT9G99999"), status_code=400, json={"error": "ID 'AT9G99999' not found"}
    )
    async with httpx.AsyncClient() as client:
        single_copy = await ensembl_plants.paralogs(client, "AT5G61850")
        outside = await ensembl_plants.paralogs(client, "AT4G13495")
        with pytest.raises(NotFoundError, match="AT9G99999"):
            await ensembl_plants.paralogs(client, "AT9G99999")

    assert single_copy["found"] is True
    assert (single_copy["total"], single_copy["paralogs"]) == (0, [])
    assert outside["found"] is False
    assert (outside["total"], outside["paralogs"], outside["counts_by_type"]) == (0, [], {})


@pytest.mark.asyncio
async def test_limit_truncates_after_counting(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=_homology_url("arabidopsis_thaliana", "AT1G19850"),
        json=_answer(
            "AT1G19850",
            [
                _paralog(f"AT1G0{i}000", "other_paralog", "Viridiplantae", 30.0 + i)
                for i in range(5)
            ],
        ),
    )
    async with httpx.AsyncClient() as client:
        out = await ensembl_plants.paralogs(client, "AT1G19850", limit=2)
        with pytest.raises(ValueError, match="limit"):
            await ensembl_plants.paralogs(client, "AT1G19850", limit=0)

    assert (out["total"], out["returned"], out["truncated"]) == (5, 2, True)
    assert out["counts_by_type"] == {"other_paralog": 5}
    assert [p["locus"] for p in out["paralogs"]] == ["AT1G04000", "AT1G03000"]


TOMATO = "solanum_lycopersicum_gca000188115v5cm"


@pytest.mark.asyncio
async def test_tomato_ids_lose_the_wire_prefix(httpx_mock: HTTPXMock) -> None:
    """SL4.0 ids go to Ensembl as `gene-<id>` under the registry's tomato slug,
    and come back that way (live, 2026-09-25: target `gene-Solyc12g006340.3`,
    species `solanum_lycopersicum_gca000188115v5cm`); the answer names genes
    the way the locus tools accept them."""
    httpx_mock.add_response(
        url=_homology_url(TOMATO, "gene-Solyc04g081240.2"),
        json=_answer(
            "gene-Solyc04g081240.2",
            [
                _paralog(
                    "gene-Solyc12g006340.3",
                    "other_paralog",
                    "Viridiplantae",
                    41.0,
                    species=TOMATO,
                    taxon_id=4081,
                )
            ],
        ),
    )
    async with httpx.AsyncClient() as client:
        out = await ensembl_plants.paralogs(
            client, "Solyc04g081240.2", organism="solanum_lycopersicum"
        )
    assert out["locus"] == "Solyc04g081240.2"
    assert out["paralogs"][0]["locus"] == "Solyc12g006340.3"


@pytest.mark.asyncio
async def test_a_paralog_in_another_species_is_refused(httpx_mock: HTTPXMock) -> None:
    """A paralogue is same-species by Ensembl's definition; a row naming another
    species means the answer is not what this tool describes. The same-species
    row beside it is the positive control."""
    httpx_mock.add_response(
        url=_homology_url("arabidopsis_thaliana", "AT2G28350"),
        json=_answer(
            "AT2G28350",
            [
                _paralog("AT1G19850", "other_paralog", "Viridiplantae", 36.7),
                _paralog(
                    "Os04g0664400",
                    "other_paralog",
                    "Viridiplantae",
                    30.0,
                    species="oryza_sativa",
                    taxon_id=39947,
                ),
            ],
        ),
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlantGenomicsError, match="oryza_sativa"):
            await ensembl_plants.paralogs(client, "AT2G28350")


LIVE = pytest.mark.skipif(
    os.environ.get("PLANT_GENOMICS_MCP_LIVE") != "1",
    reason="set PLANT_GENOMICS_MCP_LIVE=1 to hit rest.ensembl.org",
)
GENES_TSV = Path(__file__).parent.parent / "examples" / "arf_family" / "genes.tsv"


def _arabidopsis_arf_members() -> set[str]:
    rows = [line.split("\t") for line in GENES_TSV.read_text().splitlines()[1:] if line]
    return {row[0] for row in rows if row[2] == "arabidopsis_thaliana"}


@LIVE
@pytest.mark.asyncio
async def test_live_arf_paralogs_cover_the_family_gramene_left_empty() -> None:
    """The gap row's gene: Gramene gave it no paralog; Compara's paralogues hold
    every other Arabidopsis member the dossier found, as other_paralog rows."""
    family = _arabidopsis_arf_members()
    assert len(family) == 23
    async with httpx.AsyncClient() as client:
        out = await ensembl_plants.paralogs(client, "AT1G19850", limit=1000)
    assert out["found"] is True
    assert not out["truncated"]
    got = {p["locus"] for p in out["paralogs"]}
    assert family - {"AT1G19850"} <= got, sorted(family - {"AT1G19850"} - got)
    assert out["counts_by_type"].get("other_paralog", 0) > 0


@LIVE
@pytest.mark.asyncio
async def test_live_second_family_paralogs_are_family_members() -> None:
    """The premise, in a second family: IAA17's paralogues all carry the
    Aux/IAA domain (IPR003311) by Swiss-Prot's InterPro cross-references."""
    async with httpx.AsyncClient(timeout=60) as client:
        out = await ensembl_plants.paralogs(client, "AT1G04250", limit=1000)
        loci = sorted(p["locus"] for p in out["paralogs"])
        assert len(loci) >= 20, loci
        query = " OR ".join(f"gene:{locus}" for locus in loci)
        resp = await client.get(
            "https://rest.uniprot.org/uniprotkb/search",
            params={
                "query": f"({query}) AND organism_id:3702 AND reviewed:true",
                "fields": "gene_oln,xref_interpro",
                "format": "tsv",
                "size": 500,
            },
        )
        resp.raise_for_status()
    with_domain = {
        locus
        for line in resp.text.splitlines()[1:]
        for locus in loci
        if locus.lower() in line.lower() and "IPR003311" in line
    }
    assert with_domain == set(loci), sorted(set(loci) - with_domain)


@LIVE
@pytest.mark.asyncio
async def test_live_empty_answers_are_told_apart() -> None:
    async with httpx.AsyncClient() as client:
        outside = await ensembl_plants.paralogs(client, "AT4G13495")
        with pytest.raises(NotFoundError):
            await ensembl_plants.paralogs(client, "AT1G99990")
    assert outside["found"] is False


@LIVE
@pytest.mark.asyncio
async def test_live_tomato_paralogs_are_named_without_the_wire_prefix() -> None:
    async with httpx.AsyncClient() as client:
        out = await ensembl_plants.paralogs(
            client, "Solyc04g081240.2", organism="solanum_lycopersicum"
        )
    loci = [p["locus"] for p in out["paralogs"]]
    assert loci, out
    assert all(locus.startswith("Solyc") for locus in loci), loci


@LIVE
@pytest.mark.asyncio
async def test_live_a_gene_compara_holds_with_no_paralogue_is_found_and_empty() -> None:
    """The description's own example: FLS2 (AT5G46330) is in Compara, with
    orthologues, and Compara records no paralogue for it (live, 2026-09-25:
    `{"data": [{"homologies": [], "id": "AT5G46330"}]}`). That is found=true
    with an empty list; the non-coding gene beside it is found=false, so the
    flag is seen to take both values against the real API."""
    async with httpx.AsyncClient() as client:
        fls2 = await ensembl_plants.paralogs(client, "AT5G46330")
        outside = await ensembl_plants.paralogs(client, "AT4G13495")
    assert fls2["found"] is True
    assert (fls2["total"], fls2["paralogs"], fls2["counts_by_type"]) == (0, [], {})
    assert outside["found"] is False
