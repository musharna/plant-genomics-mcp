# P3 (v0.7) — Bio-Breadth Design

> Status: drafted 2026-05-21. Awaiting user review before implementation-plan handoff.

## Goal

Extend `plant-genomics-mcp` from 15 tools to 23 by adding four new biological-context backends — homology (Gramene), pathways (KEGG), interactions (STRING), and coexpression (ATTED-II) — plus a `biological_context` MCP prompt that chains them. Arabidopsis-first; fallback backends (OMA, Reactome, IntAct, EBI Expression Atlas) deferred to P4 (v0.8).

## Scope decisions (locked during brainstorming, 2026-05-21)

| Decision       | Choice                                                   | Rationale                                                                                                                                                 |
| -------------- | -------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Organism scope | Arabidopsis-only (`arabidopsis_thaliana`, taxid 3702)    | No multi-organism resolver layer this release; matches v0.x default species across existing tools; lowest-risk path to validating each upstream's quirks. |
| Domains        | All four: homology, pathways, interactions, coexpression | User-selected. Each gets one primary backend in v0.7.                                                                                                     |
| Staging        | Primary backend only per domain                          | 4 tools + 4 batch variants = 8 new tools. Fallback backends slip to v0.8 once v0.7 is field-tested.                                                       |
| Prompts        | Add one new prompt: `biological_context`                 | Three prompts total. `analyze_locus` + `find_homologs` unchanged.                                                                                         |

## Approach

**Mirror the v0.6 module pattern exactly** — one module per upstream backend, registered in `server.py`'s TOOLS list with a Pydantic output model. No new abstractions. Each module exports `fetch_*` (single-locus async) and `batch_fetch_*` (asyncio.gather under a Semaphore(5)). Caching via existing `TTLCache` instances registered with the existing `register_cache(name, cache)` helper; errors raised as existing `PlantGenomicsError` subclasses.

Two rejected alternatives, recorded for the record:

- **Domain-grouped subpackage** (`biocontext/{homology,pathways,interactions,expression}.py`) — premature; no other subpackage exists in v0.6. Revisit in v0.8 if `server.py` crosses ~1200 lines.
- **Shared `_simple_rest_client` helper** — premature; KEGG returns plain TSV needing line-by-line parsing, STRING's JSON has nested A/B identifier columns that need flattening, Gramene paginates, ATTED-II needs anti-bot headers. Different parsers, helper would erode.

## Tool surface (8 new tools)

| Tool                        | Upstream               | Endpoint                                                                                                               | Input                                                              | Output (Pydantic model)                                                                                                                     |
| --------------------------- | ---------------------- | ---------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------- |
| `gramene_homologs`          | `data.gramene.org` v69 | `GET /v69/genes?idList={locus}&fl=homology`                                                                            | `locus`, optional `homology_type` ∈ {`ortholog`, `paralog`, `all`} | `GrameneHomologs{locus, total, homologs[Homolog{target_locus, target_species, type, confidence}]}`                                          |
| `kegg_pathways`             | `rest.kegg.jp`         | `GET /link/pathway/ath:{locus}` then `GET /get/path:ath{NNNNN}` for each                                               | `locus`                                                            | `KeggPathways{locus, kegg_gene_id, pathways[Pathway{id, name, class}], errors[]}`                                                           |
| `string_interactions`       | `string-db.org`        | `GET /api/json/interaction_partners?identifiers={accession}&species=3702&limit={N}&caller_identity=plant-genomics-mcp` | `locus_or_accession`, optional `limit` (default 20, cap 500)       | `StringInteractions{query, accession, organism_taxid, partners[Partner{accession, preferred_name, score, escore, dscore, tscore, pscore}]}` |
| `atted_coexpression`        | `atted.jp`             | `GET /api/coex/Ath-u/{locus}/{top_n}`                                                                                  | `locus`, optional `top_n` (default 25, cap 300)                    | `AttedCoexpression{locus, atted_release, neighbors[CoexNeighbor{locus, mutual_rank, score}]}`                                               |
| `batch_gramene_homologs`    | same                   | same (per locus)                                                                                                       | `loci: list[str]` (cap 50)                                         | `BatchGrameneHomologs{results: list[GrameneHomologs \| BatchErrorEntry]}`                                                                   |
| `batch_kegg_pathways`       | same                   | same                                                                                                                   | `loci: list[str]` (cap 50)                                         | `BatchKeggPathways{results: list[KeggPathways \| BatchErrorEntry]}`                                                                         |
| `batch_string_interactions` | same                   | same                                                                                                                   | `loci_or_accessions: list[str]` (cap 50)                           | `BatchStringInteractions{results: list[StringInteractions \| BatchErrorEntry]}`                                                             |
| `batch_atted_coexpression`  | same                   | same                                                                                                                   | `loci: list[str]` (cap 50)                                         | `BatchAttedCoexpression{results: list[AttedCoexpression \| BatchErrorEntry]}`                                                               |

