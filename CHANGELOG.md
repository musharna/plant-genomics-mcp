# Changelog

## v1.0.2 — 2026-05-23

Hot-fix — repairs the BAR backend, which was DOA in v1.0.0 and v1.0.1. The `bar` module was missing from the `from plant_genomics_mcp import (...)` block in `server.py`, so any dispatch of `bar_gene_summary`, `bar_efp_expression`, or `bar_aiv_interactions` raised `NameError: name 'bar' is not defined` at runtime — three of the 32 tools (plus their batch variants and the silently-aliased `tair_locus_info`) were unusable over stdio/HTTP. Existing BAR unit tests passed because they call `bar.gene_summary(...)` directly and never exercise the server-level dispatcher. Also fixes the related ruff lint failure that has been red on `main` since v1.0.0 (`F821 Undefined name 'bar'` ×3 in `server.py`, `E402 Module level import not at top of file` in `tests/test_organisms.py`).

- **`src/plant_genomics_mcp/server.py`** — add `bar,` to the import block (alphabetic position between `batch,` and `blast,`), restoring the binding the dispatcher relies on.
- **`tests/test_bar.py`** — new regression test `test_dispatch_bar_gene_summary_resolves_bar_module` routes through `server._dispatch("bar_gene_summary", ...)` with mocked HTTPX so any future drop of the `bar,` import fails CI loudly. Module-level direct-call tests stayed green through the bug; this test pins the _dispatch path_ contract.
- **`tests/test_organisms.py`** — move the `from plant_genomics_mcp.errors import (...)` block above the test function defs (E402 cleanup).
- **Operational impact.** Hosted demo at `https://plant-genomics-mcp.tail4dabe.ts.net/mcp` has been silently 500-ing on BAR tool calls for the v1.0.0 → v1.0.1 window (~few hours). The Docker pipeline retags `:1.0.2` / `:1.0` / `:latest`; Diun on gt76 auto-redeploys.

## v1.0.1 — 2026-05-23

Security patch — closes the v1.0.0 fail-open gap on the HTTP transport. **Breaking change for self-hosters:** `PLANT_GENOMICS_MCP_HTTP_TOKEN` is now REQUIRED and must be at least 32 characters; `plant-genomics-mcp-http` (and any direct `build_app()` caller) aborts at startup with `SystemExit` if the env var is absent or too short. v1.0.0 documented this as fail-closed but the code shipped fail-open-on-absent; v1.0.1 makes the code match the spec. `/healthz` remains unauthenticated for liveness probes; stdio transport is unaffected.

- **`src/plant_genomics_mcp/server_http.py:build_app()`** — reads `PLANT_GENOMICS_MCP_HTTP_TOKEN` from the env, raises `SystemExit` with an actionable message (suggests `openssl rand -hex 32`) when the value is missing or `< _MIN_TOKEN_LEN = 32` chars. The dead `if expected_token:` guard inside `handle_mcp` is removed — the auth gate now runs unconditionally on every `/mcp` request, since the env var is guaranteed non-empty by construction.
- **`tests/test_http_transport.py`** — 3 new tests pinning the contract (abort-on-absent, abort-on-short, succeed-at-32-chars); existing tests refactored onto a `_VALID_TOKEN = "x" * 32` constant and an autouse fixture that sets a valid token by default; the obsolete `test_mcp_open_when_token_unset` (which asserted the fail-open mechanism we just removed) is deleted.
- **Upgrade path.** Existing `~/homelab/plant-genomics-mcp/.env` deployments on the hosted demo endpoint already satisfy the new contract (the 64-char token written during the v1.0.0 deploy is well over 32 chars). Self-hosters who relied on the documented-but-never-shipped fail-open default must now set the env var or the container will refuse to start.

## v1.0.0 — 2026-05-23

First stable release. No new backends or tools — the 32-tool, 11-backend surface from v0.10.0 is the 1.0 contract. What this tag carries is the pre-1.0 readiness sweep across three waves (audit memo `docs/superpowers/audits/2026-05-23-pre-1.0-readiness.md`): resolver-hygiene cleanup, security-hardening for the hosted HTTP endpoint, and API-polish for long-term schema stability.

**Wave A — resolver hygiene (5 commits, A1-A5):**

