"""Whole-envelope oracles for the five synthesis orchestrators.

The existing orchestrator tests assert a status list and a few result keys.
The nightly mutation run showed the gap that leaves: step numbers, skip
reasons, tool names on skip rows, backend call arguments (``program``,
``hitlist_size``, ``organism=``, ``limit=``), the sign of ``elapsed_s`` and
the ``None``-vs-float rule for it all mutated freely under those asserts.

Two oracles close it:

* ``shape(env)`` -- the envelope as a plain dict with every timing replaced by
  a marker (``"t"`` for a non-negative float, ``None`` kept as ``None``) and
  ``started_at`` checked against the ISO-8601 ``...Z`` form. Every failure
  path is asserted against a literal expected dict.
* ``Recorder`` -- a stand-in for each backend coroutine that records the exact
  positional and keyword arguments it was called with. Happy paths assert the
  full call list, so a dropped keyword or a wrong default is a diff.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx
import pytest

from plant_genomics_mcp import synthesis
from plant_genomics_mcp.errors import OrganismNotSupported, PlantGenomicsError
from plant_genomics_mcp.models import StepRow

_ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def shape(env) -> dict:
    d = env.model_dump()
    assert _ISO_Z.match(d["started_at"]), d["started_at"]
    d["started_at"] = "ISO"
    assert isinstance(d["elapsed_s"], float) and 0.0 <= d["elapsed_s"] < 60.0, d["elapsed_s"]
    d["elapsed_s"] = "t"
    for s in d["steps"]:
        if s["elapsed_s"] is not None:
            assert isinstance(s["elapsed_s"], float) and 0.0 <= s["elapsed_s"] < 60.0, s
            s["elapsed_s"] = "t"
    return d


def row(step, tool, status, *, elapsed, result=None, error=None) -> dict:
    return {
        "step": step,
        "tool": tool,
        "status": status,
        "elapsed_s": elapsed,
        "result": result,
        "error": error,
    }


def skipped(step, tool, reason) -> dict:
    return row(step, tool, "skipped", elapsed=None, error=reason)


class Recorder:
    """Async stand-in that records every call and returns/raises per call."""

    def __init__(self, *outcomes: Any):
        self.outcomes = list(outcomes)
        self.calls: list[tuple[tuple, dict]] = []

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        out = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(out, BaseException):
            raise out
        return out


CLIENT = object()  # identity-checked: the orchestrator must pass the client it was given
NF = "[OrganismNotFound] organism 'klingon_cabbage' not in registry; supported: "


def _nf_error(err: str) -> str:
    assert err.startswith(NF), err
    return err


# ---------------------------------------------------------------------------
# helpers: _timed_step / _gather_phase2 / _skipped / _bound_top_n
# ---------------------------------------------------------------------------


class _Weird(Exception):
    pass


async def _raise(exc):
    raise exc


async def _value(v):
    return v


@pytest.mark.asyncio
async def test_timed_step_table():
    ok = await synthesis._timed_step(3, "t", _value({"a": 1}))
    assert ok.model_dump() == {
        "step": 3,
        "tool": "t",
        "status": "ok",
        "elapsed_s": ok.elapsed_s,
        "result": {"a": 1},
        "error": None,
    }
    assert isinstance(ok.elapsed_s, float) and 0.0 <= ok.elapsed_s < 60.0

    ns = OrganismNotSupported(backend="kegg", organism="oryza_sativa", supported=["ath"])
    skip = await synthesis._timed_step(4, "k", _raise(ns))
    assert (skip.step, skip.tool, skip.status, skip.result) == (4, "k", "skipped", None)
    assert (
        skip.error
        == "[OrganismNotSupported] backend 'kegg' has no ID for 'oryza_sativa'; supported by 'kegg': ['ath']"
    )
    assert isinstance(skip.elapsed_s, float) and 0.0 <= skip.elapsed_s < 60.0

    pge = await synthesis._timed_step(5, "p", _raise(PlantGenomicsError("boom")))
    assert (pge.step, pge.tool, pge.status, pge.error, pge.result) == (
        5,
        "p",
        "error",
        "boom",
        None,
    )
    assert isinstance(pge.elapsed_s, float) and 0.0 <= pge.elapsed_s < 60.0

    net = await synthesis._timed_step(6, "n", _raise(httpx.ConnectTimeout("slow")))
    assert (net.step, net.tool, net.status, net.error) == (6, "n", "error", "[ConnectTimeout] slow")
    assert isinstance(net.elapsed_s, float) and 0.0 <= net.elapsed_s < 60.0

    odd = await synthesis._timed_step(7, "o", _raise(_Weird("bad projection")))
    assert (odd.step, odd.tool, odd.status, odd.error) == (
        7,
        "o",
        "error",
        "[_Weird] bad projection",
    )

    # BaseException that is not an Exception still propagates.
    with pytest.raises(asyncio.CancelledError):
        await synthesis._timed_step(8, "c", _raise(asyncio.CancelledError()))


@pytest.mark.asyncio
async def test_gather_phase2_keeps_input_order_and_times_each_slot():
    async def slow():
        await asyncio.sleep(0.01)
        return {"slow": True}

    rows = await synthesis._gather_phase2(
        [(9, "slow", slow()), (3, "err", _raise(PlantGenomicsError("x"))), (1, "fast", _value([]))]
    )
    dumped = [r.model_dump() for r in rows]
    for dr in dumped:
        assert isinstance(dr["elapsed_s"], float) and dr["elapsed_s"] >= 0.0, dr
        dr["elapsed_s"] = "t"
    assert (
        dumped[0]["elapsed_s"] == "t" and rows[0].elapsed_s >= 0.01
    )  # the slow slot really waited
    assert dumped == [
        row(9, "slow", "ok", elapsed="t", result={"slow": True}),
        row(3, "err", "error", elapsed="t", error="x"),
        row(1, "fast", "ok", elapsed="t", result=[]),
    ]
    assert await synthesis._gather_phase2([]) == []


def test_skipped_and_result_dict_contracts():
    assert synthesis._skipped(4, "t", "why").model_dump() == skipped(4, "t", "why")
    assert synthesis._result_dict(StepRow(step=1, tool="t", status="ok", result={"a": 1})) == {
        "a": 1
    }
    with pytest.raises(PlantGenomicsError) as ei:
        synthesis._result_dict(StepRow(step=6, tool="lit", status="ok", result=[1]))
    assert str(ei.value) == "synthesis: step 6 (lit) returned list, expected a dict-shaped record"


@pytest.mark.parametrize("n", [1, 2, 49, 50])
def test_bound_top_n_accepts_inclusive_range(n):
    assert synthesis._bound_top_n(n) == n


@pytest.mark.parametrize(
    ("n", "msg"),
    [
        (0, "top_n must be >=1, got 0"),
        (-3, "top_n must be >=1, got -3"),
        (51, "top_n 51 exceeds MAX_TOP_N=50"),
    ],
)
def test_bound_top_n_rejects_with_message(n, msg):
    with pytest.raises(ValueError) as ei:
        synthesis._bound_top_n(n)
    assert str(ei.value) == msg


def test_now_iso_is_utc_second_precision_z():
    s = synthesis._now_iso()
    assert _ISO_Z.match(s), s
    from datetime import UTC, datetime

    parsed = datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    assert abs((datetime.now(UTC) - parsed).total_seconds()) < 5


# ---------------------------------------------------------------------------
# _reconcile_analyze / _consensus_partners / _string_partner_locus
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ensembl", "uniprot", "xrefs", "expected"),
    [
        (
            None,
            None,
            None,
            {"canonical_gene_name": None, "best_uniprot_accession": None, "conflict_flags": []},
        ),
        (
            {"display_name": "NAC001"},
            None,
            None,
            {"canonical_gene_name": "NAC001", "best_uniprot_accession": None, "conflict_flags": []},
        ),
        # no ensembl name -> first uniprot gene name
        (
            {},
            {"geneNames": ["ANAC001", "NAC001"], "primaryAccession": "Q0WV96"},
            None,
            {
                "canonical_gene_name": "ANAC001",
                "best_uniprot_accession": "Q0WV96",
                "conflict_flags": [],
            },
        ),
        # ensembl name present in uniprot names: no conflict
        (
            {"display_name": "NAC001"},
            {"geneNames": ["ANAC001", "NAC001"], "primaryAccession": "Q0WV96"},
            None,
            {
                "canonical_gene_name": "NAC001",
                "best_uniprot_accession": "Q0WV96",
                "conflict_flags": [],
            },
        ),
        # ensembl name absent from uniprot names: conflict
        (
            {"display_name": "OTHER"},
            {"geneNames": ["NAC001"], "primaryAccession": "Q0WV96"},
            None,
            {
                "canonical_gene_name": "OTHER",
                "best_uniprot_accession": "Q0WV96",
                "conflict_flags": ["gene_name_mismatch"],
            },
        ),
        # empty uniprot names: no mismatch possible
        (
            {"display_name": "OTHER"},
            {"geneNames": [], "primaryAccession": "Q0WV96"},
            None,
            {
                "canonical_gene_name": "OTHER",
                "best_uniprot_accession": "Q0WV96",
                "conflict_flags": [],
            },
        ),
        # xref agrees
        (
            {"display_name": "N"},
            {"geneNames": ["N"], "primaryAccession": "Q0WV96"},
            {"by_db": {"Uniprot_gn": ["Q0WV96", "P1"]}},
            {"canonical_gene_name": "N", "best_uniprot_accession": "Q0WV96", "conflict_flags": []},
        ),
        # xref disagrees
        (
            {"display_name": "N"},
            {"geneNames": ["N"], "primaryAccession": "Q0WV96"},
            {"by_db": {"Uniprot_gn": ["P1"]}},
            {
                "canonical_gene_name": "N",
                "best_uniprot_accession": "Q0WV96",
                "conflict_flags": ["uniprot_xref_disagreement"],
            },
        ),
        # xref has no Uniprot_gn / no by_db: nothing to disagree with
        (
            {"display_name": "N"},
            {"geneNames": ["N"], "primaryAccession": "Q0WV96"},
            {"by_db": {"TAIR": ["x"]}},
            {"canonical_gene_name": "N", "best_uniprot_accession": "Q0WV96", "conflict_flags": []},
        ),
        (
            {"display_name": "N"},
            {"geneNames": ["N"], "primaryAccession": "Q0WV96"},
            {"by_db": None},
            {"canonical_gene_name": "N", "best_uniprot_accession": "Q0WV96", "conflict_flags": []},
        ),
        # both conflicts at once, in this order
        (
            {"display_name": "OTHER"},
            {"geneNames": ["N"], "primaryAccession": "Q0WV96"},
            {"by_db": {"Uniprot_gn": ["P1"]}},
            {
                "canonical_gene_name": "OTHER",
                "best_uniprot_accession": "Q0WV96",
                "conflict_flags": ["gene_name_mismatch", "uniprot_xref_disagreement"],
            },
        ),
        # no accession: xref check cannot fire even when xrefs disagree with nothing
        (
            {"display_name": "N"},
            {"geneNames": ["N"], "primaryAccession": ""},
            {"by_db": {"Uniprot_gn": ["P1"]}},
            {"canonical_gene_name": "N", "best_uniprot_accession": None, "conflict_flags": []},
        ),
    ],
)
def test_reconcile_analyze_table(ensembl, uniprot, xrefs, expected):
    assert synthesis._reconcile_analyze(ensembl, uniprot, xrefs) == expected


@pytest.mark.parametrize(
    ("sid", "expected"),
    [
        (None, None),
        ("", None),
        ("3702.AT3G15500.1", "AT3G15500"),
        ("3702.AT3G15500", "AT3G15500"),
        ("3702.AT3G15500.1.extra", "AT3G15500"),  # maxsplit=2: third piece is the tail
        ("AT3G15500.1", "AT3G15500.1"),  # no numeric taxid prefix: unchanged
        ("abc.def", "abc.def"),
        ("3702", "3702"),  # nothing after the taxid: unchanged
    ],
)
def test_string_partner_locus_table(sid, expected):
    assert synthesis._string_partner_locus(sid) == expected


def test_consensus_partners_full_contract():
    string_payload = {
        "partners": [
            {"string_id": "3702.AT1G00001.1", "score": 0.9},
            {"string_id": "3702.AT1G00002.1", "score": 0.4},
            {"string_id": "3702.AT1G00001.2", "score": 0.1},  # same locus twice: first wins
            {"string_id": "", "score": 0.99},  # no locus: skipped, later rows still read
            {"string_id": "3702.AT1G00004.1", "score": None},  # None score -> 0.0
        ]
    }
    atted_payload = {
        "neighbors": [
            {"locus": "AT1G00002", "score": 3.0},  # joins string: (0.4 + 0.75) / 2
            {"locus": "AT1G00003", "score": 1.0},  # 0.5
            {"locus": "AT1G00005", "score": 0.0},  # 0 -> 0.0, still a row
            {"locus": "AT1G00006", "score": -2.0},  # negative -> 0.0
            {"locus": None, "score": 9.0},  # no locus: skipped, later rows still read
            {"locus": "AT1G00007", "score": 1.0},
        ]
    }
    out = synthesis._consensus_partners(string_payload, atted_payload, top_n=10)
    assert out == [
        {
            "target_locus": "AT1G00002",
            "n_sources": 2,
            "combined_score": 0.575,
            "sources": ["string", "atted"],
        },
        {"target_locus": "AT1G00001", "n_sources": 1, "combined_score": 0.9, "sources": ["string"]},
        {"target_locus": "AT1G00003", "n_sources": 1, "combined_score": 0.5, "sources": ["atted"]},
        {"target_locus": "AT1G00007", "n_sources": 1, "combined_score": 0.5, "sources": ["atted"]},
        {"target_locus": "AT1G00004", "n_sources": 1, "combined_score": 0.0, "sources": ["string"]},
        {"target_locus": "AT1G00005", "n_sources": 1, "combined_score": 0.0, "sources": ["atted"]},
        {"target_locus": "AT1G00006", "n_sources": 1, "combined_score": 0.0, "sources": ["atted"]},
    ]
    assert [
        r["target_locus"]
        for r in synthesis._consensus_partners(string_payload, atted_payload, top_n=2)
    ] == [
        "AT1G00002",
        "AT1G00001",
    ]
    assert synthesis._consensus_partners(None, None, top_n=5) == []
    # rounding is to 4 places: 2 -> 2/3
    only = synthesis._consensus_partners(
        None, {"neighbors": [{"locus": "L", "score": 2.0}]}, top_n=5
    )
    assert only == [
        {"target_locus": "L", "n_sources": 1, "combined_score": 0.6667, "sources": ["atted"]}
    ]


# ---------------------------------------------------------------------------
# analyze_locus_synth
# ---------------------------------------------------------------------------

ANALYZE_ORGANISM_NF_STEPS = [
    "ensembl_plants_lookup_locus",
    ("resolve_locus_to_uniprot", "phase 1 failed; resolve_locus_to_uniprot skipped"),
    ("get_gene_xrefs", "phase 1 failed; get_gene_xrefs skipped"),
    ("locus_literature", "phase 1 failed; locus_literature skipped"),
    ("locus_go_annotations", "phase 1 failed; locus_go_annotations skipped"),
]


@pytest.mark.asyncio
async def test_analyze_locus_synth_unknown_organism_envelope_exact(monkeypatch):
    ens = Recorder()
    monkeypatch.setattr(synthesis.ensembl_plants, "lookup_locus", ens)
    env = await synthesis.analyze_locus_synth(CLIENT, "AT1G01010", organism="klingon_cabbage")
    d = shape(env)
    _nf_error(d["steps"][0]["error"])
    d["steps"][0]["error"] = "NF"
    assert d == {
        "tool": "analyze_locus_synth",
        "input": {"locus": "AT1G01010", "organism": "klingon_cabbage"},
        "started_at": "ISO",
        "elapsed_s": "t",
        "steps": [
            row(1, "ensembl_plants_lookup_locus", "error", elapsed=None, error="NF"),
            skipped(
                2, "resolve_locus_to_uniprot", "phase 1 failed; resolve_locus_to_uniprot skipped"
            ),
            skipped(3, "get_gene_xrefs", "phase 1 failed; get_gene_xrefs skipped"),
            skipped(4, "locus_literature", "phase 1 failed; locus_literature skipped"),
            skipped(5, "locus_go_annotations", "phase 1 failed; locus_go_annotations skipped"),
        ],
        "result": None,
    }
    assert ens.calls == []  # nothing fires on an unknown organism


@pytest.mark.asyncio
async def test_analyze_locus_synth_ensembl_failure_envelope_exact(monkeypatch):
    ens = Recorder(PlantGenomicsError("[NotFoundError] no such locus"))
    uni = Recorder({"primaryAccession": "Q0WV96", "geneNames": ["NAC001"]})
    xr = Recorder()
    monkeypatch.setattr(synthesis.ensembl_plants, "lookup_locus", ens)
    monkeypatch.setattr(synthesis.uniprot, "lookup_locus", uni)
    monkeypatch.setattr(synthesis.ensembl_plants, "lookup_xrefs", xr)
    env = await synthesis.analyze_locus_synth(CLIENT, "AT1G01010", organism="rice")
    assert shape(env) == {
        "tool": "analyze_locus_synth",
        "input": {"locus": "AT1G01010", "organism": "rice"},
        "started_at": "ISO",
        "elapsed_s": "t",
        "steps": [
            row(
                1,
                "ensembl_plants_lookup_locus",
                "error",
                elapsed="t",
                error="[NotFoundError] no such locus",
            ),
            row(
                2,
                "resolve_locus_to_uniprot",
                "ok",
                elapsed="t",
                result={"primaryAccession": "Q0WV96", "geneNames": ["NAC001"]},
            ),
            skipped(3, "get_gene_xrefs", "phase-1 ensembl lookup failed; skipped"),
            skipped(4, "locus_literature", "phase-1 ensembl lookup failed; skipped"),
            skipped(5, "locus_go_annotations", "phase-1 ensembl lookup failed; skipped"),
        ],
        "result": None,
    }
    # phase 1 fired both, with the organism the caller gave (not the resolved slug)
    assert ens.calls == [((CLIENT, "AT1G01010"), {"organism": "rice"})]
    assert uni.calls == [((CLIENT, "AT1G01010"), {"organism": "rice"})]
    assert xr.calls == []


@pytest.mark.asyncio
async def test_analyze_locus_synth_happy_path_calls_and_envelope_exact(monkeypatch):
    ens = Recorder({"id": "AT1G01010", "display_name": "NAC001"})
    uni = Recorder({"primaryAccession": "Q0WV96", "geneNames": ["NAC001"]})
    xr = Recorder({"by_db": {"Uniprot_gn": ["Q0WV96"]}})
    lit = Recorder([{"pmid": "1"}])
    go = Recorder({"annotations": []})
    monkeypatch.setattr(synthesis.ensembl_plants, "lookup_locus", ens)
    monkeypatch.setattr(synthesis.uniprot, "lookup_locus", uni)
    monkeypatch.setattr(synthesis.ensembl_plants, "lookup_xrefs", xr)
    monkeypatch.setattr(synthesis.europe_pmc, "lookup_locus", lit)
    monkeypatch.setattr(synthesis.quickgo, "lookup_by_uniprot", go)
    env = await synthesis.analyze_locus_synth(CLIENT, "AT1G01010")
    assert shape(env) == {
        "tool": "analyze_locus_synth",
        "input": {"locus": "AT1G01010", "organism": "arabidopsis_thaliana"},
        "started_at": "ISO",
        "elapsed_s": "t",
        "steps": [
            row(
                1,
                "ensembl_plants_lookup_locus",
                "ok",
                elapsed="t",
                result={"id": "AT1G01010", "display_name": "NAC001"},
            ),
            row(
                2,
                "resolve_locus_to_uniprot",
                "ok",
                elapsed="t",
                result={"primaryAccession": "Q0WV96", "geneNames": ["NAC001"]},
            ),
            row(
                3,
                "get_gene_xrefs",
                "ok",
                elapsed="t",
                result={"by_db": {"Uniprot_gn": ["Q0WV96"]}},
            ),
            row(4, "locus_literature", "ok", elapsed="t", result=[{"pmid": "1"}]),
            row(5, "locus_go_annotations", "ok", elapsed="t", result={"annotations": []}),
        ],
        "result": {
            "ensembl_record": {"id": "AT1G01010", "display_name": "NAC001"},
            "xrefs": {"by_db": {"Uniprot_gn": ["Q0WV96"]}},
            "uniprot_record": {"primaryAccession": "Q0WV96", "geneNames": ["NAC001"]},
            "literature": [{"pmid": "1"}],
            "go_annotations": {"annotations": []},
            "reconciled": {
                "canonical_gene_name": "NAC001",
                "best_uniprot_accession": "Q0WV96",
                "conflict_flags": [],
            },
        },
    }
    assert ens.calls == [((CLIENT, "AT1G01010"), {"organism": "arabidopsis_thaliana"})]
    assert uni.calls == [((CLIENT, "AT1G01010"), {"organism": "arabidopsis_thaliana"})]
    assert xr.calls == [((CLIENT, "AT1G01010"), {"organism": "arabidopsis_thaliana"})]
    assert lit.calls == [((CLIENT, "AT1G01010"), {"organism": "arabidopsis_thaliana"})]
    assert go.calls == [((CLIENT, "Q0WV96"), {})]


@pytest.mark.asyncio
async def test_analyze_locus_synth_uniprot_failure_skips_quickgo_and_keeps_order(monkeypatch):
    ens = Recorder({"id": "AT1G01010", "display_name": "NAC001"})
    uni = Recorder(httpx.ReadTimeout("slow"))
    xr = Recorder({"by_db": {}})
    lit = Recorder([])
    go = Recorder({"annotations": []})
    monkeypatch.setattr(synthesis.ensembl_plants, "lookup_locus", ens)
    monkeypatch.setattr(synthesis.uniprot, "lookup_locus", uni)
    monkeypatch.setattr(synthesis.ensembl_plants, "lookup_xrefs", xr)
    monkeypatch.setattr(synthesis.europe_pmc, "lookup_locus", lit)
    monkeypatch.setattr(synthesis.quickgo, "lookup_by_uniprot", go)
    env = await synthesis.analyze_locus_synth(CLIENT, "AT1G01010")
    d = shape(env)
    assert d["steps"] == [
        row(
            1,
            "ensembl_plants_lookup_locus",
            "ok",
            elapsed="t",
            result={"id": "AT1G01010", "display_name": "NAC001"},
        ),
        row(2, "resolve_locus_to_uniprot", "error", elapsed="t", error="[ReadTimeout] slow"),
        row(3, "get_gene_xrefs", "ok", elapsed="t", result={"by_db": {}}),
        row(4, "locus_literature", "ok", elapsed="t", result=[]),
        skipped(5, "locus_go_annotations", "phase-1 UniProt resolution failed; quickgo skipped"),
    ]
    assert d["result"] == {
        "ensembl_record": {"id": "AT1G01010", "display_name": "NAC001"},
        "xrefs": {"by_db": {}},
        "uniprot_record": None,
        "literature": [],
        "go_annotations": None,
        "reconciled": {
            "canonical_gene_name": "NAC001",
            "best_uniprot_accession": None,
            "conflict_flags": [],
        },
    }
    assert go.calls == []


# ---------------------------------------------------------------------------
# find_homologs_synth
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("program", ["blastn", "blastp", "blastx", "tblastn", "tblastx"])
@pytest.mark.asyncio
async def test_find_homologs_synth_passes_program_and_hitlist(monkeypatch, program):
    bl = Recorder(PlantGenomicsError("[UpstreamUnavailableError] blast down"))
    monkeypatch.setattr(synthesis.blast, "blast_sequence", bl)
    env = await synthesis.find_homologs_synth(CLIENT, "MEDQ", program=program, top_n=7)
    assert bl.calls == [((CLIENT, "MEDQ"), {"program": program, "hitlist_size": 7})]
    assert shape(env) == {
        "tool": "find_homologs_synth",
        "input": {"sequence_length": 4, "program": program, "top_n": 7},
        "started_at": "ISO",
        "elapsed_s": "t",
        "steps": [
            row(
                1,
                "blast_sequence",
                "error",
                elapsed="t",
                error="[UpstreamUnavailableError] blast down",
            ),
            skipped(2, "resolve_locus_to_uniprot", "phase-1 BLAST failed; subject lookup skipped"),
        ],
        "result": None,
    }


@pytest.mark.asyncio
async def test_find_homologs_synth_rejects_unknown_program_before_any_call(monkeypatch):
    bl = Recorder()
    monkeypatch.setattr(synthesis.blast, "blast_sequence", bl)
    with pytest.raises(ValueError) as ei:
        await synthesis.find_homologs_synth(CLIENT, "MEDQ", program="megablast")
    assert (
        str(ei.value)
        == "program 'megablast' not in ['blastn', 'blastp', 'blastx', 'tblastn', 'tblastx']"
    )
    with pytest.raises(ValueError):
        await synthesis.find_homologs_synth(CLIENT, "MEDQ", top_n=0)
    assert bl.calls == []


@pytest.mark.asyncio
async def test_find_homologs_synth_happy_path_dedups_lookup_and_ranks_from_one(monkeypatch):
    hits = [
        {"accession": "sp|Q0WV96.1|A"},
        {"accession": "AT1G01010.1"},  # not UniProt-shaped
        {"accession": "sp|Q0WV96.2|A"},  # same canonical accession: one lookup
        {"accession": "tr|P12345|B"},
        {"accession": "sp|Q99999|C"},  # past top_n=4: dropped from ranking
    ]
    bl = Recorder(
        {"rid": "R", "program": "blastp", "database": "swissprot", "hitCount": 5, "hits": hits}
    )
    bt = Recorder(
        {
            "results": {
                "Q0WV96": {"primaryAccession": "Q0WV96"},
                "P12345": {"primaryAccession": "P12345"},
            }
        }
    )
    monkeypatch.setattr(synthesis.blast, "blast_sequence", bl)
    monkeypatch.setattr(synthesis.batch, "batch_resolve_locus_to_uniprot", bt)
    env = await synthesis.find_homologs_synth(CLIENT, "MEDQ", top_n=4)
    assert bt.calls == [((CLIENT, ["Q0WV96", "P12345"]), {})]
    d = shape(env)
    assert d["steps"] == [
        row(
            1,
            "blast_sequence",
            "ok",
            elapsed="t",
            result={
                "rid": "R",
                "program": "blastp",
                "database": "swissprot",
                "hitCount": 5,
                "hits": hits,
            },
        ),
        row(
            2,
            "resolve_locus_to_uniprot",
            "ok",
            elapsed="t",
            result={
                "results": {
                    "Q0WV96": {"primaryAccession": "Q0WV96"},
                    "P12345": {"primaryAccession": "P12345"},
                }
            },
        ),
    ]
    assert d["result"] == {
        "blast": {"rid": "R", "program": "blastp", "database": "swissprot", "hit_count": 5},
        "ranked_hits": [
            {"rank": 1, "blast_hit": hits[0], "uniprot_record": {"primaryAccession": "Q0WV96"}},
            {"rank": 2, "blast_hit": hits[1], "uniprot_record": None},
            {"rank": 3, "blast_hit": hits[2], "uniprot_record": {"primaryAccession": "Q0WV96"}},
            {"rank": 4, "blast_hit": hits[3], "uniprot_record": {"primaryAccession": "P12345"}},
        ],
        "notes": ["non_uniprot_subject"],
    }


@pytest.mark.asyncio
async def test_find_homologs_synth_no_uniprot_subjects_skips_lookup_with_reason(monkeypatch):
    hits = [{"accession": "AT1G01010.1"}, {"accession": "Os01g0100100"}]
    bl = Recorder({"rid": "R", "program": "blastn", "database": "nt", "hitCount": 2, "hits": hits})
    bt = Recorder()
    monkeypatch.setattr(synthesis.blast, "blast_sequence", bl)
    monkeypatch.setattr(synthesis.batch, "batch_resolve_locus_to_uniprot", bt)
    env = await synthesis.find_homologs_synth(CLIENT, "ACGT", program="blastn")
    d = shape(env)
    assert d["steps"][1] == skipped(
        2, "resolve_locus_to_uniprot", "no UniProt-shaped subjects in BLAST hits"
    )
    assert d["result"]["ranked_hits"] == [
        {"rank": 1, "blast_hit": hits[0], "uniprot_record": None},
        {"rank": 2, "blast_hit": hits[1], "uniprot_record": None},
    ]
    assert d["result"]["notes"] == ["non_uniprot_subject"]
    assert bt.calls == []


@pytest.mark.asyncio
async def test_find_homologs_synth_lookup_failure_leaves_records_none(monkeypatch):
    hits = [{"accession": "sp|Q0WV96|A"}]
    bl = Recorder(
        {"rid": "R", "program": "blastp", "database": "swissprot", "hitCount": 1, "hits": hits}
    )
    bt = Recorder(httpx.ConnectError("down"))
    monkeypatch.setattr(synthesis.blast, "blast_sequence", bl)
    monkeypatch.setattr(synthesis.batch, "batch_resolve_locus_to_uniprot", bt)
    env = await synthesis.find_homologs_synth(CLIENT, "MEDQ")
    d = shape(env)
    assert d["steps"][1] == row(
        2, "resolve_locus_to_uniprot", "error", elapsed="t", error="[ConnectError] down"
    )
    assert d["result"]["ranked_hits"] == [{"rank": 1, "blast_hit": hits[0], "uniprot_record": None}]
    assert d["result"]["notes"] == []


# ---------------------------------------------------------------------------
# biological_context_synth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_biological_context_synth_unknown_organism_envelope_exact(monkeypatch):
    uni = Recorder()
    monkeypatch.setattr(synthesis.uniprot, "lookup_locus", uni)
    env = await synthesis.biological_context_synth(CLIENT, "X", organism="klingon_cabbage", top_n=3)
    d = shape(env)
    _nf_error(d["steps"][0]["error"])
    d["steps"][0]["error"] = "NF"
    assert d == {
        "tool": "biological_context_synth",
        "input": {"locus": "X", "organism": "klingon_cabbage", "top_n": 3},
        "started_at": "ISO",
        "elapsed_s": "t",
        "steps": [
            row(1, "resolve_locus_to_uniprot", "error", elapsed=None, error="NF"),
            skipped(2, "gramene_homologs", "phase 1 failed; gramene_homologs skipped"),
            skipped(3, "kegg_pathways", "phase 1 failed; kegg_pathways skipped"),
            skipped(4, "string_interactions", "phase 1 failed; string_interactions skipped"),
            skipped(5, "atted_coexpression", "phase 1 failed; atted_coexpression skipped"),
        ],
        "result": None,
    }
    assert uni.calls == []


@pytest.mark.asyncio
async def test_biological_context_synth_uniprot_failure_envelope_exact(monkeypatch):
    uni = Recorder(PlantGenomicsError("[NotFoundError] nope"))
    gr = Recorder()
    monkeypatch.setattr(synthesis.uniprot, "lookup_locus", uni)
    monkeypatch.setattr(synthesis.gramene, "lookup_homologs", gr)
    env = await synthesis.biological_context_synth(CLIENT, "AT1G01010", organism="rice")
    reason = "phase-1 UniProt resolution failed; downstream calls skipped"
    assert shape(env) == {
        "tool": "biological_context_synth",
        "input": {"locus": "AT1G01010", "organism": "rice", "top_n": 10},
        "started_at": "ISO",
        "elapsed_s": "t",
        "steps": [
            row(1, "resolve_locus_to_uniprot", "error", elapsed="t", error="[NotFoundError] nope"),
            skipped(2, "gramene_homologs", reason),
            skipped(3, "kegg_pathways", reason),
            skipped(4, "string_interactions", reason),
            skipped(5, "atted_coexpression", reason),
        ],
        "result": None,
    }
    assert uni.calls == [((CLIENT, "AT1G01010"), {"organism": "rice"})]
    assert gr.calls == []


@pytest.mark.asyncio
async def test_biological_context_synth_happy_path_calls_and_envelope_exact(monkeypatch):
    uni = Recorder({"primaryAccession": "Q0WV96"})
    gr = Recorder({"homologs": []})
    kg = Recorder(
        OrganismNotSupported(
            backend="kegg", organism="oryza_sativa", supported=["arabidopsis_thaliana"]
        )
    )
    st = Recorder({"partners": [{"string_id": "3702.AT3G15500.1", "score": 0.8}]})
    at = Recorder({"neighbors": [{"locus": "AT3G15500", "score": 3.0, "z_score": 3.0}]})
    monkeypatch.setattr(synthesis.uniprot, "lookup_locus", uni)
    monkeypatch.setattr(synthesis.gramene, "lookup_homologs", gr)
    monkeypatch.setattr(synthesis.kegg, "lookup_pathways", kg)
    monkeypatch.setattr(synthesis.string_db, "lookup_partners", st)
    monkeypatch.setattr(synthesis.atted, "lookup_coexpression", at)
    env = await synthesis.biological_context_synth(CLIENT, "AT1G01010", organism="rice", top_n=5)
    assert uni.calls == [((CLIENT, "AT1G01010"), {"organism": "rice"})]
    assert gr.calls == [((CLIENT, "AT1G01010"), {})]
    assert kg.calls == [((CLIENT, "AT1G01010"), {"organism": "rice"})]
    assert st.calls == [((CLIENT, "AT1G01010"), {"limit": 5, "organism": "rice"})]
    assert at.calls == [((CLIENT, "AT1G01010"), {"organism": "rice", "top_n": 5})]
    assert shape(env) == {
        "tool": "biological_context_synth",
        "input": {"locus": "AT1G01010", "organism": "rice", "top_n": 5},
        "started_at": "ISO",
        "elapsed_s": "t",
        "steps": [
            row(
                1,
                "resolve_locus_to_uniprot",
                "ok",
                elapsed="t",
                result={"primaryAccession": "Q0WV96"},
            ),
            row(2, "gramene_homologs", "ok", elapsed="t", result={"homologs": []}),
            row(
                3,
                "kegg_pathways",
                "skipped",
                elapsed="t",
                error="[OrganismNotSupported] backend 'kegg' has no ID for 'oryza_sativa'; supported by 'kegg': ['arabidopsis_thaliana']",
            ),
            row(
                4,
                "string_interactions",
                "ok",
                elapsed="t",
                result={"partners": [{"string_id": "3702.AT3G15500.1", "score": 0.8}]},
            ),
            row(
                5,
                "atted_coexpression",
                "ok",
                elapsed="t",
                result={"neighbors": [{"locus": "AT3G15500", "score": 3.0, "z_score": 3.0}]},
            ),
        ],
        "result": {
            "uniprot_accession": "Q0WV96",
            "homologs": {"homologs": []},
            "pathways": None,
            "string_partners": {"partners": [{"string_id": "3702.AT3G15500.1", "score": 0.8}]},
            "atted_coexpression": {
                "neighbors": [{"locus": "AT3G15500", "score": 3.0, "z_score": 3.0}]
            },
            "consensus_partners": [
                {
                    "target_locus": "AT3G15500",
                    "n_sources": 2,
                    "combined_score": 0.775,
                    "sources": ["string", "atted"],
                }
            ],
        },
    }


# ---------------------------------------------------------------------------
# gene_report
# ---------------------------------------------------------------------------

GR = synthesis._GENE_REPORT_STEPS


@pytest.mark.asyncio
async def test_gene_report_unknown_organism_envelope_exact(monkeypatch):
    ens = Recorder()
    monkeypatch.setattr(synthesis.ensembl_plants, "lookup_locus", ens)
    env = await synthesis.gene_report(CLIENT, "X", organism="klingon_cabbage", top_n=2)
    d = shape(env)
    _nf_error(d["steps"][0]["error"])
    d["steps"][0]["error"] = "NF"
    assert d == {
        "tool": "gene_report",
        "input": {"locus": "X", "organism": "klingon_cabbage", "top_n": 2},
        "started_at": "ISO",
        "elapsed_s": "t",
        "steps": [row(1, GR[0], "error", elapsed=None, error="NF")]
        + [skipped(i + 1, GR[i], "phase 1 failed; skipped") for i in range(1, 8)],
        "result": None,
    }
    assert len(d["steps"]) == 8 and ens.calls == []


@pytest.mark.asyncio
async def test_gene_report_no_resolver_envelope_exact(monkeypatch):
    # Both phase-1 resolvers fail → root failure. Ensembl failing alone is a
    # degraded dossier (#154; test_synthesis.py covers that path).
    ens = Recorder(httpx.ConnectTimeout("t"))
    uni = Recorder(httpx.ConnectTimeout("u"))
    xr = Recorder()
    monkeypatch.setattr(synthesis.ensembl_plants, "lookup_locus", ens)
    monkeypatch.setattr(synthesis.uniprot, "lookup_locus", uni)
    monkeypatch.setattr(synthesis.ensembl_plants, "lookup_xrefs", xr)
    env = await synthesis.gene_report(CLIENT, "AT1G01010")
    assert shape(env) == {
        "tool": "gene_report",
        "input": {"locus": "AT1G01010", "organism": "arabidopsis_thaliana", "top_n": 10},
        "started_at": "ISO",
        "elapsed_s": "t",
        "steps": [
            row(1, GR[0], "error", elapsed="t", error="[ConnectTimeout] t"),
            row(2, GR[1], "error", elapsed="t", error="[ConnectTimeout] u"),
        ]
        + [skipped(i + 1, GR[i], "phase-1 lookups both failed; skipped") for i in range(2, 8)],
        "result": None,
    }
    assert xr.calls == []


def _gene_report_backends(monkeypatch, *, uniprot_outcome, display_name="NAC001"):
    recs = {
        "ens": Recorder(
            {"id": "AT1G01010", "display_name": display_name, "biotype": "protein_coding"}
        ),
        "uni": Recorder(uniprot_outcome),
        "xr": Recorder({"xrefs": [{"dbname": "TAIR", "primary_id": "AT1G01010"}]}),
        "kg": Recorder({"pathways": []}),
        "st": Recorder({"partners": []}),
        "lit": Recorder({"hitCount": 0, "hits": []}),
        "go": Recorder({"annotations": []}),
        "ip": Recorder({"domains": []}),
    }
    monkeypatch.setattr(synthesis.ensembl_plants, "lookup_locus", recs["ens"])
    monkeypatch.setattr(synthesis.uniprot, "lookup_locus", recs["uni"])
    monkeypatch.setattr(synthesis.ensembl_plants, "lookup_xrefs", recs["xr"])
    monkeypatch.setattr(synthesis.kegg, "lookup_pathways", recs["kg"])
    monkeypatch.setattr(synthesis.string_db, "lookup_partners", recs["st"])
    monkeypatch.setattr(synthesis.europe_pmc, "lookup_locus", recs["lit"])
    monkeypatch.setattr(synthesis.quickgo, "lookup_by_uniprot", recs["go"])
    monkeypatch.setattr(synthesis.interpro, "lookup_by_uniprot", recs["ip"])
    return recs


@pytest.mark.asyncio
async def test_gene_report_happy_path_calls_steps_and_result_exact(monkeypatch):
    uni_rec = {"primaryAccession": "Q0WV96", "geneNames": ["ANAC001"], "uniProtkbId": "NAC1_ARATH"}
    recs = _gene_report_backends(monkeypatch, uniprot_outcome=uni_rec)
    env = await synthesis.gene_report(CLIENT, "AT1G01010", organism="rice", top_n=3)
    assert recs["ens"].calls == [((CLIENT, "AT1G01010"), {"organism": "rice"})]
    assert recs["uni"].calls == [((CLIENT, "AT1G01010"), {"organism": "rice"})]
    assert recs["xr"].calls == [((CLIENT, "AT1G01010"), {"organism": "rice"})]
    assert recs["kg"].calls == [((CLIENT, "AT1G01010"), {"organism": "rice"})]
    assert recs["st"].calls == [((CLIENT, "AT1G01010"), {"limit": 3, "organism": "rice"})]
    assert recs["lit"].calls == [((CLIENT, "AT1G01010"), {"organism": "rice"})]
    assert recs["go"].calls == [((CLIENT, "Q0WV96"), {})]
    assert recs["ip"].calls == [((CLIENT, "Q0WV96"), {})]
    d = shape(env)
    assert d["input"] == {"locus": "AT1G01010", "organism": "rice", "top_n": 3}
    # The steps are the audit trail; each payload lives once, under sections.
    assert d["steps"] == [row(i, GR[i - 1], "ok", elapsed="t") for i in range(1, 9)]
    res = d["result"]
    md = res.pop("markdown")
    assert res == {
        "locus": "AT1G01010",
        "organism": "oryza_sativa",
        "canonical_gene_name": "NAC001",  # ensembl display_name wins over UniProt geneNames
        "gene_names": {"canonical": "NAC001", "ensembl": "NAC001", "uniprot": ["ANAC001"]},
        "uniprot_accession": "Q0WV96",
        "sections": {
            "annotation": {
                "id": "AT1G01010",
                "display_name": "NAC001",
                "biotype": "protein_coding",
            },
            "protein": uni_rec,
            "xrefs": {"xrefs": [{"dbname": "TAIR", "primary_id": "AT1G01010"}]},
            "pathways": {"pathways": []},
            "interactions": {"partners": []},
            "literature": {"hitCount": 0, "hits": []},
            "go_annotations": {"annotations": []},
            "domains": {"domains": []},
        },
    }
    # the renderer got the locus, the resolved scientific name, the name and the cap
    assert md.startswith(
        "# NAC001 (UniProt: ANAC001) — `AT1G01010`\n\n*Oryza sativa* · protein_coding\n"
    )
    assert "## Interaction partners (STRING, top 3)" in md


@pytest.mark.asyncio
async def test_gene_report_uniprot_failure_skips_go_and_domains_with_reasons(monkeypatch):
    recs = _gene_report_backends(
        monkeypatch, uniprot_outcome=PlantGenomicsError("[NotFoundError] x"), display_name=None
    )
    env = await synthesis.gene_report(CLIENT, "AT1G01010")
    d = shape(env)
    assert d["steps"][1] == row(2, GR[1], "error", elapsed="t", error="[NotFoundError] x")
    assert d["steps"][6:] == [
        skipped(7, GR[6], "phase-1 UniProt resolution failed; quickgo skipped"),
        skipped(8, GR[7], "phase-1 UniProt resolution failed; interpro skipped"),
    ]
    assert recs["go"].calls == [] and recs["ip"].calls == []
    assert d["result"]["canonical_gene_name"] is None
    assert d["result"]["uniprot_accession"] is None
    assert d["result"]["sections"]["protein"] is None
    assert d["result"]["sections"]["go_annotations"] is None
    assert d["result"]["sections"]["domains"] is None


@pytest.mark.asyncio
async def test_gene_report_falls_back_to_first_uniprot_gene_name(monkeypatch):
    _gene_report_backends(
        monkeypatch,
        uniprot_outcome={"primaryAccession": "Q0WV96", "geneNames": ["ANAC001", "NAC001"]},
        display_name=None,
    )
    env = await synthesis.gene_report(CLIENT, "AT1G01010")
    assert env.result["canonical_gene_name"] == "ANAC001"
    assert env.result["markdown"].startswith("# ANAC001 — `AT1G01010`\n")
    _gene_report_backends(
        monkeypatch,
        uniprot_outcome={"primaryAccession": "Q0WV96", "geneNames": []},
        display_name="",
    )
    env = await synthesis.gene_report(CLIENT, "AT1G01010")
    assert env.result["canonical_gene_name"] is None
    assert env.result["markdown"].startswith("# AT1G01010 — `AT1G01010`\n")


# ---------------------------------------------------------------------------
# consensus_homologs
# ---------------------------------------------------------------------------

CH_TOOLS = [
    "resolve_locus_to_uniprot",
    "uniprot_fetch_sequence",
    "gramene_homologs",
    "blast_sequence",
    "gramene_homolog_enrichment",
]


def _ch_backends(monkeypatch, **outcomes):
    recs = {
        "uni": Recorder(outcomes.get("uni", {"primaryAccession": "Q0WV96"})),
        "seq": Recorder(outcomes.get("seq", "MEDQVGFGFRPNDEELVGHYLRNK")),
        "gr": Recorder(
            outcomes.get(
                "gr",
                {
                    "homologs": [
                        {"target_locus": "OS01G0100100"},
                        {"target_locus": ""},
                        {"target_locus": 7},
                    ]
                },
            )
        ),
        "bl": Recorder(
            outcomes.get("bl", {"hits": [{"accession": "sp|Q5VMS9.1|Y", "identity": "78%"}]})
        ),
        "en": Recorder(
            outcomes.get(
                "en", {"OS01G0100100": {"uniprot_acc": "Q5VMS9", "system_name": "oryza_sativa"}}
            )
        ),
    }
    monkeypatch.setattr(synthesis.uniprot, "lookup_locus", recs["uni"])
    monkeypatch.setattr(synthesis.uniprot, "fetch_sequence", recs["seq"])
    monkeypatch.setattr(synthesis.gramene, "lookup_homologs", recs["gr"])
    monkeypatch.setattr(synthesis.blast, "blast_sequence", recs["bl"])
    monkeypatch.setattr(synthesis.gramene, "fetch_homolog_enrichment_batch", recs["en"])
    return recs


@pytest.mark.asyncio
async def test_consensus_homologs_unknown_organism_envelope_exact(monkeypatch):
    recs = _ch_backends(monkeypatch)
    env = await synthesis.consensus_homologs(CLIENT, "X", organism="klingon_cabbage", top_n=4)
    d = shape(env)
    _nf_error(d["steps"][0]["error"])
    d["steps"][0]["error"] = "NF"
    assert d == {
        "tool": "consensus_homologs",
        "input": {"locus": "X", "organism": "klingon_cabbage", "top_n": 4},
        "started_at": "ISO",
        "elapsed_s": "t",
        "steps": [
            row(1, CH_TOOLS[0], "error", elapsed=None, error="NF"),
            skipped(2, CH_TOOLS[1], "phase 1 failed; uniprot_fetch_sequence skipped"),
            skipped(3, CH_TOOLS[2], "phase 1 failed; gramene_homologs skipped"),
            skipped(4, CH_TOOLS[3], "phase 1 failed; blast_sequence skipped"),
            skipped(5, CH_TOOLS[4], "phase 1 failed; gramene_homolog_enrichment skipped"),
        ],
        "result": None,
    }
    assert all(r.calls == [] for r in recs.values())


@pytest.mark.asyncio
async def test_consensus_homologs_uniprot_failure_envelope_exact(monkeypatch):
    recs = _ch_backends(monkeypatch, uni=httpx.ReadTimeout("slow"))
    env = await synthesis.consensus_homologs(CLIENT, "AT1G01010", organism="rice")
    skip = "phase-1 UniProt resolution failed; sequence + downstream skipped"
    assert shape(env) == {
        "tool": "consensus_homologs",
        "input": {"locus": "AT1G01010", "organism": "rice", "top_n": 10},
        "started_at": "ISO",
        "elapsed_s": "t",
        "steps": [
            row(1, CH_TOOLS[0], "error", elapsed="t", error="[ReadTimeout] slow"),
            skipped(2, CH_TOOLS[1], skip),
            skipped(3, CH_TOOLS[2], skip),
            skipped(4, CH_TOOLS[3], skip),
            skipped(5, CH_TOOLS[4], skip),
        ],
        "result": None,
    }
    assert recs["uni"].calls == [((CLIENT, "AT1G01010"), {"organism": "rice"})]
    assert recs["seq"].calls == []


@pytest.mark.parametrize(
    ("exc", "err"),
    [
        (PlantGenomicsError("[NotFoundError] no fasta"), "[NotFoundError] no fasta"),
        (httpx.ConnectError("down"), "[ConnectError] down"),
    ],
)
@pytest.mark.asyncio
async def test_consensus_homologs_sequence_failure_envelope_exact(monkeypatch, exc, err):
    recs = _ch_backends(monkeypatch, seq=exc)
    env = await synthesis.consensus_homologs(CLIENT, "AT1G01010")
    skip = "phase-1.b sequence fetch failed; downstream skipped"
    assert shape(env) == {
        "tool": "consensus_homologs",
        "input": {"locus": "AT1G01010", "organism": "arabidopsis_thaliana", "top_n": 10},
        "started_at": "ISO",
        "elapsed_s": "t",
        "steps": [
            row(1, CH_TOOLS[0], "ok", elapsed="t", result={"primaryAccession": "Q0WV96"}),
            row(2, CH_TOOLS[1], "error", elapsed="t", error=err),
            skipped(3, CH_TOOLS[2], skip),
            skipped(4, CH_TOOLS[3], skip),
            skipped(5, CH_TOOLS[4], skip),
        ],
        "result": None,
    }
    assert recs["seq"].calls == [((CLIENT, "Q0WV96"), {})]
    assert recs["gr"].calls == [] and recs["bl"].calls == []


@pytest.mark.asyncio
async def test_consensus_homologs_happy_path_calls_and_envelope_exact(monkeypatch):
    recs = _ch_backends(monkeypatch)
    env = await synthesis.consensus_homologs(CLIENT, "AT1G01010", top_n=3)
    assert recs["uni"].calls == [((CLIENT, "AT1G01010"), {"organism": "arabidopsis_thaliana"})]
    assert recs["seq"].calls == [((CLIENT, "Q0WV96"), {})]
    assert recs["gr"].calls == [((CLIENT, "AT1G01010"), {"homology_type": "all"})]
    # raw BLAST list is 50 regardless of top_n: paralogs dominate the top of the identity ranking
    assert recs["bl"].calls == [
        ((CLIENT, "MEDQVGFGFRPNDEELVGHYLRNK"), {"program": "blastp", "hitlist_size": 50})
    ]
    # only string, non-empty loci go to enrichment
    assert recs["en"].calls == [((CLIENT, ["OS01G0100100"]), {})]
    assert shape(env) == {
        "tool": "consensus_homologs",
        "input": {"locus": "AT1G01010", "organism": "arabidopsis_thaliana", "top_n": 3},
        "started_at": "ISO",
        "elapsed_s": "t",
        "steps": [
            row(1, CH_TOOLS[0], "ok", elapsed="t", result={"primaryAccession": "Q0WV96"}),
            row(
                2,
                CH_TOOLS[1],
                "ok",
                elapsed="t",
                result={"accession": "Q0WV96", "sequence_length": 24},
            ),
            row(
                3,
                CH_TOOLS[2],
                "ok",
                elapsed="t",
                result={
                    "homologs": [
                        {"target_locus": "OS01G0100100"},
                        {"target_locus": ""},
                        {"target_locus": 7},
                    ]
                },
            ),
            row(
                4,
                CH_TOOLS[3],
                "ok",
                elapsed="t",
                result={"hits": [{"accession": "sp|Q5VMS9.1|Y", "identity": "78%"}]},
            ),
            row(
                5,
                CH_TOOLS[4],
                "ok",
                elapsed="t",
                result={"OS01G0100100": {"uniprot_acc": "Q5VMS9", "system_name": "oryza_sativa"}},
            ),
        ],
        "result": {
            "uniprot_accession": "Q0WV96",
            "sequence_length": 24,
            "consensus": [
                {
                    "uniprot_accession": "Q5VMS9",
                    "target_species": "oryza_sativa",
                    "n_sources": 2,
                    "sources": ["gramene", "blast"],
                    "mean_identity": 0.89,
                    "score": 1.78,
                    "gramene_hit": {"target_locus": "OS01G0100100"},
                    "blast_hit": {"accession": "sp|Q5VMS9.1|Y", "identity": "78%"},
                }
            ],
        },
    }


@pytest.mark.asyncio
async def test_consensus_homologs_enrichment_skip_reasons(monkeypatch):
    # gramene ok but zero homologs -> enrichment skipped with the "0 homologs" reason
    recs = _ch_backends(monkeypatch, gr={"homologs": []})
    env = await synthesis.consensus_homologs(CLIENT, "AT1G01010")
    d = shape(env)
    assert d["steps"][4] == skipped(
        5, CH_TOOLS[4], "gramene_homologs returned 0 homologs; nothing to enrich"
    )
    assert recs["en"].calls == []
    assert d["result"]["consensus"] == [
        {
            "uniprot_accession": "Q5VMS9",
            "target_species": None,
            "n_sources": 1,
            "sources": ["blast"],
            "mean_identity": 0.78,
            "score": 0.78,
            "gramene_hit": None,
            "blast_hit": {"accession": "sp|Q5VMS9.1|Y", "identity": "78%"},
        }
    ]
    # gramene failed -> enrichment skipped with the "did not return ok" reason; blast-only consensus
    recs = _ch_backends(
        monkeypatch, gr=PlantGenomicsError("[UpstreamUnavailableError] gramene down")
    )
    env = await synthesis.consensus_homologs(CLIENT, "AT1G01010")
    d = shape(env)
    assert d["steps"][2] == row(
        3, CH_TOOLS[2], "error", elapsed="t", error="[UpstreamUnavailableError] gramene down"
    )
    assert d["steps"][4] == skipped(
        5, CH_TOOLS[4], "gramene_homologs phase did not return ok; enrichment skipped"
    )
    assert recs["en"].calls == []
    assert [c["sources"] for c in d["result"]["consensus"]] == [["blast"]]


@pytest.mark.asyncio
async def test_consensus_homologs_enrichment_failure_falls_back_to_no_xrefs(monkeypatch):
    recs = _ch_backends(monkeypatch, en=httpx.ReadTimeout("slow"))
    env = await synthesis.consensus_homologs(CLIENT, "AT1G01010")
    d = shape(env)
    assert d["steps"][4] == row(5, CH_TOOLS[4], "error", elapsed="t", error="[ReadTimeout] slow")
    assert recs["en"].calls == [((CLIENT, ["OS01G0100100"]), {})]
    # without the xref map the gramene homolog cannot join: blast-only row
    assert [(c["uniprot_accession"], c["sources"]) for c in d["result"]["consensus"]] == [
        ("Q5VMS9", ["blast"])
    ]
    # blast failed too: no consensus at all, but the envelope still composes
    _ch_backends(monkeypatch, bl=httpx.ReadTimeout("b"), en=httpx.ReadTimeout("e"))
    env = await synthesis.consensus_homologs(CLIENT, "AT1G01010")
    d = shape(env)
    assert d["steps"][3] == row(4, CH_TOOLS[3], "error", elapsed="t", error="[ReadTimeout] b")
    assert d["result"] == {"uniprot_accession": "Q0WV96", "sequence_length": 24, "consensus": []}


def test_consensus_reads_the_atted_score_whatever_index_the_release_uses():
    """Outside Arabidopsis every ATTED neighbour came back with z_score null
    (the release scores by LSmr), and consensus read z_score: each neighbour
    counted 0.0, pulling every combined score down. It reads `score` now."""
    rice = {"neighbors": [{"locus": "Os07g0438550", "score": 8.0, "z_score": None}]}
    out = synthesis._consensus_partners(None, rice, top_n=5)
    assert out == [
        {
            "target_locus": "Os07g0438550",
            "n_sources": 1,
            "combined_score": 0.8889,
            "sources": ["atted"],
        }
    ]
    # A neighbour with no numeric score is refused, not read as 0.
    for bad in (None, "8.0", True):
        with pytest.raises(PlantGenomicsError, match="carries no numeric score"):
            synthesis._consensus_partners(None, {"neighbors": [{"locus": "X", "score": bad}]}, 5)
