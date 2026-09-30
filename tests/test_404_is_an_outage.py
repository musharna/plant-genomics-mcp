"""A 404 is the service failing unless its body is the upstream's own miss.

``_http.request_with_retry`` read every 404 as ``NotFoundError`` by default, so
23 calls whose identifier travels in the query or body (UniProt search,
QuickGO, BLAST, the Ensembl batch POST ...) told a caller their gene does not
exist whenever the route was missing: a retired URL, or Phytozome's BioMart
serving Apache's 404 page at every path (2026-09-29). Now a 404 is retried and
raised as ``UpstreamUnavailableError`` unless the call passes the miss body its
upstream serves, and each such pattern is checked here against that body and
against the same upstream's missing-route page, both verbatim from live 404s.
"""

from __future__ import annotations

import os
import re

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import (
    _http,
    alphafold,
    bar,
    batch,
    jaspar,
    kegg,
    pdbe,
    string_db,
    uniprot,
)
from plant_genomics_mcp.errors import NotFoundError, UpstreamUnavailableError

LIVE = os.environ.get("PLANT_GENOMICS_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set PLANT_GENOMICS_MCP_LIVE=1 to run")

# (upstream, its miss pattern, a live unknown-id URL, its 404 miss body, a live
# missing-route URL, its 404 page), all live 2026-09-29.
_OPT_INS = [
    (
        "alphafold",
        alphafold.NO_MODEL_404_RE,
        "https://alphafold.ebi.ac.uk/api/prediction/A0A999ZZZ9",
        "{}",
        "https://alphafold.ebi.ac.uk/api/nosuchroute/Q0WV96",
        '{"detail":"Not Found"}',
    ),
    (
        "pdbe",
        pdbe.NO_STRUCTURE_404_RE,
        "https://www.ebi.ac.uk/pdbe/api/mappings/best_structures/A0A999ZZZ9",
        '{"message":"Requested endpoint does not contain any data"}',
        "https://www.ebi.ac.uk/pdbe/api/nosuchroute/P00875",
        '{"detail":"Not Found"}',
    ),
    (
        "kegg",
        kegg.NO_RECORD_404_RE,
        "https://rest.kegg.jp/list/ath:AT9G99999",
        "",
        "https://rest.kegg.jp/nosuchop/ath:AT1G01060",
        '<!DOCTYPE HTML PUBLIC "-//IETF//DTD HTML 2.0//EN">\n<html><head>\n'
        "<title>404 Not Found</title>\n</head><body>\n<h1>Not Found</h1>",
    ),
    (
        "jaspar",
        jaspar.NO_MATRIX_404_RE,
        "https://jaspar.elixir.no/api/v1/matrix/MA9999.9/",
        '{"detail":"Not found."}',
        "https://jaspar.elixir.no/api/v1/nosuchroute/MA0001.1/",
        '<!DOCTYPE html>\n<html>\n<head>\n<meta charset="utf-8">',
    ),
    (
        "uniprot",
        uniprot.NO_ENTRY_404_RE,
        "https://rest.uniprot.org/uniprotkb/A0A999ZZZ9.json",
        '{"url":"http://rest.uniprot.org/uniprotkb/A0A999ZZZ9","messages":["Resource not found"]}',
        "https://rest.uniprot.org/nosuchroute/Q0WV96.json",
        "<html>\r\n<head><title>404 Not Found</title></head>\r\n<body>\r\n"
        "<center><h1>404 Not Found</h1></center>\r\n<hr><center>nginx/1.25.3</center>",
    ),
    (
        "string",
        string_db.NO_PROTEIN_404_RE,
        "https://string-db.org/api/json/interaction_partners?identifiers=NOSUCHPROT99&species=3702",
        '[{ "Error" : "not found", "ErrorMessage" : "<p>Sorry, STRING did not find a protein'
        " called 'NOSUCHPROT99' in the taxon '3702'.</p>\" }]",
        "https://string-db.org/api/json/nosuchroute?identifiers=AT1G01060&species=3702",
        "<!DOCTYPE html><html lang='en'>\n<head>",
    ),
    (
        "bar gaia",
        bar._NO_RECORD_400_RE,
        "https://bar.utoronto.ca/api/gaia/aliases/AT1G99999",
        '{"wasSuccessful": false, "error": "Nothing found"}',
        "https://bar.utoronto.ca/api/nosuchroute/AT1G01060",
        "<!doctype html>\n<html lang=en>\n<title>404 Not Found</title>\n<h1>Not Found</h1>",
    ),
]
_IDS = [row[0] for row in _OPT_INS]


