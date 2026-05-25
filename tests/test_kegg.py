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
    """Organisms in the 8 deferred set (tomato, wheat, sorghum, barley,
    grape, poplar, medicago, brachypodium) still have ``kegg_org_code=None``
    post-v1.4.0 — the bridge only landed for rice/maize/soybean. The
    accessor must raise OrganismNotSupported(backend='kegg', ...) before
    any HTTP fires. v1.4.0: swapped from rice (now supported) to wheat.
    """
    async with httpx.AsyncClient() as client:
        with pytest.raises(OrganismNotSupported):
            await kegg.lookup_pathways(client, "TraesCS1A02G000100", organism="triticum_aestivum")


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
async def test_live_kegg_deferred_organism_raises_unsupported():
    """v1.4.0: the 8 deferred organisms (tomato/wheat/sorghum/barley/grape/
    poplar/medicago/brachypodium) still have ``kegg_org_code=None``. The
    accessor must raise before any HTTP fires; this guards against
    accidentally extending the bridge to another organism without also
    populating its matrix slot. Swapped from rice (now supported via the
    v1.4.0 bridge) to wheat.
    """
    async with httpx.AsyncClient() as client:
        with pytest.raises(OrganismNotSupported):
            await kegg.lookup_pathways(client, "TraesCS1A02G000100", organism="triticum_aestivum")


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


@pytest.mark.asyncio
async def test_resolve_locus_to_entrez_id_returns_first_entrez(httpx_mock: HTTPXMock):
    """Happy path — Ensembl /xrefs returns one EntrezGene xref; bridge
    returns the primary_id verbatim.
    """
    httpx_mock.add_response(
        url="https://rest.ensembl.org/xrefs/id/Os01g0100100?species=oryza_sativa",
        json=[
            {"dbname": "EntrezGene", "primary_id": "4326813", "display_id": "Os01g0100100"},
            {"dbname": "ArrayExpress", "primary_id": "Os01g0100100", "display_id": "Os01g0100100"},
        ],
    )
    async with httpx.AsyncClient() as client:
        entrez_id = await kegg._resolve_locus_to_entrez_id(
            client, "Os01g0100100", organism="oryza_sativa"
        )
    assert entrez_id == "4326813"


@pytest.mark.asyncio
async def test_resolve_locus_to_entrez_id_multi_entrez_picks_first(httpx_mock: HTTPXMock):
    """When Ensembl returns multiple EntrezGene xrefs (rare; read-through
    fusions, pseudogene/parent pairings), first-wins. Matches STRING's
    multi-UniProt policy.
    """
    httpx_mock.add_response(
        url="https://rest.ensembl.org/xrefs/id/Os01g0100100?species=oryza_sativa",
        json=[
            {"dbname": "EntrezGene", "primary_id": "4326813", "display_id": "primary"},
            {"dbname": "EntrezGene", "primary_id": "9876543", "display_id": "secondary"},
        ],
    )
    async with httpx.AsyncClient() as client:
        entrez_id = await kegg._resolve_locus_to_entrez_id(
            client, "Os01g0100100", organism="oryza_sativa"
        )
    assert entrez_id == "4326813"


@pytest.mark.asyncio
async def test_resolve_locus_to_entrez_id_no_entrez_xref_raises(httpx_mock: HTTPXMock):
    """Ensembl returned cross-refs but none from EntrezGene (the tomato
    case observed in the pre-impl probe — only ArrayExpress shows up).
    Bridge must raise loud — no silent empty pathway list.
    """
    httpx_mock.add_response(
        url="https://rest.ensembl.org/xrefs/id/Solyc01g005610.3?species=solanum_lycopersicum",
        json=[
            {"dbname": "ArrayExpress", "primary_id": "Solyc01g005610", "display_id": "x"},
        ],
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="none from EntrezGene"):
            await kegg._resolve_locus_to_entrez_id(
                client, "Solyc01g005610.3", organism="solanum_lycopersicum"
            )


@pytest.mark.asyncio
async def test_resolve_locus_to_entrez_id_empty_xref_list_raises(httpx_mock: HTTPXMock):
    """Ensembl returned an empty list (locus has no xrefs at all). Surface
    the same NotFoundError shape as the no-EntrezGene case — caller doesn't
    care about the distinction, just that the bridge can't proceed.
    """
    httpx_mock.add_response(
        url="https://rest.ensembl.org/xrefs/id/Os01g0100100?species=oryza_sativa",
        json=[],
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="0 cross-refs, none from EntrezGene"):
            await kegg._resolve_locus_to_entrez_id(client, "Os01g0100100", organism="oryza_sativa")


