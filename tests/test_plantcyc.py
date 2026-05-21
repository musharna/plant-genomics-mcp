"""Tests for the PlantCyc informational-stub backend.

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

_EXPECTED_KEYS_NO_TOKEN = {
    "locus",
    "plantcyc_web_url",
    "status",
    "probed_at",
    "auth_configured",
    "rationale",
    "alternatives",
    "alternatives_note",
}

_EXPECTED_KEYS_WITH_TOKEN = _EXPECTED_KEYS_NO_TOKEN | {"note_for_subscribers"}


def test_lookup_locus_at1g01010_returns_redirect_record(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(plantcyc.PLANTCYC_TOKEN_ENV, raising=False)
    result = plantcyc.lookup_locus("AT1G01010")
    assert set(result.keys()) == _EXPECTED_KEYS_NO_TOKEN
    assert result["locus"] == "AT1G01010"
    assert (
        result["plantcyc_web_url"]
        == "https://pmn.plantcyc.org/PLANT/NEW-IMAGE?type=GENE&object=AT1G01010"
    )
    assert result["status"] == "subscription_required"
    assert result["probed_at"] == "2026-05-21"
    assert result["auth_configured"] is False
    assert "ensembl_plants_lookup_locus" in result["alternatives"]
    assert "phytozome_lookup_locus" in result["alternatives"]


def test_lookup_locus_rejects_invalid_locus() -> None:
    with pytest.raises(PlantGenomicsError, match="invalid locus"):
        plantcyc.lookup_locus("AT1G01010<x>")


def test_lookup_locus_rejects_empty_locus() -> None:
    with pytest.raises(PlantGenomicsError, match="invalid locus"):
        plantcyc.lookup_locus("")


def test_lookup_locus_alternatives_match_live_tool_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Structural anti-rot guard.

    If a future rename of ``ensembl_plants_lookup_locus`` or
    ``phytozome_lookup_locus`` happens, the redirect record's alternatives
    list would silently become dangling. This test catches that by
    comparing against the live ``server.TOOLS`` registry.
    """
    monkeypatch.delenv(plantcyc.PLANTCYC_TOKEN_ENV, raising=False)
    result = plantcyc.lookup_locus("AT1G01010")
    live_tool_names = {t.name for t in server.TOOLS}
    for alt in result["alternatives"]:
        assert alt in live_tool_names, (
            f"alternative {alt!r} not in live server.TOOLS "
            f"({sorted(live_tool_names)}) — was a tool renamed?"
        )


def test_lookup_locus_flips_status_when_token_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2.20 config-slot behavior.

    Setting ``PLANT_GENOMICS_MCP_PLANTCYC_TOKEN`` must flip the returned
    record's ``status`` to ``configured_live_not_implemented`` and
    surface a ``note_for_subscribers`` pointer. The HTTP wiring is
    deliberately deferred — see module docstring.
    """
    monkeypatch.setenv(plantcyc.PLANTCYC_TOKEN_ENV, "test-token-value")
    result = plantcyc.lookup_locus("AT1G01010")
    assert set(result.keys()) == _EXPECTED_KEYS_WITH_TOKEN
    assert result["status"] == "configured_live_not_implemented"
    assert result["auth_configured"] is True
    assert "PLANT_GENOMICS_MCP_PLANTCYC_TOKEN" in result["rationale"]
    assert "_call_live_if_configured" in result["note_for_subscribers"]
    assert "ensembl_plants_lookup_locus" in result["alternatives"]


def test_empty_token_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty string token must NOT flip status — only non-empty values count."""
    monkeypatch.setenv(plantcyc.PLANTCYC_TOKEN_ENV, "")
    result = plantcyc.lookup_locus("AT1G01010")
    assert result["status"] == "subscription_required"
    assert result["auth_configured"] is False


def test_output_schema_accepts_both_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pydantic model must validate against both no-token and with-token records."""
    from plant_genomics_mcp.models import PlantCycLocusInfo

    monkeypatch.delenv(plantcyc.PLANTCYC_TOKEN_ENV, raising=False)
    PlantCycLocusInfo.model_validate(plantcyc.lookup_locus("AT1G01010"))

    monkeypatch.setenv(plantcyc.PLANTCYC_TOKEN_ENV, "test-token")
    PlantCycLocusInfo.model_validate(plantcyc.lookup_locus("AT1G01010"))
