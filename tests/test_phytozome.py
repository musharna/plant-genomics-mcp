"""Tests for the Phytozome BioMart client.

Two tiers (mirrors the ensembl_plants sibling pattern):
  1. Unit tests with mocked HTTP via pytest-httpx (always run).
  2. Live integration test gated by PLANT_GENOMICS_MCP_LIVE=1, hitting
     the real phytozome-next.jgi.doe.gov. Satisfies the real-execution-
     check doctrine — BioMart's TSV-with-200-on-error wire format drifts
     quietly and only a real call catches it.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import _http, phytozome
from plant_genomics_mcp.errors import NotFoundError, UpstreamUnavailableError

LIVE = os.environ.get("PLANT_GENOMICS_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set PLANT_GENOMICS_MCP_LIVE=1 to run")

# Controller-verified verbatim Phytozome response for AT1G01010 (2026-05-21).
_AT1G01010_TSV = (
    "Organism Name\tGene Name\tChromosome Name\tGene Start (bp)\t"
    "Gene End (bp)\tStrand\tDescription\n"
    "Athaliana_TAIR10\tAT1G01010\tChr1\t3631\t5899\t1\t"
    "(1 of 1) PTHR31989:SF215 - NAC DOMAIN-CONTAINING PROTEIN 1\n"
)

_BIOMART_URL = "https://phytozome-next.jgi.doe.gov/biomart/martservice"


# ---------- mocked unit tests ----------


@pytest.mark.asyncio
async def test_lookup_locus_at1g01010_returns_nac001(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=_BIOMART_URL,
        method="POST",
        text=_AT1G01010_TSV,
    )
    async with httpx.AsyncClient() as client:
        result = await phytozome.lookup_locus(client, "AT1G01010")
    assert result["organism_name"] == "Athaliana_TAIR10"
    assert result["gene_name"] == "AT1G01010"
    assert result["chromosome"] == "Chr1"
    # Numeric fields preserved as strings (BioMart TSV is untyped — see module docstring).
    assert result["gene_start"] == "3631"
    assert result["gene_end"] == "5899"
    assert result["strand"] == "1"
    assert "NAC" in result["description"]


@pytest.mark.asyncio
async def test_lookup_locus_default_organism_is_arabidopsis(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=_BIOMART_URL, method="POST", text=_AT1G01010_TSV)
    async with httpx.AsyncClient() as client:
        await phytozome.lookup_locus(client, "AT1G01010")
    # Inspect the request body to confirm organism_id=167 was sent.
    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    body = requests[0].content.decode()
    # Form-encoded: query=<urlencoded XML>. Decoding via httpx's helper is
    # heavier than a substring check; the XML escapes are deterministic.
    assert "organism_id" in body
    assert "value%3D%22167%22" in body or 'value="167"' in body


@pytest.mark.asyncio
async def test_lookup_locus_retries_on_429_then_succeeds(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=_BIOMART_URL,
        method="POST",
        status_code=429,
        headers={"Retry-After": "0"},
    )
    httpx_mock.add_response(url=_BIOMART_URL, method="POST", text=_AT1G01010_TSV)
    async with httpx.AsyncClient() as client:
        result = await phytozome.lookup_locus(client, "AT1G01010")
    assert result["gene_name"] == "AT1G01010"


@pytest.mark.asyncio
async def test_lookup_locus_raises_on_biomart_query_error(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=_BIOMART_URL,
        method="POST",
        text="Query ERROR: caught BioMart::Exception::Usage: Filter organism_id NOT FOUND",
    )
    httpx_mock.add_response(url=_BIOMART_URL, method="POST", text=_AT1G01010_TSV)
    async with httpx.AsyncClient() as client:
        with pytest.raises(phytozome.PlantGenomicsError, match="Query ERROR"):
            await phytozome.lookup_locus(client, "AT1G01010")
        # Positive control, same query: the error body was not stored (#96),
        # so BioMart is asked again and its answer is read.
        result = await phytozome.lookup_locus(client, "AT1G01010")
    assert result["gene_name"] == "AT1G01010"
    assert len(httpx_mock.get_requests()) == 2


@pytest.mark.asyncio
async def test_lookup_locus_raises_on_empty_results(httpx_mock: HTTPXMock) -> None:
    header_only = (
        "Organism Name\tGene Name\tChromosome Name\tGene Start (bp)\t"
        "Gene End (bp)\tStrand\tDescription\n"
    )
    httpx_mock.add_response(url=_BIOMART_URL, method="POST", text=header_only)
    async with httpx.AsyncClient() as client:
        with pytest.raises(phytozome.PlantGenomicsError, match="not found"):
            await phytozome.lookup_locus(client, "AT9G99999")


# The page the BioMart endpoint answered with on 2026-09-29, verbatim: Apache's
# own 404 for a path it no longer serves, while the site root answered 200.
_APACHE_404 = (
    '<!DOCTYPE HTML PUBLIC "-//IETF//DTD HTML 2.0//EN">\n'
    "<html><head>\n<title>404 Not Found</title>\n</head><body>\n"
    "<h1>Not Found</h1>\n<p>The requested URL was not found on this server.</p>\n"
    "</body></html>\n"
)


async def _no_sleep(_seconds: float) -> None:
    pass


@pytest.mark.asyncio
async def test_a_404_is_the_service_missing_not_the_locus(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BioMart answers a locus it lacks with 200 and the header line alone; a
    404 was read as NotFoundError, telling a caller the gene does not exist."""
    monkeypatch.setattr(_http.asyncio, "sleep", _no_sleep)
    header_only = _AT1G01010_TSV.splitlines(keepends=True)[0]
    for _ in range(3):  # retried: three attempts, not MAX_RETRIES (a no-retry 1 would pass)
        httpx_mock.add_response(url=_BIOMART_URL, method="POST", status_code=404, text=_APACHE_404)
    httpx_mock.add_response(url=_BIOMART_URL, method="POST", text=header_only)
    httpx_mock.add_response(url=_BIOMART_URL, method="POST", text=_AT1G01010_TSV)
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamUnavailableError, match=r"HTTP 404: .*404 Not Found"):
            await phytozome.lookup_locus(client, "AT1G01010")
        # Positive controls, same cache: a real miss is still NotFoundError, and
        # once BioMart answers, so does the call.
        with pytest.raises(NotFoundError, match="AT9G99999 not found"):
            await phytozome.lookup_locus(client, "AT9G99999")
        result = await phytozome.lookup_locus(client, "AT1G01010")
    assert result["gene_name"] == "AT1G01010"
    assert len(httpx_mock.get_requests()) == 3 + 2