- **`scripts/verify_organisms.py` repaired** — was crashing at import on the removed `phytozome.KNOWN_ORGANISMS` symbol; now reads `phytozome_int` from the v0.9 `organisms.ORGANISMS` registry.
- **Phytozome IDs backfilled for all 12 organisms** — 7 of 12 records had `phytozome_int = None`; live-probed and populated so `phytozome_lookup_locus` no longer raises `OrganismNotSupported` for most non-Arabidopsis input.
- **`string_interactions` + `batch_string_interactions` wired to `organism=`** — the two tools were silently dropping the parameter; added it to both inputSchemas and dispatch paths. `test_tool_schemas_use_organism_param` strengthened to assert presence (the original test was the root cause of the slip).
- **Live non-Arabidopsis synthesis test** — end-to-end probe of `analyze_locus_synth` against rice `Os01g0100100`, gated by `PLANT_GENOMICS_MCP_LIVE=1`.

**Wave B — security hardening for the hosted HTTP endpoint (7 commits, B1-B7):**

- **Bearer-token auth middleware on `/mcp`** — set `PLANT_GENOMICS_MCP_HTTP_TOKEN` to require `Authorization: Bearer <token>` on `/mcp`; when the env var is unset, `/mcp` remains open (operators self-hosting on the public internet MUST set it). `/healthz` is always exempt for liveness probes.
- **`Retry-After` capped at 60s** across all 9 backend modules with retry loops — eliminates the "upstream tells us to sleep for an hour" amplification vector.
- **HTTP body-size cap (1 MB default, env-tunable) + BLAST `sequence` `maxLength` (1 MB)** — closes the 500 MB BLAST sequence acceptance hole.
- **BLAST concurrency semaphore** (`PLANT_GENOMICS_MCP_BLAST_CONCURRENCY=2` default) and **real operator NCBI email** required via `PLANT_GENOMICS_MCP_NCBI_EMAIL` — NCBI ToS etiquette for `consensus_homologs` and `find_homologs_synth` auto-submit paths.
- **CORS deny-all** on the Starlette app — no browser-origin proxy abuse pre-auth.
- **Shared `_LOCUS_RE` validator** extracted to `validators.py` and applied to Ensembl / KEGG / Gramene paths that previously interpolated `locus` without regex validation.
- **HTTP integration tests** — auth × body-size matrix covering 200 / 401 / 413 paths.

**Wave C — API polish for 1.0 contract stability (10 items, C1-C10):**

- **`StepRow.elapsed_s` → `float | None`** — phase-2 synthesis steps (consensus, ranking) now return `None` instead of zero, removing the false-attribution of phase-1 backend latency to phase-2 reducers.
- **`/healthz` no longer leaks `__version__`** — returns `{"status":"ok"}` only.
- **`pgmcp://backends/status` includes BLAST** — with its concurrency cap surfaced as `concurrency_cap`.
- **`biological_context` prompt accepts `organism=`** — mirrors `analyze_locus`. Non-Arabidopsis organisms skip KEGG + ATTED (Arabidopsis-only data) with an explicit synthesis note.
- **Hosted endpoint README copy** — reframed as "personal demo, best-effort, no SLA — self-host for production."
- **Late `import re` hoisted to top of `synthesis.py`** — removes a `# noqa: E402` and the `_re` alias.
- **`batch_ensembl_plants_lookup_locus` POST-no-retry gap documented** — tool description + docstring note that the batch endpoint skips the single-locus path's 429/5xx retry, with shared retry layer scheduled for v1.1.
- **Coverage-matrix column header aligned** — `taxid` → `ncbi_taxid` to match the `OrganismRecord` field name and README description.
- **Reproducible Docker builds via `uv.lock`** — both `Dockerfile` and `Dockerfile.http` now use `ghcr.io/astral-sh/uv:0.11.16-python3.12-trixie-slim` with `uv sync --frozen --no-editable`; lockfile is committed and pinned through the project metadata.

**Known deferrals (scheduled for v1.1):**

- Shared `_http.py` retry refactor — gives the batch POST paths the same retry behavior as single-locus GETs.
- PyPI publish and registry-listing refresh (modelcontextprotocol.io, PulseMCP, Glama).
- Optional codecov.io coverage badge in the README header.

## v0.10.0 — 2026-05-23

BAR backend release — adds the Bio-Analytic Resource for Plant Biology (U Toronto, Global Core Biodata Resource 2023) as the tenth live backend. Surface grows 27 → 32 tools (3 new single-locus + 2 new batch). BAR is free, keyless, no rate limit; it mirrors TAIR's curator-annotated locus data plus eFP-Browser tissue expression and the AIV (Arabidopsis Interactions Viewer) protein-protein interaction graph, all without the Phoenix Bioinformatics paid subscription. `tair_locus_info` is **silently upgraded** to a direct alias of `bar_gene_summary` — the MCP tool name stays for client compatibility, but the body now returns real curator data instead of a `subscription_required` redirect record.

