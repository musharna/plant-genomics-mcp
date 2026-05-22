"""Tests for synthesis tools — Pydantic models, orchestrators, and consensus.

Mocked-HTTP unit tests run by default. Live tests are gated by
``PLANT_GENOMICS_MCP_LIVE=1`` to keep CI fast and avoid hammering NCBI BLAST.
"""

from __future__ import annotations


import pytest
from pydantic import ValidationError

from plant_genomics_mcp.models import StepRow, SynthesisEnvelope


def test_step_row_status_values_constrained_to_three():
    # ok / error / skipped — anything else must fail validation
    StepRow(step=1, tool="x", status="ok", elapsed_s=0.1, result={})
    StepRow(step=1, tool="x", status="error", elapsed_s=0.1, error="[E] x")
    StepRow(step=1, tool="x", status="skipped", elapsed_s=0.0, error="phase 1 failed")
    with pytest.raises(ValidationError):
        StepRow(step=1, tool="x", status="bogus", elapsed_s=0.0)


def test_step_row_validator_rejects_ok_without_result():
    with pytest.raises(ValidationError):
        StepRow(step=1, tool="x", status="ok", elapsed_s=0.1)


def test_step_row_validator_rejects_error_without_message():
    with pytest.raises(ValidationError):
        StepRow(step=1, tool="x", status="error", elapsed_s=0.1)


def test_step_row_validator_rejects_skipped_without_reason():
    with pytest.raises(ValidationError):
        StepRow(step=1, tool="x", status="skipped", elapsed_s=0.0)


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


import re

import httpx


@pytest.mark.asyncio
async def test_analyze_locus_synth_all_backends_succeed_returns_full_envelope(httpx_mock):
    # Ensembl lookup_id
    httpx_mock.add_response(
        url="https://rest.ensembl.org/lookup/id/AT1G01010?species=arabidopsis_thaliana&expand=0",
        json={
            "id": "AT1G01010",
            "species": "arabidopsis_thaliana",
            "biotype": "protein_coding",
            "display_name": "NAC001",
        },
    )
    # Ensembl xrefs
    httpx_mock.add_response(
        url="https://rest.ensembl.org/xrefs/id/AT1G01010?species=arabidopsis_thaliana",
        json=[{"dbname": "Uniprot_gn", "primary_id": "Q0WV96", "display_id": "NAC001"}],
    )
    # UniProt search (gene:AT1G01010)
    httpx_mock.add_response(
        url=re.compile(r"^https://rest\.uniprot\.org/uniprotkb/search.*"),
        json={
            "results": [
                {
                    "primaryAccession": "Q0WV96",
                    "uniProtkbId": "Y1010_ARATH",
                    "entryType": "UniProtKB reviewed (Swiss-Prot)",
                    "proteinDescription": {"recommendedName": {"fullName": {"value": "X"}}},
                    "genes": [{"geneName": {"value": "NAC001"}}],
                    "organism": {"scientificName": "Arabidopsis thaliana", "taxonId": 3702},
                    "sequence": {"length": 429},
                }
            ]
        },
    )
    # Europe PMC
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.ebi\.ac\.uk/europepmc/webservices/rest/search.*"),
        json={"hitCount": 1, "resultList": {"result": [{"pmid": "12345", "title": "X"}]}},
    )
    # QuickGO
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.ebi\.ac\.uk/QuickGO/services/annotation/search.*"),
        json={
            "numberOfHits": 1,
            "results": [{"goId": "GO:0006355", "goName": "regulation of transcription"}],
        },
    )

    from plant_genomics_mcp.synthesis import analyze_locus_synth

    async with httpx.AsyncClient() as client:
        env = await analyze_locus_synth(client, "AT1G01010", species="arabidopsis_thaliana")

    assert env.tool == "analyze_locus_synth"
    assert env.input == {"locus": "AT1G01010", "species": "arabidopsis_thaliana"}
    assert len(env.steps) == 5
    assert [s.tool for s in env.steps] == [
        "ensembl_plants_lookup_locus",
        "resolve_locus_to_uniprot",
        "get_gene_xrefs",
        "locus_literature",
        "locus_go_annotations",
    ]
    assert [s.status for s in env.steps] == ["ok"] * 5
    assert env.result is not None
    assert env.result["ensembl_record"]["id"] == "AT1G01010"
    assert env.result["reconciled"]["best_uniprot_accession"] == "Q0WV96"
    assert env.result["reconciled"]["canonical_gene_name"] == "NAC001"


@pytest.mark.asyncio
async def test_analyze_locus_synth_phase1_failure_skips_phase2(httpx_mock):
    # Ensembl 404 — phase-1 root resolution failure
    httpx_mock.add_response(
        url="https://rest.ensembl.org/lookup/id/AT9G99999?species=arabidopsis_thaliana&expand=0",
        status_code=404,
        json={"error": "ID 'AT9G99999' not found"},
    )
    # UniProt also fires in parallel during phase 1 (sequenced there so the
    # phase-2 QuickGO chain has primaryAccession). Mock it as 0-hits twice
    # so it also errors — this is the cleanest "phase-1 collapse" case.
    httpx_mock.add_response(
        url=re.compile(r"^https://rest\.uniprot\.org/uniprotkb/search.*"),
        json={"results": []},
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://rest\.uniprot\.org/uniprotkb/search.*"),
        json={"results": []},
    )
    from plant_genomics_mcp.synthesis import analyze_locus_synth

    async with httpx.AsyncClient() as client:
        env = await analyze_locus_synth(client, "AT9G99999")

    assert env.result is None
    assert env.steps[0].status == "error"  # ensembl
    assert env.steps[1].status == "error"  # uniprot
    assert all(s.status == "skipped" for s in env.steps[2:])
    assert len(env.steps) == 5


@pytest.mark.asyncio
async def test_analyze_locus_synth_phase2_single_failure_returns_partial(httpx_mock):
    httpx_mock.add_response(
        url="https://rest.ensembl.org/lookup/id/AT1G01010?species=arabidopsis_thaliana&expand=0",
        json={
            "id": "AT1G01010",
            "species": "arabidopsis_thaliana",
            "display_name": "NAC001",
        },
    )
    httpx_mock.add_response(
        url="https://rest.ensembl.org/xrefs/id/AT1G01010?species=arabidopsis_thaliana",
        status_code=503,
        text="upstream down",
        is_reusable=True,
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://rest\.uniprot\.org/uniprotkb/search.*"),
        json={
            "results": [
                {
                    "primaryAccession": "Q0WV96",
                    "uniProtkbId": "Y_ARATH",
                    "entryType": "UniProtKB reviewed (Swiss-Prot)",
                    "proteinDescription": {"recommendedName": {"fullName": {"value": "X"}}},
                    "genes": [{"geneName": {"value": "NAC001"}}],
                    "organism": {"scientificName": "Arabidopsis thaliana", "taxonId": 3702},
                    "sequence": {"length": 429},
                }
            ]
        },
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.ebi\.ac\.uk/europepmc/webservices/rest/search.*"),
        json={"hitCount": 0, "resultList": {"result": []}},
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.ebi\.ac\.uk/QuickGO/services/annotation/search.*"),
        json={"numberOfHits": 0, "results": []},
    )
    from plant_genomics_mcp.synthesis import analyze_locus_synth

    async with httpx.AsyncClient() as client:
        env = await analyze_locus_synth(client, "AT1G01010")

    assert env.result is not None
    statuses = [s.status for s in env.steps]
    assert statuses[0] == "ok"
    assert "error" in statuses
    assert env.result["xrefs"] is None
    assert env.result["uniprot_record"] is not None
