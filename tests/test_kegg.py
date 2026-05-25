"""KEGG pathway-membership backend unit tests.

KEGG returns plain text (TSV-like), not JSON. Each test mocks both calls
(link/pathway and get/path) so the two-step sequence is exercised end-to-end.
"""

from __future__ import annotations

import os

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import kegg
from plant_genomics_mcp.errors import NotFoundError, OrganismNotSupported


@pytest.fixture(autouse=True)
def _clear_cache():
    kegg._CACHE.clear()
    yield
    kegg._CACHE.clear()


# ---------- v1.1.0 T5 — organism= contract on lookup_pathways ----------


@pytest.mark.asyncio
async def test_lookup_pathways_requires_organism():
    """v1.1.0 BREAKING: ``organism`` is keyword-only and required.
    Calling without it must TypeError before any HTTP.
    """
    async with httpx.AsyncClient() as client:
        with pytest.raises(TypeError):
            await kegg.lookup_pathways(client, "AT1G01010")


@pytest.mark.asyncio
async def test_lookup_pathways_arabidopsis_uses_ath_prefix(httpx_mock: HTTPXMock):
    """organism='arabidopsis_thaliana' must continue to splice ``ath:`` and
    must preserve the caller's locus case verbatim — KEGG v118.0 made
    ``/link/pathway`` case-sensitive on the locus side, so down-casing
    would zero the result against the live API.
    """
    httpx_mock.add_response(
        url="https://rest.kegg.jp/link/pathway/ath:AT1G01010",
        text="ath:AT1G01010\tpath:ath04075\n",
    )
    httpx_mock.add_response(
        url="https://rest.kegg.jp/get/path:ath04075",
        text="ENTRY       ath04075                    Pathway\nNAME        Plant hormone signal transduction\nCLASS       Environmental Information Processing; Signal transduction\n",
    )
    async with httpx.AsyncClient() as client:
        result = await kegg.lookup_pathways(client, "AT1G01010", organism="arabidopsis_thaliana")
    assert result["kegg_gene_id"] == "ath:AT1G01010"
    assert result["pathways"][0]["id"] == "ath04075"


@pytest.mark.asyncio
async def test_lookup_pathways_unsupported_organism_raises():
    """Non-Arabidopsis organisms have ``kegg_org_code=None`` in v1.1.0
    (KEGG uses NCBI Entrez Gene IDs for them, which our cross-backend
    locus contract can't currently produce). The accessor must raise
    OrganismNotSupported(backend='kegg', ...) before any HTTP fires.
    """
    async with httpx.AsyncClient() as client:
        with pytest.raises(OrganismNotSupported):
            await kegg.lookup_pathways(client, "Os01g0100100", organism="oryza_sativa")


@pytest.mark.asyncio
async def test_lookup_pathways_happy_path(httpx_mock: HTTPXMock):
    # Step 1: locus → pathway list
    httpx_mock.add_response(
        url="https://rest.kegg.jp/link/pathway/ath:AT1G01010",
        text="ath:AT1G01010\tpath:ath04075\nath:AT1G01010\tpath:ath04141\n",
    )
    # Step 2: per-pathway metadata
    httpx_mock.add_response(
        url="https://rest.kegg.jp/get/path:ath04075",
        text="ENTRY       ath04075                    Pathway\nNAME        Plant hormone signal transduction - Arabidopsis thaliana\nCLASS       Environmental Information Processing; Signal transduction\n",
    )
    httpx_mock.add_response(
        url="https://rest.kegg.jp/get/path:ath04141",
        text="ENTRY       ath04141                    Pathway\nNAME        Protein processing in endoplasmic reticulum - Arabidopsis thaliana\nCLASS       Genetic Information Processing; Folding, sorting and degradation\n",
    )
    async with httpx.AsyncClient() as client:
        result = await kegg.lookup_pathways(client, "AT1G01010", organism="arabidopsis_thaliana")
    assert result["locus"] == "AT1G01010"
    assert result["kegg_gene_id"] == "ath:AT1G01010"
    assert len(result["pathways"]) == 2
    p0 = result["pathways"][0]
    assert p0["id"] == "ath04075"
    assert "Plant hormone" in p0["name"]
    assert "Signal transduction" in p0["pathway_class"]
    assert result["errors"] == []


@pytest.mark.asyncio
async def test_lookup_pathways_empty_link_raises_not_found(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="https://rest.kegg.jp/link/pathway/ath:ATNOPE",
        text="",
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError) as exc:
            await kegg.lookup_pathways(client, "ATNOPE", organism="arabidopsis_thaliana")
    assert "[NotFoundError]" in str(exc.value)