- **`src/plant_genomics_mcp/bar.py`** (new) — three live module functions wrapping the BAR REST surface:
  - `gene_summary(client, locus)` — `/api/thalemine/gene_information/{locus}` + `/api/gaia/aliases/{locus}` merged into `BarGeneSummary` (locus, symbol, ncbi_gene_id, aliases, brief_description, full_description, species).
  - `efp_expression(client, locus)` — `/api/efp/expression/{locus}` (mean ± SD per tissue across the eFP atlas).
  - `aiv_interactions(client, locus, organism)` — `/api/aiv/{ath|osa}/interactions/{locus}` with organism-dispatch (Arabidopsis + rice) over a curated PPI graph with confidence + supporting papers.
- **Three new MCP tools** with full `outputSchema` wiring: `bar_gene_summary`, `bar_efp_expression`, `bar_aiv_interactions` — all live, all keyless. EDAM tags (`operation_2422`, `topic_0780`, `topic_0114`) on `_meta` for registry indexers.
- **Two new batch tools**: `batch_bar_gene_summary`, `batch_bar_aiv_interactions` — fan-out via `asyncio.gather` over a `loci: string[]` (1–50), envelope `{tool, count, results, errors}` matches the rest of the batch family. `bar_efp_expression` has no batch variant (per-locus is the natural unit and the eFP atlas response is already large).
- **Silent upgrade: `tair_locus_info` is now an alias of `bar_gene_summary`.** The tool name is preserved so existing MCP clients keep working; the body delegates entirely to `bar.gene_summary`, and the `outputSchema` swaps from the old `SubscriptionGatedRedirect` shape to `BarGeneSummary`. Callers that pattern-matched on `status: "subscription_required"` need to switch to the new shape — see Section 7 of the README for the contract. The `[NotFoundError]` typed-prefix on invalid loci is preserved.
- **`pgmcp://backends/status`** — BAR added to the live-backend rollup with `kind: "live"`, `subscription_gated: false`. TAIR is **removed** from this resource: it's no longer a standalone backend, just a tool alias of BAR. PlantCyc remains the sole `subscription_required` stub.
- **`pgmcp://cache/stats`** — gains a `bar` rollup alongside the other 9 backends (10 live caches total).
- **TAIR module rewritten** (`src/plant_genomics_mcp/tair.py`) — was a 70-line subscription-required redirect stub returning a hardcoded `SubscriptionGatedRedirect`; now a 6-line async delegate to `bar.gene_summary`. The `TairLocusInfo` Pydantic model is removed from `models.py` (no consumers).
- **Stdio smoke test** updated for 32-tool surface; offline-stub call test pivoted from `tair_locus_info` to `plantcyc_locus_info` (which remains the deterministic-offline contract anchor). `bar_aiv_interactions` + `batch_bar_aiv_interactions` added to the canonical multi-organism set in `test_tool_schemas_use_organism_param`.

## v0.9.0 — 2026-05-23

Multi-organism resolver release. Broadens the project from Arabidopsis-default-with-ad-hoc-overrides to a curated 12-plant coverage matrix accessed through a single unified `organism=` parameter on every backend tool. Same 27 tools, same 9 live backends — what changes is that you can now pass `oryza_sativa`, `Oryza sativa`, `rice`, or `39947` and have every backend resolve to the same canonical record without the caller knowing the per-backend slug / taxid / proteome-id translation.

