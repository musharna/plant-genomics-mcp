"""Phase C — live sweep for wrong-but-plausible values.

Drives real backends over the benchmark corpus and applies the invariants from
``semantic_invariants``. Two sweeps:

**Echo sweep** — call each organism-sensitive tool with the CORRECT organism and
assert the species it echoes back is the species that was asked for.

**Negative-control sweep** — the high-yield one, and the only check here that
needs no external oracle. Take a locus that belongs to organism A, ask for it
while declaring organism B, and see what happens. A correct backend raises
``NotFoundError`` (that locus does not exist in B) or returns ``found=False``.
A backend that silently ignores the organism parameter instead hands back A's
data wearing B's label — well-formed, entirely believable, and wrong.

Operational rules, deliberately conservative because this hammers 23 public
scientific APIs that cost other people money to run:

* ``--delay`` seconds between EVERY call (default 3.0).
* ``blast_sequence`` is excluded outright — each call enqueues a real job in
  NCBI's rate-limited queue, which is why it is the server's only
  ``idempotentHint=False`` tool. It is not sweepable and does not belong here.
* Tools that documentedly ignore ``organism`` are declared in
  ``ORGANISM_IGNORING`` and skipped in the negative-control sweep. Flagging them
  would manufacture false findings — the failure mode this audit exists to
  avoid, so the exclusions carry their reason inline.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

from benchmark_annotations import _TOOLS, DEFAULT_EXPECTED_JSON  # noqa: E402
from semantic_invariants import Verdict, check_organism_echo  # noqa: E402

from plant_genomics_mcp import organisms  # noqa: E402
from plant_genomics_mcp.errors import (  # noqa: E402
    NotFoundError,
    OrganismNotSupported,
    PlantGenomicsError,
)

# Tools whose organism argument is documentedly inert. Passing a wrong organism
# to these is NOT a bug, so they are excluded from the negative-control sweep.
ORGANISM_IGNORING: dict[str, str] = {
    "gramene.lookup_homologs": "takes homology_type, not organism — organism is encoded in the locus stem",
    "bar.gene_summary": "Arabidopsis-only; taxon 3702 is hardcoded in the URL path",
    "bar.efp_expression": "Arabidopsis-only; taxon 3702 is hardcoded in the URL path",
    "organisms.resolve": "resolves the organism argument itself; that IS its job",
}

# Tools where `organism` is a SEARCH HINT rather than an identity constraint.
# Europe PMC turns it into a free-text AND term ("AT1G01010 AND rice"), so a
# populated result under the wrong organism is expected behaviour, not a leak —
# and hit counts demonstrably move (40 -> 14 -> 11 across three organisms), so
# the argument is not being ignored. Sweeping these as negative controls would
# manufacture findings.
SEARCH_SEMANTICS: dict[str, str] = {
    "europe_pmc.lookup_locus": "organism becomes a free-text AND term, not a filter",
}

# BLAST-backed tools are never swept. Each invocation enqueues a real job in
# NCBI's rate-limited queue — the reason blast_sequence is the server's only
# ``idempotentHint=False`` tool. Sweeping them would abuse a free public
# service rather than test anything.
BLAST_BACKED: dict[str, str] = {
    "blast.blast_sequence": "submits an NCBI BLAST job per call",
    "synthesis.find_homologs_synth": "BLAST-backed; also takes a sequence, not a locus",
    "synthesis.consensus_homologs": "fans out to BLAST alongside Gramene",
}

# Fields a backend may use to echo which species it answered for.
_ECHO_FIELDS = ("organism", "species", "canonical", "ensembl_slug", "organism_slug", "taxon")

#: A negative control is satisfied by any of these — all mean "I did not find
#: that locus in that organism", which is the correct answer.
_CORRECT_REJECTIONS = (NotFoundError, OrganismNotSupported)


def _echoed_organism(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for f in _ECHO_FIELDS:
        v = payload.get(f)
        if isinstance(v, str) and v:
            return v
    return None


#: Envelope bookkeeping that is present whether or not anything was found.
#: Counting these as data made the first draft of this sweep report three
#: "leaks" that were entirely my checker's fault — a synthesis envelope carries
#: `steps`/`elapsed_s` even when `result` is `{}`. Left explicit as a warning.
_ENVELOPE_METADATA = frozenset(
    {"tool", "input", "steps", "elapsed_s", "started_at", "query", "returned", "hitCount"}
)


def _looks_populated(payload: Any) -> bool:
    """Did we get real DATA back, as opposed to an empty/not-found envelope?

    Deliberately conservative: a false "populated" here becomes a fabricated
    bug report, which is the exact failure this audit exists to eliminate.
    """
    if not isinstance(payload, dict):
        return bool(payload)
    if payload.get("found") is False:
        return False
    # Synthesis tools wrap everything in an envelope; only `result` is data.
    if "result" in payload and "steps" in payload:
        result = payload.get("result")
        return bool(result)
    ignore = {"locus", "organism", "found", "tool", "species", "canonical"} | _ENVELOPE_METADATA
    for k, v in payload.items():
        if k in ignore:
            continue
        if isinstance(v, (list, dict)) and len(v) > 0:
            return True
        if isinstance(v, int) and k.endswith("count") and v > 0:
            return True
    return False


async def _call(tool: str, client: httpx.AsyncClient, locus: str, organism: str) -> Any:
    """Invoke a backend and normalise the payload to a plain dict.

    Synthesis tools return a pydantic ``SynthesisEnvelope``, not a dict. Missing
    that made every synthesis negative control report a leak: ``isinstance(obj,
    dict)`` was False, so the populated-check fell through to ``bool(obj)``,
    which is True for any model instance. Four fabricated findings before this
    line existed.
    """
    payload = await _TOOLS[tool](client, locus, organism)
    dump = getattr(payload, "model_dump", None)
    return dump() if callable(dump) else payload


async def run(delay: float, limit: int | None, out_path: Path) -> int:
    corpus = json.loads(Path(DEFAULT_EXPECTED_JSON).read_text())
    records = corpus if isinstance(corpus, list) else corpus.get("loci", [])
    if limit:
        records = records[:limit]

    all_slugs = list(organisms.ORGANISMS)
    findings: list[dict[str, Any]] = []
    stats = {
        "echo_pass": 0,
        "echo_fail": 0,
        "echo_skip": 0,
        "neg_correct": 0,
        "neg_LEAK": 0,
        "neg_skip": 0,
        "calls": 0,
    }

    async with httpx.AsyncClient() as client:
        for rec in records:
            locus, org = rec["locus_id"], rec["organism"]
            for tool in _TOOLS:
                if tool in BLAST_BACKED:
                    continue
                # Tools with a hardcoded organism echo that organism no matter
                # what was asked, so an echo check on them measures nothing and
                # would fire on every non-Arabidopsis locus. `organisms.resolve`
                # is the exception: echoing back what it resolved IS its job.
                if tool in ORGANISM_IGNORING and tool != "organisms.resolve":
                    continue

                # --- echo sweep: correct organism, must echo that organism ---
                try:
                    await asyncio.sleep(delay)
                    stats["calls"] += 1
                    payload = await _call(tool, client, locus, org)
                    echoed = _echoed_organism(payload)
                    if echoed is None:
                        stats["echo_skip"] += 1
                    else:
                        r = check_organism_echo(org, echoed, f"{tool}")
                        if r.verdict is Verdict.FAIL:
                            stats["echo_fail"] += 1
                            findings.append(
                                {
                                    "kind": "organism_echo",
                                    "tool": tool,
                                    "locus": locus,
                                    "requested": org,
                                    "echoed": echoed,
                                    "detail": r.detail,
                                }
                            )
                        elif r.verdict is Verdict.PASS:
                            stats["echo_pass"] += 1
                        else:
                            stats["echo_skip"] += 1
                except _CORRECT_REJECTIONS:
                    stats["echo_skip"] += 1
                except (TimeoutError, PlantGenomicsError, httpx.HTTPError):
                    stats["echo_skip"] += 1

                # --- negative control: declare the WRONG organism ---
                if tool in ORGANISM_IGNORING or tool in SEARCH_SEMANTICS:
                    stats["neg_skip"] += 1
                    continue
                wrong = next((s for s in all_slugs if s != org), None)
                if wrong is None:
                    continue
                try:
                    await asyncio.sleep(delay)
                    stats["calls"] += 1
                    payload = await _call(tool, client, locus, wrong)
                    if _looks_populated(payload):
                        stats["neg_LEAK"] += 1
                        findings.append(
                            {
                                "kind": "organism_leak",
                                "tool": tool,
                                "locus": locus,
                                "true_organism": org,
                                "declared_organism": wrong,
                                "detail": (
                                    f"{tool} returned populated data for {locus} "
                                    f"(a {org} locus) while organism={wrong} was "
                                    f"declared — the organism argument appears "
                                    f"not to constrain the result"
                                ),
                                "echoed": _echoed_organism(payload),
                            }
                        )
                    else:
                        stats["neg_correct"] += 1
                except _CORRECT_REJECTIONS:
                    stats["neg_correct"] += 1
                except (TimeoutError, PlantGenomicsError, httpx.HTTPError):
                    stats["neg_skip"] += 1

    out_path.write_text(json.dumps({"stats": stats, "findings": findings}, indent=2))
    print(json.dumps(stats, indent=2))
    print(f"\n{len(findings)} finding(s) -> {out_path}")
    for f in findings[:20]:
        print(f"  [{f['kind']}] {f['tool']} {f['locus']}: {f['detail'][:140]}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--delay", type=float, default=3.0, help="seconds between EVERY upstream call")
    p.add_argument("--limit", type=int, default=None, help="only the first N corpus loci")
    p.add_argument("--out", type=Path, default=Path("scripts/live_semantic_sweep.results.json"))
    a = p.parse_args()
    return asyncio.run(run(a.delay, a.limit, a.out))


if __name__ == "__main__":
    raise SystemExit(main())