@pytest.mark.asyncio
async def test_lookup_locus_rejects_xml_injection() -> None:
    # Must fail BEFORE any HTTP call — no httpx_mock interactions allowed.
    async with httpx.AsyncClient() as client:
        with pytest.raises(phytozome.PlantGenomicsError, match="invalid locus"):
            await phytozome.lookup_locus(client, "AT1G01010<x>")


@pytest.mark.asyncio
async def test_lookup_accepts_organism_alias(httpx_mock: HTTPXMock) -> None:
    """Resolver-driven organism kwarg accepts an alias (e.g. 'arabidopsis')."""
    httpx_mock.add_response(url=_BIOMART_URL, method="POST", text=_AT1G01010_TSV)
    async with httpx.AsyncClient() as client:
        result = await phytozome.lookup_locus(client, "AT1G01010", organism="arabidopsis")
    assert result["gene_name"] == "AT1G01010"


@pytest.mark.asyncio
async def test_lookup_unsupported_organism_raises_not_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolving an organism without a phytozome_int raises OrganismNotSupported.

    Wave A2 (2026-05-23) populated every record's phytozome_int. To still
    exercise the unsupported branch (the contract guarantees None slots
    raise rather than silently returning), shadow the registry with a
    None for one organism.
    """
    from dataclasses import replace

    from plant_genomics_mcp import organisms
    from plant_genomics_mcp.errors import OrganismNotSupported

    record = organisms.ORGANISMS["vitis_vinifera"]
    shadowed = dict(organisms.ORGANISMS)
    shadowed["vitis_vinifera"] = replace(record, phytozome_int=None)
    monkeypatch.setattr(organisms, "ORGANISMS", shadowed)

    async with httpx.AsyncClient() as client:
        with pytest.raises(OrganismNotSupported) as excinfo:
            await phytozome.lookup_locus(client, "irrelevant", organism="vitis_vinifera")
    assert excinfo.value.backend == "phytozome"


# ---------- live integration (real-execution check) ----------

# Every BioMart 404 is raised as an outage, and so is a 404 from a wrong
# BASE_URL: from the tool's error alone the nightly would class our own broken
# URL as upstream-side. So on an outage the live tests ask BioMart directly, at
# _BIOMART_URL and with this query, both written here rather than imported (an
# imported constant would share the regression it is meant to rule out). The
# query is the tool's, for AT1G01010 in Arabidopsis (organism 167).
_PROBE_QUERY = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE Query>
<Query virtualSchemaName="zome_mart" header="1" uniqueRows="0" count="" datasetConfigVersion="0.7">
  <Dataset name="phytozome" interface="default">
    <Filter name="organism_id" value="167"/>
    <Filter name="gene_name_filter" value="AT1G01010"/>
    <Attribute name="organism_name"/>
    <Attribute name="gene_name1"/>
    <Attribute name="chr_name1"/>
    <Attribute name="gene_chrom_start"/>
    <Attribute name="gene_chrom_end"/>
    <Attribute name="gene_chrom_strand"/>
    <Attribute name="gene_description"/>
  </Dataset>
</Query>"""


