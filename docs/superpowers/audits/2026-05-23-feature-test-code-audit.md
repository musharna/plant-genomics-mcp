# plant-genomics-mcp v0.9.0 — Feature / Test / Code-Quality Audit

**Date:** 2026-05-23
**Auditor:** feature-dev:code-reviewer (opus)
**Scope:** Pre-1.0 audit of feature completeness, test coverage, code quality, documentation, and API stability.

---

## Executive Summary

27 tools across 9 live backends + 2 stubs + 4 synthesis tools. Clean error hierarchy, correct synthesis orchestration, comprehensive mocked unit tests for every backend, real-execution HTTP transport test. **Three issues block 1.0:** (1) `string_interactions` + `batch_string_interactions` are missing the `organism=` parameter in both `inputSchema` and dispatch — the v0.9 multi-organism contract is broken for these two tools; (2) `scripts/verify_organisms.py` imports the removed symbol `phytozome.KNOWN_ORGANISMS` and crashes at import, making it impossible to populate the 7 `phytozome_int=None` records before release; (3) those 7 `None` records mean `phytozome_lookup_locus` raises `OrganismNotSupported` for rice, maize, wheat, tomato, barley, grapevine, and barrel medic. Must resolve before tagging 1.0. **Totals: 3 BLOCKER, 4 IMPORTANT, 5 POLISH.**

---

## Axis 1 — Feature Completeness

### BLOCKER — `string_interactions` missing `organism=` in inputSchema and dispatch

- **Files:** `server.py:449-465` (inputSchema — only `locus_or_accession` + `limit`), `server.py:1145-1150` (dispatch — no `organism=` forwarded), `batch.py:250-261` (same gap on `batch_string_interactions`).
- **Live verification:** confirmed 2026-05-23 — inputSchema has zero `organism` property; dispatch call passes only `(client, args["locus_or_accession"], limit=...)`.
- `string_db.lookup_partners` signature has `organism: str | int = organisms.DEFAULT_ORGANISM` (string_db.py:130) — so the dispatch silently always defaults to arabidopsis taxid 3702 regardless of caller intent.
- The v0.9 test `test_tool_schemas_use_organism_param` (test_server_stdio.py:227) only checks **absence of old field names** (`species`, `organism_id`) — it does not assert presence of `organism` on multi-organism tools, so the gap slips through.
- Tool description (server.py:443-447) also still says "Arabidopsis locus identifier" — stale post-v0.9.
- **Fix:** Add `organism` to `string_interactions` and `batch_string_interactions` inputSchema; forward it in dispatch + batch helper. Strengthen `test_tool_schemas_use_organism_param` to assert `organism` IS present on the explicit set of multi-organism tools.
- **Effort:** S

### BLOCKER — 7 of 12 `phytozome_int` are `None`; `phytozome_lookup_locus` raises `OrganismNotSupported` for most non-arabidopsis

- **File:** `organisms.py:51-165` (records); guard at `test_organisms.py:161` (`test_unverified_records_have_none_phytozome_int`).
- Unverified: `oryza_sativa`, `zea_mays`, `triticum_aestivum`, `solanum_lycopersicum`, `hordeum_vulgare`, `vitis_vinifera`, `medicago_truncatula`.
- `phytozome_int_for()` raises `OrganismNotSupported` for all of these → `phytozome_lookup_locus` and `batch_phytozome_lookup_locus` unusable for 7 of 12 supported organisms.
- Pre-1.0 decision required: (a) populate real IDs via `verify_organisms.py` (which is broken — see next), or (b) document explicitly in the coverage matrix and tool descriptions.
- **Effort:** S (IDs) once verify script is fixed.

### IMPORTANT — `verify_organisms.py` crashes at import (removed symbol)

- **File:** `scripts/verify_organisms.py:22` — `from plant_genomics_mcp.phytozome import KNOWN_ORGANISMS as PHYTOZOME_KNOWN`.
- **Live verification:** confirmed 2026-05-23 — grep on `phytozome.py` returns no `KNOWN_ORGANISMS` definition; the v0.9 CHANGELOG lists it as a breaking removal.
- Pre-release verification harness for populating `phytozome_int` is non-functional → cannot resolve BLOCKER #2 until this is fixed.
- **Fix:** Replace import with `{c: r.phytozome_int for c, r in organisms.ORGANISMS.items() if r.phytozome_int is not None}` or equivalent live source.
- **Effort:** S

### IMPORTANT — `biological_context_synth` silently always fails KEGG + ATTED for non-arabidopsis