@pytest.mark.parametrize(("name", "pattern", "_m", "miss", "_r", "route"), _OPT_INS, ids=_IDS)
def test_each_miss_pattern_reads_its_upstreams_miss_and_not_its_route_page(
    name: str, pattern: re.Pattern[str], _m: str, miss: str, _r: str, route: str
) -> None:
    assert pattern.search(miss), f"{name}: its own miss body is not read as a miss"
    assert not pattern.search(route), f"{name}: a missing route would read as a miss"


async def _no_sleep(_seconds: float) -> None:
    return None


_ROUTE_PAGE = (
    "<html>\r\n<head><title>404 Not Found</title></head>\r\n<body>\r\n"
    "<center><h1>404 Not Found</h1></center>\r\n<hr><center>nginx/1.25.3</center>"
)


@pytest.mark.asyncio
async def test_a_route_404_behind_a_query_keyed_call_is_an_outage_not_no_entry(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UniProt search carries the locus in the query and answers a miss with
    ``{"results": []}``; its 404 told seven tools the gene has no UniProt
    entry. The Ensembl batch POST carries the ids in the body and answers a
    miss with a null record; its 404 failed every locus as NotFoundError."""
    monkeypatch.setattr(_http.asyncio, "sleep", _no_sleep)
    search = re.compile(r"^https://rest\.uniprot\.org/uniprotkb/search\?")
    post = "https://rest.ensembl.org/lookup/id"
    httpx_mock.add_response(url=search, status_code=404, text=_ROUTE_PAGE, is_reusable=True)
    httpx_mock.add_response(
        url=post, method="POST", status_code=404, text=_ROUTE_PAGE, is_reusable=True
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamUnavailableError, match=r"exhausted 3 retries \(HTTP 404: "):
            await uniprot.lookup_locus(client, "AT1G01060")
        with pytest.raises(UpstreamUnavailableError, match=r"exhausted 3 retries \(HTTP 404: "):
            await batch.batch_ensembl_plants_lookup_locus(client, ["AT1G01060"])
    assert len(httpx_mock.get_requests(url=search)) == 3
    assert len(httpx_mock.get_requests(url=post, method="POST")) == 3


@pytest.mark.asyncio
async def test_the_same_calls_still_answer_when_the_upstream_does(httpx_mock: HTTPXMock) -> None:
    """Positive control for the test above: the answers those 404s stood in for."""
    httpx_mock.add_response(
        url=re.compile(r"^https://rest\.uniprot\.org/uniprotkb/search\?"),
        json={"results": [{"primaryAccession": "Q6R0H1", "entryType": "UniProtKB reviewed"}]},
    )
    httpx_mock.add_response(
        url="https://rest.ensembl.org/lookup/id",
        method="POST",
        json={
            "AT1G01060": {"id": "AT1G01060", "species": "arabidopsis_thaliana"},
            "AT9G99999": None,
        },
    )
    async with httpx.AsyncClient() as client:
        entry = await uniprot.lookup_locus(client, "AT1G01060")
        env = await batch.batch_ensembl_plants_lookup_locus(client, ["AT1G01060", "AT9G99999"])
    assert entry["primaryAccession"] == "Q6R0H1"
    assert set(env["results"]) == {"AT1G01060"}
    assert env["errors"]["AT9G99999"].startswith("[NotFoundError]")


@live_only
@pytest.mark.parametrize(
    ("name", "pattern", "miss_url", "_m", "route_url", "_r"), _OPT_INS, ids=_IDS
)
@pytest.mark.asyncio
async def test_live_each_upstream_still_serves_the_miss_its_pattern_reads(
    name: str, pattern: re.Pattern[str], miss_url: str, _m: str, route_url: str, _r: str
) -> None:
    """Real execution: an unknown id is still a miss, and a missing route is
    still an outage, on the service itself."""
    async with httpx.AsyncClient(headers={"Accept": "application/json"}) as client:
        with pytest.raises(NotFoundError):
            await _http.request_with_retry(
                client, "GET", miss_url, service=name, not_found_404_pattern=pattern
            )
        with pytest.raises(UpstreamUnavailableError, match=r"\(HTTP 404: "):
            await _http.request_with_retry(
                client,
                "GET",
                route_url,
                service=name,
                max_retries=1,
                not_found_404_pattern=pattern,
            )