@pytest.mark.asyncio
async def test_lookup_pathways_step2_failure_lands_in_errors(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="https://rest.kegg.jp/link/pathway/ath:AT1G01010",
        text="ath:AT1G01010\tpath:ath99999\n",
    )
    httpx_mock.add_response(
        url="https://rest.kegg.jp/get/path:ath99999",
        text="",
    )
    async with httpx.AsyncClient() as client:
        result = await kegg.lookup_pathways(client, "AT1G01010", organism="arabidopsis_thaliana")
    assert len(result["pathways"]) == 1
    assert result["pathways"][0]["id"] == "ath99999"
    assert result["pathways"][0]["name"] == ""
    assert len(result["errors"]) == 1
    assert "ath99999" in result["errors"][0]


@pytest.mark.asyncio
async def test_lookup_pathways_404_treated_as_empty(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="https://rest.kegg.jp/link/pathway/ath:ATNOPE",
        status_code=404,
        text="",
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError):
            await kegg.lookup_pathways(client, "ATNOPE", organism="arabidopsis_thaliana")


@pytest.mark.skipif(
    not os.environ.get("PLANT_GENOMICS_MCP_LIVE"),
    reason="set PLANT_GENOMICS_MCP_LIVE=1 to hit rest.kegg.jp",
)
@pytest.mark.asyncio
async def test_live_kegg_at3g52930_has_pathways():
    """AT3G52930 (FBA, fructose-bisphosphate aldolase) is a glycolysis
    pathway member — confirmed 8 pathways on KEGG v118 (2026-05-26 probe).
    The originally chosen AT1G01010 has zero KEGG pathway annotations and
    is not a stable canary. Asserting on AT3G52930 also catches a future
    re-introduction of ``.lower()`` (KEGG v118 returns empty for the
    lowercased form).
    """
    async with httpx.AsyncClient() as client:
        result = await kegg.lookup_pathways(client, "AT3G52930", organism="arabidopsis_thaliana")
    assert result["locus"] == "AT3G52930"
    assert result["kegg_gene_id"] == "ath:AT3G52930"
    assert len(result["pathways"]) > 0, "AT3G52930 should be in at least one KEGG pathway"


@pytest.mark.skipif(
    not os.environ.get("PLANT_GENOMICS_MCP_LIVE"),
    reason="set PLANT_GENOMICS_MCP_LIVE=1 to hit rest.kegg.jp",
)
@pytest.mark.asyncio
async def test_live_kegg_non_arabidopsis_raises_unsupported():
    """v1.1.0: non-Arabidopsis organisms have ``kegg_org_code=None`` in the
    matrix until an Entrez bridge lands. The accessor must raise before
    any HTTP fires; this guards against accidentally re-populating an
    org code without also adding the Entrez resolver.
    """
    async with httpx.AsyncClient() as client:
        with pytest.raises(OrganismNotSupported):
            await kegg.lookup_pathways(client, "Os01g0100100", organism="oryza_sativa")


# ---------- v1.4.0 — KEGG locus → Entrez bridge ----------


def test_soybean_normalizer_only_transforms_glyma_prefix() -> None:
    """The bridge rewrites SoyBase `Glyma.X` → Ensembl `GLYMA_X` only for
    soybean — every other organism + already-normalized inputs pass through.
    Literal-substring transform, no regex, so no over-matching surprises.
    """
    assert kegg._normalize_locus_for_ensembl("Glyma.04G220900", "glycine_max") == "GLYMA_04G220900"
    # Wrong organism: don't rewrite even if the prefix matches.
    assert kegg._normalize_locus_for_ensembl("Glyma.04G220900", "oryza_sativa") == "Glyma.04G220900"
    # Already-normalized: pass through (no double-prefix).
    assert kegg._normalize_locus_for_ensembl("GLYMA_04G220900", "glycine_max") == "GLYMA_04G220900"
    # No Glyma. prefix on a soybean call: pass through unchanged.
    assert kegg._normalize_locus_for_ensembl("Os01g0100100", "glycine_max") == "Os01g0100100"


# ---------- Wave B6: shared locus validator at the URL boundary ----------


@pytest.mark.asyncio
async def test_lookup_pathways_rejects_malformed_locus_before_http() -> None:
    """KEGG splices ``<org>:<locus>`` into ``/link/pathway/{gene_id}``;
    a slash or whitespace would forge a different upstream call. The
    rejection must fire before any HTTP, so no ``httpx_mock`` is used.
    """
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="invalid locus"):
            await kegg.lookup_pathways(client, "AT1G01010/etc", organism="arabidopsis_thaliana")