### Per-backend HTTP shape and error mapping

**`gramene_homologs`**

- `GET https://data.gramene.org/v69/genes?idList={locus}&fl=homology` returns JSON array
- `[0].homology[]` entries have `homology_type` (e.g. `ortholog_one2one`), `target_locus`, `target_species`, `target_protein_id`, `dn`, `ds`, `goc_score`, `is_high_confidence`
- Filter: `ortholog` → entries starting `ortholog_`; `paralog` → entries starting `paralog_`; `all` → everything
- `is_high_confidence` → `Homolog.confidence` ∈ {`high`, `low`}
- Empty `data[]` / missing `homology` → `NotFoundError("locus {x} not found in Gramene v69")`
- 5xx → `UpstreamUnavailableError`, 429 → retry then `RateLimitError`

**`kegg_pathways`**

- KEGG REST returns plain TSV (not JSON)
- Two-call sequence:
  1. `GET https://rest.kegg.jp/link/pathway/ath:{locus_lowercased}` → `ath:{locus}\tpath:ath{NNNNN}\n` per line; empty body = not found
  2. For each pathway ID: `GET https://rest.kegg.jp/get/path:ath{NNNNN}` → `NAME`/`CLASS` lines (cached separately; pathway metadata far more stable than per-gene memberships)
- Locus shape: KEGG uses lowercase (`at1g01010`); tool accepts `AT1G01010` and lowercases internally
- Step 1 empty → `NotFoundError`; step 2 non-200 → skip pathway, append to `errors[]` (partial > nothing)
- Concurrency cap of 2 inside the second-step gather (KEGG rate limit is undocumented; live-probe will tune)

**`string_interactions`**

