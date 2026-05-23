# plant-genomics-mcp v0.9.0 — Competitor Landscape

**Date:** 2026-05-23
**Researcher:** general-purpose agent (opus)
**Scope:** Survey of MCP servers in the bioinformatics / genomics / plant-science adjacent space; comparator table; pre-1.0 positioning recommendations.

---

## Competitor MCP servers reviewed

**Bioinformatics-adjacent:**

- **augmented-nature** (https://augmentednature.com) — A catalog of single-purpose MCP wrappers around individual public APIs: Ensembl, UniProt, KEGG, STRING-DB, and others. Each is a thin TypeScript wrapper exposing a few tools. **Stdio-only.** No published tests; no CI. JS/TS implementations, MIT-licensed. Highest discoverability via Anthropic's MCP registry of any bio entry.
- **bio-mcp** — Python CLI wrappers around BLAST, samtools, etc. Local-tool focus, not remote-API. Stdio-only. Sparse test coverage.
- **gget-mcp** — From the longevity-genie group; wraps the `gget` Python library for human/model-organism genomics. Stdio-only. Some test coverage.
- **aurora-mcp** — Only plant-adjacent server observed beyond plant-genomics-mcp; ag-economics / climate data focus, no genomics. Stdio-only.

**General-purpose comparators (for transport/auth patterns):**

- Multiple non-bio MCP servers shipping Streamable-HTTP (FastMCP) are using bearer-token auth on the public endpoint; this is becoming the de-facto pattern for hosted MCPs.

---

## Comparator table

| Axis                           | plant-genomics-mcp v0.9                                                   | augmented-nature              | bio-mcp      | gget-mcp          |
| ------------------------------ | ------------------------------------------------------------------------- | ----------------------------- | ------------ | ----------------- |
| Plant-organism coverage        | 12 organisms (canonical registry)                                         | Generic; mostly human-default | Generic      | Generic           |
| Backends per server            | 13 (9 live + 2 stubs + 4 synthesis)                                       | 1 per server                  | 1 per server | 1 (gget umbrella) |
| Cross-backend synthesis        | 4 synthesis tools (orthology, GO/lit/expr, partners, homologs)            | None                          | None         | Some              |
| Transports                     | stdio **+ Streamable-HTTP**                                               | stdio                         | stdio        | stdio             |
| Hosted endpoint                | Yes (Tailscale Funnel demo)                                               | No                            | No           | No                |
| Test count                     | ~262 (pytest, pytest-httpx)                                               | ~0                            | sparse       | partial           |
| Real-execution tests           | Yes — HTTP transport spins real uvicorn; per-backend live rice tests      | No                            | No           | partial           |
| Resources primitive            | 4 resources (coverage matrix, backend status, cache stats, phytozome map) | No                            | No           | No                |
| Prompts primitive              | 2 prompts                                                                 | No                            | No           | No                |
| Output schemas                 | Pydantic outputSchema on all tools                                        | No                            | No           | partial           |
| Error taxonomy                 | Typed `PlantGenomicsError` subclasses + `[ClassName]` prefix              | Untyped                       | Untyped      | partial           |
| License                        | MIT                                                                       | MIT                           | MIT          | MIT               |
| CI badges in README            | No                                                                        | No                            | No           | No                |
| Glama / PulseMCP quality score | Not yet                                                                   | Listed                        | Listed       | Listed            |
| One-liner install (uvx / npx)  | Not yet (PyPI deferred)                                                   | Yes (npx)                     | Yes (pip)    | Yes (uvx)         |
| Auth on hosted endpoint        | **No (BLOCKER)**                                                          | N/A                           | N/A          | N/A               |
| Stub-backend story             | TAIR + PlantCyc as structured redirects                                   | N/A                           | N/A          | N/A               |
| Published benchmarks           | No                                                                        | No                            | No           | No                |

---

## Where plant-genomics-mcp leads

1. **Plant-first coverage** — the only MCP focused on plant genomics with a canonical 12-organism registry, dispatched uniformly via `organism=`.
2. **Cross-backend synthesis** — 4 synthesis tools that compose live backends in parallel with a typed `SynthesisEnvelope` (phase-0 validation, phase-1 sequenced, phase-2 parallel-gather with `OrganismNotSupported → skipped` handling). No other observed bio-MCP does this.
3. **Transport parity** — stdio AND Streamable-HTTP shipped together; hosted endpoint live as a demo.
4. **Test maturity** — ~262 tests vs. zero published from competitor catalog. Real-execution boundary tests (uvicorn spinup, per-backend rice live probes).
5. **MCP primitive coverage** — tools + resources + prompts. Most competitors ship tools only.
6. **Error taxonomy** — typed exception hierarchy with `[ClassName]` wire prefix that LLM clients can route on. Competitors return untyped errors.

## Where plant-genomics-mcp lags

1. **No auth on hosted endpoint** — leading blocker; current state is "no MCP should ship publicly without bearer-token at minimum." (See security B-1.)
2. **CI badges not surfaced** — README has no green-check badge for the CI workflow even though tests run on push. Easy lift.
3. **No PyPI release** — no `uvx plant-genomics-mcp` install path. The MCP registry conventions are converging on `uvx` / `npx` one-liners.
4. **No Glama quality score** — Glama auto-scores listed servers; no listing → no score → less discoverability via the Glama gateway.
5. **Stub backends** — TAIR + PlantCyc ship as informational redirects. Defensible (their per-locus REST is subscription-gated, last probed 2026-05-21), but the 1.0 story needs explicit framing in README and registry copy.
6. **Single-backend depth** — augmented-nature's per-backend wrappers expose more of each API surface (e.g. more Ensembl endpoints) than plant-genomics-mcp covers. For depth-driven workflows, callers may want both.
7. **No published benchmarks** — no real-task evals demonstrating that the synthesis tools actually improve agent task completion. Hard to claim parity without numbers.

---

## 10 ranked 1.0-readiness recommendations

1. **Auth + rate-limiting on hosted endpoint.** Bearer token middleware + per-IP rate limit. Blocker.
2. **Surface CI badges.** Add CI / coverage / version badges to top of README. Easy lift, high credibility signal.
3. **PyPI release with `uvx` entry.** `uvx plant-genomics-mcp` should Just Work. The published `pyproject.toml` already declares `[project.scripts]` correctly — just needs `twine upload`.
4. **Lock API stability promise** at 1.0 for `organism=` slugs, tool names, return shapes, and `SynthesisEnvelope`. Document in README. (See feature audit Axis 5.)
5. **Resolve TAIR/PlantCyc stub story** — keep as structured redirects with a clear pointer to the alternatives (`ensembl_plants_lookup_locus`, `phytozome_lookup_locus`). Document explicitly in README and the tool descriptions.
6. **MCP-registry submission** — submit to modelcontextprotocol.io directory, PulseMCP, Glama. Already done partially; the v0.9 release is a good moment to refresh listings with the multi-organism story.
7. **Published walkthrough / proof transcripts** — demonstrate cross-organism orthology probe end-to-end in a markdown doc. Sells the synthesis-tool value prop.
8. **Per-backend latency benchmark** — capture wall-time per tool on a representative locus set; helps clients budget. Foundation for a future "model picker" doc.
9. **Plant-first onboarding doc** — short "Why this MCP" page targeting plant-biology agents specifically; differentiates from generic Ensembl wrappers.
10. **Tighten input schemas** — add `maxLength` to `sequence`, `maxItems` to `loci` arrays (already done in some places, audit for gaps), and confirm SDK-side server validation actually triggers (security open question #2).
