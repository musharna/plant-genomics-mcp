"""Synthesis tools — orchestrate backend calls and reconcile cross-source results.

Four MCP tools live here. Three mirror the existing prompt chains
(analyze_locus_synth, find_homologs_synth, biological_context_synth);
one is pure cross-source synthesis (consensus_homologs).

Each tool runs two phases:

1. **Phase 1.** Await the single root call (e.g. lookup_locus). If it errors,
   the envelope returns ``result=None`` and phase-2 rows carry status="skipped".
2. **Phase 2.** ``asyncio.gather(..., return_exceptions=True)`` over independent
   backends; failures land as status="error" rows but the rest of the envelope
   still composes from the successful rows.

No new HTTP, no new caches — the per-backend TTLCache layers already in place
handle repeated upstream calls.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from plant_genomics_mcp import (
    atted,
    batch,
    blast,
    ensembl_plants,
    europe_pmc,
    gramene,
    kegg,
    organisms,
    quickgo,
    string_db,
    uniprot,
)
from plant_genomics_mcp.errors import OrganismNotFound, OrganismNotSupported, PlantGenomicsError
from plant_genomics_mcp.models import StepRow, SynthesisEnvelope

DEFAULT_ORGANISM = organisms.DEFAULT_ORGANISM
DEFAULT_TOP_N = 10
MAX_TOP_N = 50  # matches batch.MAX_BATCH convention


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _bound_top_n(top_n: int) -> int:
    if top_n < 1:
        raise ValueError(f"top_n must be >=1, got {top_n}")
    if top_n > MAX_TOP_N:
        raise ValueError(f"top_n {top_n} exceeds MAX_TOP_N={MAX_TOP_N}")
    return top_n


async def _timed_step(step: int, tool: str, coro) -> StepRow:
    """Run ``coro`` and wrap its outcome in a StepRow.

    PlantGenomicsError subclasses → status="error" with the existing
    [ClassName] message wire format. Raw httpx network errors (timeout,
    connect, read) → status="error" with an explicit [ClassName] prefix
    so the wire format stays consistent. Other exceptions re-raise so
    the outer SDK handler still sees them.
    """
    started = time.perf_counter()
    try:
        result = await coro
    except OrganismNotSupported as e:
        # Backend explicitly has no ID for this organism — treat as skip,
        # not error, so the envelope composes around it.
        return StepRow(
            step=step,
            tool=tool,
            status="skipped",
            elapsed_s=time.perf_counter() - started,
            error=str(e),
        )
    except PlantGenomicsError as e:
        return StepRow(
            step=step,
            tool=tool,
            status="error",
            elapsed_s=time.perf_counter() - started,
            error=str(e),
        )
    except httpx.HTTPError as e:
        return StepRow(
            step=step,
            tool=tool,
            status="error",
            elapsed_s=time.perf_counter() - started,
            error=f"[{type(e).__name__}] {e}",
        )
    return StepRow(
        step=step,
        tool=tool,
        status="ok",
        elapsed_s=time.perf_counter() - started,
        result=result,
    )


def _gather_step(step: int, tool: str, outcome: Any, elapsed_s: float | None) -> StepRow:
    """Convert one slot of ``asyncio.gather(return_exceptions=True)`` into a StepRow.

    Used for phase-2 fanout — each coroutine's outcome lands here. Callers
    pass ``elapsed_s=None`` for gather rows: the gather wall time can't be
    honestly attributed per-coroutine, and SynthesisEnvelope.elapsed_s is the
    authoritative total. PlantGenomicsError → status="error" using its
    existing [ClassName] __str__. Raw httpx network errors → status="error"
    with explicit [ClassName] prefix. Other exceptions re-raise (caller wraps
    in try, or the gather machinery propagates).
    """
    if isinstance(outcome, OrganismNotSupported):
        # Same translation as _timed_step: phase-2 backends that don't
        # support the requested organism become "skipped", not "error".
        return StepRow(
            step=step, tool=tool, status="skipped", elapsed_s=elapsed_s, error=str(outcome)
        )
    if isinstance(outcome, PlantGenomicsError):
        return StepRow(
            step=step, tool=tool, status="error", elapsed_s=elapsed_s, error=str(outcome)
        )
    if isinstance(outcome, httpx.HTTPError):
        return StepRow(
            step=step,
            tool=tool,
            status="error",
            elapsed_s=elapsed_s,
            error=f"[{type(outcome).__name__}] {outcome}",
        )
    if isinstance(outcome, BaseException):
        raise outcome
    return StepRow(step=step, tool=tool, status="ok", elapsed_s=elapsed_s, result=outcome)


def _skipped(step: int, tool: str, reason: str) -> StepRow:
    # elapsed_s=None: a skip never actually ran the backend, so there's no
    # per-step wall time to report. SynthesisEnvelope.elapsed_s carries the
    # authoritative total for the whole orchestrator run.
    return StepRow(step=step, tool=tool, status="skipped", elapsed_s=None, error=reason)


def _result_dict(row: StepRow) -> dict[str, Any]:
    """Narrow an ``ok`` StepRow's ``result`` to the dict the backend contracts.

    ``StepRow.result`` is typed ``dict | list | None`` to cover every backend
    (some return bare lists, e.g. literature). The synthesis consumers below
    only ever index ok rows from dict-returning backends (ensembl/uniprot/blast/
    batch/gramene records). This both narrows the type for mypy and fails loud
    if a backend ever violates that contract, instead of silently doing
    ``.get`` on a list (AttributeError) or None deep in reconciliation.
    """
    payload = row.result
    if not isinstance(payload, dict):
        raise PlantGenomicsError(
            f"synthesis: step {row.step} ({row.tool}) returned "
            f"{type(payload).__name__}, expected a dict-shaped record"
        )
    return payload


async def _gather_phase2(
    items: list[tuple[int, str, Any]],
) -> list[StepRow]:
    """Run a list of (step, tool, coroutine) concurrently; return StepRows in input order.

    Phase-2 StepRows carry ``elapsed_s=None``. Per-coroutine attribution would
    require wrapping each await with its own ``perf_counter()``; the
    gather-aggregate is structurally misleading (every row reports the same
    total) and the orchestrator-level elapsed_s already captures the real
    wall time. Honest None > misleading aggregate.
    """
    raw = await asyncio.gather(*(c for _, _, c in items), return_exceptions=True)
    rows: list[StepRow] = []
    for (step, tool, _), outcome in zip(items, raw, strict=True):
        rows.append(_gather_step(step, tool, outcome, None))
    return rows


# ---------------------------------------------------------------------------
# 4.1 analyze_locus_synth
# ---------------------------------------------------------------------------


async def analyze_locus_synth(
    client: httpx.AsyncClient,
    locus: str,
    organism: str | int = DEFAULT_ORGANISM,
) -> SynthesisEnvelope:
    """Mirror the analyze_locus prompt as a single tool call.

    Phase 1: ensembl_plants.lookup_locus
    Phase 2 (gather): get_xrefs, uniprot.lookup_locus, europe_pmc, quickgo
    """
    started_at = _now_iso()
    t0 = time.perf_counter()
    input_args = {"locus": locus, "organism": organism}

    # Phase 0 — resolve the organism alias once; root-fail the whole envelope
    # if it doesn't exist so phase-1/phase-2 don't fire with a bogus slug.
    try:
        organisms.resolve(organism)
    except OrganismNotFound as exc:
        return SynthesisEnvelope(
            tool="analyze_locus_synth",
            input=input_args,
            started_at=started_at,
            elapsed_s=time.perf_counter() - t0,
            steps=[
                StepRow(
                    step=1,
                    tool="ensembl_plants_lookup_locus",
                    status="error",
                    elapsed_s=None,
                    error=str(exc),
                ),
                _skipped(
                    2,
                    "resolve_locus_to_uniprot",
                    "phase 1 failed; resolve_locus_to_uniprot skipped",
                ),
                _skipped(3, "get_gene_xrefs", "phase 1 failed; get_gene_xrefs skipped"),
                _skipped(4, "locus_literature", "phase 1 failed; locus_literature skipped"),
                _skipped(5, "locus_go_annotations", "phase 1 failed; locus_go_annotations skipped"),
            ],
            result=None,
        )

    # Phase 1 — root resolution: ensembl + uniprot in parallel.
    # UniProt is sequenced into phase 1 (not phase 2) because the QuickGO
    # call in phase 2 needs primaryAccession. Running ensembl and uniprot
    # concurrently keeps total latency at max(ensembl, uniprot).
    phase1 = await _gather_phase2(
        [
            (
                1,
                "ensembl_plants_lookup_locus",
                ensembl_plants.lookup_locus(client, locus, organism=organism),
            ),
            (
                2,
                "resolve_locus_to_uniprot",
                uniprot.lookup_locus(client, locus, organism=organism),
            ),
        ]
    )
    root, uniprot_row = phase1

    if root.status != "ok":
        # Ensembl is the entry point; without it the envelope can't reconcile.
        skipped = [
            _skipped(3, "get_gene_xrefs", "phase-1 ensembl lookup failed; skipped"),
            _skipped(4, "locus_literature", "phase-1 ensembl lookup failed; skipped"),
            _skipped(5, "locus_go_annotations", "phase-1 ensembl lookup failed; skipped"),
        ]
        return SynthesisEnvelope(
            tool="analyze_locus_synth",
            input=input_args,
            started_at=started_at,
            elapsed_s=time.perf_counter() - t0,
            steps=[root, uniprot_row, *skipped],
            result=None,
        )

    # Phase 2 — fan out the rest; quickgo only when uniprot resolved.
    phase2_items: list[tuple[int, str, Any]] = [
        (3, "get_gene_xrefs", ensembl_plants.lookup_xrefs(client, locus, organism=organism)),
        (4, "locus_literature", europe_pmc.lookup_locus(client, locus, organism=organism)),
    ]
    if uniprot_row.status == "ok":
        acc = _result_dict(uniprot_row)["primaryAccession"]
        phase2_items.append(
            (5, "locus_go_annotations", quickgo.lookup_by_uniprot(client, acc)),
        )

    p2 = await _gather_phase2(phase2_items)

    if uniprot_row.status != "ok":
        p2.append(
            _skipped(
                5,
                "locus_go_annotations",
                "phase-1 UniProt resolution failed; quickgo skipped",
            )
        )

    xrefs_row, lit_row, go_row = p2[0], p2[1], p2[2]

    # Compose result from ok rows only
    def _ok(row: StepRow) -> Any:
        return row.result if row.status == "ok" else None

    ensembl_record = _result_dict(root)
    reconciled = _reconcile_analyze(
        ensembl_record=ensembl_record,
        uniprot_record=_ok(uniprot_row),
        xrefs=_ok(xrefs_row),
    )

    return SynthesisEnvelope(
        tool="analyze_locus_synth",
        input=input_args,
        started_at=started_at,
        elapsed_s=time.perf_counter() - t0,
        steps=[root, uniprot_row, *p2],
        result={
            "ensembl_record": ensembl_record,
            "xrefs": _ok(xrefs_row),
            "uniprot_record": _ok(uniprot_row),
            "literature": _ok(lit_row),
            "go_annotations": _ok(go_row),
            "reconciled": reconciled,
        },
    )


def _reconcile_analyze(
    ensembl_record: dict | None,
    uniprot_record: dict | None,
    xrefs: dict | None,
) -> dict:
    """Cross-source name + accession reconciliation.

    Picks canonical_gene_name from ensembl.display_name first (curator-set),
    falls back to first uniprot gene name. best_uniprot_accession comes from
    uniprot_record.primaryAccession. Conflict flags are raised when xrefs
    disagrees with uniprot, or when display_name disagrees with any uniprot
    gene name.
    """
    canonical_gene_name = None
    if ensembl_record and ensembl_record.get("display_name"):
        canonical_gene_name = ensembl_record["display_name"]
    elif uniprot_record and uniprot_record.get("geneNames"):
        canonical_gene_name = uniprot_record["geneNames"][0]

    best_uniprot_accession = (uniprot_record or {}).get("primaryAccession") or None

    conflict_flags: list[str] = []
    if uniprot_record and ensembl_record:
        u_names = set(uniprot_record.get("geneNames") or [])
        e_name = ensembl_record.get("display_name")
        if e_name and u_names and e_name not in u_names:
            conflict_flags.append("gene_name_mismatch")
    if xrefs and best_uniprot_accession:
        xref_uniprot = set((xrefs.get("by_db") or {}).get("Uniprot_gn", []))
        if xref_uniprot and best_uniprot_accession not in xref_uniprot:
            conflict_flags.append("uniprot_xref_disagreement")

    return {
        "canonical_gene_name": canonical_gene_name,
        "best_uniprot_accession": best_uniprot_accession,
        "conflict_flags": conflict_flags,
    }


# ---------------------------------------------------------------------------
# 4.2 find_homologs_synth
# ---------------------------------------------------------------------------


_BLAST_PROGRAMS = {"blastn", "blastp", "blastx", "tblastn", "tblastx"}


async def find_homologs_synth(
    client: httpx.AsyncClient,
    sequence: str,
    program: str = "blastp",
    top_n: int = DEFAULT_TOP_N,
) -> SynthesisEnvelope:
    """Mirror the find_homologs prompt: BLAST + UniProt lookup of subject accessions.

    Phase 1: blast.blast_sequence (hitlist=top_n)
    Phase 2: batch_resolve_locus_to_uniprot over deduped UniProt-shaped subjects
    """
    if program not in _BLAST_PROGRAMS:
        raise ValueError(f"program {program!r} not in {sorted(_BLAST_PROGRAMS)}")
    top_n = _bound_top_n(top_n)
    started_at = _now_iso()
    t0 = time.perf_counter()
    input_args = {"sequence_length": len(sequence), "program": program, "top_n": top_n}

    # Phase 1 — BLAST
    root = await _timed_step(
        1,
        "blast_sequence",
        blast.blast_sequence(client, sequence, program=program, hitlist_size=top_n),
    )
    if root.status != "ok":
        return SynthesisEnvelope(
            tool="find_homologs_synth",
            input=input_args,
            started_at=started_at,
            elapsed_s=time.perf_counter() - t0,
            steps=[
                root,
                _skipped(
                    2, "resolve_locus_to_uniprot", "phase-1 BLAST failed; subject lookup skipped"
                ),
            ],
            result=None,
        )

    blast_payload = _result_dict(root)
    hits = list(blast_payload.get("hits") or [])[:top_n]

    # Phase 2 — extract UniProt-shaped accessions per hit (single pass), dedupe,
    # batch-resolve. blast._parse_hit_table emits hits keyed on "accession"
    # (no "subject_id", no "rank"). Rank is positional from the BLAST table.
    notes: list[str] = []
    hit_accessions: list[str | None] = []  # parallel to hits; None = non-UniProt
    canonical_seen: set[str] = set()
    to_lookup: list[str] = []
    for hit in hits:
        raw_acc = _extract_uniprot_accession(hit.get("accession", ""))
        hit_accessions.append(raw_acc)
        if raw_acc is None:
            if "non_uniprot_subject" not in notes:
                notes.append("non_uniprot_subject")
            continue
        # Canonicalize: strip ".N" version suffix so Q0WV96 and Q0WV96.1
        # collapse to one batch lookup. uniprot._fetch_by_accession strips
        # the suffix downstream regardless.
        canonical = raw_acc.split(".", 1)[0]
        if canonical in canonical_seen:
            continue
        canonical_seen.add(canonical)
        to_lookup.append(canonical)

    if to_lookup:
        lookup_step = await _timed_step(
            2,
            "resolve_locus_to_uniprot",
            batch.batch_resolve_locus_to_uniprot(client, to_lookup),
        )
    else:
        lookup_step = _skipped(
            2,
            "resolve_locus_to_uniprot",
            "no UniProt-shaped subjects in BLAST hits",
        )

    by_acc: dict[str, dict] = {}
    if lookup_step.status == "ok":
        by_acc = dict(_result_dict(lookup_step).get("results", {}))

    ranked = []
    for rank, (hit, raw_acc) in enumerate(zip(hits, hit_accessions, strict=True), start=1):
        canonical_acc = raw_acc.split(".", 1)[0] if raw_acc else None
        record = by_acc.get(canonical_acc) if canonical_acc else None
        ranked.append(
            {
                "rank": rank,
                "blast_hit": hit,
                "uniprot_record": record,
            }
        )

    return SynthesisEnvelope(
        tool="find_homologs_synth",
        input=input_args,
        started_at=started_at,
        elapsed_s=time.perf_counter() - t0,
        steps=[root, lookup_step],
        result={
            "blast": {
                "rid": blast_payload.get("rid"),
                "program": blast_payload.get("program"),
                "database": blast_payload.get("database"),
                "hit_count": blast_payload.get("hitCount"),
            },
            "ranked_hits": ranked,
            "notes": notes,
        },
    )


# UniProt accession syntax — same regex shape uniprot._looks_like_uniprot_accession uses.
# Subject IDs come in many forms; we look for the accession token where possible.
_UNIPROT_ACCESSION_TOKEN = re.compile(
    r"\b(?:[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2})(?:\.[0-9]+)?\b"
)


def _extract_uniprot_accession(subject_id: str) -> str | None:
    """Pull a UniProtKB accession out of a BLAST subject_id, or None.

    Handles forms NCBI BLAST emits in plant searches:
      - ``sp|Q0WV96.1|Y_ARATH`` / ``tr|A0A1B2C3D4|X_ARATH``  — Swiss-Prot / TrEMBL
      - ``Q0WV96`` / ``Q0WV96.1``                              — bare accession
      - Plant-specific locus IDs (e.g. ``AT1G01010.1``) → return None.

    The bare regex above matches an accession anywhere in the string, so we
    pick the first match. ``.N`` version suffix is preserved and stripped by
    downstream uniprot._fetch_by_accession.
    """
    if not subject_id:
        return None
    m = _UNIPROT_ACCESSION_TOKEN.search(subject_id)
    return m.group(0) if m else None


# ---------------------------------------------------------------------------
# 4.3 biological_context_synth
# ---------------------------------------------------------------------------


async def biological_context_synth(
    client: httpx.AsyncClient,
    locus: str,
    organism: str | int = DEFAULT_ORGANISM,
    top_n: int = DEFAULT_TOP_N,
) -> SynthesisEnvelope:
    """Mirror the biological_context prompt: gramene + KEGG + STRING + ATTED.

    Phase 1: uniprot.lookup_locus (returned in the envelope; not threaded
    into STRING — STRING does its own canonical-accession resolution from
    the locus, see string_db.lookup_partners).
    Phase 2 (gather): gramene.homologs, kegg.pathways, string_db.partners,
                      atted.coexpression.

    Phase-1 failure skips all of phase 2 — keeps envelope atomic (either
    complete coordinated result set or unambiguous root failure). Per spec 4.3.

    Per-backend signatures verified against live source 2026-05-22:
      - gramene.lookup_homologs(client, locus, homology_type="ortholog") — no species/top_n
      - kegg.lookup_pathways(client, locus, organism=...) — v1.1.0 requires organism;
        Arabidopsis-only (non-ath organisms raise OrganismNotSupported until an
        Entrez bridge lands)
      - string_db.lookup_partners(client, locus_or_accession, limit=..., organism=...) —
        v1.1.1 drops the pre-resolve-through-UniProt step; pass loci directly
        and let STRING's internal resolver pick the canonical accession (avoids
        Q0JRI1-vs-A0A0P0UX28 ambiguity bugs on multi-accession loci).
      - atted.lookup_coexpression(client, locus, organism=..., top_n=...) —
        v1.1.0 requires organism; release resolved per-organism (5 organisms
        currently have no ATTED-II release → OrganismNotSupported).
    """
    top_n = _bound_top_n(top_n)
    started_at = _now_iso()
    t0 = time.perf_counter()
    input_args = {"locus": locus, "organism": organism, "top_n": top_n}

    # Phase 0 — resolve organism alias; root-fail the envelope on unknown.
    try:
        organisms.resolve(organism)
    except OrganismNotFound as exc:
        return SynthesisEnvelope(
            tool="biological_context_synth",
            input=input_args,
            started_at=started_at,
            elapsed_s=time.perf_counter() - t0,
            steps=[
                StepRow(
                    step=1,
                    tool="resolve_locus_to_uniprot",
                    status="error",
                    elapsed_s=None,
                    error=str(exc),
                ),
                _skipped(2, "gramene_homologs", "phase 1 failed; gramene_homologs skipped"),
                _skipped(3, "kegg_pathways", "phase 1 failed; kegg_pathways skipped"),
                _skipped(4, "string_interactions", "phase 1 failed; string_interactions skipped"),
                _skipped(5, "atted_coexpression", "phase 1 failed; atted_coexpression skipped"),
            ],
            result=None,
        )

    # Phase 1 — UniProt for accession
    root = await _timed_step(
        1,
        "resolve_locus_to_uniprot",
        uniprot.lookup_locus(client, locus, organism=organism),
    )
    if root.status != "ok":
        skip_reason = "phase-1 UniProt resolution failed; downstream calls skipped"
        return SynthesisEnvelope(
            tool="biological_context_synth",
            input=input_args,
            started_at=started_at,
            elapsed_s=time.perf_counter() - t0,
            steps=[
                root,
                _skipped(2, "gramene_homologs", skip_reason),
                _skipped(3, "kegg_pathways", skip_reason),
                _skipped(4, "string_interactions", skip_reason),
                _skipped(5, "atted_coexpression", skip_reason),
            ],
            result=None,
        )

    uniprot_acc = _result_dict(root)["primaryAccession"]  # surfaced in the envelope result

    p2 = await _gather_phase2(
        [
            (2, "gramene_homologs", gramene.lookup_homologs(client, locus)),
            (3, "kegg_pathways", kegg.lookup_pathways(client, locus, organism=organism)),
            (
                4,
                "string_interactions",
                string_db.lookup_partners(client, locus, limit=top_n, organism=organism),
            ),
            (
                5,
                "atted_coexpression",
                atted.lookup_coexpression(client, locus, organism=organism, top_n=top_n),
            ),
        ]
    )
    gramene_row, kegg_row, string_row, atted_row = p2

    def _ok(row: StepRow) -> Any:
        return row.result if row.status == "ok" else None

    consensus = _consensus_partners(
        string_payload=_ok(string_row),
        atted_payload=_ok(atted_row),
        top_n=top_n,
    )

    return SynthesisEnvelope(
        tool="biological_context_synth",
        input=input_args,
        started_at=started_at,
        elapsed_s=time.perf_counter() - t0,
        steps=[root, *p2],
        result={
            "uniprot_accession": uniprot_acc,
            "homologs": _ok(gramene_row),
            "pathways": _ok(kegg_row),
            "string_partners": _ok(string_row),
            "atted_coexpression": _ok(atted_row),
            "consensus_partners": consensus,
        },
    )


def _string_partner_locus(string_id: str | None) -> str | None:
    """Strip the taxid prefix + transcript suffix from STRING's <taxid>.<locus>.<N>.

    STRING-DB's stringId_B for Arabidopsis is ``3702.AT3G15500.1``; we
    surface the bare locus (``AT3G15500``) so it can merge with ATTED's
    ``locus`` field. Returns the input unchanged when it doesn't look
    taxid-prefixed.
    """
    if not string_id:
        return None
    parts = string_id.split(".", 2)
    if len(parts) >= 2 and parts[0].isdigit():
        return parts[1]
    return string_id


def _consensus_partners(
    string_payload: dict | None,
    atted_payload: dict | None,
    top_n: int,
) -> list[dict]:
    """Rank-merge STRING partners + ATTED coexpression neighbors.

    Live-shape consumption (verified 2026-05-22 against backend modules):
      - STRING normalized partners carry ``string_id`` (``<taxid>.<locus>.<N>``)
        and ``score`` (combined_score, already 0-1).
      - ATTED normalized neighbors carry ``locus`` and ``z_score`` (NOT
        ``mr`` — atted._normalize does not emit a mutual_rank field).

    Scoring:
      STRING combined_score already 0-1.
      ATTED z-score normalized via ``z / (1 + z)`` → bounded 0-1
        (z=3 → 0.75, z=5 → 0.83, z=10 → 0.91).
      combined_score = mean(normalized_scores across sources).
      sort by (n_sources desc, combined_score desc, target_locus asc), top-N.
    """
    scores: dict[str, dict[str, Any]] = {}

    if string_payload:
        for p in string_payload.get("partners") or []:
            locus = _string_partner_locus(p.get("string_id"))
            if not locus:
                continue
            score = float(p.get("score") or 0.0)  # combined_score, already 0-1
            entry = scores.setdefault(
                locus, {"target_locus": locus, "sources": [], "normalized": []}
            )
            if "string" not in entry["sources"]:
                entry["sources"].append("string")
                entry["normalized"].append(score)

    if atted_payload:
        for n in atted_payload.get("neighbors") or []:
            locus = n.get("locus")
            if not locus:
                continue
            z = float(n.get("z_score") or 0.0)
            normalized = z / (1.0 + z) if z > 0 else 0.0
            entry = scores.setdefault(
                locus, {"target_locus": locus, "sources": [], "normalized": []}
            )
            if "atted" not in entry["sources"]:
                entry["sources"].append("atted")
                entry["normalized"].append(normalized)

    out: list[dict] = []
    for locus, entry in scores.items():
        if not entry["normalized"]:
            continue
        combined = sum(entry["normalized"]) / len(entry["normalized"])
        out.append(
            {
                "target_locus": locus,
                "n_sources": len(entry["sources"]),
                "combined_score": round(combined, 4),
                "sources": list(entry["sources"]),
            }
        )
    out.sort(key=lambda d: (-d["n_sources"], -d["combined_score"], d["target_locus"]))
    return out[:top_n]


# ---------------------------------------------------------------------------
# 4.3b gene_report — one-shot Markdown gene dossier
# ---------------------------------------------------------------------------


_GENE_REPORT_STEPS = [
    "ensembl_plants_lookup_locus",
    "resolve_locus_to_uniprot",
    "get_gene_xrefs",
    "kegg_pathways",
    "string_interactions",
    "locus_literature",
    "locus_go_annotations",
]


async def gene_report(
    client: httpx.AsyncClient,
    locus: str,
    organism: str | int = DEFAULT_ORGANISM,
    top_n: int = DEFAULT_TOP_N,
) -> SynthesisEnvelope:
    """One-shot "tell me about this gene" dossier.

    Unions the analyze_locus chain (annotation, cross-refs, protein, GO,
    literature) with the biological_context pathway + interaction backends into
    a single :class:`SynthesisEnvelope` whose ``result["markdown"]`` is a
    rendered Markdown gene dossier — the headline deliverable — alongside a
    structured ``result["sections"]`` mirror.

    Phase 1 (parallel): ``ensembl_plants.lookup_locus`` (root) +
      ``uniprot.lookup_locus`` (sequenced here because QuickGO needs the
      resolved primaryAccession).
    Phase 2 (gather): ``get_gene_xrefs``, ``kegg_pathways``,
      ``string_interactions``, ``locus_literature``, and ``locus_go_annotations``
      (only when UniProt resolved).

    Ensembl is the root: if it fails the envelope returns ``result=None`` with
    every downstream row ``skipped``. Any individual phase-2 failure degrades
    that one section to an "Unavailable" note in the Markdown (and ``None`` in
    the structured mirror); the rest of the dossier still renders.
    """
    top_n = _bound_top_n(top_n)
    started_at = _now_iso()
    t0 = time.perf_counter()
    input_args = {"locus": locus, "organism": organism, "top_n": top_n}

    # Phase 0 — resolve organism alias; root-fail on unknown so nothing fires
    # with a bogus slug.
    try:
        resolved = organisms.resolve(organism)
    except OrganismNotFound as exc:
        return SynthesisEnvelope(
            tool="gene_report",
            input=input_args,
            started_at=started_at,
            elapsed_s=time.perf_counter() - t0,
            steps=[
                StepRow(
                    step=1,
                    tool=_GENE_REPORT_STEPS[0],
                    status="error",
                    elapsed_s=None,
                    error=str(exc),
                ),
                *[
                    _skipped(i + 1, _GENE_REPORT_STEPS[i], "phase 1 failed; skipped")
                    for i in range(1, 7)
                ],
            ],
            result=None,
        )

    # Phase 1 — ensembl (root) + uniprot in parallel; latency = max(ensembl, uniprot).
    phase1 = await _gather_phase2(
        [
            (
                1,
                _GENE_REPORT_STEPS[0],
                ensembl_plants.lookup_locus(client, locus, organism=organism),
            ),
            (2, _GENE_REPORT_STEPS[1], uniprot.lookup_locus(client, locus, organism=organism)),
        ]
    )
    root, uniprot_row = phase1

    if root.status != "ok":
        # Ensembl is the entry point; without it the dossier can't anchor.
        skipped = [
            _skipped(i + 1, _GENE_REPORT_STEPS[i], "phase-1 ensembl lookup failed; skipped")
            for i in range(2, 7)
        ]
        return SynthesisEnvelope(
            tool="gene_report",
            input=input_args,
            started_at=started_at,
            elapsed_s=time.perf_counter() - t0,
            steps=[root, uniprot_row, *skipped],
            result=None,
        )

    # Phase 2 — fan out the rest; quickgo only when uniprot resolved.
    phase2_items: list[tuple[int, str, Any]] = [
        (3, _GENE_REPORT_STEPS[2], ensembl_plants.lookup_xrefs(client, locus, organism=organism)),
        (4, _GENE_REPORT_STEPS[3], kegg.lookup_pathways(client, locus, organism=organism)),
        (
            5,
            _GENE_REPORT_STEPS[4],
            string_db.lookup_partners(client, locus, limit=top_n, organism=organism),
        ),
        (6, _GENE_REPORT_STEPS[5], europe_pmc.lookup_locus(client, locus, organism=organism)),
    ]
    if uniprot_row.status == "ok":
        acc = _result_dict(uniprot_row)["primaryAccession"]
        phase2_items.append((7, _GENE_REPORT_STEPS[6], quickgo.lookup_by_uniprot(client, acc)))

    p2 = await _gather_phase2(phase2_items)

    if uniprot_row.status != "ok":
        p2.append(
            _skipped(
                7,
                _GENE_REPORT_STEPS[6],
                "phase-1 UniProt resolution failed; quickgo skipped",
            )
        )

    xrefs_row, kegg_row, string_row, lit_row, go_row = p2[0], p2[1], p2[2], p2[3], p2[4]

    def _ok(row: StepRow) -> Any:
        return row.result if row.status == "ok" else None

    ensembl_record = _result_dict(root)
    uniprot_record = _ok(uniprot_row)

    canonical_gene_name = ensembl_record.get("display_name")
    if not canonical_gene_name and uniprot_record:
        names = uniprot_record.get("geneNames") or []
        canonical_gene_name = names[0] if names else None

    uniprot_accession = (uniprot_record or {}).get("primaryAccession")

    rows = {
        "annotation": root,
        "protein": uniprot_row,
        "xrefs": xrefs_row,
        "pathways": kegg_row,
        "interactions": string_row,
        "literature": lit_row,
        "go_annotations": go_row,
    }
    markdown = _render_gene_report_md(
        locus=locus,
        organism_display=resolved.scientific,
        canonical_gene_name=canonical_gene_name,
        rows=rows,
        top_n=top_n,
    )

    return SynthesisEnvelope(
        tool="gene_report",
        input=input_args,
        started_at=started_at,
        elapsed_s=time.perf_counter() - t0,
        steps=[root, uniprot_row, *p2],
        result={
            "locus": locus,
            "organism": resolved.canonical,
            "canonical_gene_name": canonical_gene_name,
            "uniprot_accession": uniprot_accession,
            "markdown": markdown,
            "sections": {name: _ok(row) for name, row in rows.items()},
        },
    )


def _section_note(row: StepRow) -> str | None:
    """Italic 'Unavailable — <reason>' line for a non-ok section, else None."""
    if row.status == "ok":
        return None
    return f"_Unavailable — {row.error or 'no data'}_"


def _render_gene_report_md(
    locus: str,
    organism_display: str,
    canonical_gene_name: str | None,
    rows: dict[str, StepRow],
    top_n: int,
) -> str:
    """Render the composed backend rows into a single Markdown gene dossier.

    Each section renders from its ok row, or falls back to the row's
    error/skip message so a partial dossier stays legible and self-explaining.
    """

    def _ok(name: str) -> Any:
        row = rows[name]
        return row.result if row.status == "ok" else None

    title = canonical_gene_name or locus
    lines: list[str] = [f"# {title} — `{locus}`", ""]

    # Header — organism · biotype · location · assembly
    ann = _ok("annotation") or {}
    header_bits: list[str] = [f"*{organism_display}*"]
    if ann.get("biotype"):
        header_bits.append(str(ann["biotype"]))
    if ann.get("seq_region_name") and ann.get("start") and ann.get("end"):
        strand = "+" if ann.get("strand", 1) >= 0 else "-"
        header_bits.append(f"{ann['seq_region_name']}:{ann['start']:,}–{ann['end']:,} ({strand})")
    if ann.get("assembly_name"):
        header_bits.append(str(ann["assembly_name"]))
    lines.append(" · ".join(header_bits))
    if ann.get("description"):
        lines += ["", str(ann["description"])]

    # Protein
    lines += ["", "## Protein"]
    prot = _ok("protein")
    note = _section_note(rows["protein"])
    if prot:
        acc = prot.get("primaryAccession", "")
        lines.append(f"**{prot.get('recommendedName') or prot.get('uniProtkbId') or acc}**")
        meta_bits = [
            str(b)
            for b in (
                prot.get("uniProtkbId"),
                prot.get("entryType"),
                f"{prot['sequenceLength']} aa" if prot.get("sequenceLength") else None,
            )
            if b
        ]
        if meta_bits:
            lines.append(" · ".join(meta_bits))
        if prot.get("web_url"):
            lines.append(f"UniProt: [{acc}]({prot['web_url']})")
    elif note:
        lines.append(note)
    else:
        lines.append("_No UniProt record found._")

    # GO annotations, grouped by aspect
    lines += ["", "## GO annotations"]
    go = _ok("go_annotations")
    note = _section_note(rows["go_annotations"])
    if go and go.get("annotations"):
        by_aspect: dict[str, list[str]] = {}
        for a in go["annotations"]:
            label = f"[{a.get('goId')}] {a.get('goName')}"
            if a.get("goEvidence"):
                label += f" ({a['goEvidence']})"
            by_aspect.setdefault(a.get("goAspect", "other"), []).append(label)
        for aspect in ("molecular_function", "biological_process", "cellular_component"):
            if by_aspect.get(aspect):
                lines.append(f"**{aspect.replace('_', ' ').title()}**")
                lines += [f"- {x}" for x in by_aspect[aspect][:top_n]]
    elif note:
        lines.append(note)
    else:
        lines.append("_No GO annotations found._")

    # KEGG pathways
    lines += ["", "## Pathways (KEGG)"]
    kegg_p = _ok("pathways")
    note = _section_note(rows["pathways"])
    if kegg_p and kegg_p.get("pathways"):
        for p in kegg_p["pathways"][:top_n]:
            cls = f" — {p['pathway_class']}" if p.get("pathway_class") else ""
            lines.append(f"- `{p.get('id')}` {p.get('name')}{cls}")
    elif note:
        lines.append(note)
    else:
        lines.append("_No KEGG pathway memberships found._")

    # STRING interaction partners
    lines += ["", f"## Interaction partners (STRING, top {top_n})"]
    string_p = _ok("interactions")
    note = _section_note(rows["interactions"])
    if string_p and string_p.get("partners"):
        lines += ["| Partner | Score |", "| --- | --- |"]
        for pt in string_p["partners"][:top_n]:
            name = pt.get("preferred_name") or pt.get("string_id") or "?"
            lines.append(f"| {name} | {pt.get('score', '')} |")
    elif note:
        lines.append(note)
    else:
        lines.append("_No STRING interaction partners found._")

    # Cross-references (deduped, capped)
    lines += ["", "## Cross-references"]
    xrefs = _ok("xrefs")
    note = _section_note(rows["xrefs"])
    if xrefs and xrefs.get("xrefs"):
        seen: set[str] = set()
        for x in xrefs["xrefs"]:
            db = x.get("db_display_name") or x.get("dbname") or "?"
            pid = x.get("primary_id") or x.get("display_id") or "?"
            key = f"{db}:{pid}"
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"- {db}: {pid}")
            if len(seen) >= top_n:
                break
    elif note:
        lines.append(note)
    else:
        lines.append("_No cross-references found._")

    # Literature
    lines += ["", "## Literature"]
    lit = _ok("literature")
    note = _section_note(rows["literature"])
    if lit and lit.get("hits"):
        if lit.get("hitCount"):
            lines.append(
                f"{lit['hitCount']} hits total; showing top {min(top_n, len(lit['hits']))}."
            )
        for h in lit["hits"][:top_n]:
            title_txt = (h.get("title") or "").rstrip(".")
            ref_bits = [
                b
                for b in (
                    f"PMID:{h['pmid']}" if h.get("pmid") else None,
                    f"doi:{h['doi']}" if h.get("doi") else None,
                )
                if b
            ]
            ref = f" ({'; '.join(ref_bits)})" if ref_bits else ""
            lines.append(f"- **{title_txt}** — {h.get('authorString', '')}{ref}")
    elif note:
        lines.append(note)
    else:
        lines.append("_No literature found._")

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 4.4 consensus_homologs
# ---------------------------------------------------------------------------


def _parse_blast_identity_pct(raw: object) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        f = float(raw)
        return f / 100.0 if f > 1.0 else f
    s = str(raw).strip().rstrip("%")
    try:
        return float(s) / 100.0
    except ValueError:
        return None


async def consensus_homologs(
    client: httpx.AsyncClient,
    locus: str,
    organism: str | int = DEFAULT_ORGANISM,
    top_n: int = DEFAULT_TOP_N,
) -> SynthesisEnvelope:
    top_n = _bound_top_n(top_n)
    started_at = _now_iso()
    t0 = time.perf_counter()
    input_args = {"locus": locus, "organism": organism, "top_n": top_n}

    # Phase 0 — resolve organism alias; root-fail the envelope on unknown.
    try:
        organisms.resolve(organism)
    except OrganismNotFound as exc:
        return SynthesisEnvelope(
            tool="consensus_homologs",
            input=input_args,
            started_at=started_at,
            elapsed_s=time.perf_counter() - t0,
            steps=[
                StepRow(
                    step=1,
                    tool="resolve_locus_to_uniprot",
                    status="error",
                    elapsed_s=None,
                    error=str(exc),
                ),
                _skipped(
                    2, "uniprot_fetch_sequence", "phase 1 failed; uniprot_fetch_sequence skipped"
                ),
                _skipped(3, "gramene_homologs", "phase 1 failed; gramene_homologs skipped"),
                _skipped(4, "blast_sequence", "phase 1 failed; blast_sequence skipped"),
                _skipped(
                    5,
                    "gramene_homolog_enrichment",
                    "phase 1 failed; gramene_homolog_enrichment skipped",
                ),
            ],
            result=None,
        )

    # Phase 1.a — UniProt
    step1 = await _timed_step(
        1,
        "resolve_locus_to_uniprot",
        uniprot.lookup_locus(client, locus, organism=organism),
    )
    if step1.status != "ok":
        skip = "phase-1 UniProt resolution failed; sequence + downstream skipped"
        return SynthesisEnvelope(
            tool="consensus_homologs",
            input=input_args,
            started_at=started_at,
            elapsed_s=time.perf_counter() - t0,
            steps=[
                step1,
                _skipped(2, "uniprot_fetch_sequence", skip),
                _skipped(3, "gramene_homologs", skip),
                _skipped(4, "blast_sequence", skip),
                _skipped(5, "gramene_homolog_enrichment", skip),
            ],
            result=None,
        )

    # Phase 1.b — fetch sequence.
    #
    # Bypass _timed_step here: uniprot.fetch_sequence returns a bare str, but
    # StepRow.result is typed `dict | list | None` (models.py:488, extra=forbid).
    # Time it inline and put metadata (accession + length) in the envelope —
    # the raw 400+ residue protein sequence doesn't belong in an envelope JSON
    # blob, and we need the bare string for the phase-2 BLAST call anyway.
    acc = _result_dict(step1)["primaryAccession"]
    t_step2 = time.perf_counter()
    try:
        sequence = await uniprot.fetch_sequence(client, acc)
        step2 = StepRow(
            step=2,
            tool="uniprot_fetch_sequence",
            status="ok",
            elapsed_s=time.perf_counter() - t_step2,
            result={"accession": acc, "sequence_length": len(sequence)},
        )
    except PlantGenomicsError as e:
        step2 = StepRow(
            step=2,
            tool="uniprot_fetch_sequence",
            status="error",
            elapsed_s=time.perf_counter() - t_step2,
            error=str(e),
        )
        skip = "phase-1.b sequence fetch failed; downstream skipped"
        return SynthesisEnvelope(
            tool="consensus_homologs",
            input=input_args,
            started_at=started_at,
            elapsed_s=time.perf_counter() - t0,
            steps=[
                step1,
                step2,
                _skipped(3, "gramene_homologs", skip),
                _skipped(4, "blast_sequence", skip),
                _skipped(5, "gramene_homolog_enrichment", skip),
            ],
            result=None,
        )
    except httpx.HTTPError as e:
        step2 = StepRow(
            step=2,
            tool="uniprot_fetch_sequence",
            status="error",
            elapsed_s=time.perf_counter() - t_step2,
            error=f"[HTTPError] {e}",
        )
        skip = "phase-1.b sequence fetch failed; downstream skipped"
        return SynthesisEnvelope(
            tool="consensus_homologs",
            input=input_args,
            started_at=started_at,
            elapsed_s=time.perf_counter() - t0,
            steps=[
                step1,
                step2,
                _skipped(3, "gramene_homologs", skip),
                _skipped(4, "blast_sequence", skip),
                _skipped(5, "gramene_homolog_enrichment", skip),
            ],
            result=None,
        )

    # Phase 2 — gather Gramene + BLAST.
    # v1.3.0: homology_type="all" includes within-species paralogs. BLAST's
    # swissprot top hits are ranked by sequence identity, so they are dominated
    # by same-species paralogs (e.g. AT5G38420 → other RBCS isoforms). The v1.2
    # default ("ortholog") explicitly excluded that class, making any 2-source
    # intersect structurally impossible for Arabidopsis-rooted queries.
    raw_top = 50
    p2 = await _gather_phase2(
        [
            (3, "gramene_homologs", gramene.lookup_homologs(client, locus, homology_type="all")),
            (
                4,
                "blast_sequence",
                blast.blast_sequence(client, sequence, program="blastp", hitlist_size=raw_top),
            ),
        ]
    )
    gramene_row, blast_row = p2

    # Phase 3 — enrich Gramene loci with UniProt accession + system_name so
    # we can dedup against BLAST in UniProt-accession-space. Gramene's
    # fl=homology projection doesn't carry xrefs, so this is a second batched
    # call against the same /v69/genes endpoint with fl=_id,xrefs,system_name.
    # Without this projection, cross-source dedup is structurally impossible:
    # Gramene returns species-prefixed locus IDs (e.g. ORYSA_OS01G0100100)
    # while NCBI BLAST against SwissProt returns bare UniProt accessions
    # (e.g. Q5VMS9.1) — there's no overlap to join on.
    xref_map: dict[str, dict[str, str | None]] = {}
    if gramene_row.status == "ok" and gramene_row.result:
        gramene_payload = _result_dict(gramene_row)
        loci = [
            h.get("target_locus")
            for h in (gramene_payload.get("homologs") or [])
            if isinstance(h.get("target_locus"), str) and h.get("target_locus")
        ]
        if loci:
            step5 = await _timed_step(
                5,
                "gramene_homolog_enrichment",
                gramene.fetch_homolog_enrichment_batch(client, loci),
            )
            if step5.status == "ok" and isinstance(step5.result, dict):
                xref_map = step5.result
        else:
            step5 = _skipped(
                5,
                "gramene_homolog_enrichment",
                "gramene_homologs returned 0 homologs; nothing to enrich",
            )
    else:
        step5 = _skipped(
            5,
            "gramene_homolog_enrichment",
            "gramene_homologs phase did not return ok; enrichment skipped",
        )

    consensus = _consensus_homologs_compose(
        gramene_payload=_result_dict(gramene_row) if gramene_row.status == "ok" else None,
        blast_payload=_result_dict(blast_row) if blast_row.status == "ok" else None,
        xref_map=xref_map,
        top_n=top_n,
    )

    return SynthesisEnvelope(
        tool="consensus_homologs",
        input=input_args,
        started_at=started_at,
        elapsed_s=time.perf_counter() - t0,
        steps=[step1, step2, *p2, step5],
        result={
            "uniprot_accession": acc,
            "sequence_length": len(sequence),
            "consensus": consensus,
        },
    )


def _consensus_homologs_compose(
    gramene_payload: dict | None,
    blast_payload: dict | None,
    xref_map: dict[str, dict[str, str | None]] | None,
    *,
    top_n: int,
) -> list[dict]:
    """Dedup Gramene homologs ∩ NCBI BLAST hits on UniProt accession.

    Both sources project into UniProt-accession-space:
      - Gramene: via ``xref_map`` (built by ``gramene.fetch_homolog_enrichment_batch``)
      - BLAST: parsed from ``hit['accession']`` (e.g. ``sp|Q5VMS9.1|Y_ORYSJ``)
        via the shared ``_extract_uniprot_accession`` helper + ``.N`` version strip.

    Gramene homologs whose ``xref_map`` entry has no UniProt accession are
    dropped — without an accession they can't dedup with BLAST, so keeping
    them as single-source rows would just dilute the consensus. This is a
    known coverage gap: ~half of v69 entries in fringe organisms (cucurbits,
    bryophytes) have no Swiss-Prot or TrEMBL xref. Tracked in CHANGELOG v1.2.0.

    ``target_species`` is sourced from the Gramene xref (``system_name``).
    BLAST-only hits report ``target_species=None`` because the NCBI SwissProt
    defline format (``RecName: Full=...``) doesn't carry the EBI-style
    ``OS=`` species token — recovering it would need a separate UniProt
    lookup per hit, which is outside this tool's budget.
    """
    groups: dict[str, dict] = {}
    xref_map = xref_map or {}

    if gramene_payload:
        for h in gramene_payload.get("homologs") or []:
            raw_locus = h.get("target_locus") or ""
            if not raw_locus:
                continue
            xref = xref_map.get(raw_locus) or {}
            acc = xref.get("uniprot_acc")
            if not acc:
                continue
            entry = groups.setdefault(
                acc,
                {
                    "uniprot_accession": acc,
                    "target_species": xref.get("system_name"),
                    "sources": [],
                    "identities": [],
                    "gramene_hit": None,
                    "blast_hit": None,
                },
            )
            if "gramene" not in entry["sources"]:
                entry["sources"].append("gramene")
                entry["identities"].append(1.0)
                entry["gramene_hit"] = h

    if blast_payload:
        for h in blast_payload.get("hits") or []:
            raw_acc = _extract_uniprot_accession(h.get("accession") or "")
            if not raw_acc:
                continue
            # Strip the ``.N`` version suffix so ``Q5VMS9.1`` (BLAST) and
            # ``Q5VMS9`` (Gramene xref) collapse into the same dedup key.
            acc = raw_acc.split(".", 1)[0]
            identity_frac = _parse_blast_identity_pct(h.get("identity"))
            if identity_frac is None:
                # Drop hits with missing identity — scoring weights
                # mean_identity by n_sources, so a None-identity hit would
                # inflate n_sources without contributing a measurable signal.
                continue
            entry = groups.setdefault(
                acc,
                {
                    "uniprot_accession": acc,
                    "target_species": None,
                    "sources": [],
                    "identities": [],
                    "gramene_hit": None,
                    "blast_hit": None,
                },
            )
            if "blast" not in entry["sources"]:
                entry["sources"].append("blast")
                entry["identities"].append(identity_frac)
                entry["blast_hit"] = h

    out: list[dict] = []
    for entry in groups.values():
        if not entry["identities"]:
            continue
        mean_identity = sum(entry["identities"]) / len(entry["identities"])
        n_sources = len(entry["sources"])
        score = n_sources * mean_identity
        out.append(
            {
                "uniprot_accession": entry["uniprot_accession"],
                "target_species": entry["target_species"],
                "n_sources": n_sources,
                "sources": list(entry["sources"]),
                "mean_identity": round(mean_identity, 4),
                "score": round(score, 4),
                "gramene_hit": entry["gramene_hit"],
                "blast_hit": entry["blast_hit"],
            }
        )
    out.sort(key=lambda d: (-d["n_sources"], -d["score"], d["uniprot_accession"]))
    return out[:top_n]