def _biomart_down(timeout_s: float = 30.0) -> str | None:
    """None when BioMart answers the probe with its header line and a row; why
    it is down on a 404, a 5xx or no answer. Anything else fails: the probe is
    wrong."""
    try:
        resp = httpx.post(_BIOMART_URL, data={"query": _PROBE_QUERY}, timeout=timeout_s)
    except httpx.TransportError as e:
        return f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
    if resp.status_code == 404 or resp.status_code >= 500:
        return f"HTTP {resp.status_code}"
    lines = resp.text.splitlines()
    if resp.status_code == 200 and len(lines) >= 2 and lines[0] == _AT1G01010_TSV.splitlines()[0]:
        return None
    raise AssertionError(f"BioMart probe answered HTTP {resp.status_code} {resp.text[:120]!r}")


async def _lookup_or_skip_outage(
    client: httpx.AsyncClient,
    locus: str,
    organism: str = "arabidopsis_thaliana",
    probe: Callable[[], str | None] = _biomart_down,
) -> dict[str, Any]:
    """The tool's answer; a skip when it reports BioMart down and BioMart is
    down when asked directly; a failure, classed a regression, when BioMart
    answers directly."""
    try:
        return await phytozome.lookup_locus(client, locus, organism=organism)
    except UpstreamUnavailableError as exc:
        why = probe()
        if why is None:
            # No upstream tag in this message: the nightly classes by it.
            raise AssertionError(
                f"the tool reports Phytozome BioMart down for {locus}, but BioMart "
                f"answers at {_BIOMART_URL} directly: the tool's own call is broken"
            ) from exc
        pytest.skip(f"Phytozome BioMart down when probed directly ({why})")


# A page some proxy or maintenance mode could serve on a 200 in BioMart's place.
_MAINTENANCE_PAGE = "<html>\n<body>Phytozome is down for maintenance</body>\n</html>\n"


def test_the_probe_reads_biomart_down_only_on_404_5xx_or_no_answer(
    httpx_mock: HTTPXMock,
) -> None:
    httpx_mock.add_response(url=_BIOMART_URL, method="POST", status_code=404, text=_APACHE_404)
    httpx_mock.add_response(url=_BIOMART_URL, method="POST", status_code=503)
    httpx_mock.add_exception(httpx.ConnectTimeout("timed out"), url=_BIOMART_URL)
    httpx_mock.add_response(url=_BIOMART_URL, method="POST", text="Query ERROR: bad filter")
    httpx_mock.add_response(url=_BIOMART_URL, method="POST", text=_MAINTENANCE_PAGE)
    httpx_mock.add_response(url=_BIOMART_URL, method="POST", text=_AT1G01010_TSV)
    assert _biomart_down() == "HTTP 404"
    assert _biomart_down() == "HTTP 503"
    assert _biomart_down() == "ConnectTimeout: timed out"
    with pytest.raises(AssertionError, match="BioMart probe answered HTTP 200 'Query ERROR"):
        _biomart_down()
    # A 200 of two lines or more that is not BioMart's table is not an answer either.
    with pytest.raises(AssertionError, match="BioMart probe answered HTTP 200 '<html>"):
        _biomart_down()
    # Positive control: the answer the tool reads is BioMart up.
    assert _biomart_down() is None
    assert b"AT1G01010" in httpx_mock.get_requests()[-1].content