- **`src/plant_genomics_mcp/organisms.py`** (new) — curated registry of 12 plant `OrganismRecord` entries (frozen dataclass with `canonical`, `scientific`, `common`, `ncbi_taxid`, `ensembl_slug`, `phytozome_int`, `string_taxid`, `europe_pmc_slug`, `aliases`) keyed by canonical slug. Lookup goes through `resolve(query: str | int)` which normalizes via `_ALIAS_INDEX` + `_TAXID_INDEX` and raises `OrganismNotFound`. Per-backend accessor helpers (`ensembl_slug_for`, `phytozome_int_for`, `ncbi_taxid_for`, `string_taxid_for`, `europe_pmc_slug_for`) raise `OrganismNotSupported` when an organism is in the registry but lacks coverage on the target backend.
- **`OrganismNotFound` + `OrganismNotSupported`** added to `errors.py` as `PlantGenomicsError` subclasses — `[OrganismNotFound]` / `[OrganismNotSupported]` wire prefixes integrate cleanly with the v0.8 `SynthesisEnvelope` step-row error model.
- **Unified `organism=` parameter** across every backend that previously took `species=` or `organism_id=` — `ensembl_plants`, `uniprot`, `europe_pmc`, `phytozome`, `string_db`, `batch.*`, `synthesis.*`, `prompts.analyze_locus`, `prompts.biological_context`. Default value is `organisms.DEFAULT_ORGANISM` (`"arabidopsis_thaliana"`), so calls that never set the param continue to work. Wire-format URL params (Ensembl `species=`, STRING `species=<taxid>`) are preserved — the rename is on OUR surface, not the upstream contract.
- **Output Pydantic models renamed** to match the input contract: `EnsemblPlantsLocus.species` / `GeneXrefs.species` / `LocusLiterature.species` are now `organism` (string slug); `StringInteractions.organism_taxid` (int) is now `organism` (string slug). Clients that pattern-match on output field names need a one-time rename — see `## Migrating from v0.8 to v0.9` in the README.
- **`scripts/verify_organisms.py`** (new) — live-probe harness that hits every backend with every supported organism and prints a pass/fail matrix. Run pre-release to catch wire-format drift; results feed Phytozome `phytozome_int` cell verification.
- **`pgmcp://organisms/coverage`** new MCP resource — markdown table of all 12 organisms × 5 backend ID slots (ensembl, phytozome, string, europe_pmc, ncbi). Missing slots render as em-dash; the europe_pmc "None means no slug-strip needed" contract renders as `"None (no strip)"`. Replaces the per-organism `resolve_organism` probe loop a client would otherwise need.
- **`pgmcp://organisms/phytozome`** resource description corrected — the v0.8 blurb said "only `arabidopsis_thaliana=167` is controller-verified"; the v0.9 resource now sources its slug → integer-ID map from `organisms.ORGANISMS` (filtered to records with a non-None `phytozome_int`).
- **Live tests per backend against rice** (`tests/test_*_live_rice_*`) — gated by `PLANT_GENOMICS_MCP_LIVE=1`, real-execution check that the resolver → backend wire-format chain works for at least one non-Arabidopsis plant on each backend.
- **Breaking removals**: `phytozome.KNOWN_ORGANISMS` module-level dict (use `organisms.ORGANISMS`); `synthesis.DEFAULT_SPECIES` + `prompts.DEFAULT_SPECIES` (use `organisms.DEFAULT_ORGANISM`); `uniprot.ARABIDOPSIS_TAXID` constant.
- **Migration**: `sed -i 's/species=/organism=/g; s/organism_id=/organism=/g' your_code.py` covers the majority of downstream callers. README has the full upgrade guide with name/abbrev/taxid resolution examples.

## v0.8.1 — 2026-05-22

Hosted endpoint release — no new tools or backends. Adds a public Streamable-HTTP deployment so MCP clients and registry indexers can connect without cloning the repo. Image published to GHCR as a parallel artifact alongside the existing stdio image.

- **`GET /healthz` route** added to `server_http.build_app()` ahead of the `/mcp` mount. Returns `200 {"status":"ok","version":<__version__>}`. No new dependency, no MCP-protocol entanglement — drop-in target for Uptime Kuma, Diun, or curl-in-cron.
- **`Dockerfile.http` + `ghcr.io/musharna/plant-genomics-mcp-http`** new image (two-stage builder + slim runtime, non-root mcp uid 10001, EXPOSE 8765, ENTRYPOINT `plant-genomics-mcp-http`). The existing `plant-genomics-mcp` stdio image is unchanged.
- **`.github/workflows/docker.yml` publishes both images** from the same trigger via parallel `metadata-action` + `build-push-action` steps sharing the buildx GHA cache. Same tag policy on both — push to `main` → `:edge`; semver tag → `:vX.Y.Z` + `:vX.Y` + `:latest`.
- **Hosted instance** at `https://mjarnoldgt76.tail86d19d.ts.net/mcp` (Tailscale Funnel → gt76 → Docker on `127.0.0.1:8765`). Open access — no token, no IP allowlist; upstream backends self-rate-limit. Best-effort uptime, demo-grade. README has the full `claude mcp add` recipe.

