"""Tests for the PlantCyc informational-redirect backend.

Pure unit tests — PlantCyc has no free per-locus REST API (BioCyc PLANT
orgid requires a paid SRI/Phoenix subscription, controller-verified
2026-05-21), so there's nothing live to probe. No httpx, no asyncio, no
LIVE gate. Structural anti-rot guard included to catch dangling
alternative-tool references if those are renamed in the future.
"""

from __future__ import annotations

import pytest

from plant_genomics_mcp import plantcyc, server
from plant_genomics_mcp.ensembl_plants import PlantGenomicsError

_EXPECTED_KEYS = {
    "locus",
    "plantcyc_web_url",
    "status",
    "probed_at",
    "rationale",
    "alternatives",
    "alternatives_note",
}


def test_lookup_locus_at1g01010_returns_redirect_record() -> None:
    result = plantcyc.lookup_locus("AT1G01010")
    assert set(result.keys()) == _EXPECTED_KEYS
    assert result["locus"] == "AT1G01010"
    assert (
        result["plantcyc_web_url"]
        == "https://pmn.plantcyc.org/PLANT/NEW-IMAGE?type=GENE&object=AT1G01010"
    )
    assert result["status"] == "subscription_required"
    assert result["probed_at"] == "2026-05-21"
    assert "ensembl_plants_lookup_locus" in result["alternatives"]
    assert "phytozome_lookup_locus" in result["alternatives"]


def test_lookup_locus_rejects_invalid_locus() -> None:
    with pytest.raises(PlantGenomicsError, match="invalid locus"):
        plantcyc.lookup_locus("AT1G01010<x>")


def test_lookup_locus_rejects_empty_locus() -> None:
    with pytest.raises(PlantGenomicsError, match="invalid locus"):
        plantcyc.lookup_locus("")


def test_lookup_locus_alternatives_match_live_tool_names() -> None:
    """Structural anti-rot guard.

    If a future rename of ``ensembl_plants_lookup_locus`` or
    ``phytozome_lookup_locus`` happens, the redirect record's alternatives
    list would silently become dangling. This test catches that by
    comparing against the live ``server.TOOLS`` registry.
    """
    result = plantcyc.lookup_locus("AT1G01010")
    live_tool_names = {t.name for t in server.TOOLS}
    for alt in result["alternatives"]:
        assert alt in live_tool_names, (
            f"alternative {alt!r} not in live server.TOOLS "
            f"({sorted(live_tool_names)}) — was a tool renamed?"
        )


def test_output_schema_validates_redirect_record() -> None:
    """Pydantic model must validate the redirect record shape."""
    from plant_genomics_mcp.models import PlantCycLocusInfo

    PlantCycLocusInfo.model_validate(plantcyc.lookup_locus("AT1G01010"))