@pytest.mark.asyncio
async def test_a_wrong_url_is_a_regression_not_an_outage(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wrong BASE_URL answers 404 too, raised as an outage; with BioMart
    answering directly the live test must fail, and be classed a regression."""
    monkeypatch.setattr(_http.asyncio, "sleep", _no_sleep)
    moved = "https://phytozome-next.jgi.doe.gov/biomart/martservice-moved"
    monkeypatch.setattr(phytozome, "BASE_URL", moved)
    for _ in range(6):
        httpx_mock.add_response(url=moved, method="POST", status_code=404, text=_APACHE_404)
    async with httpx.AsyncClient() as client:
        try:
            with pytest.raises(AssertionError, match="the tool's own call is broken") as broken:
                await _lookup_or_skip_outage(client, "AT1G01010", probe=lambda: None)
        except pytest.skip.Exception as skipped:
            pytest.fail(f"a wrong URL was skipped as an outage: {skipped}")
        # Positive control, same 404s: with BioMart down directly too, a skip.
        with pytest.raises(pytest.skip.Exception, match=r"down when probed directly \(HTTP 404\)"):
            await _lookup_or_skip_outage(client, "AT1G01010", probe=lambda: "HTTP 404")
        # And an answer is passed through.
        monkeypatch.setattr(phytozome, "BASE_URL", _BIOMART_URL)
        httpx_mock.add_response(url=_BIOMART_URL, method="POST", text=_AT1G01010_TSV)
        row = await _lookup_or_skip_outage(client, "AT1G01010", probe=lambda: "unused")
    assert row["gene_name"] == "AT1G01010"
    # The nightly's classifier reads the failure message pytest reports.
    assert _classify(f"AssertionError: {broken.value}", monkeypatch) == "regression"


def _classify(message: str, monkeypatch: pytest.MonkeyPatch) -> str:
    spec = importlib.util.spec_from_file_location(
        "classify_live_failures",
        Path(__file__).resolve().parents[1] / "scripts" / "classify_live_failures.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)  # its dataclasses look it up
    spec.loader.exec_module(module)
    result: str = module.classify(message)
    return result


@live_only
@pytest.mark.asyncio
async def test_live_lookup_at1g01010_phytozome() -> None:
    """Real call to phytozome-next.jgi.doe.gov — verifies wire format hasn't drifted."""
    async with httpx.AsyncClient() as client:
        result = await _lookup_or_skip_outage(client, "AT1G01010")
    assert result["gene_name"] == "AT1G01010"
    assert "NAC" in result["description"]


# Canonical first-gene probes for organisms that have a Phytozome ID.
# Trimmed from the legacy 10-entry KNOWN_ORGANISMS table (2026-05-21
# P2.19 probes) to the 5 organisms also present in
# organisms.ORGANISMS. Drives the live-only regression below.
_PHYTOZOME_PROBES: dict[str, str] = {
    "arabidopsis_thaliana": "AT1G01010",
    "glycine_max": "Glyma.01G000100",
    "sorghum_bicolor": "Sobic.001G000200",
    "brachypodium_distachyon": "Bradi1g00200",
    "populus_trichocarpa": "Potri.001G000100",
    "oryza_sativa": "LOC_Os01g01010",
    "zea_mays": "Zm00001eb000010",
    "triticum_aestivum": "TraesCS5A03G0137600",
    "solanum_lycopersicum": "Solyc01g005000",
    "hordeum_vulgare": "HORVU.MOREX.r3.UnG0790040",
    "vitis_vinifera": "VIT_201s0011g00010",
    "medicago_truncatula": "Medtr1g004990",
}


def test_phytozome_probes_match_organisms_with_phytozome_int() -> None:
    """Cheap consistency check between the probe table and organisms.ORGANISMS.

    Runs without network: guards against silently dropping an organism's
    phytozome_int (or vice versa) without updating the probe table.
    """
    from plant_genomics_mcp import organisms

    supported = {
        canon for canon, rec in organisms.ORGANISMS.items() if rec.phytozome_int is not None
    }
    assert set(_PHYTOZOME_PROBES) == supported


@live_only
@pytest.mark.asyncio
async def test_live_phytozome_probes_all_resolve() -> None:
    """Every organism with a phytozome_int must resolve its canonical probe.

    Real-execution check guards against ID drift in BioMart (Phytozome
    occasionally renumbers proteome IDs across releases). If this test
    starts failing, re-probe via scripts/verify_organisms.py.
    """
    from plant_genomics_mcp import organisms

    async with httpx.AsyncClient() as client:
        for canon, probe in _PHYTOZOME_PROBES.items():
            row = await _lookup_or_skip_outage(client, probe, organism=canon)
            assert row["gene_name"] == probe, (
                f"{canon} (phyto_int={organisms.ORGANISMS[canon].phytozome_int}) "
                f"probe {probe} returned {row['gene_name']!r}"
            )
