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


def test_step_row_accepts_none_elapsed_s():
    # Wave C1: elapsed_s is float | None — None signals "not separately
    # measurable" (phase-2 gather rows, phase-0 pre-call validation failures).
    StepRow(step=1, tool="x", status="ok", elapsed_s=None, result={})
    StepRow(step=1, tool="x", status="error", elapsed_s=None, error="[E] x")
    StepRow(step=1, tool="x", status="skipped", elapsed_s=None, error="phase 1 failed")
    # Default is None when omitted entirely.
    row = StepRow(step=1, tool="x", status="ok", result={})
    assert row.elapsed_s is None


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


import re  # noqa: E402 — intentionally late: only used by tests below this point

import httpx  # noqa: E402 — same as above


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
        env = await analyze_locus_synth(client, "AT1G01010", organism="arabidopsis_thaliana")

    assert env.tool == "analyze_locus_synth"
    assert env.input == {"locus": "AT1G01010", "organism": "arabidopsis_thaliana"}
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
    # Wave C1 contract: gather rows carry elapsed_s=None because per-coroutine
    # wall time can't be honestly attributed in asyncio.gather. analyze_locus_synth
    # uses _gather_phase2 for both the parallel ensembl+uniprot root pair AND the
    # phase-2 fanout, so all 5 rows are gather rows → all None.
    # Envelope.elapsed_s remains the authoritative orchestrator total.
    assert all(s.elapsed_s is None for s in env.steps)
    assert isinstance(env.elapsed_s, float)


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
async def test_analyze_locus_synth_network_error_records_error_row_not_crash(
    httpx_mock, monkeypatch
):
    """A persistent transport error on one phase-2 backend (xrefs) must record
    a status="error" StepRow while the rest of the envelope still composes from
    the other successful rows. ``_http.request_with_retry`` now retries transport
    errors and, once the budget is exhausted, surfaces a typed
    ``UpstreamUnavailableError`` whose message preserves the original exception
    class — so the row prefix is ``[UpstreamUnavailableError]`` and the body
    still names the underlying ``ReadTimeout``."""

    async def _noop_sleep(_: float) -> None:
        return None

    # Skip the retry backoff so the test doesn't sleep through the schedule.
    monkeypatch.setattr("plant_genomics_mcp._http.asyncio.sleep", _noop_sleep)
    httpx_mock.add_response(
        url="https://rest.ensembl.org/lookup/id/AT1G01010?species=arabidopsis_thaliana&expand=0",
        json={
            "id": "AT1G01010",
            "species": "arabidopsis_thaliana",
            "display_name": "NAC001",
        },
    )
    # xrefs hits a transport error on every attempt; request_with_retry retries
    # the default 3 times before raising the typed UpstreamUnavailableError.
    for _ in range(3):
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
    assert xrefs_row.error.startswith("[UpstreamUnavailableError]")
    # The original transport-error class is preserved in the message body.
    assert "ReadTimeout" in xrefs_row.error


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
    # Wave C1 contract: find_homologs_synth phase-1 BLAST root uses _timed_step,
    # which DOES measure per-step wall time, so step 0 is a real float. Phase-2
    # batch lookup also uses _timed_step (single sequential call, not a gather)
    # so it's also a float. Both _timed_step and orchestrator total are measurable.
    assert isinstance(env.steps[0].elapsed_s, float)  # _timed_step BLAST
    assert isinstance(env.steps[1].elapsed_s, float)  # _timed_step batch lookup
    assert isinstance(env.elapsed_s, float)


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
    # Phase 2 — KEGG: /link/pathway/<gene_id> then /get/path:<id> per pathway.
    # KEGG v118.0 (May 2026) is case-sensitive on the locus; v1.1.0 preserves
    # the caller's case verbatim.
    httpx_mock.add_response(
        url="https://rest.kegg.jp/link/pathway/ath:AT1G01010",
        text="ath:AT1G01010\tpath:ath00010\nath:AT1G01010\tpath:ath01100\n",
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
async def test_biological_context_synth_partial_phase2_failure_returns_composed_envelope(
    httpx_mock,
):
    # Phase 1 — UniProt resolve succeeds.
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
    # Phase 2 — Gramene OK.
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
    # Phase 2 — KEGG: 404 on /link/pathway/... → empty body → NotFoundError in kegg_pathways.
    httpx_mock.add_response(
        url="https://rest.kegg.jp/link/pathway/ath:AT1G01010",
        status_code=404,
        text="",
    )
    # Phase 2 — STRING OK.
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
    # Phase 2 — ATTED-II OK.
    httpx_mock.add_response(
        url=re.compile(r"^https://atted\.jp/api5/.*"),
        json={
            "request": {"gene": "AT1G01010"},
            "result_set": [
                {
                    "entrez_gene_id": 839580,
                    "type": "z",
                    "results": [
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
    # Envelope composes despite the KEGG failure: result is present.
    assert env.result is not None
    # 1 phase-1 + 4 phase-2 step rows.
    assert len(env.steps) == 5
    by_tool = {s.tool: s for s in env.steps}
    assert by_tool["kegg_pathways"].status == "error"
    assert by_tool["gramene_homologs"].status == "ok"
    assert by_tool["string_interactions"].status == "ok"
    assert by_tool["atted_coexpression"].status == "ok"
    # Result still reflects the OK backends.
    r = env.result
    assert r["uniprot_accession"] == "Q0WV96"
    assert r["homologs"]["total"] == 1
    assert r["string_partners"]["partners"][0]["string_id"] == "3702.AT3G15500.1"
    assert r["atted_coexpression"]["neighbors"][0]["locus"] == "AT3G15500"
    # KEGG payload is absent / empty under the failure row.
    assert not r.get("pathways") or not r["pathways"].get("pathways")


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
            "organism": "arabidopsis_thaliana",
            "kegg_gene_id": "ath:AT1G01010",
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
            "organism": "arabidopsis_thaliana",
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


@pytest.mark.asyncio
async def test_consensus_homologs_happy_path(httpx_mock, monkeypatch):
    # Phase 1.a — uniprot lookup (search endpoint, JSON results)
    httpx_mock.add_response(
        url=re.compile(r"https://rest\.uniprot\.org/uniprotkb/search.*"),
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
    # Phase 1.b — fetch_sequence (FASTA endpoint, plain text)
    httpx_mock.add_response(
        url="https://rest.uniprot.org/uniprotkb/Q0WV96.fasta",
        text=">sp|Q0WV96|Y_ARATH\nMEDQVGFGFRPNDEELVGHYLRNK\n",
    )
    # Phase 2.a — Gramene v69 homology projection (list[record] with homology block).
    httpx_mock.add_response(
        url="https://data.gramene.org/v69/genes?idList=AT1G01010&fl=homology",
        json=[
            {
                "homology": {
                    "gene_tree": {"id": "EPlGT00190000001", "root_taxon_name": "Liliopsida"},
                    "homologous_genes": {
                        "ortholog_one2one": ["OS01G0100100"],
                    },
                }
            }
        ],
    )
    # Phase 3 — Gramene v69 enrichment projection (UniProt acc + system_name)
    # so we can dedup Gramene homologs against BLAST in UniProt-accession-space.
    httpx_mock.add_response(
        url="https://data.gramene.org/v69/genes?idList=OS01G0100100&fl=_id%2Cxrefs%2Csystem_name",
        json=[
            {
                "_id": "OS01G0100100",
                "system_name": "oryza_sativa",
                "xrefs": [{"db": "Uniprot/SWISSPROT", "ids": ["Q5VMS9"]}],
            }
        ],
    )

    # Phase 2.b — stub BLAST. Real shape per blast._parse_hit_table + blast_sequence.
    async def fake_blast(client, sequence, **kw):
        return {
            "rid": "FAKE",
            "program": "blastp",
            "database": "swissprot",
            "status": "READY",
            "hitCount": 1,
            "hits": [
                {
                    "accession": "sp|Q5VMS9.1|Y_ORYSJ",
                    "description": "Hypothetical protein OS=Oryza sativa GN=Os01g0100100 PE=4 SV=1",
                    "bit_score": 412.0,
                    "evalue": 1e-50,
                    "identity": "78%",
                }
            ],
            "raw_report_excerpt": "",
            "raw_report_truncated": False,
            "elapsed_seconds": 0.0,
        }

    monkeypatch.setattr("plant_genomics_mcp.synthesis.blast.blast_sequence", fake_blast)

    from plant_genomics_mcp.synthesis import consensus_homologs

    async with httpx.AsyncClient() as client:
        env = await consensus_homologs(client, "AT1G01010", top_n=10)

    assert env.tool == "consensus_homologs"
    statuses = [s.status for s in env.steps]
    # Steps: 1=uniprot, 2=fetch_sequence, 3=gramene_homologs, 4=blast, 5=gramene_homolog_enrichment
    assert statuses == ["ok", "ok", "ok", "ok", "ok"]
    assert env.result is not None
    # Step 2 envelope row carries metadata-only payload (not the raw sequence).
    assert env.steps[1].result == {"accession": "Q0WV96", "sequence_length": 24}
    consensus = env.result["consensus"]
    assert len(consensus) == 1
    pick = consensus[0]
    # BLAST returns Q5VMS9.1 (stripped of the .N version suffix); Gramene xref
    # returns Q5VMS9 — both collapse to the same canonical UniProt-accession key.
    assert pick["uniprot_accession"] == "Q5VMS9"
    assert pick["target_species"] == "oryza_sativa"
    assert pick["n_sources"] == 2
    assert set(pick["sources"]) == {"gramene", "blast"}
    # mean_identity = (1.0 + 0.78) / 2 = 0.89; score = 2 * 0.89 = 1.78
    assert pick["mean_identity"] == 0.89
    assert pick["score"] == 1.78


_PHASE1B_SKIP = "phase-1.b sequence fetch failed; downstream skipped"


@pytest.mark.asyncio
async def test_consensus_homologs_phase1b_plant_genomics_error_skips_downstream(monkeypatch):
    """P7: phase-1.b sequence fetch raising a PlantGenomicsError records an error
    step2 and skips steps 3-5 with the phase-1.b reason (synthesis.py:807-829).

    step1 (resolve) succeeds; only the FASTA fetch fails. These fail-loud error
    paths build a specific 5-step skip-everything envelope and were uncovered.
    """
    from plant_genomics_mcp.errors import NotFoundError
    from plant_genomics_mcp.synthesis import consensus_homologs

    async def fake_lookup(client, locus, organism="arabidopsis_thaliana"):
        return {"primaryAccession": "Q0WV96", "uniProtkbId": "Y_ARATH"}

    async def fake_fetch_sequence(client, accession):
        raise NotFoundError(f"UniProt FASTA: no sequence for {accession}")

    monkeypatch.setattr("plant_genomics_mcp.synthesis.uniprot.lookup_locus", fake_lookup)
    monkeypatch.setattr("plant_genomics_mcp.synthesis.uniprot.fetch_sequence", fake_fetch_sequence)

    async with httpx.AsyncClient() as client:
        env = await consensus_homologs(client, "AT1G01010", top_n=10)

    assert env.tool == "consensus_homologs"
    assert env.result is None
    assert [s.status for s in env.steps] == ["ok", "error", "skipped", "skipped", "skipped"]
    step2 = env.steps[1]
    assert step2.tool == "uniprot_fetch_sequence"
    assert step2.error.startswith("[NotFoundError]")
    # Steps 3-5 skipped with the phase-1.b reason and correct tool labels/order.
    assert [s.tool for s in env.steps[2:]] == [
        "gramene_homologs",
        "blast_sequence",
        "gramene_homolog_enrichment",
    ]
    for s in env.steps[2:]:
        assert s.error == _PHASE1B_SKIP


@pytest.mark.asyncio
async def test_consensus_homologs_phase1b_httpx_error_skips_downstream(monkeypatch):
    """P7: phase-1.b fetch raising a raw httpx.HTTPError takes the second except
    arm (synthesis.py:830-852); step2.error carries the [HTTPError] prefix."""
    from plant_genomics_mcp.synthesis import consensus_homologs

    async def fake_lookup(client, locus, organism="arabidopsis_thaliana"):
        return {"primaryAccession": "Q0WV96", "uniProtkbId": "Y_ARATH"}

    async def fake_fetch_sequence(client, accession):
        raise httpx.ConnectError("connection reset by peer")

    monkeypatch.setattr("plant_genomics_mcp.synthesis.uniprot.lookup_locus", fake_lookup)
    monkeypatch.setattr("plant_genomics_mcp.synthesis.uniprot.fetch_sequence", fake_fetch_sequence)

    async with httpx.AsyncClient() as client:
        env = await consensus_homologs(client, "AT1G01010", top_n=10)

    assert env.result is None
    assert [s.status for s in env.steps] == ["ok", "error", "skipped", "skipped", "skipped"]
    step2 = env.steps[1]
    assert step2.tool == "uniprot_fetch_sequence"
    assert step2.error.startswith("[HTTPError]")
    assert "connection reset by peer" in step2.error
    for s in env.steps[2:]:
        assert s.error == _PHASE1B_SKIP


def test_parse_blast_identity_pct_handles_percent_string_and_float():
    from plant_genomics_mcp.synthesis import _parse_blast_identity_pct

    assert _parse_blast_identity_pct("78%") == 0.78
    assert _parse_blast_identity_pct("78") == 0.78
    assert _parse_blast_identity_pct(78.5) == 0.785
    assert _parse_blast_identity_pct(0.785) == 0.785
    assert _parse_blast_identity_pct(None) is None
    assert _parse_blast_identity_pct("bad%") is None


def test_consensus_homologs_dedupe_groups_by_uniprot_accession():
    """Gramene xref (no .N suffix) and BLAST accession (with .N suffix) must
    collapse into a single 2-source consensus row keyed by UniProt accession.
    This is the v1.2.0 BREAKING dedup contract — replaces the v1.1 locus-
    token namespace that BLAST's RecName-format deflines could never match."""
    from plant_genomics_mcp.synthesis import _consensus_homologs_compose

    gramene = {
        "homologs": [
            {"target_locus": "OS01G0100100", "type": "ortholog_one2one", "gene_tree_id": "T1"}
        ]
    }
    xref_map = {
        "OS01G0100100": {"uniprot_acc": "Q5VMS9", "system_name": "oryza_sativa"},
    }
    blast = {
        "hits": [
            {
                "accession": "sp|Q5VMS9.1|Y_ORYSJ",
                "description": "X OS=Oryza sativa GN=Os01g0100100",
                "bit_score": 400.0,
                "evalue": 1e-50,
                "identity": "80%",
            }
        ]
    }
    out = _consensus_homologs_compose(gramene, blast, xref_map=xref_map, top_n=10)
    assert len(out) == 1
    assert out[0]["n_sources"] == 2
    assert out[0]["uniprot_accession"] == "Q5VMS9"
    assert out[0]["target_species"] == "oryza_sativa"


def test_consensus_homologs_scoring_prefers_two_source_hits():
    from plant_genomics_mcp.synthesis import _consensus_homologs_compose

    gramene = {
        "homologs": [
            {"target_locus": "OS01G0100001"},
            {"target_locus": "OS01G0100002"},
        ]
    }
    xref_map = {
        "OS01G0100001": {"uniprot_acc": "Q5VMS1", "system_name": "oryza_sativa"},
        "OS01G0100002": {"uniprot_acc": "Q5VMS2", "system_name": "oryza_sativa"},
    }
    blast = {
        "hits": [
            {
                "accession": "sp|Q5VMS2.1|Y_ORYSJ",
                "description": "matched ortholog",
                "bit_score": 100.0,
                "evalue": 1e-10,
                "identity": "50%",
            },
        ]
    }
    out = _consensus_homologs_compose(gramene, blast, xref_map=xref_map, top_n=10)
    assert out[0]["uniprot_accession"] == "Q5VMS2"
    assert out[0]["n_sources"] == 2
    assert out[0]["sources"] == ["gramene", "blast"]
    assert out[0]["mean_identity"] == 0.75
    assert out[0]["score"] == 1.5
    assert out[1]["n_sources"] == 1


def test_consensus_homologs_single_source_degenerates_gracefully():
    """Gramene-only (BLAST=None): enriched loci produce 1-source rows; loci
    with no UniProt xref are dropped (no accession ⇒ structurally unable to
    dedup with BLAST, so keeping them as singletons would just dilute the
    consensus). Coverage gap documented at envelope level, not per-locus."""
    from plant_genomics_mcp.synthesis import _consensus_homologs_compose

    out = _consensus_homologs_compose(
        gramene_payload={
            "homologs": [
                {"target_locus": "OS01G0100001"},
                {"target_locus": "OS01G0100002"},
                {"target_locus": "Cla97C03G067000"},  # no UniProt xref — dropped
            ]
        },
        blast_payload=None,
        xref_map={
            "OS01G0100001": {"uniprot_acc": "Q5VMS1", "system_name": "oryza_sativa"},
            "OS01G0100002": {"uniprot_acc": "Q5VMS2", "system_name": "oryza_sativa"},
            "Cla97C03G067000": {"uniprot_acc": None, "system_name": "citrullus_lanatus"},
        },
        top_n=10,
    )
    assert all(c["n_sources"] == 1 for c in out)
    assert all(c["target_species"] == "oryza_sativa" for c in out)
    assert {c["uniprot_accession"] for c in out} == {"Q5VMS1", "Q5VMS2"}


def test_consensus_homologs_blast_only_strips_version_suffix():
    """BLAST-only entry: accession parser strips the .N version suffix so a
    future cross-source join lands on the same canonical key. target_species
    stays None for BLAST-only — the NCBI SwissProt defline (``RecName: Full=``)
    carries no ``OS=`` token, and we don't pay for a per-hit UniProt lookup."""
    from plant_genomics_mcp.synthesis import _consensus_homologs_compose

    out = _consensus_homologs_compose(
        gramene_payload=None,
        blast_payload={
            "hits": [
                {
                    "accession": "sp|Q0WV96.2|NAC001_ARATH",
                    "description": "RecName: Full=NAC domain-containing protein 1",
                    "bit_score": 600.0,
                    "evalue": 1e-100,
                    "identity": "95%",
                }
            ]
        },
        xref_map={},
        top_n=10,
    )
    assert len(out) == 1
    assert out[0]["uniprot_accession"] == "Q0WV96"
    assert out[0]["target_species"] is None
    assert out[0]["n_sources"] == 1
    assert out[0]["sources"] == ["blast"]


def test_consensus_homologs_fixtures_match_real_response_shapes():
    """Boundary check: fixture shapes used by the happy-path test must validate
    against the live Pydantic wrappers (extra=forbid outer schemas)."""
    from plant_genomics_mcp.gramene import _normalize as _gramene_normalize
    from plant_genomics_mcp.models import BlastResult, GrameneHomologs

    gramene_homologs = [
        _gramene_normalize("ortholog_one2one", "OS01G0100100", "EPlGT00190000001"),
    ]
    GrameneHomologs.model_validate(
        {
            "locus": "AT1G01010",
            "release": "v69",
            "total": len(gramene_homologs),
            "homologs": gramene_homologs,
        }
    )

    BlastResult.model_validate(
        {
            "rid": "FAKE",
            "program": "blastp",
            "database": "swissprot",
            "status": "READY",
            "hitCount": 1,
            "hits": [
                {
                    "accession": "sp|Q5VMS9.1|Y_ORYSJ",
                    "description": "Hypothetical protein OS=Oryza sativa GN=Os01g0100100 PE=4 SV=1",
                    "bit_score": 412.0,
                    "evalue": 1e-50,
                    "identity": "78%",
                }
            ],
            "raw_report_excerpt": "",
            "raw_report_truncated": False,
            "elapsed_seconds": 0.0,
        }
    )


# ---------------------------------------------------------------------------
# v0.9 — organism resolver migration (T14)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_analyze_locus_synth_resolves_organism_alias(httpx_mock):
    """organism='thale cress' resolves to arabidopsis_thaliana; wire calls
    still target the canonical slug; envelope.input echoes the user form."""
    # Wire format uses the canonical Ensembl slug — the same mocks apply
    # whether the caller passes "thale cress" or "arabidopsis_thaliana".
    httpx_mock.add_response(
        url="https://rest.ensembl.org/lookup/id/AT1G01010?species=arabidopsis_thaliana&expand=0",
        json={
            "id": "AT1G01010",
            "species": "arabidopsis_thaliana",
            "biotype": "protein_coding",
            "display_name": "NAC001",
        },
    )
    httpx_mock.add_response(
        url="https://rest.ensembl.org/xrefs/id/AT1G01010?species=arabidopsis_thaliana",
        json=[{"dbname": "Uniprot_gn", "primary_id": "Q0WV96", "display_id": "NAC001"}],
    )
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
        env = await analyze_locus_synth(client, "AT1G01010", organism="thale cress")

    # envelope.input echoes the caller's literal form
    assert env.input == {"locus": "AT1G01010", "organism": "thale cress"}
    # Phase-1 ensembl call succeeded because the wire used the canonical slug
    assert env.steps[0].status == "ok"
    assert env.steps[0].tool == "ensembl_plants_lookup_locus"


def test_analyze_locus_synth_unknown_organism_root_fails():
    import asyncio

    import httpx as _httpx

    async def run():
        async with _httpx.AsyncClient() as client:
            from plant_genomics_mcp.synthesis import analyze_locus_synth

            return await analyze_locus_synth(client, "AT1G01010", organism="zucchini")

    envelope = asyncio.run(run())
    assert envelope.result is None
    assert envelope.steps[0].status == "error"
    assert "[OrganismNotFound]" in (envelope.steps[0].error or "")
    for step in envelope.steps[1:]:
        assert step.status == "skipped"
        assert "phase 1 failed" in (step.error or "")


def test_biological_context_synth_unknown_organism_root_fails():
    import asyncio

    import httpx as _httpx

    async def run():
        async with _httpx.AsyncClient() as client:
            from plant_genomics_mcp.synthesis import biological_context_synth

            return await biological_context_synth(client, "AT1G01010", organism="zucchini")

    envelope = asyncio.run(run())
    assert envelope.result is None
    assert envelope.steps[0].status == "error"
    assert "[OrganismNotFound]" in (envelope.steps[0].error or "")
    for step in envelope.steps[1:]:
        assert step.status == "skipped"


# ---------------------------------------------------------------------------
# gene_report — 5th synthesis tool (one-shot Markdown gene dossier)
# ---------------------------------------------------------------------------


def _gene_report_success_mocks(httpx_mock):
    """Register happy-path mocks for all seven gene_report backends."""
    # Phase 1a — Ensembl lookup (root)
    httpx_mock.add_response(
        url="https://rest.ensembl.org/lookup/id/AT1G01010?species=arabidopsis_thaliana&expand=0",
        json={
            "id": "AT1G01010",
            "species": "arabidopsis_thaliana",
            "biotype": "protein_coding",
            "display_name": "NAC001",
            "description": "NAC domain containing protein 1 [Source:NCBI gene;Acc:839580]",
            "seq_region_name": "1",
            "start": 3631,
            "end": 5899,
            "strand": 1,
            "assembly_name": "TAIR10",
        },
    )
    # Phase 1b — UniProt (needed for GO accession)
    httpx_mock.add_response(
        url=re.compile(r"^https://rest\.uniprot\.org/uniprotkb/search.*"),
        json={
            "results": [
                {
                    "primaryAccession": "Q0WV96",
                    "uniProtkbId": "NAC1_ARATH",
                    "entryType": "UniProtKB reviewed (Swiss-Prot)",
                    "proteinDescription": {
                        "recommendedName": {
                            "fullName": {"value": "NAC domain-containing protein 1"}
                        }
                    },
                    "genes": [{"geneName": {"value": "NAC001"}}],
                    "organism": {"scientificName": "Arabidopsis thaliana", "taxonId": 3702},
                    "sequence": {"length": 429},
                }
            ]
        },
    )
    # Phase 2 — xrefs
    httpx_mock.add_response(
        url="https://rest.ensembl.org/xrefs/id/AT1G01010?species=arabidopsis_thaliana",
        json=[
            {"dbname": "Uniprot_gn", "primary_id": "Q0WV96", "display_id": "NAC001"},
            {"dbname": "TAIR_LOCUS", "primary_id": "AT1G01010", "display_id": "AT1G01010"},
        ],
    )
    # Phase 2 — KEGG (link + per-pathway get)
    httpx_mock.add_response(
        url="https://rest.kegg.jp/link/pathway/ath:AT1G01010",
        text="ath:AT1G01010\tpath:ath00010\n",
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://rest\.kegg\.jp/get/path:ath.*"),
        text=(
            "ENTRY       ath00010                    Pathway\n"
            "NAME        Glycolysis / Gluconeogenesis - Arabidopsis thaliana\n"
            "CLASS       Metabolism; Carbohydrate metabolism\n"
        ),
        is_reusable=True,
    )
    # Phase 2 — STRING
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
    # Phase 2 — Europe PMC literature
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.ebi\.ac\.uk/europepmc/webservices/rest/search.*"),
        json={
            "hitCount": 40,
            "resultList": {
                "result": [
                    {
                        "pmid": "41152268",
                        "doi": "10.1038/s41526-025-00525-5",
                        "title": "Spaceflight transcriptome patterns in Arabidopsis.",
                        "authorString": "Seo D, Paul AL, Ferl RJ.",
                    }
                ]
            },
        },
    )
    # Phase 2 — QuickGO (keyed on the resolved UniProt accession)
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.ebi\.ac\.uk/QuickGO/services/annotation/search.*"),
        json={
            "numberOfHits": 1,
            "results": [
                {
                    "goId": "GO:0006355",
                    "goName": "regulation of DNA-templated transcription",
                    "goAspect": "biological_process",
                    "goEvidence": "IEA",
                    "qualifier": "involved_in",
                }
            ],
        },
    )


@pytest.mark.asyncio
async def test_gene_report_all_backends_succeed_returns_dossier(httpx_mock):
    _gene_report_success_mocks(httpx_mock)

    from plant_genomics_mcp.synthesis import gene_report

    async with httpx.AsyncClient() as client:
        env = await gene_report(client, "AT1G01010", organism="arabidopsis_thaliana")

    assert env.tool == "gene_report"
    assert env.input == {"locus": "AT1G01010", "organism": "arabidopsis_thaliana", "top_n": 10}
    assert [s.tool for s in env.steps] == [
        "ensembl_plants_lookup_locus",
        "resolve_locus_to_uniprot",
        "get_gene_xrefs",
        "kegg_pathways",
        "string_interactions",
        "locus_literature",
        "locus_go_annotations",
    ]
    assert [s.status for s in env.steps] == ["ok"] * 7
    assert env.result is not None
    assert env.result["locus"] == "AT1G01010"
    assert env.result["uniprot_accession"] == "Q0WV96"
    assert env.result["canonical_gene_name"] == "NAC001"

    # The headline deliverable: a rendered Markdown dossier that stitches
    # every section together.
    md = env.result["markdown"]
    assert isinstance(md, str)
    assert "NAC001" in md and "AT1G01010" in md  # header
    assert "Q0WV96" in md  # protein section
    assert "regulation of DNA-templated transcription" in md  # GO
    assert "Glycolysis" in md  # KEGG pathway
    assert "NAC3" in md  # STRING partner
    assert "Spaceflight transcriptome" in md  # literature

    # Structured mirror alongside the prose.
    sections = env.result["sections"]
    assert set(sections) >= {
        "annotation",
        "protein",
        "xrefs",
        "pathways",
        "interactions",
        "literature",
        "go_annotations",
    }
    assert sections["annotation"]["id"] == "AT1G01010"


@pytest.mark.asyncio
async def test_gene_report_phase1_ensembl_failure_skips_rest(httpx_mock):
    # Ensembl root 404 → whole dossier root-fails.
    httpx_mock.add_response(
        url="https://rest.ensembl.org/lookup/id/AT1G01010?species=arabidopsis_thaliana&expand=0",
        status_code=404,
    )
    # UniProt runs concurrently in phase 1; give it a response so only the
    # root (ensembl) drives the failure path. Empty results → lookup_locus
    # makes two search passes (reviewed → TrEMBL), so the mock is reusable.
    httpx_mock.add_response(
        url=re.compile(r"^https://rest\.uniprot\.org/uniprotkb/search.*"),
        json={"results": []},
        is_reusable=True,
    )

    from plant_genomics_mcp.synthesis import gene_report

    async with httpx.AsyncClient() as client:
        env = await gene_report(client, "AT1G01010", organism="arabidopsis_thaliana")

    assert env.result is None
    assert env.steps[0].tool == "ensembl_plants_lookup_locus"
    assert env.steps[0].status == "error"
    downstream = {s.tool: s for s in env.steps[2:]}
    for tool in (
        "get_gene_xrefs",
        "kegg_pathways",
        "string_interactions",
        "locus_literature",
        "locus_go_annotations",
    ):
        assert downstream[tool].status == "skipped"


@pytest.mark.asyncio
async def test_gene_report_uniprot_failure_skips_go_but_composes(httpx_mock):
    # Ensembl ok; UniProt returns no hits → NotFoundError. GO depends on the
    # UniProt accession, so it must be skipped, but the dossier still composes
    # from the remaining backends.
    httpx_mock.add_response(
        url="https://rest.ensembl.org/lookup/id/AT1G01010?species=arabidopsis_thaliana&expand=0",
        json={"id": "AT1G01010", "species": "arabidopsis_thaliana", "display_name": "NAC001"},
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://rest\.uniprot\.org/uniprotkb/search.*"),
        json={"results": []},
        is_reusable=True,
    )
    httpx_mock.add_response(
        url="https://rest.ensembl.org/xrefs/id/AT1G01010?species=arabidopsis_thaliana",
        json=[],
    )
    httpx_mock.add_response(
        url="https://rest.kegg.jp/link/pathway/ath:AT1G01010",
        text="",
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://string-db\.org/api/json/interaction_partners.*"),
        json=[],
    )
    httpx_mock.add_response(
        url=re.compile(r"^https://www\.ebi\.ac\.uk/europepmc/webservices/rest/search.*"),
        json={"hitCount": 0, "resultList": {"result": []}},
    )

    from plant_genomics_mcp.synthesis import gene_report

    async with httpx.AsyncClient() as client:
        env = await gene_report(client, "AT1G01010", organism="arabidopsis_thaliana")

    assert env.result is not None  # composes despite UniProt failure
    by_tool = {s.tool: s for s in env.steps}
    assert by_tool["resolve_locus_to_uniprot"].status == "error"
    assert by_tool["locus_go_annotations"].status == "skipped"
    # The Markdown still renders, noting the unavailable sections.
    md = env.result["markdown"]
    assert "AT1G01010" in md
    assert env.result["uniprot_accession"] is None


def test_gene_report_unknown_organism_root_fails():
    import asyncio

    import httpx as _httpx

    async def run():
        async with _httpx.AsyncClient() as client:
            from plant_genomics_mcp.synthesis import gene_report

            return await gene_report(client, "AT1G01010", organism="zucchini")

    envelope = asyncio.run(run())
    assert envelope.result is None
    assert envelope.steps[0].status == "error"
    assert "[OrganismNotFound]" in (envelope.steps[0].error or "")
    for step in envelope.steps[1:]:
        assert step.status == "skipped"


# ---------------------------------------------------------------------------
# Live tests — gated by PLANT_GENOMICS_MCP_LIVE=1
#
# Real-execution checks per feedback_real_execution_testing.md. These hit
# UniProt / Ensembl / Gramene / KEGG / STRING / ATTED-II / NCBI BLAST and
# are SKIPPED by default. Run locally with PLANT_GENOMICS_MCP_LIVE=1.
# ---------------------------------------------------------------------------
import os  # noqa: E402 — for PLANT_GENOMICS_MCP_LIVE gate (live-test section)

LIVE = os.environ.get("PLANT_GENOMICS_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="requires PLANT_GENOMICS_MCP_LIVE=1")


@live_only
@pytest.mark.asyncio
async def test_analyze_locus_synth_live_at1g01010():
    from plant_genomics_mcp.synthesis import analyze_locus_synth

    async with httpx.AsyncClient() as client:
        env = await analyze_locus_synth(client, "AT1G01010")
    assert env.result is not None
    assert env.result["reconciled"]["best_uniprot_accession"] == "Q0WV96"
    statuses = [s.status for s in env.steps]
    assert statuses[0] == "ok"
    # At least 3 of 4 phase-2 backends typically succeed; partial OK.
    ok_count = sum(1 for s in env.steps[1:] if s.status == "ok")
    assert ok_count >= 3, f"expected ≥3 phase-2 successes, got {ok_count}: {statuses}"


@live_only
@pytest.mark.asyncio
async def test_find_homologs_synth_live_at1g01010_seq():
    # AT1G01010 (Q0WV96) N-terminal — short enough for fast BLAST
    sequence = (
        "MEDQVGFGFRPNDEELVGHYLRNKIEGNTSRDVEVAISEVNICSYDPWNLRFQSKYKSRDA"
        "MWYFFSRRENNKGNRQSRTTVSGKWKLTGES"
    )
    from plant_genomics_mcp.synthesis import find_homologs_synth

    async with httpx.AsyncClient(timeout=900.0) as client:
        env = await find_homologs_synth(client, sequence, program="blastp", top_n=5)
    assert env.steps[0].status == "ok", f"BLAST failed: {env.steps[0].error}"
    # At least one self-hit with a UniProt record populated
    populated = [h for h in env.result["ranked_hits"] if h["uniprot_record"]]
    assert populated, "no ranked hit had a UniProt record"


@live_only
@pytest.mark.asyncio
async def test_biological_context_synth_live_at1g01010():
    from plant_genomics_mcp.synthesis import biological_context_synth

    async with httpx.AsyncClient() as client:
        env = await biological_context_synth(client, "AT1G01010", top_n=5)
    assert env.result is not None
    assert env.result["uniprot_accession"] == "Q0WV96"
    # consensus_partners might be empty if both STRING and ATTED return zero
    # neighbors for this locus, but the field must exist as a list.
    assert isinstance(env.result["consensus_partners"], list)


@live_only
@pytest.mark.asyncio
async def test_consensus_homologs_live_at1g01010():
    from plant_genomics_mcp.synthesis import consensus_homologs

    async with httpx.AsyncClient(timeout=900.0) as client:
        env = await consensus_homologs(client, "AT1G01010", top_n=5)
    assert env.result is not None
    consensus = env.result["consensus"]
    assert consensus, "consensus list empty — at least one cross-source pick expected"
    two_source = [c for c in consensus if c["n_sources"] == 2]
    # If BLAST and Gramene both returned hits, at least one should overlap
    # by (species, gene) — this is the dedup validation.
    if env.steps[2].status == "ok" and env.steps[3].status == "ok":
        assert two_source, "both backends succeeded but no 2-source consensus pick"


@live_only
@pytest.mark.asyncio
async def test_analyze_locus_synth_live_rice_os01g0100100():
    """Wave A5 (pre-1.0): real synthesis call against a non-Arabidopsis
    organism. Confirms the end-to-end multi-organism path — organism
    resolve → ensembl_plants → uniprot → europe_pmc → quickgo — all
    route correctly when organism='oryza_sativa' is threaded through.

    Rice locus Os01g0100100 is canonical (first protein-coding gene on
    chromosome 1, RAP-DB convention). Phase-1 always works (Ensembl Plants
    has rice); phase-2 backends may individually skip if the locus has no
    UniProt accession yet — we only require phase-1 ok and reconciled
    organism == 'oryza_sativa'.
    """
    from plant_genomics_mcp.synthesis import analyze_locus_synth

    async with httpx.AsyncClient() as client:
        env = await analyze_locus_synth(client, "Os01g0100100", organism="oryza_sativa")
    assert env.tool == "analyze_locus_synth"
    assert env.steps[0].status == "ok", (
        f"phase-1 ensembl lookup failed for rice: {env.steps[0].error}"
    )
    assert env.result is not None
    # Phase-1 envelope key is ``ensembl_record``; T8 wire-format adapter
    # rewrites species → organism on the returned dict.
    ensembl_record = env.result.get("ensembl_record") or {}
    assert ensembl_record.get("id") == "Os01g0100100"
    assert (
        ensembl_record.get("organism") == "oryza_sativa"
        or ensembl_record.get("species") == "oryza_sativa"
    ), f"expected oryza_sativa in ensembl_record, got {ensembl_record}"


@live_only
@pytest.mark.asyncio
async def test_gene_report_live_at1g01010():
    """Real-execution check: drive gene_report against every live upstream
    (Ensembl Plants, UniProt, Ensembl xrefs, KEGG, STRING, Europe PMC, QuickGO)
    and confirm the Markdown dossier composes end-to-end for the canonical NAC001
    locus. Individual phase-2 backends may occasionally degrade to an
    "Unavailable" note; we only require the root to resolve and the dossier to
    render with the gene identity present.
    """
    from plant_genomics_mcp.synthesis import gene_report

    async with httpx.AsyncClient(timeout=120.0) as client:
        env = await gene_report(client, "AT1G01010", top_n=5)

    assert env.tool == "gene_report"
    assert env.steps[0].status == "ok", f"phase-1 ensembl lookup failed: {env.steps[0].error}"
    assert env.result is not None
    assert env.result["locus"] == "AT1G01010"
    assert env.result["uniprot_accession"] == "Q0WV96"
    md = env.result["markdown"]
    assert isinstance(md, str) and md
    assert "AT1G01010" in md
    assert "## Protein" in md and "## Literature" in md
