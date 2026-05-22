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
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from plant_genomics_mcp import (
    atted,
    blast,
    ensembl_plants,
    europe_pmc,
    gramene,
    kegg,
    quickgo,
    string_db,
    uniprot,
)
from plant_genomics_mcp.errors import PlantGenomicsError
from plant_genomics_mcp.models import StepRow, SynthesisEnvelope

DEFAULT_SPECIES = "arabidopsis_thaliana"
DEFAULT_TOP_N = 10
MAX_TOP_N = 50  # matches batch.MAX_BATCH convention


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


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


def _gather_step(step: int, tool: str, outcome: Any, elapsed_s: float) -> StepRow:
    """Convert one slot of ``asyncio.gather(return_exceptions=True)`` into a StepRow.

    Used for phase-2 fanout — each coroutine's outcome lands here.
    PlantGenomicsError → status="error" using its existing [ClassName] __str__.
    Raw httpx network errors → status="error" with explicit [ClassName] prefix.
    Other exceptions re-raise (caller wraps in try, or the gather machinery
    propagates).
    """
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
    return StepRow(step=step, tool=tool, status="skipped", elapsed_s=0.0, error=reason)


async def _gather_phase2(
    items: list[tuple[int, str, Any]],
) -> list[StepRow]:
    """Run a list of (step, tool, coroutine) concurrently; return StepRows in input order."""
    started = time.perf_counter()
    raw = await asyncio.gather(*(c for _, _, c in items), return_exceptions=True)
    elapsed = time.perf_counter() - started
    # All slots share the gather wall time — we can't attribute per-coroutine
    # without instrumenting each await, and the orchestrator-level elapsed_s
    # on SynthesisEnvelope captures the real total. Per-step elapsed_s for
    # phase-2 rows is the gather-aggregate; this is documented in the spec.
    rows: list[StepRow] = []
    for (step, tool, _), outcome in zip(items, raw, strict=True):
        rows.append(_gather_step(step, tool, outcome, elapsed))
    return rows


# ---------------------------------------------------------------------------
# 4.1 analyze_locus_synth
# ---------------------------------------------------------------------------


async def analyze_locus_synth(
    client: httpx.AsyncClient,
    locus: str,
    species: str = DEFAULT_SPECIES,
) -> SynthesisEnvelope:
    """Mirror the analyze_locus prompt as a single tool call.

    Phase 1: ensembl_plants.lookup_locus
    Phase 2 (gather): get_xrefs, uniprot.lookup_locus, europe_pmc, quickgo
    """
    started_at = _now_iso()
    t0 = time.perf_counter()
    input_args = {"locus": locus, "species": species}
    taxon_id = uniprot.KNOWN_TAXA.get(species, uniprot.DEFAULT_TAXON_ID)

    # Phase 1 — root resolution: ensembl + uniprot in parallel.
    # UniProt is sequenced into phase 1 (not phase 2) because the QuickGO
    # call in phase 2 needs primaryAccession. Running ensembl and uniprot
    # concurrently keeps total latency at max(ensembl, uniprot).
    phase1 = await _gather_phase2(
        [
            (
                1,
                "ensembl_plants_lookup_locus",
                ensembl_plants.lookup_locus(client, locus, species=species),
            ),
            (
                2,
                "resolve_locus_to_uniprot",
                uniprot.lookup_locus(client, locus, organism_id=taxon_id),
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
        (3, "get_gene_xrefs", ensembl_plants.lookup_xrefs(client, locus, species=species)),
        (4, "locus_literature", europe_pmc.lookup_locus(client, locus, species=species)),
    ]
    if uniprot_row.status == "ok":
        acc = uniprot_row.result["primaryAccession"]
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

    ensembl_record = root.result
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

from plant_genomics_mcp import batch  # late import to avoid circular


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

    blast_payload = root.result
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
        by_acc = dict((lookup_step.result or {}).get("results", {}))

    ranked = []
    for rank, (hit, raw_acc) in enumerate(zip(hits, hit_accessions), start=1):
        canonical = raw_acc.split(".", 1)[0] if raw_acc else None
        record = by_acc.get(canonical) if canonical else None
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
import re as _re

_UNIPROT_ACCESSION_TOKEN = _re.compile(
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
    species: str = DEFAULT_SPECIES,
    top_n: int = DEFAULT_TOP_N,
) -> SynthesisEnvelope:
    """Mirror the biological_context prompt: gramene + KEGG + STRING + ATTED.

    Phase 1: uniprot.lookup_locus (need accession for STRING).
    Phase 2 (gather): gramene.homologs, kegg.pathways, string_db.partners,
                      atted.coexpression.

    Phase-1 failure skips all of phase 2 — keeps envelope atomic (either
    complete coordinated result set or unambiguous root failure). Per spec 4.3.

    Per-backend signatures verified against live source 2026-05-22:
      - gramene.lookup_homologs(client, locus, homology_type="ortholog") — no species/top_n
      - kegg.lookup_pathways(client, locus) — no species (ath-only)
      - string_db.lookup_partners(client, locus_or_accession, limit=..., organism_taxid=...)
        Passing the uniprot accession bypasses the internal locus→accession
        re-resolution (string_db.py:144 _looks_like_accession path).
      - atted.lookup_coexpression(client, locus, top_n=...) — no species (Ath-only upstream).
    """
    top_n = _bound_top_n(top_n)
    started_at = _now_iso()
    t0 = time.perf_counter()
    input_args = {"locus": locus, "species": species, "top_n": top_n}
    taxon_id = uniprot.KNOWN_TAXA.get(species, uniprot.DEFAULT_TAXON_ID)

    # Phase 1 — UniProt for accession
    root = await _timed_step(
        1,
        "resolve_locus_to_uniprot",
        uniprot.lookup_locus(client, locus, organism_id=taxon_id),
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

    uniprot_acc = root.result["primaryAccession"]

    p2 = await _gather_phase2(
        [
            (2, "gramene_homologs", gramene.lookup_homologs(client, locus)),
            (3, "kegg_pathways", kegg.lookup_pathways(client, locus)),
            (
                4,
                "string_interactions",
                string_db.lookup_partners(
                    client, uniprot_acc, limit=top_n, organism_taxid=taxon_id
                ),
            ),
            (5, "atted_coexpression", atted.lookup_coexpression(client, locus, top_n=top_n)),
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
