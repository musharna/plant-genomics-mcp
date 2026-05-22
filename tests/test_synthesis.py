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


@pytest.mark.asyncio
async def test_timed_step_network_error_becomes_error_steprow():
    """Raw httpx network errors must land as status="error" StepRows with
    the [ClassName] prefix wire format — they must NOT propagate out of
    _timed_step and crash the envelope."""
    from plant_genomics_mcp.synthesis import _timed_step

    async def _raises_timeout():
        raise httpx.TimeoutException("upstream slow")

    row = await _timed_step(1, "fake", _raises_timeout())
    assert row.status == "error"
    assert row.tool == "fake"
    assert row.step == 1
    assert row.error is not None
    assert row.error.startswith("[TimeoutException]")


@pytest.mark.asyncio
async def test_analyze_locus_synth_network_error_records_error_row_not_crash(httpx_mock):
    """A raw httpx network error on one phase-2 backend (xrefs) must record
    a status="error" StepRow with the [ClassName] prefix, while the rest of
    the envelope still composes from the other successful rows."""
    httpx_mock.add_response(
        url="https://rest.ensembl.org/lookup/id/AT1G01010?species=arabidopsis_thaliana&expand=0",
        json={
            "id": "AT1G01010",
            "species": "arabidopsis_thaliana",
            "display_name": "NAC001",
        },
    )
    # xrefs raises a raw httpx network error (not a PlantGenomicsError)
    httpx_mock.add_exception(
        httpx.ReadTimeout("read timed out"),
        url="https://rest.ensembl.org/xrefs/id/AT1G01010?species=arabidopsis_thaliana",
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
    assert env.result["xrefs"] is None
    # Find the xrefs row (tool == "get_gene_xrefs")
    xrefs_rows = [s for s in env.steps if s.tool == "get_gene_xrefs"]
    assert len(xrefs_rows) == 1
    xrefs_row = xrefs_rows[0]
    assert xrefs_row.status == "error"
    assert xrefs_row.error is not None
    assert xrefs_row.error.startswith("[ReadTimeout]")


@pytest.mark.asyncio
async def test_find_homologs_synth_all_backends_succeed_returns_full_envelope(
    httpx_mock, monkeypatch
):
    # Stub blast_sequence to skip the actual NCBI Put/Poll/Get cycle.
    async def fake_blast(
        client,
        sequence,
        program="blastp",
        database=None,
        *,
        hitlist_size=10,
        expect=10.0,
        megablast=False,
        poll_interval=60.0,
        max_wait=600.0,
    ):
        # Real blast._parse_hit_table emits hits keyed on accession + description
        # + bit_score + evalue + identity; the wrapper emits hitCount (camelCase),
        # status="READY", raw_report_truncated, elapsed_seconds. We mirror that
        # shape verbatim so the synthesis composer is exercised against the wire
        # contract, not a fictional shape. BlastResult is validated below to
        # catch future drift at test-collection time.
        return {
            "rid": "FAKE",
            "program": program,
            "database": database or "swissprot",
            "status": "READY",
            "hitCount": 2,
            "hits": [
                {
                    "accession": "Q0WV96.1",
                    "description": "Probable transcription factor",
                    "bit_score": 250.0,
                    "evalue": 0.0,
                    "identity": "100%",
                },
                {
                    "accession": "Q9LIV2.1",
                    "description": "Another transcription factor",
                    "bit_score": 180.0,
                    "evalue": 1e-90,
                    "identity": "88%",
                },
            ],
            "raw_report_excerpt": "...",
            "raw_report_truncated": False,
            "elapsed_seconds": 12.3,
        }

    monkeypatch.setattr("plant_genomics_mcp.synthesis.blast.blast_sequence", fake_blast)

    httpx_mock.add_response(
        url="https://rest.uniprot.org/uniprotkb/Q0WV96.json",
        json={
            "primaryAccession": "Q0WV96",
            "uniProtkbId": "Y_ARATH",
            "entryType": "UniProtKB reviewed (Swiss-Prot)",
            "proteinDescription": {"recommendedName": {"fullName": {"value": "X"}}},
            "genes": [{"geneName": {"value": "NAC001"}}],
            "organism": {"scientificName": "Arabidopsis thaliana", "taxonId": 3702},
            "sequence": {"length": 429},
        },
    )
    httpx_mock.add_response(
        url="https://rest.uniprot.org/uniprotkb/Q9LIV2.json",
        json={
            "primaryAccession": "Q9LIV2",
            "uniProtkbId": "X_ARATH",
            "entryType": "UniProtKB reviewed (Swiss-Prot)",
            "proteinDescription": {"recommendedName": {"fullName": {"value": "Y"}}},
            "genes": [{"geneName": {"value": "X1"}}],
            "organism": {"scientificName": "Arabidopsis thaliana", "taxonId": 3702},
            "sequence": {"length": 200},
        },
    )

    from plant_genomics_mcp.synthesis import find_homologs_synth

    async with httpx.AsyncClient() as client:
        env = await find_homologs_synth(client, "MEDQ", program="blastp", top_n=10)

    assert env.tool == "find_homologs_synth"
    assert env.input["program"] == "blastp"
    assert len(env.steps) == 2  # phase 1 blast + phase 2 rolled-up batch lookup
    assert [s.status for s in env.steps] == ["ok", "ok"]
    assert env.result is not None
    ranked = env.result["ranked_hits"]
    assert len(ranked) == 2
    assert ranked[0]["uniprot_record"]["primaryAccession"] == "Q0WV96"
    assert ranked[1]["uniprot_record"]["primaryAccession"] == "Q9LIV2"
    assert env.result["notes"] == []


@pytest.mark.asyncio
async def test_find_homologs_synth_non_uniprot_subjects_flagged(monkeypatch):
    async def fake_blast(client, sequence, **kw):
        # Real BLAST hit shape: NCBI accessions like NP_001185207.1 (RefSeq
        # protein) and locus identifiers like AT1G01010.1 don't match the
        # UniProt accession regex and should be flagged.
        return {
            "rid": "FAKE",
            "program": "blastp",
            "database": "core_nt",
            "status": "READY",
            "hitCount": 1,
            "hits": [
                {
                    "accession": "AT1G01010.1",
                    "description": "Locus-style identifier (no UniProt mapping)",
                    "bit_score": 200.0,
                    "evalue": 0.0,
                    "identity": "99%",
                },
            ],
            "raw_report_excerpt": "",
            "raw_report_truncated": False,
            "elapsed_seconds": 8.0,
        }

    monkeypatch.setattr("plant_genomics_mcp.synthesis.blast.blast_sequence", fake_blast)

    from plant_genomics_mcp.synthesis import find_homologs_synth

    async with httpx.AsyncClient() as client:
        env = await find_homologs_synth(client, "MEDQ", program="blastp", top_n=5)
    assert env.result["notes"] == ["non_uniprot_subject"]
    assert env.result["ranked_hits"][0]["uniprot_record"] is None


@pytest.mark.asyncio
async def test_find_homologs_synth_phase1_failure_skips_phase2(monkeypatch):
    from plant_genomics_mcp.errors import UpstreamUnavailableError

    async def fake_blast(*a, **kw):
        raise UpstreamUnavailableError("BLAST RID=X reported Status=FAILED after 60s")

    monkeypatch.setattr("plant_genomics_mcp.synthesis.blast.blast_sequence", fake_blast)

    from plant_genomics_mcp.synthesis import find_homologs_synth

    async with httpx.AsyncClient() as client:
        env = await find_homologs_synth(client, "MEDQ", program="blastp")
    assert env.result is None
    assert env.steps[0].status == "error"
    assert env.steps[1].status == "skipped"


def test_extract_uniprot_accession_handles_all_blast_subject_forms():
    from plant_genomics_mcp.synthesis import _extract_uniprot_accession

    assert _extract_uniprot_accession("sp|Q0WV96.1|Y_ARATH") == "Q0WV96.1"
    assert _extract_uniprot_accession("tr|A0A1B2C3D4|X_ARATH") == "A0A1B2C3D4"
    assert _extract_uniprot_accession("Q0WV96") == "Q0WV96"
    assert _extract_uniprot_accession("AT1G01010.1") is None
    assert _extract_uniprot_accession("") is None


def test_find_homologs_synth_test_fixtures_match_real_blast_result_shape():
    """Real-execution check: validate the fake_blast fixtures against the
    BlastResult pydantic model (extra="forbid") so future shape drift in
    blast.blast_sequence breaks the test at collection time rather than
    silently slipping through with stub-only assertions.
    """
    from plant_genomics_mcp.models import BlastResult

    # Mirror the all-backends-succeed fixture verbatim — extra="forbid" on
    # BlastResult and BlastHit catches any added/removed/renamed key.
    BlastResult.model_validate(
        {
            "rid": "FAKE",
            "program": "blastp",
            "database": "swissprot",
            "status": "READY",
            "hitCount": 2,
            "hits": [
                {
                    "accession": "Q0WV96.1",
                    "description": "Probable transcription factor",
                    "bit_score": 250.0,
                    "evalue": 0.0,
                    "identity": "100%",
                },
                {
                    "accession": "Q9LIV2.1",
                    "description": "Another transcription factor",
                    "bit_score": 180.0,
                    "evalue": 1e-90,
                    "identity": "88%",
                },
            ],
            "raw_report_excerpt": "...",
            "raw_report_truncated": False,
            "elapsed_seconds": 12.3,
        }
    )


# ---------------------------------------------------------------------------
# Task 4 — biological_context_synth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_biological_context_synth_all_backends_succeed_returns_full_envelope(httpx_mock):
    # Phase 1: UniProt search (gene:AT1G01010 AND organism_id:3702 AND reviewed:true)
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
    # Phase 2 — Gramene /v69/genes?idList=...&fl=homology
    # Real shape: list of records; homology.homologous_genes is dict[category]→list[locus_str].
    httpx_mock.add_response(
        url=re.compile(r"^https://data\.gramene\.org/v69/genes.*"),
        json=[
            {
                "_id": "AT1G01010",
                "homology": {
                    "gene_tree": {"id": "EPlGT01130000406172"},
                    "homologous_genes": {"ortholog_one2one": ["Os01g0100100"]},
                },
            }
        ],
    )
    # Phase 2 — KEGG: /link/pathway/<lowercased-gene_id> then /get/path:<id> per pathway
    httpx_mock.add_response(
        url="https://rest.kegg.jp/link/pathway/ath:at1g01010",
        text="ath:at1g01010\tpath:ath00010\nath:at1g01010\tpath:ath01100\n",
    )
    # asyncio.gather over both pathways → 2 GETs. is_reusable=True covers both.
    httpx_mock.add_response(
        url=re.compile(r"^https://rest\.kegg\.jp/get/path:ath.*"),
        text=(
            "ENTRY       ath00010                    Pathway\n"
            "NAME        Glycolysis / Gluconeogenesis - Arabidopsis thaliana\n"
            "CLASS       Metabolism; Carbohydrate metabolism\n"
        ),
        is_reusable=True,
    )
    # Phase 2 — STRING: real endpoint is /api/json/interaction_partners
    httpx_mock.add_response(
        url=re.compile(r"^https://string-db\.org/api/json/interaction_partners.*"),
        json=[
            {
                "stringId_A": "3702.AT1G01010.1",
                "stringId_B": "3702.AT3G15500.1",
                "preferredName_A": "NAC001",
                "preferredName_B": "NAC3",
                "score": 0.85,
                "escore": 0.4,
                "dscore": 0.0,
                "tscore": 0.2,
                "pscore": 0.1,
            }
        ],
    )
    # Phase 2 — ATTED-II /api5/?gene=...&topN=...&db=Ath-u.c4-0
    # Real shape: {result_set: [{entrez_gene_id, type, results: [{gene, other_id, z}], other_id}]}
    httpx_mock.add_response(
        url=re.compile(r"^https://atted\.jp/api5/.*"),
        json={
            "request": {"gene": "AT1G01010"},
            "result_set": [
                {
                    "entrez_gene_id": 839580,
                    "type": "z",
                    "results": [
                        {"gene": 842367, "other_id": ["AT4G36990"], "z": 7.0},
                        {"gene": 820194, "other_id": ["AT3G15500"], "z": 5.5},
                    ],
                    "other_id": "AT1G01010",
                }
            ],
        },
    )

    from plant_genomics_mcp.synthesis import biological_context_synth

    async with httpx.AsyncClient() as client:
        env = await biological_context_synth(client, "AT1G01010", top_n=10)

    assert env.tool == "biological_context_synth"
    assert len(env.steps) == 5  # 1 phase-1 + 4 phase-2
    assert [s.status for s in env.steps] == ["ok"] * 5
    r = env.result
    assert r["uniprot_accession"] == "Q0WV96"
    assert r["homologs"]["total"] == 1
    assert r["homologs"]["homologs"][0]["target_locus"] == "Os01g0100100"
    pathway_ids = sorted(p["id"] for p in r["pathways"]["pathways"])
    assert pathway_ids == ["ath00010", "ath01100"]
    assert r["string_partners"]["partners"][0]["string_id"] == "3702.AT3G15500.1"
    atted_loci = [n["locus"] for n in r["atted_coexpression"]["neighbors"]]
    assert atted_loci == ["AT4G36990", "AT3G15500"]
    # consensus_partners: AT3G15500 appears in both STRING + ATTED → 2 sources → ranks first.
    consensus = r["consensus_partners"]
    assert isinstance(consensus, list)
    assert consensus[0]["target_locus"] == "AT3G15500"
    assert consensus[0]["n_sources"] == 2
    assert consensus[0]["sources"] == ["string", "atted"]


@pytest.mark.asyncio
async def test_biological_context_synth_phase1_failure_skips_all_phase2(httpx_mock):
    # UniProt search: 0 reviewed hits → fallback (drop reviewed) → 0 hits → NotFoundError
    httpx_mock.add_response(
        url=re.compile(r"^https://rest\.uniprot\.org/uniprotkb/search.*"),
        json={"results": []},
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://rest\.uniprot\.org/uniprotkb/search.*"),
        json={"results": []},
    )
    from plant_genomics_mcp.synthesis import biological_context_synth

    async with httpx.AsyncClient() as client:
        env = await biological_context_synth(client, "AT9G99999")
    assert env.result is None
    assert env.steps[0].status == "error"
    assert all(s.status == "skipped" for s in env.steps[1:])


def test_consensus_partners_two_source_ranks_above_single_source():
    from plant_genomics_mcp.synthesis import _consensus_partners

    # STRING uses normalized shape: string_id=<taxid>.<locus>.<N>, score already 0-1.
    string_payload = {
        "partners": [
            {"string_id": "3702.AT1A.1", "preferred_name": "X", "score": 0.5},
            {"string_id": "3702.AT1B.1", "preferred_name": "Y", "score": 0.95},
        ]
    }
    # ATTED uses normalized shape: locus, z_score (NOT mr).
    atted_payload = {
        "neighbors": [
            {"locus": "AT1A", "z_score": 5.0},
            {"locus": "AT2C", "z_score": 7.0},
        ]
    }
    consensus = _consensus_partners(string_payload, atted_payload, top_n=10)
    # AT1A is in both → n_sources=2 → ranks first regardless of combined_score.
    assert consensus[0]["target_locus"] == "AT1A"
    assert consensus[0]["n_sources"] == 2
    # Single-source partners follow; ordering between them is combined_score desc.
    # AT1B: 0.95, AT2C: 7/(1+7)=0.875 → AT1B first.
    rest_loci = [c["target_locus"] for c in consensus[1:]]
    assert rest_loci == ["AT1B", "AT2C"]


def test_consensus_partners_single_source_degenerates_gracefully():
    from plant_genomics_mcp.synthesis import _consensus_partners

    out = _consensus_partners(
        string_payload={"partners": [{"string_id": "3702.X.1", "score": 0.7}]},
        atted_payload=None,
        top_n=10,
    )
    assert out == [
        {"target_locus": "X", "n_sources": 1, "combined_score": 0.7, "sources": ["string"]}
    ]


def test_biological_context_synth_fixtures_match_real_response_shapes():
    """Real-execution check at system boundary: validate per-backend Pydantic
    wrappers (extra='forbid' on each outer wrapper) against the shapes the
    synth composer consumes from gramene/kegg/string_db/atted .lookup_*().
    Mirrors test_find_homologs_synth_test_fixtures_match_real_blast_result_shape.
    """
    from plant_genomics_mcp.models import (
        AttedCoexpression,
        GrameneHomologs,
        KeggPathways,
        StringInteractions,
    )

    # Gramene composer output
    GrameneHomologs.model_validate(
        {
            "locus": "AT1G01010",
            "release": "v69",
            "total": 1,
            "homologs": [
                {
                    "target_locus": "Os01g0100100",
                    "type": "ortholog_one2one",
                    "gene_tree_id": "EPlGT01130000406172",
                }
            ],
        }
    )

    # KEGG composer output
    KeggPathways.model_validate(
        {
            "locus": "AT1G01010",
            "kegg_gene_id": "ath:at1g01010",
            "pathways": [
                {
                    "id": "ath00010",
                    "name": "Glycolysis / Gluconeogenesis - Arabidopsis thaliana",
                    "pathway_class": "Metabolism; Carbohydrate metabolism",
                }
            ],
            "errors": [],
        }
    )

    # STRING composer output (normalized partner shape — string_id NOT stringId_B)
    StringInteractions.model_validate(
        {
            "query": "Q0WV96",
            "accession": "Q0WV96",
            "organism_taxid": 3702,
            "partners": [
                {
                    "string_id": "3702.AT3G15500.1",
                    "accession": "3702.AT3G15500.1",
                    "preferred_name": "NAC3",
                    "score": 0.85,
                    "escore": 0.4,
                    "dscore": 0.0,
                    "tscore": 0.2,
                    "pscore": 0.1,
                }
            ],
        }
    )

    # ATTED composer output (z_score, NOT mr)
    AttedCoexpression.model_validate(
        {
            "locus": "AT1G01010",
            "atted_release": "Ath-u.c4-0",
            "neighbors": [{"locus": "AT4G36990", "entrez_gene_id": 842367, "z_score": 7.0}],
        }
    )