## v0.8.0 — 2026-05-22

P3.5 closeout — synthesis layer. Adds 4 MCP tools that compose the v0.7 live backends in parallel and reconcile cross-source results, with per-step `SynthesisEnvelope` accounting. No new backend integrations this release; v0.9 adds multi-organism resolution and v0.10 brings sequence + structure backends. Server surface grows 23 → 27 tools.

- **`analyze_locus_synth`** (Task 2): one-call equivalent of the `analyze_locus` prompt — fans out Ensembl + xrefs + UniProt + Europe PMC + QuickGO in parallel, reconciles into `{canonical_gene_name, best_uniprot_accession, conflict_flags}`. ~2 s wall against AT1G01010 in the live capture (`examples/v0.8_synthesis_walkthrough.md`).
- **`find_homologs_synth`** (Task 3): one-call equivalent of the `find_homologs` prompt — runs `blast_sequence`, then fans out per-hit `resolve_locus_to_uniprot` for every UniProt-shaped accession and attaches the full record under `ranked_hits[*].uniprot_record`. Eliminates the N+1 client-side round trip.
- **`biological_context_synth`** (Task 4): one-call equivalent of the `biological_context` prompt — UniProt resolution then parallel Gramene + KEGG + STRING + ATTED with a cross-source `consensus_partners` ranker. Partial-failure tolerant — KEGG `NotFoundError` doesn't bring down the other three sources.
- **`consensus_homologs`** (Task 5): pure cross-source synthesis. Fetches the locus's UniProt sequence, runs BLAST + Gramene in parallel, joins by accession, scores as `n_sources × mean_identity`. Surfaces multi-source orthologs that either source alone would miss.
- **`SynthesisEnvelope` + `StepRow` Pydantic models** with `outputSchema` exposure. Per-step `status` (`ok` / `error` / `skipped`) + `elapsed_s` + structured result. Strict typing (`extra="forbid"`).
- **`uniprot.fetch_sequence(client, accession)` helper** for FASTA retrieval (used by `consensus_homologs`).
- **`examples/v0.8_synthesis_walkthrough.md`** real-execution capture against AT1G01010 + reproducible runner at **`examples/_run_synthesis_chain.py`**.
- **EDAM ontology tags** on the 4 synthesis tools: operations `0224` (Query and retrieval) + `2424` (Comparison) + `2422` (Data retrieval); topics `0780` (Plant biology) + `0085` (Functional genomics).
- **TAIR / PlantCyc reframed as informational-only redirects** (P2.20 walked back): removed the dead `_call_live_if_configured` hook, the `PLANT_GENOMICS_MCP_{TAIR,PLANTCYC}_TOKEN` env-var slots, the `auth_configured` + `note_for_subscribers` response fields, and the unused `SubscriptionGatedError` class. The two tools now have a single redirect path; `status` is always `"subscription_required"`. Live wiring against Phoenix Bioinformatics / SRI BioCyc remains out of scope for this MCP — the published per-locus REST surfaces are paid-only and don't document an auth scheme, so shipping a token slot without an upstream contract was YAGNI. `SubscriptionGatedRedirect` schema tightened to 7 fields under `extra="forbid"`; 6 test cases dropped from `tests/test_tair.py` + `tests/test_plantcyc.py`.

## v0.7.1 — 2026-05-22

Patch release rolling up four follow-up items from the v0.7.0 code-quality review. No functional surface changes — same 23 tools, 3 prompts, 9 live backends.

- **README lede fix**: the v0.7.0 quote block accidentally broke into two paragraphs, orphaning the word `TAIR` on its own line and turning `- PlantCyc are informational stubs…` into a bullet list item. Restored to a single sentence: `TAIR + PlantCyc are informational stubs that redirect to the free alternatives (both services are paid-subscription-gated, probed 2026-05-21).`
- **`biological_context` prompt — `DEFAULT_TOP_N` typed as `int`**: was `"10"` (string), now `10`. The renderer's `int()` cast still works in both cases, but the constant now matches the post-parse type. The argument parser is also now stricter: `args.get("top_n")` of empty string `""` falls through to the default the same way `None` does, instead of crashing in `int("")`.
- **`examples/_run_chain.py` step 4 hardened**: if step 3 (UniProt resolution) returns no accession, we now skip STRING with an explicit `SkippedError` row in the transcript rather than re-passing the locus to `string_db.lookup_partners` (which would only re-trigger the same UniProt resolution path internally and re-incur the identical failure). The transcript stays informative; we don't double-count an upstream error.
- **`tests/test_prompts.py` cleanup**: removed a redundant local `NotFoundError` import (already imported at the top of the module) and switched three string-literal `"biological_context"` call sites to the `prompts.BIOLOGICAL_CONTEXT` constant. Added two new tests covering the int `DEFAULT_TOP_N` default-substitution path and the typed-error path for non-integer `top_n`.

