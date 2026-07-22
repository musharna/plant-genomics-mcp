"""Tests for the PlantCyc / PMN metabolism backend.

Two tiers (mirrors the quickgo pattern):
  1. Unit tests with mocked HTTP via pytest-httpx — the multi-hop getxml
     traversal (gene → enzyme → reactions → pathways) is driven by a routing
     callback keyed on the requested frame.
  2. Live integration tests gated by PLANT_GENOMICS_MCP_LIVE=1.

v1.13: this module was previously a subscription-gated stub; the BioCyc
web-services API is in fact free (re-probed 2026-07-19), so it now makes
real calls.
"""

from __future__ import annotations

import os

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import organisms, plantcyc
from plant_genomics_mcp.errors import OrganismNotSupported, PlantGenomicsError

LIVE = os.environ.get("PLANT_GENOMICS_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set PLANT_GENOMICS_MCP_LIVE=1 to run")

# --- minimal ptools-XML fixtures for one gene's full traversal ---
_RESOLVE = (
    "<ptools-xml><metadata><num_results>1</num_results></metadata>"
    "<Gene ID='ARA:AT3G51240' orgid='ARA' frameid='AT3G51240'/></ptools-xml>"
)
_RESOLVE_EMPTY = "<ptools-xml><metadata><num_results>0</num_results></metadata></ptools-xml>"
_GENE = (
    "<ptools-xml><Gene ID='ARA:AT3G51240' orgid='ARA' frameid='AT3G51240'>"
    "<common-name datatype='string'>F3H</common-name>"
    "<product><Protein ID='ARA:AT3G51240-MONOMER' orgid='ARA' frameid='AT3G51240-MONOMER'/>"
    "</product></Gene></ptools-xml>"
)
_MONOMER = (
    "<ptools-xml><Protein ID='ARA:AT3G51240-MONOMER' frameid='AT3G51240-MONOMER'>"
    "<catalyzes><Enzymatic-Reaction><reaction>"
    "<Reaction ID='ARA:RXN-1' orgid='ARA' frameid='RXN-1'/>"
    "</reaction></Enzymatic-Reaction></catalyzes></Protein></ptools-xml>"
)
_REACTION = (
    "<ptools-xml><Reaction ID='ARA:RXN-1' frameid='RXN-1'>"
    "<common-name datatype='string'>naringenin 3-dioxygenase</common-name>"
    "<in-pathway><Pathway resource='getxml?ARA:PWY-1' orgid='ARA' frameid='PWY-1'/></in-pathway>"
    "</Reaction></ptools-xml>"
)
_PATHWAY = (
    "<ptools-xml><Pathway ID='ARA:PWY-1' frameid='PWY-1'>"
    "<common-name datatype='string'>flavonoid biosynthesis</common-name></Pathway></ptools-xml>"
)

_FRAMES = {
    "AT3G51240": _GENE,
    "AT3G51240-MONOMER": _MONOMER,
    "RXN-1": _REACTION,
    "PWY-1": _PATHWAY,
}


def _install_router(httpx_mock: HTTPXMock, *, resolve: str = _RESOLVE, frames=None) -> None:
    """Route xmlquery → resolution XML and getxml?ORG:FRAME → the frame XML."""
    table = _FRAMES if frames is None else frames

    def handler(request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.path.endswith("/xmlquery"):
            return httpx.Response(200, text=resolve)
        query = url.query.decode()  # e.g. "ARA:AT3G51240"
        frame = query.split(":", 1)[1] if ":" in query else query
        if frame in table:
            return httpx.Response(200, text=table[frame])
        return httpx.Response(404, text="<html>not found</html>")

    httpx_mock.add_callback(handler, is_reusable=True)


# ---------- mocked unit tests ----------


@pytest.mark.asyncio
async def test_lookup_locus_full_traversal(httpx_mock: HTTPXMock) -> None:
    _install_router(httpx_mock)
    async with httpx.AsyncClient() as client:
        result = await plantcyc.lookup_locus(client, "AT3G51240", "arabidopsis")
    assert result["found"] is True
    assert result["organism"] == "arabidopsis_thaliana"
    assert result["orgid"] == "ARA"
    assert result["gene_frame"] == "AT3G51240"
    assert result["gene_common_name"] == "F3H"
    assert result["enzymes"] == ["AT3G51240-MONOMER"]
    assert result["reactions"] == [{"id": "RXN-1", "name": "naringenin 3-dioxygenase"}]
    assert result["pathways"] == [{"id": "PWY-1", "name": "flavonoid biosynthesis"}]
    assert result["reaction_count"] == 1
    assert result["pathway_count"] == 1


@pytest.mark.asyncio
async def test_lookup_locus_unresolved_is_graceful(httpx_mock: HTTPXMock) -> None:
    """A locus that resolves to no gene (num_results=0) → found=False, no error."""
    _install_router(httpx_mock, resolve=_RESOLVE_EMPTY)
    async with httpx.AsyncClient() as client:
        result = await plantcyc.lookup_locus(client, "AT1G01010", "arabidopsis")
    assert result["found"] is False
    assert result["gene_frame"] is None
    assert result["reactions"] == []
    assert result["pathways"] == []
    assert result["reaction_count"] == 0


@pytest.mark.asyncio
async def test_lookup_locus_gene_without_enzyme(httpx_mock: HTTPXMock) -> None:
    """Gene resolves but its product catalyzes nothing → found=True, empty rxns."""
    gene_no_product = (
        "<ptools-xml><Gene ID='ARA:AT3G51240' frameid='AT3G51240'>"
        "<common-name datatype='string'>X</common-name></Gene></ptools-xml>"
    )
    _install_router(httpx_mock, frames={"AT3G51240": gene_no_product})
    async with httpx.AsyncClient() as client:
        result = await plantcyc.lookup_locus(client, "AT3G51240", "arabidopsis")
    assert result["found"] is True
    assert result["enzymes"] == []
    assert result["reactions"] == []
    assert result["pathways"] == []


@pytest.mark.asyncio
async def test_lookup_locus_resolves_frame_for_query(httpx_mock: HTTPXMock) -> None:
    """The BioVelo resolution query is sent to the org's xmlquery endpoint."""
    _install_router(httpx_mock)
    async with httpx.AsyncClient() as client:
        await plantcyc.lookup_locus(client, "AT3G51240", "arabidopsis")
    xq = [r for r in httpx_mock.get_requests() if r.url.path.endswith("/xmlquery")]
    assert xq, "expected an xmlquery resolution call"
    q = xq[0].url.params["query"]
    assert "accession-1=" in q and '"AT3G51240"' in q
    assert 'x^"accession-1"' not in q, "slot name must be UNQUOTED in BioVelo"


@pytest.mark.asyncio
async def test_lookup_locus_malformed_xml_raises(httpx_mock: HTTPXMock) -> None:
    _install_router(httpx_mock, frames={"AT3G51240": "<<not xml>>"})
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlantGenomicsError, match="unparseable XML"):
            await plantcyc.lookup_locus(client, "AT3G51240", "arabidopsis")