@pytest.mark.asyncio
async def test_resolve_locus_to_entrez_id_soybean_normalizes_on_wire(httpx_mock: HTTPXMock):
    """Caller passes SoyBase form ``Glyma.04G220900``; the bridge MUST hit
    Ensembl with the normalized ``GLYMA_04G220900``. If the registered mock
    URL contains the un-normalized form, pytest-httpx errors with
    'No response found' — making the test guard the normalizer firing.
    """
    httpx_mock.add_response(
        url="https://rest.ensembl.org/xrefs/id/GLYMA_04G220900?species=glycine_max",
        json=[
            {"dbname": "EntrezGene", "primary_id": "100810680", "display_id": "x"},
        ],
    )
    async with httpx.AsyncClient() as client:
        entrez_id = await kegg._resolve_locus_to_entrez_id(
            client, "Glyma.04G220900", organism="glycine_max"
        )
    assert entrez_id == "100810680"


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


# ---------- v1.4.0 integration: lookup_pathways dispatches via bridge ----------


@pytest.mark.asyncio
async def test_lookup_pathways_rice_via_bridge(httpx_mock: HTTPXMock):
    """Rice (Os01g0100100 → osa:4326813) — the v1.4.0 headline path.
    Asserts ``locus`` is the user-facing form, ``kegg_gene_id`` is the
    Entrez-bound KEGG form, ``entrez_gene_id`` surfaces the bridge result.
    """
    # Bridge step: Ensembl /xrefs
    httpx_mock.add_response(
        url="https://rest.ensembl.org/xrefs/id/Os01g0100100?species=oryza_sativa",
        json=[{"dbname": "EntrezGene", "primary_id": "4326813", "display_id": "x"}],
    )
    # KEGG step 1
    httpx_mock.add_response(
        url="https://rest.kegg.jp/link/pathway/osa:4326813",
        text="osa:4326813\tpath:osa00010\n",
    )
    # KEGG step 2 (per-pathway record)
    httpx_mock.add_response(
        url="https://rest.kegg.jp/get/path:osa00010",
        text="ENTRY       osa00010                    Pathway\nNAME        Glycolysis / Gluconeogenesis - Oryza sativa\nCLASS       Metabolism; Carbohydrate metabolism\n",
    )
    async with httpx.AsyncClient() as client:
        result = await kegg.lookup_pathways(client, "Os01g0100100", organism="oryza_sativa")
    assert result["locus"] == "Os01g0100100"
    assert result["kegg_gene_id"] == "osa:4326813"
    assert result["entrez_gene_id"] == "4326813"
    assert len(result["pathways"]) == 1
    assert result["pathways"][0]["id"] == "osa00010"
    assert "Glycolysis" in result["pathways"][0]["name"]


@pytest.mark.asyncio
async def test_lookup_pathways_maize_via_bridge(httpx_mock: HTTPXMock):
    """Maize (Zm00001eb000010 → zma:103644366)."""
    httpx_mock.add_response(
        url="https://rest.ensembl.org/xrefs/id/Zm00001eb000010?species=zea_mays",
        json=[{"dbname": "EntrezGene", "primary_id": "103644366", "display_id": "x"}],
    )
    httpx_mock.add_response(
        url="https://rest.kegg.jp/link/pathway/zma:103644366",
        text="zma:103644366\tpath:zma04075\n",
    )
    httpx_mock.add_response(
        url="https://rest.kegg.jp/get/path:zma04075",
        text="ENTRY       zma04075                    Pathway\nNAME        Plant hormone signal transduction - Zea mays\nCLASS       Environmental Information Processing; Signal transduction\n",
    )
    async with httpx.AsyncClient() as client:
        result = await kegg.lookup_pathways(client, "Zm00001eb000010", organism="zea_mays")
    assert result["locus"] == "Zm00001eb000010"
    assert result["kegg_gene_id"] == "zma:103644366"
    assert result["entrez_gene_id"] == "103644366"
    assert result["pathways"][0]["id"] == "zma04075"