## v0.7.0 — 2026-05-21

P3 closeout — bio-breadth release. Adds four new biological-context backends (Gramene homology, KEGG pathways, STRING interactions, ATTED-II coexpression) with batch variants, a `biological_context` MCP prompt that chains them, and a third real-execution proof transcript. Server surface grows 15 → 23 tools, 2 → 3 prompts. Arabidopsis-only this release; fallback backends (OMA, Reactome, IntAct, EBI Expression Atlas) and multi-organism resolver layer defer to v0.8.

- **`gramene_homologs` + `batch_gramene_homologs`** (P3.1-P3.5): async httpx client for `data.gramene.org/v69/genes?fl=homology`. Returns ortholog / paralog / all entries with target locus + homology category (ortholog_one2one / ortholog_one2many / ortholog_many2many / within_species_paralog / between_species_paralog) + Gramene gene_tree_id; the `fl=homology` projection does not carry per-row taxon, identity, protein ID, dn/ds, or goc_score. 24h cache TTL (Gramene v69 is a frozen release). Live-gated regression test (`PLANT_GENOMICS_MCP_LIVE=1`) hits the real endpoint.
- **`kegg_pathways` + `batch_kegg_pathways`** (P3.6-P3.8): two-call sequence against `rest.kegg.jp` — `/link/pathway/ath:{locus}` then `/get/path:athNNNNN` per pathway. Parses KEGG's flat-file format. Per-pathway step-2 failures land in inline `errors[]` rather than aborting the call. 24h cache TTL.
- **`string_interactions` + `batch_string_interactions`** (P3.9-P3.11): STRING-DB `/api/json/interaction_partners` with input-shape dispatch (accepts UniProt accession or Arabidopsis locus; locus input resolves via `resolve_locus_to_uniprot` first). Returns first-neighbor partners with combined STRING score plus per-channel `escore`/`dscore`/`tscore`/`pscore`. `caller_identity=plant-genomics-mcp` etiquette parameter. 1h cache TTL.
- **`atted_coexpression` + `batch_atted_coexpression`** (P3.12-P3.14): ATTED-II `/api5/?gene={locus}&topN={n}&db=Ath-u.c4-0` against the tissue-aggregated Arabidopsis release (API v5). Returns co-expression neighbors with locus + Entrez gene ID + z-score (higher = stronger coex). Friendly User-Agent header. 24h cache TTL.
- **`biological_context` MCP prompt** (P3.15): third parameterized prompt. Args: `locus` (required), `top_n` (optional, default 10). Renders a 5-tool chain: gramene_homologs → kegg_pathways → resolve_locus_to_uniprot → string_interactions → atted_coexpression. Synthesis instructions cross-reference the three result sets to surface high-confidence functional partners (interactors that are also coexpressed).
- **Real-execution proof transcript** (P3.16): `examples/biological_context_AT1G01010.{json,md}` captures the full chain against live upstreams, in the same shape as the v0.6 `analyze_locus` and `find_homologs` transcripts. Doubles as a real-execution smoke that mocked unit tests can't replace.
- **Resources updated**: `pgmcp://cache/stats` now enumerates 9 backends (was 5); `pgmcp://backends/status` lists the 4 new backends with `kind=live`, `subscription_gated=false`. No new exception classes — existing `NotFoundError` / `RateLimitError` / `UpstreamUnavailableError` cover all 4 new backends.

## v0.6.0 — 2026-05-21

P2 closeout — adds `blast_sequence`, MCP resources + prompts primitives, subscription-token config slots, live-verified Phytozome organism IDs, and verbatim real-execution transcripts of both `prompts/get` chains (which surfaced + fixed two latent bugs in the BLAST parser and UniProt accession-input dispatch).

