"""Tests for the InterPro protein-domain backend.

Two tiers (mirrors the alphafold / quickgo pattern):
  1. Unit tests with mocked HTTP via pytest-httpx. ``lookup_by_uniprot`` is
     tested against a mocked paginated ``/entry/all/protein/uniprot`` endpoint
     (incl. a ``next`` page follow + cap); ``lookup_locus`` is tested with
     ``uniprot.lookup_locus`` monkeypatched.
  2. Live integration tests gated by PLANT_GENOMICS_MCP_LIVE=1.
"""

from __future__ import annotations

import os

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import interpro, uniprot
from plant_genomics_mcp.errors import NotFoundError, PlantGenomicsError

LIVE = os.environ.get("PLANT_GENOMICS_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set PLANT_GENOMICS_MCP_LIVE=1 to run")

_URL = f"{interpro.BASE_URL}/interpro/api/entry/all/protein/uniprot/Q9SZ92/"


def _row(
    accession: str, name: str, db: str, typ: str, integrated: str | None, start: int, end: int
):
    return {
        "metadata": {
            "accession": accession,
            "name": name,
            "source_database": db,
            "type": typ,
            "integrated": integrated,
            "go_terms": None,
        },
        "proteins": [{"entry_protein_locations": [{"fragments": [{"start": start, "end": end}]}]}],
    }


_PAGE = {
    "count": 2,
    "next": None,
    "previous": None,
    "results": [
        _row("PF01633", "Choline kinase", "pfam", "domain", "IPR002575", 40, 300),
        _row("cd05157", "Ethanolamine kinase", "cdd", "domain", None, 38, 336),
    ],
}


def _fake_uniprot(acc: str | None):
    async def _lookup(client, locus, organism="arabidopsis"):  # noqa: ANN001, ARG001
        if acc is None:
            raise NotFoundError(f"no UniProt entry for {locus!r}")
        return {"primaryAccession": acc, "uniProtkbId": "CK3_ARATH"}

    return _lookup


# ---------- mocked unit tests: lookup_by_uniprot ----------


@pytest.mark.asyncio
async def test_lookup_by_uniprot_full(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=_URL, json=_PAGE)
    async with httpx.AsyncClient() as client:
        r = await interpro.lookup_by_uniprot(client, "Q9SZ92")
    assert r["found"] is True
    assert r["accession"] == "Q9SZ92"
    assert r["domain_count"] == 2
    assert len(r["domains"]) == 2
    first = r["domains"][0]
    assert first["accession"] == "PF01633"
    assert first["name"] == "Choline kinase"
    assert first["source_database"] == "pfam"
    assert first["type"] == "domain"
    assert first["interpro"] == "IPR002575"
    assert first["locations"] == [{"start": 40, "end": 300}]
    assert r["count_by_type"] == {"domain": 2}
    assert r["truncated"] is False  # count == returned


@pytest.mark.asyncio
async def test_lookup_by_uniprot_empty_is_found_true(httpx_mock: HTTPXMock) -> None:
    """A protein with no annotated domains → found=True, empty list (not error)."""
    httpx_mock.add_response(
        url=_URL, json={"count": 0, "next": None, "previous": None, "results": []}
    )
    async with httpx.AsyncClient() as client:
        r = await interpro.lookup_by_uniprot(client, "Q9SZ92")
    assert r["found"] is True
    assert r["domain_count"] == 0
    assert r["domains"] == []
    assert r["count_by_type"] == {}


@pytest.mark.asyncio
async def test_lookup_by_uniprot_follows_next_page(httpx_mock: HTTPXMock) -> None:
    """Pagination: a `next` URL is followed and results are concatenated."""
    page2_url = f"{interpro.BASE_URL}/interpro/api/entry/all/protein/uniprot/Q9SZ92/?page=2"
    page1 = {
        "count": 3,
        "next": page2_url,
        "previous": None,
        "results": [_row("PF01633", "Choline kinase", "pfam", "domain", "IPR002575", 40, 300)],
    }
    page2 = {
        "count": 3,
        "next": None,
        "previous": None,
        "results": [
            _row("G3DSA:3.30", "Kinase-like", "cathgene3d", "homologous_superfamily", None, 1, 340),
            _row("PTHR100", "Choline/ethanolamine kinase", "panther", "family", None, 1, 346),
        ],
    }
    httpx_mock.add_response(url=_URL, json=page1)
    httpx_mock.add_response(url=page2_url, json=page2)
    async with httpx.AsyncClient() as client:
        r = await interpro.lookup_by_uniprot(client, "Q9SZ92")
    assert len(r["domains"]) == 3
    assert r["count_by_type"] == {"domain": 1, "homologous_superfamily": 1, "family": 1}
    assert r["truncated"] is False


@pytest.mark.asyncio
async def test_lookup_by_uniprot_page_cap_truncates(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MAX_PAGES bounds the request count; domain_count keeps the true total and
    truncated flags the cap (audit L1/T2)."""
    monkeypatch.setattr(interpro, "MAX_PAGES", 1)
    page1 = {
        "count": 5,
        "next": f"{interpro.BASE_URL}/interpro/api/entry/all/protein/uniprot/Q9SZ92/?page=2",
        "previous": None,
        "results": [_row("PF01633", "Choline kinase", "pfam", "domain", "IPR002575", 40, 300)],
    }
    # Only page 1 is mocked — if the cap were ignored, the page-2 fetch would
    # fail the test with an unmocked request.
    httpx_mock.add_response(url=_URL, json=page1)
    async with httpx.AsyncClient() as client:
        r = await interpro.lookup_by_uniprot(client, "Q9SZ92")
    assert r["domain_count"] == 5
    assert len(r["domains"]) == 1
    assert r["truncated"] is True


@pytest.mark.asyncio
async def test_lookup_by_uniprot_malformed_raises(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=_URL, json=["not", "a", "dict"])
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlantGenomicsError, match="unexpected payload"):
            await interpro.lookup_by_uniprot(client, "Q9SZ92")


# ---------- mocked unit tests: lookup_locus (uniprot monkeypatched) ----------


@pytest.mark.asyncio
async def test_lookup_locus_wraps_with_locus(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(uniprot, "lookup_locus", _fake_uniprot("Q9SZ92"))
    httpx_mock.add_response(url=_URL, json=_PAGE)
    async with httpx.AsyncClient() as client:
        r = await interpro.lookup_locus(client, "AT4G09760", "arabidopsis")
    assert r["locus"] == "AT4G09760"
    assert r["accession"] == "Q9SZ92"
    assert r["domain_count"] == 2


@pytest.mark.asyncio
async def test_lookup_locus_unresolvable_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(uniprot, "lookup_locus", _fake_uniprot(None))
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError):
            await interpro.lookup_locus(client, "NOSUCHLOCUS", "arabidopsis")


# ---------- live integration (real-execution check) ----------


@live_only
@pytest.mark.asyncio
async def test_live_arabidopsis_has_domains() -> None:
    """Real InterPro call — AT4G09760 (Q9SZ92) has ≥1 domain incl. a pfam row."""
    async with httpx.AsyncClient() as client:
        r = await interpro.lookup_locus(client, "AT4G09760", "arabidopsis")
    assert r["found"] is True
    assert r["domain_count"] >= 1
    assert any(d["source_database"] == "pfam" for d in r["domains"])