@pytest.mark.asyncio
async def test_lookup_locus_rejects_bad_locus() -> None:
    # Typed PlantGenomicsError (NotFoundError) so the wire keeps the [ClassName]
    # prefix — raised before any network call.
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlantGenomicsError, match="invalid locus"):
            await plantcyc.lookup_locus(client, "AT1G01010; DROP", "arabidopsis")


@pytest.mark.asyncio
async def test_lookup_locus_unsupported_organism_raises() -> None:
    # Wheat's PGDB orgid is not mapped → OrganismNotSupported before any HTTP.
    async with httpx.AsyncClient() as client:
        with pytest.raises(OrganismNotSupported):
            await plantcyc.lookup_locus(client, "TraesCS1A02G000100", "wheat")


def test_eleven_organisms_have_a_pgdb() -> None:
    supported = [r.canonical for r in organisms.ORGANISMS.values() if r.plantcyc_orgid]
    assert len(supported) == 11
    assert "triticum_aestivum" not in supported  # wheat not yet mapped


# ---------- live integration (real-execution check) ----------


@live_only
@pytest.mark.asyncio
async def test_live_f3h_has_flavonoid_pathway() -> None:
    """Real PMN call — Arabidopsis F3H (AT3G51240) → flavonoid pathway."""
    async with httpx.AsyncClient() as client:
        result = await plantcyc.lookup_locus(client, "AT3G51240", "arabidopsis")
    assert result["found"] is True
    assert result["enzymes"]
    assert result["pathway_count"] > 0
    names = " ".join((p["name"] or "").lower() for p in result["pathways"])
    assert "flavonoid" in names or "leucopelargonidin" in names


@live_only
@pytest.mark.asyncio
async def test_live_transcription_factor_returns_empty() -> None:
    """NAC001 (a TF) has no metabolic annotation → found=False, not an error."""
    async with httpx.AsyncClient() as client:
        result = await plantcyc.lookup_locus(client, "AT1G01010", "arabidopsis")
    assert result["found"] is False
    assert result["reactions"] == []


@live_only
@pytest.mark.asyncio
async def test_live_rice_cross_species_resolves() -> None:
    """Rice locus resolves through OryzaCyc (cross-species accession mapping)."""
    async with httpx.AsyncClient() as client:
        result = await plantcyc.lookup_locus(client, "Os11g0530600", "rice")
    assert result["orgid"] == "ORYZA"
    assert result["found"] is True


# ---------- negative caching (audit 2026-07-22, M2) ----------


@pytest.mark.asyncio
async def test_404_frame_is_cached_so_a_repeat_fetch_stays_off_the_wire(
    httpx_mock: HTTPXMock,
) -> None:
    """A missing frame is a normal, frequent answer — it must not re-hit PMN.

    The router here is deliberately reusable (a second request would succeed
    rather than fail the test), so this counts handler invocations instead:
    two ``_getxml`` calls for the same absent frame must reach upstream once.
    """
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.query.decode())
        return httpx.Response(404, text="<html>not found</html>")

    httpx_mock.add_callback(handler, is_reusable=True)
    async with httpx.AsyncClient() as client:
        assert await plantcyc._getxml(client, "ARA", "NO-SUCH-FRAME") is None
        assert await plantcyc._getxml(client, "ARA", "NO-SUCH-FRAME") is None
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_a_cached_404_does_not_mask_a_different_frame(
    httpx_mock: HTTPXMock,
) -> None:
    """The negative is keyed per frame, not shared across them."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.query.decode()
        calls.append(query)
        if query.endswith("MISSING"):
            return httpx.Response(404, text="<html>not found</html>")
        return httpx.Response(200, text=_GENE)

    httpx_mock.add_callback(handler, is_reusable=True)
    async with httpx.AsyncClient() as client:
        assert await plantcyc._getxml(client, "ARA", "MISSING") is None
        assert await plantcyc._getxml(client, "ARA", "AT3G51240") is not None
    assert len(calls) == 2