- **Real-execution proof transcripts** (P2.b): `examples/` directory ships verbatim captures of both `prompts/get` chains driven against the live upstream APIs — `analyze_locus_AT1G01010.{json,md}` (5-tool walkthrough of Arabidopsis NAC001, resolves to Swiss-Prot Q0WV96 / NAC1_ARATH) and `find_homologs_AT1G01010_NAC_domain.{json,md}` (BLAST NAC DNA-binding domain + per-hit UniProt enrichment, 10 plant NAC-family hits resolved to NC100_ARATH / NAC92_ARATH / NAC79_ARATH). Real-execution surfaced and fixed **two latent bugs** the synthetic-fixture unit tests had missed: (a) `_parse_hit_table` in `blast.py` assumed accession at end-of-row; live NCBI output places accession at the **start** with `(Bits)/Value/Ident` as the trailing three columns — fixture in `tests/test_blast.py` had been hand-built to match the parser's wrong assumption, so unit tests passed while live parsing emitted `accession="66%"`. (b) `resolve_locus_to_uniprot` only accepted gene-name input; the `find_homologs` prompt's documented step-2 ("call `resolve_locus_to_uniprot` with `locus=<accession>`") then NotFoundError'd on every BLAST hit. Fixed by routing UniProt-accession-shaped input (with optional `.N` version-suffix strip) to a new `_fetch_by_accession` direct `/uniprotkb/{accession}.json` path. Adds `identity` field to `BlastHit` model (e.g. `"66%"`, kept as string per NCBI's literal-`%` ship), 2 new uniprot tests + parametrized accession-regex coverage, 1 corrected blast fixture. The example transcripts double as a real-execution smoke that mocked unit tests cannot replace.
- **Subscription-token config slots for TAIR / PlantCyc** (P2.20): New env vars `PLANT_GENOMICS_MCP_TAIR_TOKEN` and `PLANT_GENOMICS_MCP_PLANTCYC_TOKEN`. When set, the corresponding `tair_locus_info` / `plantcyc_locus_info` tool's response flips `status` to `"configured_live_not_implemented"`, sets `auth_configured: true`, and surfaces a `note_for_subscribers` pointer to the `_call_live_if_configured` hook. The live HTTP wiring against Phoenix Bioinformatics / SRI BioCyc is intentionally deferred — the auth schemes are undocumented in the public surface (probes return 403/404 without revealing a documented Bearer/Cookie scheme), and shipping an unverifiable client would mislead the first subscriber. Output-schema `SubscriptionGatedRedirect` updated to allow the new fields under `extra="forbid"`. Adds `tests/test_tair.py` + `tests/test_plantcyc.py` coverage for both branches (empty-string token treated as unset).
- **`KNOWN_ORGANISMS` live verification** (P2.19): All 10 entries in `phytozome.KNOWN_ORGANISMS` controller-verified against BioMart on 2026-05-21 by resolving a canonical first-gene per genome. Two IDs corrected (registry hints were stale): `sorghum_bicolor` 313 → **454** (Sbicolor_v3.1.1), `phaseolus_vulgaris` 218 → **442** (Pvulgaris_v2.1). The old IDs are absent from BioMart's filter registry (`?type=filters&dataset=phytozome`). Eight other entries verified correct as-is. Added `tests/test_phytozome.py::test_live_known_organisms_all_resolve` (gated by `PLANT_GENOMICS_MCP_LIVE=1`) as a regression for future BioMart proteome-ID renumbering.
- **MCP prompts primitive** (P2.18): `prompts/list` + `prompts/get` exposing two parameterized workflows. `analyze_locus` (args: `locus`, optional `species`) chains the canonical gene-profile walkthrough — Ensembl annotation → xrefs → UniProt → Europe PMC literature → QuickGO. `find_homologs` (args: `sequence`, optional `program`) chains `blast_sequence` → per-hit UniProt resolution. Each prompt renders to a single user-role message; clients can populate slash-command menus from `prompts/list`. Typed `[NotFoundError]` on unknown prompt / missing required arg / unsupported BLAST program.
- **MCP resources primitive** (P2.17): `resources/list` + `resources/read` exposing three read-only JSON resources — `pgmcp://cache/stats` (per-backend `TTLCache` hits/misses/size), `pgmcp://organisms/phytozome` (slug → Phytozome `organism_id` map), and `pgmcp://backends/status` (per-backend `kind`/`subscription_gated`/`probed_at` rollup). Lets operators verify caching is doing work and clients enumerate supported organisms / backends without parsing docstrings. SDK auto-advertises the `resources` capability once the handlers are registered.
- **`blast_sequence` tool** (P2.16): NCBI BLAST URLAPI client — async Put/Get polling against `https://blast.ncbi.nlm.nih.gov/Blast.cgi`. Supports blastn / blastp / blastx / tblastn / tblastx; honors NCBI etiquette (per-RID 60s poll floor, `tool=` + `email=` identity params, `PLANT_GENOMICS_MCP_NCBI_EMAIL` env override). Emits `notifications/progress` on each poll. Returns parsed top hits + raw text report excerpt (capped at 50 KB). Long searches that exceed `max_wait` raise `[NotFoundError]` with the RID preserved so the client can re-poll.

