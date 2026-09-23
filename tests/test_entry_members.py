"""Issue #124: from an InterPro / Pfam / PANTHER entry to its members' loci.

No tool took a family or entry accession as input, so the ARF dossier had to
enumerate its family through 742 locus-level calls. ``entry_members`` asks
UniProt for the reviewed proteins cross-referenced to the entry in one
organism and reads each member's locus from the cross-reference the server's
own locus tools accept.

Mocked rows reproduce the live shapes (2026-09-22): Arabidopsis carries its
AGI as an Araport xref (Q84WU6 -> AT1G77850), rice and wheat as the GeneId
property of an EnsemblPlants xref (Q8S983 -> Os04g0664400,
A0A3B5XY33 -> TraesCS1A02G156600), the total arrives in ``x-total-results``
and the next page as a cursor in the ``Link`` header.
"""

from __future__ import annotations

import os
import re
from typing import Any

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import uniprot
from plant_genomics_mcp.errors import InvalidArguments, UpstreamUnavailableError

LIVE = os.environ.get("PLANT_GENOMICS_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set PLANT_GENOMICS_MCP_LIVE=1 to run")

SEARCH = re.compile(r"^https://rest\.uniprot\.org/uniprotkb/search\?.*")
NEXT = (
    '<https://rest.uniprot.org/uniprotkb/search?query=x&cursor=82giuzutyxte42km&size=2>; rel="next"'
)


def _hit(accession: str, symbol: str, xrefs: list[dict[str, Any]], oln: list[str] | None = None):
    return {
        "primaryAccession": accession,
        "entryType": "UniProtKB reviewed (Swiss-Prot)",
        "proteinDescription": {"recommendedName": {"fullName": {"value": "Auxin response factor"}}},
        "genes": [
            {
                "geneName": {"value": symbol},
                "orderedLocusNames": [{"value": v} for v in (oln or [])],
            }
        ],
        "uniProtKBCrossReferences": xrefs,
    }


ATH = _hit(
    "Q84WU6",
    "ARF17",
    [
        {"database": "Araport", "id": "AT1G77850", "properties": []},
        {"database": "TAIR", "id": "AT1G77850", "properties": []},
    ],
    ["At1g77850"],
)
RICE = _hit(
    "Q8S983",
    "ARF11",
    [
        {
            "database": "EnsemblPlants",
            "id": "Os04t0664400-02",
            "properties": [
                {"key": "ProteinId", "value": "Os04t0664400-02"},
                {"key": "GeneId", "value": "Os04g0664400"},
            ],
        }
    ],
    ["Os04g0664400", "LOC_Os04g56850"],
)
OLN_ONLY = _hit("P00001", "X1", [], ["At5g00001"])


@pytest.mark.asyncio
async def test_members_carry_the_locus_the_other_tools_accept(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=SEARCH,
        json={"results": [ATH, RICE, OLN_ONLY]},
        headers={"x-total-results": "3"},
    )
    async with httpx.AsyncClient() as client:
        r = await uniprot.entry_members(client, "IPR010525", "arabidopsis_thaliana")

    rows = {m["accession"]: m for m in r["members"]}
    assert (rows["Q84WU6"]["locus"], rows["Q84WU6"]["locus_source"]) == ("AT1G77850", "Araport")
    assert (rows["Q8S983"]["locus"], rows["Q8S983"]["locus_source"]) == (
        "Os04g0664400",
        "EnsemblPlants",
    )
    # Last resort, and it says so: UniProt's ordered-locus name, recased as an AGI.
    assert (rows["P00001"]["locus"], rows["P00001"]["locus_source"]) == (
        "AT5G00001",
        "ordered_locus_name",
    )
    assert rows["Q84WU6"]["symbol"] == "ARF17" and rows["Q84WU6"]["reviewed"] is True
    assert (r["total"], r["returned"], r["next_cursor"], r["truncated"]) == (3, 3, None, False)

    query = httpx_mock.get_requests()[0].url.params["query"]
    assert query == "xref:interpro-IPR010525 AND organism_id:3702 AND reviewed:true"