- **Files:** `synthesis.py:568-579` (orchestration), `kegg.py:139` (`gene_id = f"ath:{locus.lower()}"` — Arabidopsis-only), `atted.py` (Arabidopsis-only by data scope).
- Tool accepts `organism=` but KEGG and ATTED steps will always return `status="error"` for any non-arabidopsis locus. Synthesis envelope handles this gracefully (`_gather_phase2` traps exceptions), but the tool description (server.py:914-919) does not mention the limitation.
- **Fix:** Add note to tool description: "KEGG and ATTED backends are Arabidopsis-only; for non-arabidopsis organisms those steps will return status=error." Add one mocked test with `organism="oryza_sativa"` asserting KEGG+ATTED are `status="error"` while gramene+string succeed.
- **Effort:** S (docs) + S (test)

### POLISH — `biological_context` prompt missing `organism` argument

- **File:** `prompts.py:97-119`. Inconsistent with `analyze_locus` prompt (which exposes `organism`). Document the Arabidopsis-only nature explicitly or add an optional `organism` argument.
- **Effort:** XS

### POLISH — `pgmcp://backends/status` and `pgmcp://cache/stats` omit BLAST

- **File:** `resources.py:118-128` and `131-207`. `blast.py` has a live `_CACHE` and `BASE_URL`; including it would make these resources complete.
- **Effort:** XS

---

## Axis 2 — Test Coverage Gaps

### IMPORTANT — No live synthesis test exercising non-arabidopsis organism end-to-end

- **File:** `tests/test_synthesis.py` (no live non-Arabidopsis test).
- v0.9 added per-backend rice live tests in individual test files but no end-to-end synthesis path covering resolver → wire format for non-arabidopsis.
- **Fix:** Add `@live_only async def test_analyze_locus_synth_live_rice` against `Os01g0100100, organism="oryza_sativa"`; assert `steps[0].status == "ok"`.
- **Effort:** S

### IMPORTANT — `test_tool_schemas_use_organism_param` doesn't assert `organism` IS present

- **File:** `test_server_stdio.py:227-248`. Only checks `species not in props` and `organism_id not in props`. This is why the `string_interactions` regression (Axis 1 BLOCKER) goes undetected.
- **Fix:** Maintain an explicit allowlist of tools that must expose `organism=` and assert presence.
- **Effort:** S

### POLISH — HTTP transport test only spot-checks 4 of 27 tools in `tools/list`

- **File:** `test_http_transport.py:151-157`. Stdio smoke does exact-set assertion (27). Adding the same to HTTP confirms parity.
- **Effort:** XS

---

## Axis 3 — Code Quality

### IMPORTANT — `StepRow.elapsed_s` is documented as per-step but phase-2 rows carry shared gather wall time

- **Files:** `synthesis.py:144-158` (`_gather_phase2` assigns same `elapsed` to all rows), `models.py:464` (`StepRow.elapsed_s` description: "Wall time for this step alone").
- 1.0 API-contract concern: clients summing step elapsed times overcount by `(n_phase2_steps - 1) × max_backend_latency`.
- **Fix option A (preferred):** instrument each phase-2 coroutine with `_timed_step` instead of one shared timer.
- **Fix option B:** update `StepRow.elapsed_s` description: "For phase-2 (parallel gather) steps, this is the gather total, not per-step time."
- **Effort:** S (B) / M (A)

### POLISH — `synthesis.py` late `re` import

- **File:** `synthesis.py:460` — `import re as _re  # noqa: E402`. No functional reason; move to top.
- **Effort:** XS

### POLISH — `batch_ensembl_plants_lookup_locus` POST lacks retry on 429/5xx

- **File:** `batch.py:95-141`. Single-locus has retry; batch POST does not (documented inline). Either add retry or surface the gap explicitly in the tool description.
- **Effort:** S

### POLISH — Cross-backend duplication of `_get` retry pattern

- **Files:** `gramene.py:37-84`, `atted.py:~90`, `string_db.py:63-107`, `kegg.py:42-84`, `ensembl_plants.py:42-101`, etc.
- ~150 lines duplicated across backends with only `BASE_URL` and JSON-vs-text variation. Shared `_http.py` helper would centralize retry policy + the `Retry-After` cap from security B-2. Not a 1.0 blocker but the right next refactor.
- **Effort:** M

