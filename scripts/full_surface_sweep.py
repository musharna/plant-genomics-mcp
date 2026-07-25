"""Full-surface semantic sweep — all 50 tools, driven through real dispatch.

The Phase C sweep reached only 11 tools because it called backend functions via
the benchmark registry, which hand-wraps them in a ``(client, locus, organism)``
contract. That contract does not generalise: 19 of the 50 tools take something
other than a bare locus (loci lists, regions, alleles, matrix ids, sequences).

This driver goes through ``server._dispatch(name, args)`` instead — the same
entry point a real MCP client hits — so argument shapes come from each tool's
own ``inputSchema`` and the validators run exactly as they do in production.
Eight distinct required-argument signatures cover all 50 tools, so one builder
keyed on that signature is enough.

Checks applied per call:

* **organism echo** — the species echoed back must be the species asked for.
* **negative control** — a locus from organism A, requested while declaring
  organism B, must be rejected rather than answered with A's data.
* **count semantics** — declared vs documented vs observed, via COUNT_SPECS.

Every skip is attributed. The Phase C run could not distinguish "no comparable
organism field in the payload" from "the backend correctly 404'd for this
locus/organism pair", which left a 70% skip rate uninterpretable — a clean run
was indistinguishable from a run that checked almost nothing. ``SkipReason``
fixes that; the per-reason tally is the first thing to read in the output.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter, defaultdict
from enum import StrEnum
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from semantic_invariants import (  # noqa: E402
    COUNT_SPECS,
    Verdict,
    check_count_semantics,
    check_organism_echo,
    check_truncated_flag,
)

from plant_genomics_mcp import organisms, server  # noqa: E402
from plant_genomics_mcp.errors import (  # noqa: E402
    NotFoundError,
    OrganismNotFound,
    OrganismNotSupported,
    PlantGenomicsError,
)

#: Correct answers to "that locus does not exist in that organism".
_CORRECT_REJECTIONS = (NotFoundError, OrganismNotSupported, OrganismNotFound)

#: Ensembl signals an unknown identifier with HTTP **400**, not 404 —
#: `{"error":"ID 'AT1G01010' not found"}` — and `_http` only maps 404 to
#: NotFoundError, so this arrives as a bare PlantGenomicsError. That is a real
#: typed-error gap in the server (recorded as a finding), but for sweep purposes
#: it is still a CORRECT rejection: the backend refused to answer rather than
#: returning the wrong organism's data. Classifying it as a transport error
#: instead silently emptied the negative-control bucket — 12 of 12 controls
#: landed in "upstream error" and the check measured nothing.
_REJECTION_PHRASES = ("not found", "no results", "unknown identifier", "http 400", "http 404")


def _is_rejection(exc: BaseException) -> bool:
    """Did the backend REFUSE the request, as opposed to failing to answer?

    The distinction is the whole point of a negative control: a refusal proves
    the organism argument constrained the query; an infrastructure failure
    proves nothing either way and must be reported as a skip.
    """
    if isinstance(exc, _CORRECT_REJECTIONS):
        return True
    if isinstance(exc, PlantGenomicsError):
        return any(p in str(exc).lower() for p in _REJECTION_PHRASES)
    return False


#: Never swept in bulk: each call enqueues a real job in NCBI's rate-limited
#: queue. This is why blast_sequence is the server's only non-idempotent tool.
BLAST_BACKED = {"blast_sequence", "find_homologs_synth", "consensus_homologs"}

#: organism is a free-text search term here, not a constraint, so a populated
#: result under a wrong organism is expected. Verified live: hit counts move
#: 40 -> 14 -> 11 across three organisms, so the argument is NOT ignored.
SEARCH_SEMANTICS = {"locus_literature", "batch_locus_literature"}

#: Tools whose organism is hardcoded upstream (Arabidopsis-only endpoints).
#: Echo-checking them measures the hardcode, not the request.
ORGANISM_INERT = {
    "bar_gene_summary",
    "bar_efp_expression",
    "tair_locus_info",
    "locus_gene_rifs",
    "experimental_interactions",
    "aragwas_associations",
    "arabidopsis_natural_variation",
    "gramene_homologs",
    "batch_gramene_homologs",
    "batch_bar_gene_summary",
}

#: Curated fixtures for tools that need genomic coordinates rather than a locus.
#: Only Arabidopsis is curated; other organisms are skipped WITH a reason rather
#: than fed a guessed region, because a wrong-but-valid region would return real
#: data for the wrong place — a wrong-but-plausible result of our own making.
REGION_FIXTURES: dict[str, tuple[str, int, int]] = {
    "arabidopsis_thaliana": ("1", 1, 20000),
}
VEP_FIXTURES: dict[str, tuple[str, str]] = {
    "arabidopsis_thaliana": ("1:10000-10000:1", "C"),
}
JASPAR_MATRIX_ID = "MA0570.1"


class SkipReason(StrEnum):
    NO_ECHO_FIELD = "payload carries no comparable organism field"
    BACKEND_REJECTED = "backend correctly rejected this locus/organism pair"
    UPSTREAM_ERROR = "upstream/transport error"
    NO_FIXTURE = "no curated fixture for this organism"
    BLAST_EXCLUDED = "BLAST-backed; excluded from bulk sweep"
    SEARCH_TOOL = "organism is a search hint, not a constraint"
    ORGANISM_INERT = "organism hardcoded upstream"
    UNSUPPORTED_SHAPE = "no argument builder for this input signature"


_ECHO_FIELDS = ("organism", "species", "canonical", "ensembl_slug", "organism_slug")
_ENVELOPE_META = frozenset(
    {"tool", "input", "steps", "elapsed_s", "started_at", "query", "returned", "hitCount", "count"}
)

#: Non-empty lists that are NOT evidence the tool returned data. Two kinds:
#: input echoes (`sources`, `gene_names_searched`) and — importantly —
#: NEGATIVE results, i.e. explicit reports of what could NOT be resolved.
#:
#: This cost 12 fabricated findings. Every `go_enrichment` negative control was
#: flagged as an organism leak because the payload carried
#: `unmapped: ["Os01g0100100", ...]` alongside `enriched: []`, `mapped: 0`,
#: `total_terms: 0`. The tool was behaving exactly right — refusing to map rice
#: loci under Arabidopsis and SAYING SO instead of silently dropping them — and
#: the checker read that honesty as data. A tool that reports its failures well
#: must not be punished for it.
_NOT_DATA_FIELDS = frozenset(
    {"unmapped", "sources", "errors", "gene_names_searched", "name_only_matches"}
)


def _required(tool: Any) -> tuple[str, ...]:
    return tuple(sorted((tool.inputSchema or {}).get("required", [])))


def _accepts(tool: Any, field: str) -> bool:
    return field in (tool.inputSchema or {}).get("properties", {})


def build_args(
    tool: Any, locus: str, organism: str, second_locus: str
) -> tuple[dict[str, Any] | None, SkipReason | None]:
    """Construct a valid call for `tool`, or explain why we cannot."""
    req = _required(tool)
    args: dict[str, Any] = {}
    if _accepts(tool, "organism"):
        args["organism"] = organism

    if req == ("locus",):
        args["locus"] = locus
    elif req == ("loci",):
        args["loci"] = [locus, second_locus]
    elif req == ("locus_or_accession",):
        args["locus_or_accession"] = locus
    elif req == ("loci_or_accessions",):
        args["loci_or_accessions"] = [locus, second_locus]
    elif req == ("matrix_id",):
        args = {"matrix_id": JASPAR_MATRIX_ID}
    elif req == ("end", "region", "start"):
        fixture = REGION_FIXTURES.get(organism)
        if fixture is None:
            return None, SkipReason.NO_FIXTURE
        region, start, end = fixture
        args.update({"region": region, "start": start, "end": end})
    elif req == ("allele", "region"):
        fixture = VEP_FIXTURES.get(organism)
        if fixture is None:
            return None, SkipReason.NO_FIXTURE
        region, allele = fixture
        args.update({"region": region, "allele": allele})
    elif req == ("sequence",):
        return None, SkipReason.BLAST_EXCLUDED
    else:
        return None, SkipReason.UNSUPPORTED_SHAPE
    return args, None


def _echoed(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for f in _ECHO_FIELDS:
        v = payload.get(f)
        if isinstance(v, str) and v:
            return v
    return None


def _looks_populated(payload: Any) -> bool:
    """Real data, as opposed to an empty envelope. Conservative on purpose: a
    false positive here becomes a fabricated bug report."""
    if not isinstance(payload, dict):
        return bool(payload)
    if payload.get("found") is False:
        return False
    if "results" in payload and "errors" in payload:  # batch envelope
        return bool(payload.get("results"))
    if "result" in payload and "steps" in payload:  # synthesis envelope
        return bool(payload.get("result"))

    # An explicit zero-hit signal settles it regardless of what else is present.
    for zero_field in ("mapped", "total_terms", "returned", "hitCount"):
        if payload.get(zero_field) == 0:
            return False

    ignore = (
        {"locus", "organism", "found", "species", "canonical"} | _ENVELOPE_META | _NOT_DATA_FIELDS
    )
    for k, v in payload.items():
        if k in ignore:
            continue
        if isinstance(v, (list, dict)) and len(v) > 0:
            return True
    return False


def _corpus_loci(per_organism: int) -> list[tuple[str, str]]:
    corpus = json.loads(
        (Path(__file__).resolve().parent / "benchmark_annotations.expected.json").read_text()
    )
    records = corpus if isinstance(corpus, list) else corpus.get("loci", [])
    seen: dict[str, int] = defaultdict(int)
    out: list[tuple[str, str]] = []
    for rec in records:
        org = rec["organism"]
        if seen[org] >= per_organism:
            continue
        seen[org] += 1
        out.append((rec["locus_id"], org))
    return out


async def run(delay: float, per_organism: int, out_path: Path, only: str | None) -> int:
    pairs = _corpus_loci(per_organism)
    tools = [t for t in server.TOOLS if (only is None or t.name == only)]
    all_slugs = list(organisms.ORGANISMS)
    count_specs = defaultdict(list)
    for spec in COUNT_SPECS:
        count_specs[spec.tool].append(spec)

    findings: list[dict[str, Any]] = []
    skips: Counter[str] = Counter()
    stats = Counter()
    covered: set[str] = set()

    for index, tool in enumerate(tools, start=1):
        if tool.name in BLAST_BACKED:
            skips[SkipReason.BLAST_EXCLUDED] += 1
            continue

        # Emit progress per tool, flushed. Not cosmetic: a rate-limited sweep
        # runs for the better part of an hour, and a run that prints only its
        # final report is INDISTINGUISHABLE FROM A HUNG ONE. jobd's default idle
        # timeout is 3600s, so the silent version was SIGTERM'd one second past
        # the hour (exit -15 at 3601s) and lost every result it had gathered —
        # after making ~900 real calls to public APIs. Raising the timeout alone
        # would have left the job unobservable and merely moved the cliff.
        print(
            f"[{index}/{len(tools)}] {tool.name} "
            f"(calls={stats['calls']} echo_pass={stats['echo_pass']} "
            f"neg_correct={stats['neg_correct']} findings={len(findings)})",
            flush=True,
        )

        for locus, org in pairs:
            second = next((lo for lo, o in pairs if o == org and lo != locus), locus)
            args, why = build_args(tool, locus, org, second)
            if args is None:
                skips[why or SkipReason.UNSUPPORTED_SHAPE] += 1
                continue

            # --- positive call: echo + count invariants ---
            await asyncio.sleep(delay)
            stats["calls"] += 1
            try:
                payload = await server._dispatch(tool.name, dict(args))
                covered.add(tool.name)
            except Exception as exc:  # noqa: BLE001 - a sweep must never abort
                reason = (
                    SkipReason.BACKEND_REJECTED if _is_rejection(exc) else SkipReason.UPSTREAM_ERROR
                )
                skips[reason] += 1
                payload = None

            if payload is not None:
                if tool.name in ORGANISM_INERT:
                    skips[SkipReason.ORGANISM_INERT] += 1
                else:
                    echoed = _echoed(payload)
                    if echoed is None:
                        skips[SkipReason.NO_ECHO_FIELD] += 1
                    else:
                        r = check_organism_echo(org, echoed, tool.name)
                        if r.verdict is Verdict.FAIL:
                            stats["echo_fail"] += 1
                            findings.append(
                                {
                                    "kind": "organism_echo",
                                    "tool": tool.name,
                                    "locus": locus,
                                    "requested": org,
                                    "echoed": echoed,
                                    "detail": r.detail,
                                }
                            )
                        elif r.verdict is Verdict.PASS:
                            stats["echo_pass"] += 1
                        else:
                            skips[SkipReason.NO_ECHO_FIELD] += 1

                props = (tool.outputSchema or {}).get("properties", {})
                for spec in count_specs.get(tool.name, []):
                    desc = (props.get(spec.field) or {}).get("description", "")
                    cr = check_count_semantics(spec, payload, desc)
                    if cr.verdict is Verdict.FAIL:
                        stats["count_fail"] += 1
                        findings.append(
                            {
                                "kind": "count_semantics",
                                "tool": tool.name,
                                "locus": locus,
                                "detail": cr.detail,
                            }
                        )
                    elif cr.verdict is Verdict.PASS:
                        stats["count_pass"] += 1
                    tr = check_truncated_flag(spec, payload)
                    if tr.verdict is Verdict.FAIL:
                        stats["truncated_fail"] += 1
                        findings.append(
                            {
                                "kind": "truncated_flag",
                                "tool": tool.name,
                                "locus": locus,
                                "detail": tr.detail,
                            }
                        )
                    elif tr.verdict is Verdict.PASS:
                        stats["truncated_pass"] += 1

            # --- negative control ---
            if tool.name in SEARCH_SEMANTICS:
                skips[SkipReason.SEARCH_TOOL] += 1
                continue
            if tool.name in ORGANISM_INERT or not _accepts(tool, "organism"):
                skips[SkipReason.ORGANISM_INERT] += 1
                continue
            wrong = next((s for s in all_slugs if s != org), None)
            if wrong is None:
                continue
            neg_args, why = build_args(tool, locus, wrong, second)
            if neg_args is None:
                skips[why or SkipReason.UNSUPPORTED_SHAPE] += 1
                continue
            await asyncio.sleep(delay)
            stats["calls"] += 1
            try:
                payload = await server._dispatch(tool.name, dict(neg_args))
                if _looks_populated(payload):
                    stats["neg_LEAK"] += 1
                    findings.append(
                        {
                            "kind": "organism_leak",
                            "tool": tool.name,
                            "locus": locus,
                            "true_organism": org,
                            "declared_organism": wrong,
                            "detail": (
                                f"{tool.name} returned populated data for {locus} "
                                f"(a {org} locus) while organism={wrong} was declared"
                            ),
                        }
                    )
                else:
                    stats["neg_correct"] += 1
            except Exception as exc:  # noqa: BLE001 - a sweep must never abort
                if _is_rejection(exc):
                    stats["neg_correct"] += 1
                else:
                    skips[SkipReason.UPSTREAM_ERROR] += 1

    sweepable = [t.name for t in server.TOOLS if t.name not in BLAST_BACKED]
    report = {
        "stats": dict(stats),
        "skips": {str(k): v for k, v in skips.most_common()},
        "coverage": {
            "tools_total": len(server.TOOLS),
            "tools_sweepable": len(sweepable),
            "tools_reached": len(covered),
            "never_reached": sorted(set(sweepable) - covered),
        },
        "findings": findings,
    }
    out_path.write_text(json.dumps(report, indent=2))
    print(json.dumps({k: report[k] for k in ("stats", "skips", "coverage")}, indent=2))
    print(f"\n{len(findings)} finding(s) -> {out_path}")
    for f in findings[:25]:
        print(f"  [{f['kind']}] {f['tool']} {f.get('locus')}: {f['detail'][:130]}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--delay", type=float, default=3.0)
    p.add_argument("--per-organism", type=int, default=1, help="corpus loci per organism")
    p.add_argument("--only", type=str, default=None, help="sweep a single tool by name")
    p.add_argument("--out", type=Path, default=Path("scripts/full_surface_sweep.results.json"))
    a = p.parse_args()
    return asyncio.run(run(a.delay, a.per_organism, a.out, a.only))


if __name__ == "__main__":
    raise SystemExit(main())