@pytest.mark.asyncio
async def test_ensembl_gene_id_outranks_araport_when_they_disagree(httpx_mock: HTTPXMock) -> None:
    """The locus must be the id ensembl_plants_lookup_locus takes, which is by
    construction the EnsemblPlants GeneId; Araport is the fallback when UniProt
    carries no Ensembl xref (as for Q84WU6 above)."""
    both = _hit(
        "P00002",
        "X2",
        [
            {"database": "Araport", "id": "AT1G00010", "properties": []},
            {
                "database": "EnsemblPlants",
                "id": "AT1G00020.1",
                "properties": [{"key": "GeneId", "value": "AT1G00020"}],
            },
        ],
    )
    httpx_mock.add_response(
        url=SEARCH, json={"results": [both, ATH]}, headers={"x-total-results": "2"}
    )
    async with httpx.AsyncClient() as client:
        r = await uniprot.entry_members(client, "IPR010525", "arabidopsis_thaliana")

    rows = {m["accession"]: (m["locus"], m["locus_source"]) for m in r["members"]}
    assert rows == {"P00002": ("AT1G00020", "EnsemblPlants"), "Q84WU6": ("AT1G77850", "Araport")}


@pytest.mark.asyncio
async def test_a_page_that_cannot_hold_the_set_hands_back_a_cursor(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=SEARCH, json={"results": [ATH, RICE]}, headers={"x-total-results": "3", "link": NEXT}
    )
    httpx_mock.add_response(
        url=SEARCH, json={"results": [OLN_ONLY]}, headers={"x-total-results": "3"}
    )
    async with httpx.AsyncClient() as client:
        first = await uniprot.entry_members(
            client, "PTHR31384", "arabidopsis_thaliana", reviewed_only=False, page_size=2
        )
        second = await uniprot.entry_members(
            client,
            "PTHR31384",
            "arabidopsis_thaliana",
            reviewed_only=False,
            page_size=2,
            cursor=first["next_cursor"],
        )
    assert (first["returned"], first["truncated"]) == (2, True)
    assert first["next_cursor"] is not None
    assert (second["returned"], second["truncated"], second["next_cursor"]) == (1, False, None)
    first_req, second_req = httpx_mock.get_requests()
    assert first_req.url.params["query"] == "xref:panther-PTHR31384 AND organism_id:3702"
    assert "cursor" not in first_req.url.params
    # UniProt's own cursor is what goes back on the wire, inside ours.
    assert second_req.url.params["cursor"] == "82giuzutyxte42km"
    assert second_req.url.host == "rest.uniprot.org"  # the cursor is a parameter, never a URL


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "other",
    [
        {"entry": "IPR010525"},  # another family
        {"organism": "oryza_sativa"},  # another organism
        {"reviewed_only": True},  # another filter
        {"page_size": 3},  # another page size
    ],
    ids=["entry", "organism", "reviewed_only", "page_size"],
)
async def test_a_cursor_continues_only_the_query_it_came_from(
    httpx_mock: HTTPXMock, other: dict[str, Any]
) -> None:
    """Audit 2026-09-22 M5: entry_members handed back UniProt's raw cursor, and
    UniProt's cursor does not carry the query, so passing it with another
    entry or organism silently paged a DIFFERENT list from an offset into it.
    The six other cursor tools (#123) bind the cursor to tool + query; this
    one now does too."""
    httpx_mock.add_response(
        url=SEARCH, json={"results": [ATH, RICE]}, headers={"x-total-results": "3", "link": NEXT}
    )
    httpx_mock.add_response(
        url=SEARCH, json={"results": [OLN_ONLY]}, headers={"x-total-results": "3"}
    )
    base: dict[str, Any] = {
        "entry": "PTHR31384",
        "organism": "arabidopsis_thaliana",
        "reviewed_only": False,
        "page_size": 2,
    }
    async with httpx.AsyncClient() as client:
        first = await uniprot.entry_members(client, **base)
        with pytest.raises(InvalidArguments, match="entry_members: cursor continues"):
            await uniprot.entry_members(client, **{**base, **other}, cursor=first["next_cursor"])
        # Positive control: the query it came from continues.
        second = await uniprot.entry_members(client, **base, cursor=first["next_cursor"])
    assert second["returned"] == 1
    assert len(httpx_mock.get_requests()) == 2  # the refused call never reached UniProt


