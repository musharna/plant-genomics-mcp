"""Real-execution proof-transcript generator for the two prompts.

Drives the underlying client functions (NOT the MCP wire) for both
canonical chains and dumps the captured inputs/outputs to JSON so the
sibling Markdown files can quote real upstream responses.

Usage::

    .venv/bin/python examples/_run_chain.py

Why direct module calls instead of MCP stdio: the goal is to demonstrate
the BACKEND behavior — the MCP envelope is identical to what the server
serializes around these dicts, so going through stdio adds latency and
test-server orchestration without showing anything new. The `prompts/get`
output (rendered in prompts.py) already captures the chain framing an LLM
would receive.

Runtime budget:
  * analyze_locus chain: ~5-15s (5 fast REST calls)
  * find_homologs chain: ~60-180s (BLAST polls have a 60s NCBI floor)

Each chain writes:
  * examples/<chain>_<query>.json  — full raw outputs per step
  * examples/<chain>_<query>.md    — human-readable narrative

If you re-run, the .json files will be overwritten with fresh upstream
responses (which may have drifted — that's the point of capturing them).
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx

from plant_genomics_mcp import (
    blast,
    ensembl_plants,
    europe_pmc,
    quickgo,
    uniprot,
)

EXAMPLES_DIR = Path(__file__).resolve().parent

ANALYZE_LOCUS_QUERY = "AT1G01010"
ANALYZE_LOCUS_SPECIES = "arabidopsis_thaliana"

# NAC domain of Arabidopsis NAC001 (AT1G01010 product) — short enough to
# keep BLAST runtime tractable but distinctive enough that the top hits
# should be plant NAC-family proteins.
FIND_HOMOLOGS_SEQUENCE = (
    "MEDQVGFGFRPNDEELVGHYLRNKIESQTSRSAIEVDLNKCEPWDLPGKAKMGEKEWYFFCQRDRKYPTGTRTNRATVAGFW"
    "KATGRDKAIYSGKSLVGMKKTLVFYKGRAPHGQKTDWIMHEYRLEGNHAHSRPNALENGAWSVAGCRVHKMQNQNHHQNH"
)
FIND_HOMOLOGS_LABEL = "AT1G01010_NAC_domain"


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n")
    print(f"  wrote {path.relative_to(EXAMPLES_DIR.parent)}")


def _excerpt(blob: Any, *, max_chars: int = 2000) -> str:
    """Pretty-print a JSON-able blob, truncating with marker if long."""
    text = json.dumps(blob, indent=2, sort_keys=False)
    if len(text) <= max_chars:
        return text
    return (
        text[:max_chars].rstrip()
        + f"\n... [{len(text) - max_chars} bytes truncated; see full .json]"
    )


async def run_analyze_locus() -> dict[str, Any]:
    """Drive the analyze_locus chain end-to-end and capture per-step output."""
    locus = ANALYZE_LOCUS_QUERY
    species = ANALYZE_LOCUS_SPECIES
    print(f"\n[analyze_locus] locus={locus} species={species}")
    captured: dict[str, Any] = {
        "chain": "analyze_locus",
        "query": {"locus": locus, "species": species},
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "steps": [],
    }

    async with httpx.AsyncClient() as client:
        t0 = time.monotonic()
        step1 = await ensembl_plants.lookup_locus(client, locus, species=species)
        captured["steps"].append(
            {
                "step": 1,
                "tool": "ensembl_plants_lookup_locus",
                "input": {"locus": locus, "species": species},
                "elapsed_s": round(time.monotonic() - t0, 2),
                "output": step1,
            }
        )
        print(f"  step 1 ensembl_plants_lookup_locus → {step1.get('display_name')}")

        t0 = time.monotonic()
        step2 = await ensembl_plants.lookup_xrefs(client, locus, species=species)
        captured["steps"].append(
            {
                "step": 2,
                "tool": "get_gene_xrefs",
                "input": {"locus": locus, "species": species},
                "elapsed_s": round(time.monotonic() - t0, 2),
                "output": step2,
            }
        )
        print(f"  step 2 get_gene_xrefs → {len(step2)} xrefs")

        t0 = time.monotonic()
        step3 = await uniprot.lookup_locus(client, locus)
        captured["steps"].append(
            {
                "step": 3,
                "tool": "resolve_locus_to_uniprot",
                "input": {"locus": locus},
                "elapsed_s": round(time.monotonic() - t0, 2),
                "output": step3,
            }
        )
        uniprot_acc = step3.get("primaryAccession")
        print(f"  step 3 resolve_locus_to_uniprot → {uniprot_acc}")

        t0 = time.monotonic()
        step4 = await europe_pmc.lookup_locus(client, locus, species=species, size=10)
        captured["steps"].append(
            {
                "step": 4,
                "tool": "locus_literature",
                "input": {"locus": locus, "species": species, "size": 10},
                "elapsed_s": round(time.monotonic() - t0, 2),
                "output": step4,
            }
        )
        n_hits = len(step4.get("hits", []))
        print(f"  step 4 locus_literature → {n_hits} hits (total {step4.get('total_hits')})")

        t0 = time.monotonic()
        step5 = await quickgo.lookup_by_uniprot(client, uniprot_acc)
        captured["steps"].append(
            {
                "step": 5,
                "tool": "locus_go_annotations",
                "input": {"locus": locus, "uniprot_accession": uniprot_acc},
                "elapsed_s": round(time.monotonic() - t0, 2),
                "output": step5,
            }
        )
        n_terms = step5.get("annotation_count", 0)
        print(f"  step 5 locus_go_annotations → {n_terms} annotations")

    return captured


async def run_find_homologs() -> dict[str, Any]:
    """Drive the find_homologs chain end-to-end and capture per-step output."""
    sequence = FIND_HOMOLOGS_SEQUENCE
    program = "blastp"
    label = FIND_HOMOLOGS_LABEL
    print(f"\n[find_homologs] label={label} program={program} len={len(sequence)}")
    captured: dict[str, Any] = {
        "chain": "find_homologs",
        "query": {"label": label, "program": program, "sequence_length": len(sequence)},
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "steps": [],
    }

    async with httpx.AsyncClient(timeout=None) as client:
        t0 = time.monotonic()
        step1 = await blast.blast_sequence(
            client,
            sequence,
            program=program,
            hitlist_size=10,
            max_wait=600.0,
        )
        captured["steps"].append(
            {
                "step": 1,
                "tool": "blast_sequence",
                "input": {
                    "program": program,
                    "hitlist_size": 10,
                    "sequence_length": len(sequence),
                },
                "elapsed_s": round(time.monotonic() - t0, 2),
                "output": step1,
            }
        )
        hits = step1.get("hits", [])
        print(f"  step 1 blast_sequence → RID={step1.get('rid')} hits={len(hits)}")

        captured["steps"].append(
            {
                "step": 2,
                "tool": "resolve_locus_to_uniprot (per-hit)",
                "input": {
                    "strategy": "extract UniProt-like accessions from top 3 hits and resolve"
                },
                "resolved": [],
            }
        )
        top_hits = hits[:3]
        for i, hit in enumerate(top_hits, start=1):
            acc = hit.get("accession", "")
            row: dict[str, Any] = {
                "hit_rank": i,
                "blast_accession": acc,
                "description": hit.get("description", ""),
                "evalue": hit.get("evalue"),
                "bit_score": hit.get("bit_score"),
            }
            # NCBI accessions like XP_*, NP_* are NOT UniProt; only attempt
            # lookup if the accession matches the UniProt-ID shape.
            if _looks_like_uniprot(acc):
                try:
                    t0 = time.monotonic()
                    resolved = await uniprot.lookup_locus(client, acc)
                    row["uniprot_resolved"] = resolved
                    row["resolve_elapsed_s"] = round(time.monotonic() - t0, 2)
                except Exception as e:  # noqa: BLE001 — record class+msg for the transcript
                    row["uniprot_resolved"] = None
                    row["resolve_error"] = f"{type(e).__name__}: {e}"
            else:
                row["uniprot_resolved"] = None
                row["resolve_skipped_reason"] = "accession not UniProt-shaped (RefSeq/GenBank)"
            captured["steps"][-1]["resolved"].append(row)
            print(f"    hit {i}: {acc} ({row.get('uniprot_resolved', {}) or '(skipped)'!s:.80})")

    return captured


_UNIPROT_RE = __import__("re").compile(
    r"^[OPQ][0-9][A-Z0-9]{3}[0-9]$|^[A-NR-Z][0-9][A-Z][A-Z0-9]{2}[0-9]$"
)


def _looks_like_uniprot(acc: str) -> bool:
    """UniProtKB accession pattern. Matches P12345, Q9LIV2, A0A1B2C3D4-style trims."""
    if not acc:
        return False
    base = acc.split(".")[0]
    return bool(_UNIPROT_RE.match(base))


def render_analyze_locus_md(data: dict[str, Any]) -> str:
    q = data["query"]
    steps = data["steps"]

    sections = [
        f"# Example chain — `analyze_locus` for {q['locus']}",
        "",
        f"**Query:** `{q['locus']}` (species `{q['species']}`)",
        f"**Captured:** {data['captured_at']}",
        "",
        "Real-execution transcript of the five-tool chain rendered by the "
        "`analyze_locus` MCP prompt. Outputs below are verbatim from upstream "
        "(Ensembl Plants, UniProt, Europe PMC, QuickGO) at capture time and "
        "may drift on re-run — the matching `.json` sibling preserves the "
        "full payload.",
        "",
        "---",
        "",
    ]

    for s in steps:
        sections.append(f"## Step {s['step']} — `{s['tool']}`")
        sections.append("")
        sections.append(f"**Input:** `{json.dumps(s['input'])}`  ")
        sections.append(f"**Elapsed:** {s['elapsed_s']}s")
        sections.append("")
        sections.append("```json")
        sections.append(_excerpt(s["output"]))
        sections.append("```")
        sections.append("")

    return "\n".join(sections)


def render_find_homologs_md(data: dict[str, Any]) -> str:
    q = data["query"]
    steps = data["steps"]
    blast_step = steps[0]
    resolve_step = steps[1]

    sections = [
        f"# Example chain — `find_homologs` for {q['label']}",
        "",
        f"**Query label:** `{q['label']}` (program `{q['program']}`, "
        f"sequence length {q['sequence_length']})",
        f"**Captured:** {data['captured_at']}",
        "",
        "Real-execution transcript of the BLAST → per-hit-resolve chain "
        "rendered by the `find_homologs` MCP prompt. The query sequence is "
        "the NAC DNA-binding domain of Arabidopsis NAC001 (AT1G01010 "
        "product); the top BLAST hits should be plant NAC-family proteins. "
        "Full payload preserved in the matching `.json` sibling.",
        "",
        "---",
        "",
        "## Step 1 — `blast_sequence`",
        "",
        f"**Input:** `{json.dumps(blast_step['input'])}`  ",
        f"**Elapsed:** {blast_step['elapsed_s']}s  ",
        f"**RID:** `{blast_step['output'].get('rid')}`",
        "",
        "Top hits (full set in .json):",
        "",
    ]

    hits = blast_step["output"].get("hits", [])
    if hits:
        sections.append("| # | accession | e-value | bit score | identity | description |")
        sections.append("|---|---|---|---|---|---|")
        for i, h in enumerate(hits[:10], start=1):
            desc = (h.get("description") or "").replace("|", "\\|")[:80]
            sections.append(
                f"| {i} | `{h.get('accession')}` | "
                f"{h.get('evalue')} | {h.get('bit_score')} | "
                f"{h.get('identity') or '—'} | {desc} |"
            )
    sections.extend(
        [
            "",
            "## Step 2 — per-hit `resolve_locus_to_uniprot`",
            "",
            "For each of the top 3 BLAST hits we attempt a UniProt lookup if the "
            "accession matches the UniProtKB ID pattern. NCBI RefSeq / GenBank "
            "accessions are noted but not resolved (out of scope for this tool).",
            "",
        ]
    )
    for r in resolve_step["resolved"]:
        sections.append(f"### Hit #{r['hit_rank']} — `{r['blast_accession']}`")
        sections.append("")
        sections.append(
            f"- description: {r.get('description', '')[:200]}\n"
            f"- e-value: {r.get('evalue')}\n"
            f"- bit score: {r.get('bit_score')}\n"
        )
        if r.get("uniprot_resolved"):
            sections.append("```json")
            sections.append(_excerpt(r["uniprot_resolved"], max_chars=1200))
            sections.append("```")
        else:
            note = r.get("resolve_skipped_reason") or r.get("resolve_error") or "(no UniProt match)"
            sections.append(f"_skipped:_ {note}")
        sections.append("")

    return "\n".join(sections)


async def main() -> None:
    print("Generating real-execution proof transcripts...")

    a = await run_analyze_locus()
    json_path = EXAMPLES_DIR / f"analyze_locus_{ANALYZE_LOCUS_QUERY}.json"
    md_path = EXAMPLES_DIR / f"analyze_locus_{ANALYZE_LOCUS_QUERY}.md"
    _write_json(json_path, a)
    md_path.write_text(render_analyze_locus_md(a))
    print(f"  wrote {md_path.relative_to(EXAMPLES_DIR.parent)}")

    h = await run_find_homologs()
    json_path = EXAMPLES_DIR / f"find_homologs_{FIND_HOMOLOGS_LABEL}.json"
    md_path = EXAMPLES_DIR / f"find_homologs_{FIND_HOMOLOGS_LABEL}.md"
    _write_json(json_path, h)
    md_path.write_text(render_find_homologs_md(h))
    print(f"  wrote {md_path.relative_to(EXAMPLES_DIR.parent)}")

    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