> **Note on a false positive in the original audit pass:** `synthesis.py:314` `uniprot_record["geneNames"][0]` was flagged as unguarded `IndexError`. Re-reading shows the `elif uniprot_record and uniprot_record.get("geneNames"):` guard correctly excludes `[]` (falsy in Python), so an empty `geneNames` list cannot reach the indexer. Finding downgraded — not an issue.

---

## Axis 4 — Documentation

### IMPORTANT — Hosted endpoint URL in README is maintainer's Tailnet

- **File:** `README.md:122-139`. URL `mjarnoldgt76.tail86d19d.ts.net` is a personal Tailscale Funnel address. README does flag "best-effort uptime", but for public 1.0 the user-facing endpoint story needs explicit framing (personal demo vs. project-maintained vs. self-host).

### POLISH — `pgmcp://organisms/coverage` header column inconsistency with README

- **File:** `resources.py:231-246` vs. `README.md:73`. Table header shows `taxid` not `ncbi`; README description references "ncbi" column. Minor self-description drift.

---

## Axis 5 — API Stability for 1.0

### Lock these at 1.0 tag

1. **Organism canonical slugs** — all 12 keys in `organisms.ORGANISMS` (arabidopsis_thaliana, oryza_sativa, zea_mays, triticum_aestivum, solanum_lycopersicum, glycine_max, sorghum_bicolor, hordeum_vulgare, vitis_vinifera, populus_trichocarpa, medicago_truncatula, brachypodium_distachyon).
2. **Tool names** — all 27 enumerated in `server.TOOLS`. `test_server_stdio.py` exact-set assertion is the guard.
3. **`organism=` parameter schema** — `{"type": ["string", "integer"], "default": "arabidopsis_thaliana"}` on every multi-organism tool. After Axis-1 BLOCKER fix, `string_interactions` + `batch_string_interactions` join this set.
4. **`SynthesisEnvelope` shape** — `{tool, input, started_at, elapsed_s, steps[], result}`. `StepRow` status literals `ok | error | skipped`. Already `extra="forbid"`.
5. **Resource URIs** — `pgmcp://cache/stats`, `pgmcp://organisms/phytozome`, `pgmcp://backends/status`, `pgmcp://organisms/coverage`. MIME types fixed.
6. **Error wire format** — `[ClassName] message` prefix on all `PlantGenomicsError` subclasses.
7. **Stub shape** — `SubscriptionGatedRedirect` with `status="subscription_required"` and `probed_at`. `TairLocusInfo.tair_web_url`, `PlantCycLocusInfo.plantcyc_web_url`.

### Intentionally NOT locked at 1.0

- `phytozome_int` values for the 7 unverified organisms (will change as `verify_organisms.py` runs).
- Gramene release string (`"v69"`) — will update with Gramene releases.
- ATTED release string (`"Ath-u.c4-0"`) — same.
- `_PROBED_AT` constants in TAIR/PlantCyc stubs — updated as access is re-probed.

### TAIR / PlantCyc stubs for 1.0

Keep as stubs. Documented rationale (`tair.py`) cites 2026-05-21 live 403 probe. Structured redirects with `status="subscription_required"` are more useful than removal. If subscriptions lift before 1.0, that's a different scope item.

---

## Affirmations

- Error hierarchy is clean and consistent; `[ClassName]` prefix verified by unit + smoke tests.
- `organisms.py` resolver is well-structured (frozen dataclass registry, alias index, robust `_normalize`); all 12 organisms + aliases + taxid lookup tested in `test_organisms.py`.
- `synthesis.py` phase orchestration is correct: phase-0 organism validation → phase-1 sequenced root calls → phase-2 `asyncio.gather` with `return_exceptions=True` → `OrganismNotSupported` → `skipped` translation, `httpx.HTTPError` → `error` translation. Tested end-to-end.
- `SynthesisEnvelope` / `StepRow` Pydantic models with `model_validator` coherence checks are correct and well-tested.
- HTTP transport test (`test_http_transport.py`) includes a real-execution uvicorn spinup — right boundary check.
- `TTLCache` (`cache.py`) is correct: LRU eviction, lazy TTL, `_disabled()` re-reads env each call, `stats()` returns live counts.
- `batch.py` `_gather` correctly re-raises non-`PlantGenomicsError` exceptions and uses `zip(..., strict=True)`.
- EDAM ontology tags on tools are present and correctly scoped.
- All 21 test files present with matching backend modules — no backend untested.
- `test_server_stdio.py` exact 27-tool set assertion and v0.9 schema test are good regression guards.
- CHANGELOG maintained through v0.9; README migration guide is comprehensive.
