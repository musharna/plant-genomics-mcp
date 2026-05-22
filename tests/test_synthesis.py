"""Tests for synthesis tools — Pydantic models, orchestrators, and consensus.

Mocked-HTTP unit tests run by default. Live tests are gated by
``PLANT_GENOMICS_MCP_LIVE=1`` to keep CI fast and avoid hammering NCBI BLAST.
"""

from __future__ import annotations


import pytest

from plant_genomics_mcp.models import StepRow, SynthesisEnvelope


def test_step_row_status_values_constrained_to_three():
    # ok / error / skipped — anything else must fail validation
    StepRow(step=1, tool="x", status="ok", elapsed_s=0.1)
    StepRow(step=1, tool="x", status="error", elapsed_s=0.1, error="[E] x")
    StepRow(step=1, tool="x", status="skipped", elapsed_s=0.0, error="phase 1 failed")
    with pytest.raises(Exception):
        StepRow(step=1, tool="x", status="bogus", elapsed_s=0.0)


def test_synthesis_envelope_round_trips_through_model_json_schema():
    schema = SynthesisEnvelope.model_json_schema()
    assert schema["type"] == "object"
    required = set(schema.get("required", []))
    assert {"tool", "input", "started_at", "elapsed_s", "steps"}.issubset(required)
    assert schema.get("additionalProperties") is False


def test_synthesis_envelope_round_trip_dict():
    env = SynthesisEnvelope(
        tool="analyze_locus_synth",
        input={"locus": "AT1G01010"},
        started_at="2026-05-22T00:00:00Z",
        elapsed_s=1.23,
        steps=[
            StepRow(
                step=1,
                tool="ensembl_plants_lookup_locus",
                status="ok",
                elapsed_s=0.5,
                result={"id": "AT1G01010"},
            ),
        ],
        result={"ensembl_record": {"id": "AT1G01010"}},
    )
    round_tripped = SynthesisEnvelope.model_validate(env.model_dump())
    assert round_tripped == env