@pytest.mark.asyncio
async def test_lookup_pathways_soybean_via_bridge_normalizes_locus(httpx_mock: HTTPXMock):
    """Soybean — caller passes SoyBase form ``Glyma.04G220900``; the
    bridge MUST hit Ensembl with normalized ``GLYMA_04G220900``. The
    user-facing ``locus`` field stays the SoyBase form. The mocked Ensembl
    URL with normalized form is the discriminator — if the normalizer
    doesn't fire, pytest-httpx errors with no-mock-match.
    """
    httpx_mock.add_response(
        url="https://rest.ensembl.org/xrefs/id/GLYMA_04G220900?species=glycine_max",
        json=[{"dbname": "EntrezGene", "primary_id": "100810680", "display_id": "x"}],
    )
    httpx_mock.add_response(
        url="https://rest.kegg.jp/link/pathway/gmx:100810680",
        text="gmx:100810680\tpath:gmx00010\n",
    )
    httpx_mock.add_response(
        url="https://rest.kegg.jp/get/path:gmx00010",
        text="ENTRY       gmx00010                    Pathway\nNAME        Glycolysis / Gluconeogenesis - Glycine max\nCLASS       Metabolism; Carbohydrate metabolism\n",
    )
    async with httpx.AsyncClient() as client:
        result = await kegg.lookup_pathways(client, "Glyma.04G220900", organism="glycine_max")
    # User-facing locus stays the SoyBase form even though the bridge
    # rewrote it on the wire.
    assert result["locus"] == "Glyma.04G220900"
    assert result["kegg_gene_id"] == "gmx:100810680"
    assert result["entrez_gene_id"] == "100810680"