@pytest.mark.asyncio
async def test_an_entry_with_no_members_here_is_an_answer_of_zero(httpx_mock: HTTPXMock) -> None:
    """The null arm: no members in this organism is a finding, not an error."""
    httpx_mock.add_response(url=SEARCH, json={"results": []}, headers={"x-total-results": "0"})
    async with httpx.AsyncClient() as client:
        r = await uniprot.entry_members(client, "PF06507", "triticum_aestivum")
    assert (r["total"], r["returned"], r["members"], r["next_cursor"]) == (0, 0, [], None)


@pytest.mark.asyncio
async def test_an_answer_without_its_total_is_an_upstream_fault(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=SEARCH, json={"results": [ATH]})
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamUnavailableError, match="x-total-results"):
            await uniprot.entry_members(client, "IPR010525", "arabidopsis_thaliana")


@pytest.mark.asyncio
async def test_only_entry_accessions_it_can_query_are_accepted(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=SEARCH, json={"results": []}, headers={"x-total-results": "0"})
    async with httpx.AsyncClient() as client:
        # Positive control: a well-formed accession reaches UniProt.
        await uniprot.entry_members(client, "IPR010525", "arabidopsis_thaliana")
        for bad in ("ipr010525", "IPR01052", "PTHR31384:SF10", "IPR010525 OR x", "GO:0005634"):
            with pytest.raises(InvalidArguments):
                await uniprot.entry_members(client, bad, "arabidopsis_thaliana")
    assert len(httpx_mock.get_requests()) == 1


# ---------- live: the premise the tool was built on ----------


@live_only
@pytest.mark.asyncio
async def test_live_the_arf_domain_returns_the_dossiers_family() -> None:
    """IPR010525 in Arabidopsis is the dossier's 23 members, each locus one
    ensembl_plants_lookup_locus accepts (checked 2026-09-22: sets equal)."""
    from pathlib import Path

    from plant_genomics_mcp import ensembl_plants

    genes = Path(__file__).resolve().parents[1] / "examples" / "arf_family" / "genes.tsv"
    dossier = {
        line.split("\t")[0]
        for line in genes.read_text().splitlines()[1:]
        if line.split("\t")[2] == "arabidopsis_thaliana"
    }
    async with httpx.AsyncClient() as client:
        ath = await uniprot.entry_members(client, "IPR010525", "arabidopsis_thaliana")
        rice = await uniprot.entry_members(client, "IPR010525", "oryza_sativa")
        wheat_rows: list[dict[str, Any]] = []
        cursor = None
        for _ in range(10):
            page = await uniprot.entry_members(
                client,
                "IPR010525",
                "triticum_aestivum",
                reviewed_only=False,
                page_size=50,
                cursor=cursor,
            )
            wheat_rows += page["members"]
            cursor = page["next_cursor"]
            if cursor is None:
                break
        # Rice (live 2026-09-22): 18 of 24 carry an EnsemblPlants GeneId, 6 only an
        # ordered-locus name. Both routes must yield the RAP id Ensembl accepts.
        by_source = {m["locus_source"]: m["locus"] for m in rice["members"]}
        checked = {
            src: (await ensembl_plants.lookup_locus(client, locus, organism="oryza_sativa"))["id"]
            for src, locus in by_source.items()
        }

    assert ath["total"] == len(ath["members"]) == 23 and ath["next_cursor"] is None
    assert {m["locus"] for m in ath["members"]} == dossier
    assert rice["total"] >= 20
    assert set(by_source) == {"EnsemblPlants", "ordered_locus_name"}
    assert all(re.fullmatch(r"Os\d{2}g\d{7}", str(m["locus"])) for m in rice["members"])
    assert checked == by_source
    # Paged to the end, every wheat member arrives exactly once, with its IWGSC locus.
    assert len(wheat_rows) == page["total"] == len({m["accession"] for m in wheat_rows}) > 50
    # 127 of 132 map to an IWGSC gene; 5 are cDNA submissions (e.g. Q6U8C6) with no
    # genome cross-reference and say so with a null locus (live 2026-09-22).
    mapped = [m for m in wheat_rows if m["locus"] is not None]
    assert all(str(m["locus"]).startswith("TraesCS") for m in mapped) and len(mapped) > 100
    assert all(m["locus_source"] is None for m in wheat_rows if m["locus"] is None)