- `GET https://string-db.org/api/json/interaction_partners?identifiers={accession}&species=3702&limit={N}&caller_identity=plant-genomics-mcp`
- Accepts either locus or UniProt accession (mirrors v0.6's `resolve_locus_to_uniprot` input-shape dispatch added in P2.b)
- If input matches UniProt accession regex → use directly; else → `resolve_locus_to_uniprot(locus)` internally first
- Response: JSON array with `stringId_A`, `stringId_B`, `preferredName_A`, `preferredName_B`, `score`, `escore`, `dscore`, `tscore`, `pscore`
- Empty array OR 400 body containing `not found` → `NotFoundError("no STRING interactions for {accession}")`
- Etiquette: `caller_identity=plant-genomics-mcp` hardcoded (matches NCBI tool/email pattern)

**`atted_coexpression`**

- `GET https://atted.jp/api/coex/Ath-u/{locus}/{top_n}` returns JSON array
- Ath-u = tissue-aggregated unmoderated; Ath-m is meta-only, less useful as default
- Anti-bot: `User-Agent: plant-genomics-mcp/{version}` header
- Response: `[{"locus": "AT4G36990", "mutual_rank": 1.2, "score": 4.31}, ...]`
- Mutual rank lower-is-better; score higher-is-better. Both surfaced as-is in `CoexNeighbor`
- Empty array → `NotFoundError`; 5xx → `UpstreamUnavailableError`

### Batch variants

All four follow v0.6's `BatchEnsemblPlantsLookup` / `BatchGetGeneXrefs` convention:

- `loci: list[str]`, cap 50 (raise `ValueError` if exceeded)
- Output: `BatchX{results: list[X | BatchErrorEntry{locus, error_type, error_message}]}`
- Concurrency: `asyncio.Semaphore(5)` (matches existing batch pattern; KEGG's internal sequence uses its own Semaphore(2) under this)
- Per-locus errors serialized into `results` as `BatchErrorEntry`; never abort the batch

### Live-probe risk register (resolved before lock-in)

Implementation plan's task 0 will live-probe each. WSL egress is broken for Gramene; will use WebFetch (Anthropic egress) or jobd-dispatch from gt76.

1. **Gramene `fl=homology` parameter** — base endpoint `/v69/genes?idList={locus}` confirmed via WebFetch 2026-05-21 (returns a gene record for `AT1G01010`); the `fl=homology` filter parameter and exact `homology[]` array shape are unverified — implementation task 0 must confirm both, or fall back to a separate Gramene compara endpoint
2. **KEGG `rest.kegg.jp` rate limit** — undocumented; will measure throughput before locking 50-batch cap × 5-concurrency. May drop to Semaphore(2) for KEGG specifically
3. **STRING `api/json/interaction_partners` accession format** — STRING uses internal `stringId` (`3702.AT1G01010.1`); confirm bare UniProt accession works, or add `identifiers/?identifiers=...` resolve hop first
4. **ATTED-II API URL** — multiple versions have shipped; some are login-gated now. Confirm `Ath-u` endpoint at current release (v9) is open

## Module architecture

**New files:**

| Path                                  | Lines (est.) | Purpose                           |
| ------------------------------------- | ------------ | --------------------------------- |
| `src/plant_genomics_mcp/gramene.py`   | ~140         | Homology backend                  |
| `src/plant_genomics_mcp/kegg.py`      | ~160         | KEGG REST + TSV parsing           |
| `src/plant_genomics_mcp/string_db.py` | ~150         | STRING + accession-input dispatch |
| `src/plant_genomics_mcp/atted.py`     | ~120         | Coexpression backend              |

`string_db.py` uses the `_db` suffix because `string` is a stdlib name.

Each exports:

- `fetch_*(locus, **opts) -> <Model>` — single-locus async (the tool body)
- `batch_fetch_*(loci, **opts) -> Batch<Model>` — bounded-concurrency batched form
- Module-level `TTLCache` keyed by `(locus, options_tuple)`, registered via `register_cache(name, cache)` so `pgmcp://cache/stats` picks them up automatically

**Cache TTLs:**

| Module         | TTL          | Rationale                                                                                                      |
| -------------- | ------------ | -------------------------------------------------------------------------------------------------------------- |
| `gramene.py`   | 86400s (24h) | Homology stable across a Gramene release; v69 won't change mid-release                                         |
| `kegg.py`      | 86400s (24h) | KEGG pathway memberships stable; weekly mirror updates at most                                                 |
| `string_db.py` | 3600s (1h)   | STRING refreshes annually; 1h conservative but matches existing UniProt cache TTL → uniform cache-stats output |
| `atted.py`     | 86400s (24h) | ATTED-II releases versioned (Ath-u, Ath-m); within a release data is frozen                                    |

## Existing modules touched

| Path                                  | Change                                                                                                  |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| `src/plant_genomics_mcp/server.py`    | +8 TOOLS entries, +8 dispatch arms, +4 module imports, +1 prompt entry (~120-line delta)                |
| `src/plant_genomics_mcp/models.py`    | +4 top-level response models + 4 nested item models                                                     |
| `src/plant_genomics_mcp/prompts.py`   | +`BIOLOGICAL_CONTEXT` constant + renderer + arg schema                                                  |
| `src/plant_genomics_mcp/resources.py` | Auto-picks up new caches via existing registry; verify `pgmcp://backends/status` enumerates new modules |
| `src/plant_genomics_mcp/errors.py`    | No changes — existing `NotFoundError` / `RateLimitError` / `UpstreamUnavailableError` cover all 4       |
| `src/plant_genomics_mcp/cache.py`     | No changes — each module instantiates its own `TTLCache` per existing pattern                           |
| `src/plant_genomics_mcp/__init__.py`  | `__version__ = "0.7.0"`                                                                                 |
| `pyproject.toml`                      | `version = "0.7.0"`                                                                                     |

## `biological_context` prompt

- **Name:** `biological_context`
- **Arguments:**
  - `locus` (required) — Arabidopsis AGI locus (e.g., `AT1G01010`)
  - `top_n` (optional, default `"10"`) — caps STRING partners and ATTED-II neighbors (MCP prompt args are always strings; renderer casts)
- **Rendered chain** (single user-role message):
  1. `gramene_homologs(locus=<L>, homology_type="ortholog")` → orthologs across plant species
  2. `kegg_pathways(locus=<L>)` → pathway membership in Arabidopsis
  3. `resolve_locus_to_uniprot(locus=<L>)` → UniProt accession (needed for STRING)
  4. `string_interactions(locus_or_accession=<accession>, limit=<top_n>)` → first-neighbor PPI partners
  5. `atted_coexpression(locus=<L>, top_n=<top_n>)` → coexpression neighbors
  6. Synthesis: cross-reference orthologs vs interactors vs coex neighbors; flag overlap (interactors that are also coexpressed = higher-confidence functional partners)
- **Description (`prompts/list`):** "Build a biological context profile for an Arabidopsis locus by chaining homology (Gramene) → pathways (KEGG) → interactions (STRING) → coexpression (ATTED-II). Cross-references the result lists to surface high-confidence functional partners."
- **Failure mode:** unknown args / missing required → existing `NotFoundError("[NotFoundError] unknown prompt argument …")` path in `prompts.py` covers it

## Test surface

**New test files:**

| Path                         | Lines (est.) | Coverage                                                                                      |
| ---------------------------- | ------------ | --------------------------------------------------------------------------------------------- |
| `tests/test_gramene.py`      | ~120         | pytest-httpx unit + `PLANT_GENOMICS_MCP_LIVE=1`-gated real probe                              |
| `tests/test_kegg.py`         | ~130         | Two-call sequence + TSV parse; live-gated                                                     |
| `tests/test_string_db.py`    | ~140         | Accession/locus dispatch + live-gated                                                         |
| `tests/test_atted.py`        | ~110         | Unit + live-gated                                                                             |
| `tests/test_prompts.py`      | (extend)     | New tests for `biological_context`                                                            |
| `tests/test_server_stdio.py` | (modify)     | Tool set 15→23; new `biological_context` parametrized case in `test_get_prompt_renders_chain` |

Per-module unit tests cover: happy path, 404 → `NotFoundError`, 429 → retry then `RateLimitError`, empty results, batch with mixed success/error.

Each module also gets at least one `PLANT_GENOMICS_MCP_LIVE=1`-gated real-execution test matching the real-execution doctrine and how `phytozome.py::test_live_known_organisms_all_resolve` is structured.

## Real-execution proof transcript

Following the v0.6 `examples/` precedent (which surfaced two latent bugs the synthetic-fixture tests missed):

| Path                                         | Purpose                                             |
| -------------------------------------------- | --------------------------------------------------- |
| `examples/biological_context_AT1G01010.json` | Full payload from live chain execution              |
| `examples/biological_context_AT1G01010.md`   | Markdown sibling quoting load-bearing fields inline |
| `examples/_run_chain.py`                     | Extended with `biological_context` chain driver     |
| `examples/README.md`                         | Third transcript row                                |

## Documentation deltas

| Path           | Change                                                                                                                                       |
| -------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| `CHANGELOG.md` | New `## v0.7.0 — <DATE>` section above v0.6.0; P3 entries (one per new tool + prompt + transcript)                                           |
| `README.md`    | Tool-count headline 15→23; new category table rows (Homology, Pathways, Interactions, Expression); new chain recipe for `biological_context` |

## Success criteria

v0.7 ships when:

1. All 8 new tools pass synthetic-fixture unit tests (pytest-httpx mocked)
2. All 4 new live-gated tests pass against real upstreams (CI runs them weekly via `PLANT_GENOMICS_MCP_LIVE=1`)
3. `tests/test_server_stdio.py` passes end-to-end with `PLANT_GENOMICS_MCP_STDIO_SMOKE=1` — 23 tools advertised, `biological_context` discoverable, typed-error prefix preserved
4. `examples/biological_context_AT1G01010.{json,md}` regenerated by `examples/_run_chain.py` against live upstreams — proof transcript shipped
5. `pgmcp://cache/stats` resource enumerates the 4 new caches
6. `CHANGELOG.md` + `README.md` reflect new surface; `__version__` and `pyproject.toml` bumped to `0.7.0`

## Out of scope for v0.7 (slips to v0.8 / P4)

- Fallback backends: OMA Browser, Reactome, IntAct, EBI Expression Atlas
- Multi-organism resolver layer (rice, soy, sorghum, etc.)
- Production-readiness items (deferred per user to P5): PyPI publish, GHCR `:latest` push, ownership reconciliation (`mjarnold` vs `musharna`), REGISTRIES.md:34 falsehood cleanup

## Open items for the implementation plan

These need verifying / probing during plan execution (not blocking design approval):

- Live-probe verdicts for the 4 risk-register items above
- Whether KEGG concurrency needs to drop from Semaphore(5) to Semaphore(2)
- Whether STRING needs an `identifiers/?` resolve hop or accepts bare UniProt accessions
- Whether ATTED-II `Ath-u` endpoint at v9 is still open (login-gating risk)