## v0.5.0 — 2026-05-21

Publishability milestone — closes P0 readiness work.

- **Differentiated exception subclasses** (`plant_genomics_mcp.errors`): `PlantGenomicsError` base + `RateLimitError` / `NotFoundError` / `UpstreamUnavailableError` / `SubscriptionGatedError` subclasses. The base `__str__` prepends `[ClassName]` so the SDK error-result serializer preserves type info on the wire — clients can route on failure kind without parsing the message.
- **Pydantic output models** (`plant_genomics_mcp.models`): `EnsemblPlantsLocus`, `PhytozomeLocus`, `TairLocusInfo`, `PlantCycLocusInfo`. Each `Tool` entry now publishes `outputSchema = Model.model_json_schema()`. The SDK now returns both `structuredContent` (dict) and `content[]` (JSON-stringified text) and validates against the schema. `EnsemblPlantsLocus` keeps only `id` + `species` required (extra="allow") to absorb sparse/future Ensembl payloads without raising.
- **EDAM ontology tags** on every tool's `_meta`: `operation_2422` (Data retrieval); topic `topic_0780` (Plant biology) + `topic_0114` (Gene structure). Smithery / Glama / bio.tools can categorize.
- **Stdio end-to-end smoke test** (`tests/test_server_stdio.py`, opt-in via `PLANT_GENOMICS_MCP_STDIO_SMOKE=1`): spawns the server as a subprocess, drives `initialize` + `list_tools` + `call_tool` over real stdio. Anti-rot guard verifies the `[NotFoundError]` wire prefix actually surfaces. CI runs the smoke on every push/PR.
- **Dockerfile + GHCR publish workflow**: two-stage `python:3.12-slim` image (~140 MB, runs as uid 10001), multi-arch `linux/amd64,linux/arm64` via QEMU+Buildx. Tags `:edge` on `main`, `:vX.Y.Z` + `:vX.Y` + `:latest` on `v*.*.*` tag push.
- **README rewrite** to the registry-discoverable scaffold popular MCP servers use: tool-count headline, category table, transport matrix, install paths (pipx + GHCR + source), per-tool usage examples, error-prefix table, chain recipes, CI/Docker/Python/License shield badges.

## v0.4.0 — 2026-05-21

- Add `plantcyc_locus_info` tool — informational stub. BioCyc PLANT orgid returns 404 for per-locus REST (live probe 2026-05-21); SRI/Phoenix subscription required. Returns a structured redirect to `ensembl_plants_lookup_locus` and `phytozome_lookup_locus`. MetaCyc parent (`META` orgid) is publicly accessible but lacks Arabidopsis gene mappings.

## v0.3.0 — 2026-05-21

- Add `tair_locus_info` tool — informational stub. TAIR's free per-locus REST API was retired (live probe 2026-05-21: public `arabidopsis.org` is a Vue SPA shell; `/api/*` endpoints return 403, gated by Phoenix Bioinformatics subscription). Returns a structured redirect to the live Ensembl Plants and Phytozome backends, which cover the same Arabidopsis annotation.

## v0.2.0 — 2026-05-21

- Add `phytozome_lookup_locus` tool — async Phytozome BioMart XML POST client (`phytozome-next.jgi.doe.gov`). Default `organism_id=167` (Arabidopsis thaliana TAIR10, live-verified). `KNOWN_ORGANISMS` dict ships 9 additional unverified hints (Glycine max, Sorghum bicolor, Brachypodium distachyon, Manihot esculenta, Eucalyptus grandis, Populus trichocarpa, Phaseolus vulgaris, Chlamydomonas reinhardtii, Daucus carota). Detects BioMart's `Query ERROR:` and empty-results idioms.

## v0.1.0 — 2026-05-21

- Initial release. `ensembl_plants_lookup_locus` tool — async httpx client for `rest.ensembl.org/lookup/id/{locus}?species={species}` with 429/5xx retry and exponential backoff. Default species `arabidopsis_thaliana`.