@pytest.mark.asyncio
async def test_lookup_pathways_arabidopsis_bypasses_bridge(httpx_mock: HTTPXMock):
    """Arabidopsis path is unchanged — KEGG's ``ath:`` scope accepts AGI
    loci natively, so no Ensembl /xrefs call should fire. We register the
    KEGG mocks only; if the bridge fires unexpectedly, pytest-httpx errors
    with 'No response can be found' on the unmatched Ensembl request.
    The output MUST NOT contain ``entrez_gene_id``.
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
    assert "entrez_gene_id" not in result, (
        "Arabidopsis path must not surface entrez_gene_id — bridge did not fire"
    )
    # Positive guard: confirm no Ensembl request fired (in case pytest-httpx's
    # "no mock match" raises ever get downgraded to warnings).
    ensembl_requests = [r for r in httpx_mock.get_requests() if "ensembl" in str(r.url)]
    assert ensembl_requests == [], (
        f"Arabidopsis path must not call Ensembl /xrefs; saw: {[str(r.url) for r in ensembl_requests]}"
    )


@pytest.mark.asyncio
async def test_lookup_pathways_bridge_no_entrez_xref_raises_not_found(httpx_mock: HTTPXMock):
    """Ensembl returns cross-refs but none from EntrezGene (tomato's pre-impl
    probe case — ArrayExpress only). The bridge's NotFoundError must surface
    through lookup_pathways with the KEGG-bridge breadcrumb (item #4), so
    users see 'KEGG bridge (Ensembl Plants /xrefs): ...' rather than a bare
    bridge-helper message.
    """
    httpx_mock.add_response(
        url="https://rest.ensembl.org/xrefs/id/Os01g0100100?species=oryza_sativa",
        json=[
            {"dbname": "ArrayExpress", "primary_id": "Os01g0100100", "display_id": "x"},
        ],
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match=r"KEGG bridge.*no Entrez Gene ID.*cross-ref"):
            await kegg.lookup_pathways(client, "Os01g0100100", organism="oryza_sativa")


@pytest.mark.asyncio
async def test_lookup_pathways_ensembl_xrefs_503_propagates(httpx_mock: HTTPXMock):
    """If Ensembl is unhealthy during the bridge call, the
    UpstreamUnavailableError raised by _http.request_with_retry must
    propagate — no swallowing, no fallback to "0 pathways". 3× 503
    matches MAX_RETRIES in ensembl_plants.
    """
    from plant_genomics_mcp.errors import UpstreamUnavailableError

    for _ in range(3):
        httpx_mock.add_response(
            url="https://rest.ensembl.org/xrefs/id/Os01g0100100?species=oryza_sativa",
            status_code=503,
            text="",
        )
    async with httpx.AsyncClient() as client:
        with pytest.raises(UpstreamUnavailableError):
            await kegg.lookup_pathways(client, "Os01g0100100", organism="oryza_sativa")


@pytest.mark.skipif(
    not os.environ.get("PLANT_GENOMICS_MCP_LIVE"),
    reason="set PLANT_GENOMICS_MCP_LIVE=1 to hit rest.kegg.jp + rest.ensembl.org",
)
@pytest.mark.asyncio
async def test_live_kegg_rice_returns_pathways_via_bridge():
    """Rice RAP-DB locus ``Os05g0375100`` (hexokinase-10-like) — live probe
    (2026-05-25) confirmed Ensembl /xrefs → EntrezGene 107275630 and KEGG
    ``osa:107275630`` has ≥8 pathway memberships (glycolysis + starch/sucrose
    metabolism). Original spec locus Os01g0100100 resolved the Entrez ID
    correctly but had zero KEGG pathway annotations. ≥1 pathway expected.
    """
    async with httpx.AsyncClient() as client:
        result = await kegg.lookup_pathways(client, "Os05g0375100", organism="oryza_sativa")
    assert result["locus"] == "Os05g0375100"
    assert int(result["entrez_gene_id"]) > 0, "rice should resolve to a positive Entrez Gene ID"
    assert result["kegg_gene_id"].startswith("osa:")
    assert len(result["pathways"]) > 0, (
        "Rice Os05g0375100 should have ≥1 KEGG pathway via bridge; got 0"
    )


@pytest.mark.skipif(
    not os.environ.get("PLANT_GENOMICS_MCP_LIVE"),
    reason="set PLANT_GENOMICS_MCP_LIVE=1 to hit rest.kegg.jp + rest.ensembl.org",
)
@pytest.mark.asyncio
async def test_live_kegg_maize_returns_pathways_via_bridge():
    """Maize MaizeGDB locus ``Zm00001eb148000`` (aldose reductase) — live
    probe (2026-05-25) confirmed Ensembl /xrefs → EntrezGene 100037812 and
    KEGG ``zma:100037812`` has ≥3 pathway memberships (glycolysis, pentose
    phosphate). Original spec locus Zm00001eb000010 resolved the Entrez ID
    correctly but had zero KEGG pathway annotations. ≥1 pathway expected.
    """
    async with httpx.AsyncClient() as client:
        result = await kegg.lookup_pathways(client, "Zm00001eb148000", organism="zea_mays")
    assert result["locus"] == "Zm00001eb148000"
    assert int(result["entrez_gene_id"]) > 0, "maize should resolve to a positive Entrez Gene ID"
    assert result["kegg_gene_id"].startswith("zma:")
    assert len(result["pathways"]) > 0, (
        "Maize Zm00001eb148000 should have ≥1 KEGG pathway via bridge; got 0"
    )


@pytest.mark.skipif(
    not os.environ.get("PLANT_GENOMICS_MCP_LIVE"),
    reason="set PLANT_GENOMICS_MCP_LIVE=1 to hit rest.kegg.jp + rest.ensembl.org",
)
@pytest.mark.asyncio
async def test_live_kegg_soybean_returns_pathways_via_bridge():
    """Soybean SoyBase locus ``Glyma.19G000700`` (pyruvate kinase) — live
    probe (2026-05-25) confirmed Glyma.→GLYMA_ normalizer fires, Ensembl
    ``GLYMA_19G000700`` → EntrezGene 100037452, and KEGG ``gmx:100037452``
    has ≥3 pathway memberships (glycolysis, pyruvate metabolism). Original
    spec locus Glyma.04G220900 resolved the Entrez ID correctly but had zero
    KEGG pathway annotations. Live assertion uses the SoyBase ``Glyma.`` form
    to exercise the normalizer end-to-end. ≥1 pathway expected.
    """
    async with httpx.AsyncClient() as client:
        result = await kegg.lookup_pathways(client, "Glyma.19G000700", organism="glycine_max")
    assert result["locus"] == "Glyma.19G000700", "user-facing form must be preserved"
    assert result["entrez_gene_id"] == "100037452"
    assert result["kegg_gene_id"] == "gmx:100037452"
    assert len(result["pathways"]) > 0, (
        "Soybean Glyma.19G000700 should have ≥1 KEGG pathway via bridge; got 0"
    )


# ---------- v1.5 — deferred-organism expansion ----------
#
# 3 mocked happy-path tests for the v1.5 pass organisms (barley, poplar,
# brachypodium) + 5 still-unsupported tests for the v1.5 falsified
# organisms (wheat, sorghum, grape, medicago, tomato). Probe evidence
# lives in scripts/probe_kegg_bridge_candidates.json (generated
# 2026-05-25). Pathway-annotation counts in the probe are 0 across all
# three pass organisms — the probe gate was relaxed to entrez_xref
# presence — so these mocks register a single synthetic pathway to
# exercise the end-to-end bridge wiring (Ensembl /xrefs → KEGG
# /link/pathway → KEGG /get/path) rather than to mirror live counts.


@pytest.mark.asyncio
async def test_lookup_pathways_barley_via_bridge(httpx_mock: HTTPXMock):
    """Barley (HORVU.MOREX.r3.1HG0000090 → hvg:123427420) — v1.5 probe
    pass. Probe evidence: chr1 locus HORVU.MOREX.r3.1HG0000090, EntrezGene
    123427420; pathway-annotation count 0 (informational — probe relaxed
    the pathway gate). See scripts/probe_kegg_bridge_candidates.json.
    """
    httpx_mock.add_response(
        url="https://rest.ensembl.org/xrefs/id/HORVU.MOREX.r3.1HG0000090?species=hordeum_vulgare",
        json=[{"dbname": "EntrezGene", "primary_id": "123427420", "display_id": "x"}],
    )
    httpx_mock.add_response(
        url="https://rest.kegg.jp/link/pathway/hvg:123427420",
        text="hvg:123427420\tpath:hvg00010\n",
    )
    httpx_mock.add_response(
        url="https://rest.kegg.jp/get/path:hvg00010",
        text="ENTRY       hvg00010                    Pathway\nNAME        Glycolysis / Gluconeogenesis - Hordeum vulgare\nCLASS       Metabolism; Carbohydrate metabolism\n",
    )
    async with httpx.AsyncClient() as client:
        result = await kegg.lookup_pathways(
            client, "HORVU.MOREX.r3.1HG0000090", organism="hordeum_vulgare"
        )
    assert result["locus"] == "HORVU.MOREX.r3.1HG0000090"
    assert result["kegg_gene_id"] == "hvg:123427420"
    assert result["entrez_gene_id"] == "123427420"
    assert len(result["pathways"]) == 1
    assert result["pathways"][0]["id"] == "hvg00010"
    assert "Glycolysis" in result["pathways"][0]["name"]


@pytest.mark.asyncio
async def test_lookup_pathways_poplar_via_bridge(httpx_mock: HTTPXMock):
    """Poplar (Potri.001G006600.v4.1 → pop:7483252) — v1.5 probe pass.
    Probe evidence: chr1 locus Potri.001G006600.v4.1, EntrezGene 7483252;
    pathway-annotation count 0 (informational — probe relaxed the pathway
    gate). See scripts/probe_kegg_bridge_candidates.json.
    """
    httpx_mock.add_response(
        url="https://rest.ensembl.org/xrefs/id/Potri.001G006600.v4.1?species=populus_trichocarpa",
        json=[{"dbname": "EntrezGene", "primary_id": "7483252", "display_id": "x"}],
    )
    httpx_mock.add_response(
        url="https://rest.kegg.jp/link/pathway/pop:7483252",
        text="pop:7483252\tpath:pop00010\n",
    )
    httpx_mock.add_response(
        url="https://rest.kegg.jp/get/path:pop00010",
        text="ENTRY       pop00010                    Pathway\nNAME        Glycolysis / Gluconeogenesis - Populus trichocarpa\nCLASS       Metabolism; Carbohydrate metabolism\n",
    )
    async with httpx.AsyncClient() as client:
        result = await kegg.lookup_pathways(
            client, "Potri.001G006600.v4.1", organism="populus_trichocarpa"
        )
    assert result["locus"] == "Potri.001G006600.v4.1"
    assert result["kegg_gene_id"] == "pop:7483252"
    assert result["entrez_gene_id"] == "7483252"
    assert len(result["pathways"]) == 1
    assert result["pathways"][0]["id"] == "pop00010"
    assert "Glycolysis" in result["pathways"][0]["name"]


@pytest.mark.asyncio
async def test_lookup_pathways_brachypodium_via_bridge(httpx_mock: HTTPXMock):
    """Brachypodium (BRADI_1g00485v3 → bdi:100837010) — v1.5 probe pass.
    Probe evidence: chr1 locus BRADI_1g00485v3, EntrezGene 100837010;
    pathway-annotation count 0 (informational — probe relaxed the pathway
    gate). See scripts/probe_kegg_bridge_candidates.json.
    """
    httpx_mock.add_response(
        url="https://rest.ensembl.org/xrefs/id/BRADI_1g00485v3?species=brachypodium_distachyon",
        json=[{"dbname": "EntrezGene", "primary_id": "100837010", "display_id": "x"}],
    )
    httpx_mock.add_response(
        url="https://rest.kegg.jp/link/pathway/bdi:100837010",
        text="bdi:100837010\tpath:bdi00010\n",
    )
    httpx_mock.add_response(
        url="https://rest.kegg.jp/get/path:bdi00010",
        text="ENTRY       bdi00010                    Pathway\nNAME        Glycolysis / Gluconeogenesis - Brachypodium distachyon\nCLASS       Metabolism; Carbohydrate metabolism\n",
    )
    async with httpx.AsyncClient() as client:
        result = await kegg.lookup_pathways(
            client, "BRADI_1g00485v3", organism="brachypodium_distachyon"
        )
    assert result["locus"] == "BRADI_1g00485v3"
    assert result["kegg_gene_id"] == "bdi:100837010"
    assert result["entrez_gene_id"] == "100837010"
    assert len(result["pathways"]) == 1
    assert result["pathways"][0]["id"] == "bdi00010"
    assert "Glycolysis" in result["pathways"][0]["name"]


@pytest.mark.asyncio
async def test_lookup_pathways_wheat_still_unsupported():
    """v1.5 probe verdict: no_entrez_xref (Ensembl /xrefs returned only
    ArrayExpress/KNETMINER_WHEAT/WHEATEXP_GENE, no EntrezGene). Matrix
    kegg_org_code stays None. Guards against accidental flip-on in a
    future edit. See scripts/probe_kegg_bridge_candidates.json.
    """
    async with httpx.AsyncClient() as client:
        with pytest.raises(OrganismNotSupported, match="triticum_aestivum"):
            await kegg.lookup_pathways(client, "ignored", organism="triticum_aestivum")


@pytest.mark.asyncio
async def test_lookup_pathways_sorghum_still_unsupported():
    """v1.5 probe verdict: no_entrez_xref (Ensembl /xrefs returned only
    ArrayExpress-tier dbs, no EntrezGene). Matrix kegg_org_code stays
    None. Guards against accidental flip-on in a future edit. See
    scripts/probe_kegg_bridge_candidates.json.
    """
    async with httpx.AsyncClient() as client:
        with pytest.raises(OrganismNotSupported, match="sorghum_bicolor"):
            await kegg.lookup_pathways(client, "ignored", organism="sorghum_bicolor")


@pytest.mark.asyncio
async def test_lookup_pathways_grape_still_unsupported():
    """v1.5 probe verdict: no_entrez_xref (Ensembl /xrefs returned only
    ArrayExpress-tier dbs, no EntrezGene). Matrix kegg_org_code stays
    None. Guards against accidental flip-on in a future edit. See
    scripts/probe_kegg_bridge_candidates.json.
    """
    async with httpx.AsyncClient() as client:
        with pytest.raises(OrganismNotSupported, match="vitis_vinifera"):
            await kegg.lookup_pathways(client, "ignored", organism="vitis_vinifera")


@pytest.mark.asyncio
async def test_lookup_pathways_medicago_still_unsupported():
    """v1.5 probe verdict: no_entrez_xref (Ensembl /xrefs returned only
    ArrayExpress-tier dbs, no EntrezGene). Matrix kegg_org_code stays
    None. Guards against accidental flip-on in a future edit. See
    scripts/probe_kegg_bridge_candidates.json.
    """
    async with httpx.AsyncClient() as client:
        with pytest.raises(OrganismNotSupported, match="medicago_truncatula"):
            await kegg.lookup_pathways(client, "ignored", organism="medicago_truncatula")


@pytest.mark.asyncio
async def test_lookup_pathways_tomato_still_unsupported():
    """v1.5 probe verdict: no_entrez_xref (Ensembl /xrefs returned only
    ArrayExpress-tier dbs, no EntrezGene). Matrix kegg_org_code stays
    None. Guards against accidental flip-on in a future edit. See
    scripts/probe_kegg_bridge_candidates.json.
    """
    async with httpx.AsyncClient() as client:
        with pytest.raises(OrganismNotSupported, match="solanum_lycopersicum"):
            await kegg.lookup_pathways(client, "ignored", organism="solanum_lycopersicum")
