# Changelog

## v1.14.0 — 2026-07-20

Adds the **protein structure + domain-architecture** view — the largest wholly-uncovered biological category per the 2026-07-20 competitor/gap audit, and the category every general-bio MCP competitor already ships. Two new tools over two new backends: **`alphafold_structure`** (AlphaFold DB predicted model) and **`interpro_domains`** (InterPro domain / family architecture, Pfam included). Both are **UniProt-keyed** — they reuse the `locus → UniProt` resolution the server already performs, so no new organism-ID mapping is needed and **all 12 organisms** work. InterPro domains also feed the `gene_report` dossier. Tool count 37 → 39; backend count 14 → 16. Minor: two new tools + two new backends, no breaking changes, no new dependencies.

**Added**

- **`alphafold_structure`** (`alphafold.lookup_locus`, async, organism-aware) — resolves the locus → UniProt accession, then fetches AlphaFold DB's `/api/prediction/{acc}`. Returns the predicted model's global mean pLDDT (`mean_plddt`), the per-band confidence distribution (`plddt_bands`), modelled residue span, latest model version, and mmCIF / PDB / PAE download URLs. A valid protein with **no deposited model** returns `found=false` (a normal outcome); a locus with no UniProt entry raises a typed `NotFoundError`. New `AlphaFoldStructure` output model.
- **`interpro_domains`** (`interpro.lookup_locus`, async, organism-aware) — resolves the locus → UniProt accession, then fetches InterPro's per-protein entries (paginated, capped at `MAX_PAGES`=5). Each row: `accession`, `name`, `type` (domain / family / homologous_superfamily / …), `source_database` (**Pfam appears here** as `source_database="pfam"`, not a separate tool), integrated InterPro `interpro` accession, and residue `locations` — plus a `count_by_type` rollup. A protein with no annotated domains returns `found=true` with an empty list; `domain_count` is the true total even when page-capped. New `InterProDomain` + `InterProDomains` output models.
- **Two new backends** (`alphafold.py`, `interpro.py`) on the standard template (per-module `TTLCache`, shared retry, typed errors). Both key on the universal `locus → UniProt` seam (mirrors quickgo), so there is **no coverage-slot gating and no coverage-matrix column** — organisms resolve via UniProt.
- **`gene_report` gains a `## Protein domains` section** — a phase-2 `interpro.lookup_by_uniprot` call keyed on the accession already resolved in phase 1 (step 8), rendered with the same graceful-degradation pattern as the other sections; `result.sections` gains a `domains` key.
- **Resources** — both backends appear in `pgmcp://cache/stats` and `pgmcp://backends/status` (`kind=live`, not gated).
- **Tests** — `test_alphafold.py` (6 mocked + 1 live) and `test_interpro.py` (6 mocked + 1 live, incl. pagination follow + cap) drive the two modules to **100% line coverage**; `gene_report` dossier test extended for the domains section. Dispatch specs, stdio-smoke name + organism sets, and resource assertions updated. Suite 520 → 527; total coverage 95%.

**Changed**

- **`README.md`** — 37 → 39 tools, 14 → 16 backends; new `alphafold_structure` + `interpro_domains` matrix rows; `gene_report` row notes the domains section.
- **`server.py` / `pyproject.toml` / `__init__.py`** — docstrings + description updated (structure + domains; 39 tools / 16 backends).

## v1.13.0 — 2026-07-19

Turns **`plantcyc_locus_info`** from a subscription-gated stub into a **live PlantCyc / Plant Metabolic Network (PMN) metabolism backend**. The earlier stub's "subscription_required" was a **misclassification** — the BioCyc web-services API (`getxml` / `xmlquery`) is free and open, re-probed 2026-07-19. The tool now returns real metabolic annotation: the enzymes, reactions, and PlantCyc pathways a locus participates in — the metabolic-pathway view that KEGG and GO don't provide. Tool count unchanged at 37 (in-place upgrade); live backend count 13 → 14. Minor: no new tool, no breaking dependency; the `plantcyc_locus_info` output schema changes from a redirect record to metabolic annotation.

**Added / Changed**

- **`plantcyc_locus_info` is now live** (`plantcyc.lookup_locus`, async, organism-aware). Walks the BioCyc data model — locus → (BioVelo `accession-1` resolution) gene frame → product enzyme(s) → catalyzed reactions → `in-pathway` pathways — with bounded, cached, concurrency-limited `getxml` hops (`MAX_REACTIONS`=25, `MAX_PATHWAYS`=40, 6-way concurrency). Returns `enzymes[]` + `reactions[]` (id/name) + `pathways[]` (id/name) + `reaction_count` / `pathway_count` (true totals even when capped). A non-enzymatic gene (e.g. a transcription factor) returns `found=false` with empty lists — a normal result, not an error. New `PlantCycReaction` / `PlantCycPathway` / `PlantCycLocusInfo` models replace the retired `SubscriptionGatedRedirect`.
- **`plantcyc_orgid` slot on `OrganismRecord`** (+ `plantcyc_orgid_for` accessor + coverage-matrix column) — maps each organism to its PMN PGDB org id (AraCyc=`ARA`, OryzaCyc=`ORYZA`, CornCyc=`CORN`, …). 11 of 12 mapped and verified via `getxml?<ORGID>:PWY-101` (2026-07-19); wheat's PGDB (wheatCyc) exists but its orgid was not resolved — left gated.
- **Resources** — PlantCyc flips from `kind=stub` to `kind=live` in `pgmcp://backends/status`, joins `pgmcp://cache/stats`, and gains a `plantcyc` column in `pgmcp://organisms/coverage`.
- **Tests** — `test_plantcyc.py` fully rewritten from the stub: 8 mocked tests driving the multi-hop traversal via a frame-routing callback (full traversal, unresolved→graceful, gene-without-enzyme, BioVelo unquoted-slot guard, malformed-XML, bad-locus typed error, unsupported-organism, 11-PGDB registry check) + 3 `PLANT_GENOMICS_MCP_LIVE=1` real-execution tests (F3H → flavonoid pathway; a TF → empty; rice cross-species via OryzaCyc). Dispatch spec (now async + organism), stdio-smoke organism set, and resource assertions updated. `plantcyc.py` at 94% coverage; suite 513 → 520.

**Changed**

- **`README.md`** — backends 13 → 14, `plantcyc_locus_info` matrix row (subscription redirect → live metabolism).
- **`server.py` / `pyproject.toml` / `__init__.py`** — docstrings + description updated (PlantCyc now live; 14 backends).

## v1.12.0 — 2026-07-19

Adds **`locus_plant_ontology`** — Plant Ontology (PO) + Trait Ontology (TO) + experimental-condition (PECO) annotations for a locus — over a new **Planteome** backend (browser.planteome.org, AmiGO2/GOlr Solr; free, no API key). Complements `locus_go_annotations`: QuickGO serves GO (species-agnostic), Planteome serves the plant-specific ontologies GO doesn't cover. Backend count 12 → 13, tool count 36 → 37. Minor: one new tool + one new backend, no breaking changes, no new dependencies.

**Added**

- **`locus_plant_ontology`** (`planteome.lookup_locus`) — queries Planteome's open Solr `/select` endpoint (edismax across searchable bioentity fields) and filters by the organism's **NCBI taxon**, so a locus that exists in multiple species resolves to the requested organism. Returns `annotations[]` (`term_id` / `term_name` / `ontology` / `aspect` / `evidence` / `reference` / `assigned_by`) + a `by_ontology` rollup (`{PO: [{term_id, term_name}], TO: […], PECO: […]}`, deduped on term_id — mirrors QuickGO's `by_aspect`). New `PlantOntologyAnnotation` + `LocusPlantOntology` output models.
- **Planteome backend** (`planteome.py`) — new live backend on the standard template (TTLCache, shared retry, typed errors). Organism handling uses the universal NCBI taxid (every organism has one), so there is **no coverage-slot gating and no coverage-matrix column**: organisms Planteome doesn't curate return an **empty** annotation list rather than an error. Coverage is strong for arabidopsis, rice, maize, grape, soybean, tomato (probed live 2026-07-19); thinner elsewhere — documented honestly in the tool description.
- **Resources** — Planteome now appears in `pgmcp://cache/stats` and `pgmcp://backends/status` (the coverage matrix is unchanged — Planteome keys on taxon, not a per-backend ID slot).
- **Tests** — 10 mocked unit tests (happy path, PO/TO namespace rollup + term_id dedup, empty-is-graceful, taxon-filter-tracks-organism, limit clamp, malformed-term-id skip, empty-locus / non-dict / missing-`response` / docs-not-list guards) + 2 `PLANT_GENOMICS_MCP_LIVE=1` real-execution tests (NAC001 → PO terms incl. "guard cell"; a thin-coverage organism returns empty, not an error). Dispatch-coverage spec, stdio-smoke name/organism sets, and resource assertions updated. `planteome.py` at 98% line coverage.

**Changed**

- **`README.md`** — tool count 36 → 37, backends 12 → 13, new `locus_plant_ontology` matrix row.
- **`server.py` / `pyproject.toml` / `__init__.py`** — module docstring and package description tool counts updated to 37.

## v1.11.0 — 2026-07-19

Adds **`go_enrichment`** — GO + KEGG over-representation analysis for a gene **list** — over a new **g:Profiler g:GOSt** backend (free, no API key). This closes the last P1 backlog item: `locus_go_annotations` answers "what terms does _this locus_ have?", while `go_enrichment` answers the dominant downstream question — "what is my differential-expression / co-expression _set_ enriched for?". First new live backend since v1.x; backend count 11 → 12, tool count 35 → 36. Minor: one new tool + one new backend, no breaking changes, no new dependencies.

**Added**

- **`go_enrichment`** (`gprofiler.go_enrichment`) — POSTs a query gene list to g:Profiler's `/api/gost/profile/`. Inputs: `loci` (the gene set), `organism` (any of the 12, resolved to a g:Profiler org ID), `sources` (default `GO:BP`/`GO:MF`/`GO:CC`/`KEGG`), optional `background` (custom statistical domain → `domain_scope=custom`), `user_threshold` (default 0.05, g:SCS-corrected), `top_n` (default 50, capped at 200). Returns `enriched[]` (term_id / name / p_value / intersection_size / precision / recall, sorted by p-value) plus **`unmapped[]`** — query loci g:Profiler could not recognize, surfaced rather than silently dropped so a locus-namespace mismatch is visible. New `GoEnrichmentTerm` + `GoEnrichmentResult` output models.
- **g:Profiler backend** (`gprofiler.py`) — new live backend on the standard template (TTLCache, shared retry, typed errors). All 12 organisms covered; the `gprofiler_id` slot on `OrganismRecord` maps each to its g:Profiler org ID (verified against `/api/util/organisms_list/` 2026-07-19 — _not_ derivable from taxid: barley → `hvulgare`, wheat → the Lancer cultivar). A `json=` passthrough was added to `_http.request_with_retry` for POST-with-JSON backends.
- **Resources** — g:Profiler now appears in `pgmcp://cache/stats` and `pgmcp://backends/status`; `pgmcp://organisms/coverage` gains a `gprofiler` column (12-organism × 6-backend matrix).
- **Tests** — 14 mocked unit tests (happy path, p-value sort + top_n cap, unmapped surfacing, default/subset/empty sources, custom background payload, bad-source / empty-loci / non-list / oversized-query / bad-threshold validation, non-dict + result-not-list payload guards) + 2 `PLANT_GENOMICS_MCP_LIVE=1` real-execution tests (Arabidopsis clock genes enrich for circadian rhythm `GO:0007623`; rice resolves to `osativa`). Dispatch-coverage spec + stdio-smoke name/organism sets + resource assertions updated. `gprofiler.py` at 100% line coverage.

**Changed**

- **`README.md`** — tool count 35 → 36, backends 11 → 12, new `go_enrichment` matrix row.
- **`server.py` / `pyproject.toml` / `__init__.py`** — module docstring and package description tool counts updated to 36.

## v1.10.0 — 2026-07-19

Adds two Ensembl Plants query tools — **`get_sequence`** and **`ensembl_region_query`** — both new query modes on the existing Ensembl REST client (no new backend, no new dependencies). Tool count 33 → 35. Minor: two new tools, no breaking changes.

**Added**

- **`get_sequence`** (`ensembl_plants.get_sequence`) — fetches a locus's sequence from Ensembl `/sequence/id`, `seq_type` ∈ `genomic` / `cds` / `cdna` / `protein` (default `protein` — the canonical-transcript product). Closes the **lookup → fetch → BLAST** loop: the returned `sequence` feeds straight into `blast_sequence` (protein for `blastp`, cds/cdna for `blastn`). Previously a user had to bring their own sequence to `blast_sequence`; now the server can produce one from a locus. New `EnsemblSequence` output model.
- **`ensembl_region_query`** (`ensembl_plants.region_query`) — lists features overlapping a genomic interval via Ensembl `/overlap/region`. Inputs: `region` (seq-region name, e.g. `"1"`), `start`, `end` (1-based inclusive), `feature` ∈ `gene` / `transcript` / `cds` / `exon` (default `gene`). Answers "what genes are in this QTL interval / assembly window" without a per-locus lookup. New `EnsemblRegionFeatures` + `RegionFeature` output models. Ensembl caps the span; oversized regions surface as `PlantGenomicsError`.
- **Tests** — 11 mocked unit tests (success paths, `seq_type`/`feature`/coordinate validation, malformed-locus-before-HTTP, non-dict/non-list payload guards, empty region) + 2 `PLANT_GENOMICS_MCP_LIVE=1` real-execution tests (protein length 429 for NAC001; AT1G01020/ARV1 found in `1:3000-10000`). Dispatch-coverage specs + stdio-smoke name/organism sets updated. Suite 471 → 492 mocked.

**Changed**

- **`README.md`** — tool count 33 → 35, two new matrix rows.
- **`server.py` / `pyproject.toml`** — module docstring and package description tool counts updated to 35.

## v1.9.0 — 2026-07-19

Adds `gene_report`, a **5th cross-source synthesis tool** — the one-shot "tell me about this gene" dossier. A single call fans out across seven live backends (Ensembl Plants annotation, UniProt, Ensembl xrefs, KEGG pathways, STRING interactors, Europe PMC literature, QuickGO GO terms) and returns a `SynthesisEnvelope` whose `result.markdown` is a rendered Markdown gene dossier — the headline, screenshot-worthy output — alongside a structured `result.sections` mirror. Minor: one new tool, no breaking changes, no new dependencies. Tool count 32 → 33; synthesis tools 4 → 5.

**Added**

- **`gene_report` synthesis tool** (`synthesis.gene_report` + `_render_gene_report_md`) — unions the `analyze_locus` chain (annotation, cross-refs, protein, GO, literature) with the `biological_context` pathway + interaction backends. Phase 1 runs Ensembl (root) + UniProt in parallel; phase 2 fans out xrefs / KEGG / STRING / literature, plus QuickGO GO when UniProt resolved. Ensembl is the root — its failure returns `result=None` with every downstream row `skipped`; any individual phase-2 failure degrades **only** that section to an "Unavailable — <reason>" note in the Markdown (and `null` in the structured mirror) so the rest of the dossier still renders. Reuses the existing `SynthesisEnvelope` `outputSchema` and `_EDAM_SYNTHESIS` tags; no new Pydantic model.
- **Tests** — 4 mocked orchestrator tests (all-backends-succeed dossier, phase-1 ensembl-failure skips rest, UniProt-failure skips GO but composes, unknown-organism root-fail) + 1 `PLANT_GENOMICS_MCP_LIVE=1` real-execution test that drives the tool against every live upstream. Dispatch-coverage spec + stdio smoke tool count updated. Suite 467 → 471 mocked.
- **Example walkthrough** — [`examples/gene_report_AT1G01010.md`](examples/gene_report_AT1G01010.md), a real-execution transcript for NAC001 (`AT1G01010`) showing the composed dossier and the graceful KEGG degradation (this locus has no KEGG pathway membership).

**Changed**

- **`README.md`** — tool count 32 → 33, synthesis 4 → 5, new `gene_report` matrix row + examples-table entry.
- **`server.py` / `pyproject.toml`** — module docstring and package description tool counts updated to 33.

## v1.8.0 — 2026-05-29

Hardens the scientific-validation benchmark (shipped in v1.6.0) into a continuously-monitored drift detector, and resolves the long-standing Phytozome rice/soybean "data drift" caveat. **No runtime change to the MCP server** — no new tools, output fields, schemas, or dependencies; the published package behaves identically. Everything added is developer/operator tooling, CI, tests, docs, and the benchmark corpus. Minor (not patch) because it lands a substantial new validation + monitoring capability and a 3×-expanded corpus, consistent with versioning repo milestones in this changelog. These are the four v1.7+ benchmark seeds carried from `workflow_audit_2026-05-29` / the v1.7.0 deploy memo, plus two roll-ins. Detail: project memory `v1_7_seeds_2026-05-29.md`.

**Added**

- **KEGG happy-path benchmark coverage (seed 1)** — `scripts/probe_kegg_happy_path.py` discovers one pathway-annotated locus per KEGG organism (chr1-window scan → bridge → confirm through production `kegg.lookup_pathways`); 7 happy-path loci added to the corpus (Arabidopsis + the 6 bridge organisms), asserting the KEGG _success_ path that the original corpus — all `expects_exception` for non-Arabidopsis — never exercised.
- **Cross-source consistency invariants (seed 2)** — a new assertion class in `scripts/benchmark_annotations.py` (`INVARIANTS` registry) checking agreement _across_ backends for one locus: `kegg_entrez_in_ensembl_xrefs` (the Entrez id KEGG's bridge resolved to must be one Ensembl `/xrefs` attests — guards the v1.4 bridge against phantom resolutions) and `kegg_orgcode_matches_resolver` (KEGG gene-id org-code prefix == resolver `kegg_org_code`). Rendered in a CROSS-SOURCE INVARIANTS report block + per-locus sidecar key; verdicts fold into the exit code.
- **Phytozome happy-path coverage for all 12 organisms (seed 3 + roll-in)** — the rice/soybean Phytozome `NotFoundError` was diagnosed (via the new `scripts/probe_phytozome_namespace.py`, sweeping all 12 proteomes) as an **ID-namespace mismatch, not data drift**: Phytozome's `gene_name_filter` indexes each genome's native ids (rice MSU `LOC_Os…`, soybean `Glyma.…`), not the Ensembl-style ids the corpus used. Native ids round-trip-confirmed live and rolled into the corpus as happy-path loci for every supported organism (was rice/maize/soybean only; wheat + tomato gain their first happy-path assertions). The `organism_name` echo is asserted via a `startswith` prefix so a Phytozome assembly-version bump doesn't FAIL.
- **Scheduled drift monitoring (seed 4)** — `.github/workflows/benchmark.yml` runs the benchmark weekly (Mon ~6–7am ET) + on manual dispatch. Two-strikes anti-flake: on a non-zero run it re-runs only the failing loci (via the new `scripts/benchmark_failing_loci.py` classifier) and pages an ntfy push (over a Tailscale-join step) + red ✗ only if the same loci fail twice. Every run uploads the sidecar(s) as an artifact. Not run on push/PR — that CI stays mocked/offline.
- **Offline corpus-integrity test** — `tests/test_benchmark_corpus.py` validates the committed `expected.json` (27 loci) against the live `_TOOLS` registry + organism resolver + fact-shape schema in normal PR CI, so a malformed corpus edit fails the PR instead of only the weekly live run. Plus unit tests for the invariants, the failing-loci classifier, and the Phytozome TSV parser (+31 tests; suite 432 → 463).

**Changed**

- **Benchmark corpus grew 9 → 27 loci** — original 9 + 7 KEGG happy-path + 11 Phytozome native-id happy-path (one per organism). Latest full sweep: 250 assertions, 0 DRIFT / 0 FAIL / 11 EXCEPTION_OK, exit 0. The originally-flagged rice/soybean Phytozome `expects_exception` entries are **kept** as namespace-mismatch regression guards.
- **`README.md`** — tool-matrix row 9 (`kegg_pathways`) corrected from 4 to the actual 7 KEGG-supported organisms (adds barley/poplar/brachypodium, the v1.5 bridge extension); new "Scientific validation / drift detection" note in the Development section pointing to the benchmark, the monitoring workflow, and `docs/benchmarking.md`.
- **`docs/benchmarking.md`** extended with Cross-source invariants, KEGG/Phytozome happy-path, and Continuous-monitoring sections (incl. the three operator secrets the workflow needs).

**Operational**

- **New operator setup for monitoring to page:** add repo secrets `BENCHMARK_NTFY_URL`, `TS_OAUTH_CLIENT_ID`, `TS_OAUTH_SECRET` (OAuth client tagged `tag:ci`), then trigger one manual `gh workflow run benchmark.yml` to validate end-to-end — PR CI cannot exercise it (live calls + secrets) and the cron won't fire until its next slot.
- No new MCP tools/resources/prompts, no new dependencies, no HTTP-transport/auth/registry change.

## v1.7.0 — 2026-05-29

Remediates all 13 findings from the first multi-agent `/workflows` code audit of v1.6.0 (0 blockers / 4 important / 9 polish, each adversarially verified against live source). No release-blocker was found; this release closes contract inconsistencies, backfills the dispatch/batch/progress/consensus test gaps that let the v0.9 wrong-arg-key class ship undetected, and makes the cache + locus-validation contracts uniform across all backends. Happy-path behavior is unchanged. Minor (not patch) for the new `KeggPathways.organism` output field + the surface-wide input-validation tightening; not major (nothing removed; only undocumented extra args are now rejected). Audit + per-finding rationale: project memory `workflow_audit_2026-05-29.md`.

**Added**

- **`KeggPathways.organism`** — `kegg_pathways` / `batch_kegg_pathways` now echo the resolved canonical organism slug, matching every other species-scoped wrapper (`EnsemblPlantsLocus`, `GeneXrefs`, `LocusLiterature`, `StringInteractions`, `BarAIVInteractions`). The output model also now declares the conditionally-emitted `entrez_gene_id` field (present for the non-Arabidopsis KEGG↔Entrez bridge, absent for `ath`), so the advertised output schema matches runtime. (audit P3)
- **`additionalProperties: false` on all 28 single-locus/batch tool input schemas** — uniform reject-unknown-args contract matching the 4 synthesis tools. A misspelled or spurious arg key (e.g. `organsim=`) is now rejected at the boundary instead of silently dropped by the `args.get(...)` dispatcher. (audit I1/P4)
- **Coverage floor** — `[tool.coverage.report] fail_under = 92` in `pyproject.toml`, honored by the existing CI `pytest --cov` run; coverage can no longer regress silently. (audit P8)
- **Tests (+45):** `tests/test_server_dispatch.py` (every tool arm through `server._dispatch` with stubbed backends; identifier+default-organism routing; spec↔`TOOLS` lock; unknown-tool branch — audit I2), three batch tools incl. the two-stage `batch_locus_go_annotations` resolve-ok/QuickGO-404 split (I4), `tests/test_progress_bridge.py` (progressToken→Reporter bridge end-to-end — I3), `consensus_homologs` phase-1.b error arms (P7), `tests/test_server_schema.py` (P4 invariant), plus cache copy-on-hit / make-key-collision / bar trailing-newline / string_db separator regressions. server.py coverage 41%→91%, TOTAL 90%→94%.

**Changed**

- **`biological_context` prompt** — dropped the bogus `organism=` from the step-1 `gramene_homologs` instruction (both Arabidopsis and non-Arabidopsis branches). `gramene_homologs` accepts only `locus` + `homology_type`; the Gramene locus already encodes species, so the key was silently dropped — a model following the prompt could have believed it scoped to a species it never did. `organism=` is kept on the kegg/uniprot/string/atted steps where the tools accept it. (audit I1)
- **`TTLCache.get` / `set` now copy on read and store** (`copy.deepcopy`) — a consumer that mutates a returned value (or aliases it into tool output) can no longer corrupt the shared cache entry for the next concurrent reader. Fulfills the cache's own share-safety docstring promise at the store layer, making the read-only-cached-value contract uniform across all 9 backends in one place rather than per-helper. `ensembl_plants.lookup_locus` also rebuilt to construct a fresh dict instead of mutating the response in place. (audit P5)
- **`cache.make_key` params serialization** — JSON-serializes the sorted `(k, v)` pairs instead of joining with literal `&`/`=`, so a param value containing those separators can't alias a different param set. Order-invariance preserved. (audit P6)
- **`string_db.lookup_partners` now validates its identifier** via `validators.assert_valid_locus` — it was the lone locus-accepting backend that skipped pre-flight validation, letting an identifier with cache-key separators reach `make_key` unescaped. UniProt accessions and loci both match the `[A-Za-z0-9._-]` class, so only genuinely malformed input is rejected. (audit P6)
- **`bar.py` switched to the shared `\Z`-anchored validator** (`validators.assert_valid_locus`) at all three path-interpolating sites, replacing its local `$`-anchored regex (Python's `$` matched before a trailing newline). Now every path-interpolating backend rejects the same inputs. (audit P2)

**Fixed**

- **`bar.py` module docstring** column order corrected to match the verified ThaleMine index map (`brief_description`@3, `tair_short_description`@8; the nonexistent `display_name` removed). Code and tests already used the correct indices — this was a stale doc that could have misled a maintainer editing the constants. (audit P1)

**Operational impact**

- **Input contract tightening:** clients passing unrecognized arguments to any tool now receive a rejection instead of having the extra key silently ignored. No documented argument is affected; only typos/extras. Review any client that relied on passing ignored keys before upgrading.
- **Output additive:** `KeggPathways` gains `organism` (always present) and `entrez_gene_id` (present for non-Arabidopsis). Consumers reading specific fields are unaffected; strict-schema consumers gain two fields.
- No new MCP tools/resources/prompts. No new dependencies. No HTTP-transport, auth, or registry-metadata change.
- Verification: 432 passed / 41 skipped, coverage 94% (floor 92), `ruff check .` clean, CI green on 3.11 + 3.12.
- Docker tags `:1.7.0` / `:1.7` / `:latest` republish on tag-push; gt76 redeploy via `docker compose pull && docker compose up -d` (Diun is notifier-only per `bugs_fixed.md` #4).

## v1.6.0 — 2026-05-26

Added `scripts/benchmark_annotations.py` — operator-runnable scientific-validation + drift detector covering all 9 backend modules + 5 synthesis pipelines against a curated 9-locus corpus. Twin-tier assertions: strict for stable facts (organism canonical, taxid, KEGG org code, gene_id prefix), tolerance-band for variable facts (annotation counts). Operationalizes external review critique #3 (scientific validation) + #4 (benchmark/eval script).

**Added**

- **`scripts/benchmark_annotations.py`** — driver script (single Python file, sync `main()`, async backend calls via `asyncio.run`). Reuses production async code path; no new HTTP logic. Per-organism `signal.alarm(120)` walltime guard. 2s sleep between organism-blocks. Verdict enumeration: PASS / DRIFT / FAIL / EXCEPTION_OK / EXCEPTION_BAD / EXCEPTION_DIFFERENT / TIMEOUT / SKIPPED.
- **`scripts/benchmark_annotations.expected.json`** — frozen baseline corpus (9 loci × 12 tools = 92 assertions). Hand-curated stable facts + first-run-captured variable facts with default 25% tolerance bands and floors. Re-baselineable via `--regenerate-baseline-all` (interactive confirmation) or `--regenerate-baseline <locus> <key>` (per-key).
- **`scripts/benchmark_annotations.last_run.json`** — most-recent run output committed for diff visibility against `expected.json`.
- **`docs/benchmarking.md`** — operator guide (read-the-table / triage-drift / re-baseline / pre-release ritual / known sparse-coverage caveats).

**First-attested baseline**

- 92 assertions / 81 PASS / 11 EXCEPTION_OK / 0 DRIFT / 0 FAIL. Exit code 0.
- 11 EXCEPTION_OK cases: 8 KEGG NotFoundError (rice/maize/soybean/barley/poplar/brachypodium chr1-first-gene loci aren't pathway-annotated; bridge mechanism still validated via resolved Entrez ID in error message) + 2 KEGG OrganismNotSupported (wheat + tomato matrix-falsified) + 1 Phytozome NotFoundError (rice + soybean canonical loci return empty BioMart response; possibly upstream data drift).
- No KEGG happy-path is currently validated — pathway-annotated loci per organism would need to be operator-selected; out of scope for v1.6.

**Operational impact**

- Non-breaking. No runtime code change. No new dependencies. No new MCP tool/resource/prompt. No CI changes.
- Added to per-release ritual: run `benchmark_annotations.py` pre-tag; pin summary counts in deploy memo. Establishes per-release benchmark history.
- Exit codes: 0 = all PASS+DRIFT (safe to ship). 1 = any FAIL (block release; investigate). 2 = script error.
- Default sweep ~3-5 min wall (without `--include-blast`); ~10-15 min with BLAST opt-in.
- Frozen baseline + tolerance bands honor the v1.4.0 #10 doctrine: Ensembl/KEGG/UniProt drift on ~6-monthly release cycles surfaces as DRIFT, not FAIL.
- Docker tags `:1.6.0` / `:1.6` / `:latest` republish on tag-push; gt76 redeploy via `docker compose pull && docker compose up -d` (Diun is notifier-only per `bugs_fixed.md` #4). No HTTP-transport, auth, or registry-metadata change.

## v1.5.0 — 2026-05-25

KEGG ↔ NCBI Entrez bridge extended to 3 additional organisms: barley (`hordeum_vulgare` → `hvg`), poplar (`populus_trichocarpa` → `pop`), Brachypodium (`brachypodium_distachyon` → `bdi`). `kegg_pathways` and `batch_kegg_pathways` now exercise the v1.4.0 bridge for these organisms instead of raising `OrganismNotSupported` at the matrix guard. The bridge mechanism is unchanged from v1.4.0 — these organisms passed the same Ensembl Plants `/xrefs/id → EntrezGene → KEGG /link/pathway` round-trip; v1.4.0 helpers in `kegg.py` are organism-agnostic and required no code change. Frozen probe evidence at `scripts/probe_kegg_bridge_candidates.json`.

**Added**

- **`kegg_org_code` flipped from `None` → populated for 3 matrix organisms:** `hordeum_vulgare` (`"hvg"`), `populus_trichocarpa` (`"pop"`), `brachypodium_distachyon` (`"bdi"`). The matrix guard at `organisms.kegg_org_code_for` now accepts these organisms; `OrganismNotSupported` is no longer raised for them.
- **`scripts/probe_kegg_bridge_candidates.py`** — operator/CI tool that probes each candidate organism's chromosome-1 first protein-coding locus through Ensembl `/info/assembly` (to discover the chromosome region name), `/overlap/region` (to pick the first gene), `/xrefs/id` (to check for EntrezGene cross-references), and KEGG `/link/pathway` (to confirm the org code is accepted). Emits a structured `pass` / `falsified` verdict per organism. Reusable for future organism additions.
- **`scripts/probe_kegg_bridge_candidates.json`** — frozen probe evidence for the 8 v1.5-scope organisms (verdict, observed cross-reference dbnames, chr1 probe locus, Entrez Gene ID, KEGG pathway count for the specific probe locus). Matrix line comments in `organisms.py` cite this artifact.

**Probed but still deferred**

- 5 organisms still raise `OrganismNotSupported` at matrix-guard time because Ensembl Plants `/xrefs/id` does not expose `EntrezGene` cross-references for the probed chr1-first-gene locus (only `ArrayExpress`-tier dbs): `triticum_aestivum`, `sorghum_bicolor`, `vitis_vinifera`, `medicago_truncatula`, `solanum_lycopersicum`. Matrix comments in `src/plant_genomics_mcp/organisms.py` document the probe date and observed cross-reference dbnames per organism. A UniProt → Entrez two-hop bridge is a candidate v1.6+ mechanism for organisms returning only non-EntrezGene cross-references; no version commitment (the UniProt fallback has not been probed against these loci).

**Pathway-annotation coverage caveat**

- The 3 v1.5 pass organisms' chr1-first-gene loci (the probe loci) happen to have 0 KEGG pathway annotations. Calling `kegg_pathways(<chr1-first-gene>, organism="hordeum_vulgare")` etc. raises `NotFoundError` ("no pathway memberships") — the bridge mechanism fires, the gene_id is constructed, but KEGG returns an empty pathway body. This matches v1.4.0 behavior for any un-annotated rice/maize/soybean locus. KEGG indexes 163 pathways for each of `hvg`/`pop`/`bdi`; pathway-annotated loci within these organisms return real data. The v1.5 ship-set widens which organisms the bridge CAN serve, not which loci are KEGG-annotated.

**Operational impact**

- Non-breaking. The 3 newly-enabled organisms previously raised `OrganismNotSupported` at resolution time, so no live consumer has stable behavior to regress against.
- Output schema unchanged — the additive `entrez_gene_id` field shipped in v1.4.0 appears for the expanded organism set; Arabidopsis still omits it.
- One additional Ensembl `/xrefs/id` call per newly-enabled `kegg_pathways` invocation, same shape as v1.4.0. Both calls share their respective `_CACHE` TTL (~24h); cold call ≈ 1 Ensembl + 1 KEGG `/link` + N KEGG `/get` in parallel (or zero KEGG `/get` if `/link` returned empty → NotFoundError); warm call hits zero network.
- `batch_kegg_pathways` picks up the new organisms automatically via composition — it fans out to `lookup_pathways` per locus.
- Docker tags `:1.5.0` / `:1.5` / `:latest` republish on tag-push; gt76 redeploy via `docker compose pull && docker compose up -d` (Diun is notifier-only per `bugs_fixed.md` #4). No HTTP-transport, auth, or registry-metadata change.

## v1.4.0 — 2026-05-25

KEGG ↔ NCBI Entrez bridge — `kegg_pathways` + `batch_kegg_pathways` now return real pathway memberships for rice (`oryza_sativa` / `osa`), maize (`zea_mays` / `zma`), and soybean (`glycine_max` / `gmx`) instead of `OrganismNotSupported`. The v1.1.0 KEGG migration intentionally left `kegg_org_code = None` for these organisms because KEGG's non-Arabidopsis scopes index NCBI Entrez Gene IDs, not the community locus namespaces (RAP-DB, MaizeGDB, SoyBase) the rest of the project speaks. v1.4.0 adds the missing locus → Entrez bridge via Ensembl Plants `/xrefs/id`. Tomato (`solanum_lycopersicum`) and the other 7 matrix organisms remain `kegg_org_code=None` — tomato was falsified at pre-impl probe (Ensembl /xrefs does not expose `EntrezGene` for tomato, only `ArrayExpress`; deferred to v1.5.0 via a different mechanism), and the other 7 are queued for a future wave once the bridge proves stable in the wild.

**Added**

- **Locus → Entrez bridge inside `kegg.py`.** Private async helper `_resolve_locus_to_entrez_id(client, locus, *, organism)` reads `ensembl_plants.lookup_xrefs(...)["by_db"]["EntrezGene"][0]` and raises `NotFoundError` with `"none from EntrezGene"` context if the locus has no EntrezGene cross-reference. When multiple EntrezGene xrefs exist (rare — read-through fusions, pseudogene/parent pairings), first-wins. Pure module-private — no new MCP tool, no new exported helper. The decision to keep the bridge inside `kegg.py` rather than promote to a standalone `entrez.py` is YAGNI: KEGG is the only consumer today. If v1.5+ adds another Entrez-bound consumer, refactor then.
- **Soybean locus normalizer `_normalize_locus_for_ensembl(locus, organism_canonical)`.** Soybean's community locus (`Glyma.04G220900` per SoyBase) is NOT what Ensembl Plants indexes (`GLYMA_04G220900` — uppercase + underscore). The bridge organism-aware-rewrites inside the helper so the user-facing `locus` field stays the SoyBase form. Other organisms and already-normalized inputs pass through unchanged. Scoped to the KEGG bridge — `ensembl_plants.lookup_xrefs` is exposed as its own MCP tool with other callers and is left untouched.
- **`entrez_gene_id` output field on `lookup_pathways`** for the bridge-firing case. Additive — omitted from the output when the bridge didn't fire (Arabidopsis). Schema stays loose: no `entrez_gene_id: null` placeholder.

**Changed**

- **`organisms.ORGANISMS["oryza_sativa"].kegg_org_code` flipped from `None` → `"osa"`**; same for `zea_mays` → `"zma"` and `glycine_max` → `"gmx"`. The matrix guard at `organisms.kegg_org_code_for` now accepts these 4 organisms (Arabidopsis + 3 new); the other 8 still raise `OrganismNotSupported(backend="kegg", ...)` pre-HTTP.
- **`kegg_pathways(locus, organism=...)` output schema gains `entrez_gene_id` for the 3 newly-supported organisms.** `kegg_gene_id` for the 3 newly-supported organisms is `<code>:<entrez-id>` (e.g., `osa:4326813`) rather than the `<code>:<community-locus>` form a naive port would have produced. No live consumer existed pre-v1.4.0 — the prior call path raised `OrganismNotSupported` before reaching output.
- **`kegg.lookup_pathways` dispatches on `org_code != "ath"`** to bridge; Arabidopsis continues to splice `gene_id = f"ath:{locus}"` with no bridge call.
- **Two existing kegg tests** (`test_lookup_pathways_unsupported_organism_raises`, `test_live_kegg_non_arabidopsis_raises_unsupported`) **swapped their organism from rice → wheat** (`triticum_aestivum`) so they continue to guard the deferred-organism branch post-bridge.

**Operational impact**

- Non-breaking. The 3 in-scope organisms previously raised `OrganismNotSupported` at resolution time, so no live consumer could have stable behavior to regress against.
- One additional Ensembl `/xrefs/id` call per non-Arabidopsis `kegg_pathways` invocation. Both calls (Ensembl + KEGG) share their respective `_CACHE` TTL (~24h) and pipeline across the same `httpx.AsyncClient` — cold call ≈ 1 Ensembl + 1 KEGG `/link` + N KEGG `/get` in parallel; warm call hits 0 network.
- Pre-impl probe confirmed live coverage (`scripts/verify_organisms.py` matrix re-probe ready): rice `Os01g0100100` → `EntrezGene 4326813`, maize `Zm00001eb000010` → `103644366`, soybean `GLYMA_01G001700` → `100810680`. Tomato `Solyc01g005610.3` returned only `ArrayExpress` — bridge mechanism falsified for tomato, deferred to v1.5.0 with a different mechanism (UniProt → Entrez two-hop, or NCBI Datasets).
- `batch_kegg_pathways` picks up the bridge automatically via composition — it fans out to `lookup_pathways` per locus.
- Docker tags `:1.4.0` / `:1.4` / `:latest` republish on tag-push; Diun on gt76 auto-redeploys the hosted demo. No HTTP-transport, auth, or registry-metadata change.

## v1.3.0 — 2026-05-24

**BREAKING** for `consensus_homologs` callers: the synthesis compose now passes `homology_type="all"` to `gramene.lookup_homologs` instead of relying on the module default `"ortholog"`. The semantic contract widens from "cross-species ortholog consensus" to "all-homology consensus" — within-species paralogs (Gramene `within_species_paralog` filter class) are now eligible for the Gramene set on every `consensus_homologs` call. Live test `test_consensus_homologs_live_at1g01010` (passed v1.2.0 unit suite but failed post-deploy real-execution) now returns 9 two-source picks for AT1G01010 and 2 for AT5G38420.

**Fixed (causal — mechanism removal, root cause was data-scope, not code)**

- **`consensus_homologs` now produces non-empty 2-source intersections for Arabidopsis-rooted queries.** v1.2.0's UniProt-accession pivot was the correct mechanism removal for the v0.8 defline-regex namespace bug, but post-deploy real-execution against `AT1G01010` revealed a structurally disjoint biological scope between the two sources: NCBI BLAST `blastp` vs `swissprot` ranks hits by sequence identity, so its top-N is dominated by **within-species paralogs** (e.g. `AT5G38420` → other RBCS isoforms `P10795`/`P10796`/`P10798`). Gramene `homology_type="ortholog"` — the module default at `gramene.py:64-68`, inherited at `synthesis.py:858` — explicitly excludes the `within_species_paralog` filter class. The two sources' output sets were therefore disjoint at the biological-scope layer, **before** any UniProt-acc dedup ran. Switching the compose to `"all"` aligns Gramene's filter scope with BLAST's actual ranking behavior. Validated via jobd job 801 (760s, laptop): `AT1G01010` → 9 shared accs (A4VCM0, A8MQY1, B5X570, O81913, O81914, Q5PP28, Q9FFI5, Q9M126, Q9SCK6); `AT5G38420` → 2 shared (P10795, P10796). Per `feedback_causal_vs_bandaid`: switching the filter (Option A in the recon memo) is mechanism removal — alternative options C/D (weakening the live-test assertion / xfail) were band-aids hiding the upstream scope mismatch.

**Changed (semantic contract, not signature)**

- **`synthesis.consensus_homologs` Gramene phase now includes within-species paralogs.** No tool signature or output-schema change; the same compose still emits `uniprot_accession`/`target_species`/`n_sources`/`sources`/`mean_identity`/`score`/`gramene_hit`/`blast_hit` keys per consensus row. Callers that depended on Gramene rows being strictly cross-species (one2one/one2many/many2many) will now see within-species paralog rows mixed in, with `target_species` matching the input organism. Filter downstream if you need the prior behavior.

**Operational impact**

- Docker tags `:1.3.0` / `:1.3` / `:latest` republish on tag-push; Diun on gt76 auto-redeploys the hosted demo. No HTTP-transport, auth, or registry-metadata change.
- Module-level `gramene.lookup_homologs(client, locus)` (with no `homology_type` kwarg) still defaults to `"ortholog"` — only the synthesis compose is affected. Direct callers of `gramene.lookup_homologs` see no behavior change.
- Unit tests pass unchanged: the Gramene fixture uses `ortholog_one2one`, which is in both the `"ortholog"` and `"all"` filter sets, so `homology_type` does not leak to the mocked HTTP layer.

## v1.2.0 — 2026-05-24

**BREAKING** for `consensus_homologs` callers: the per-consensus row dedup key + output schema flip from Gramene-locus-token to UniProt accession. `target_locus_normalized` (string, lowercased species-prefix-stripped locus) is removed; `uniprot_accession` (string, BLAST `.N` version suffix stripped, SWISSPROT-preferred from Gramene xref) takes its place. `target_species` semantics narrow: in v1.1.x it could be inferred from either source's locus token; in v1.2.0 it's sourced exclusively from the Gramene xref `system_name` field and is `None` for BLAST-only rows. Gramene homologs whose UniProt xref returns no accession (~half of v69 entries in fringe organisms like cucurbits + bryophytes) are now dropped from the consensus rather than emitted as 1-source rows. Live test `test_consensus_homologs_live_at1g01010` (red since v0.8 ship) now passes; root cause was an architectural namespace mismatch, not the threshold or fixture choice the prior investigation memo suggested.

**Fixed (causal — root cause was load-bearing)**

- **`consensus_homologs` cross-source dedup now actually works.** v0.8–v1.1.1 dedupped Gramene homologs against BLAST hits on a normalized locus-token namespace built by stripping Gramene's `<SPECIES>_` prefix off `target_locus` strings and parsing `OS=...GN=...` tokens from each BLAST hit's `description` field. Gramene's `fl=homology` projection returns species-prefixed locus IDs (`ORYSA_OS01G0100100`); NCBI BLAST against SwissProt returns deflines in `RecName: Full=...; Short=...` format with **no** `OS=` / `GN=` tokens (the `OS=`/`GN=` convention is EBI's, not NCBI's). The locus-token namespace and the defline-parse namespace never overlapped, so `n_sources==2` was structurally unreachable across the entire v0.8→v1.1.1 series; every consensus row was 1-source even when the same gene appeared in both backends. Fix shifts the join key to UniProt accession, which both backends carry natively: Gramene exposes it via the `fl=xrefs` projection (Uniprot/SWISSPROT preferred, Uniprot/SPTREMBL fallback) and BLAST returns it as `hit['accession']` (e.g. `sp|Q5VMS9.1|Y_ORYSJ`) after a `.N` version-suffix strip. Mechanism removal, not a tripwire fix — the stale regexes and the parser are deleted, not bypassed.

**Added**

- **`src/plant_genomics_mcp/gramene.py:fetch_homolog_enrichment_batch(client, loci, *, chunk_size=100)`** — new module helper. Enriches a list of Gramene loci with their preferred UniProt accession + `system_name` (organism slug) via the `/v69/genes?idList=...&fl=_id,xrefs,system_name` projection, batched comma-separated and chunked for URL-length safety on long homology lists. Returns a dict total over the input list: every input locus maps to `{"uniprot_acc": <SWISSPROT or SPTREMBL or None>, "system_name": <slug or None>}` so callers' joins don't need to handle `KeyError`. Same 24h cache as `lookup_homologs` (v69 is a frozen release).
- **`consensus_homologs` envelope now has 5 steps.** New `step=5, tool="gramene_homolog_enrichment"` row sits after the parallel `gramene_homologs` + `blast_sequence` gather. All 4 early-exit paths (unknown organism, phase-1 UniProt failure, phase-1.b sequence-fetch PlantGenomicsError, phase-1.b sequence-fetch HTTPError) emit a 5-row envelope with step 5 `_skipped` so envelope shape stays stable across success and failure paths.

**Removed (deleted — not deprecated)**

- **`synthesis._BLAST_DEFLINE_GN`**, **`_BLAST_DEFLINE_OS`**, **`_PLANT_LOCUS_TOKEN`**, **`_GRAMENE_SPECIES_PREFIX`**, **`_GRAMENE_PREFIX_TO_SPECIES`** — five module-level regex / prefix-map constants. None had callers outside `_parse_blast_subject_for_consensus` and `_normalize_locus_token`, both also deleted.
- **`synthesis._normalize_locus_token`**, **`synthesis._species_from_gramene_locus`**, **`synthesis._parse_blast_subject_for_consensus`** — three internal helpers that propped up the broken locus-token dedup. Their unit tests (`test_parse_blast_subject_extracts_species_and_gene_from_swissprot_defline`, `test_parse_blast_subject_falls_back_to_plant_locus_token`, `test_parse_blast_subject_returns_nones_for_unparseable`, `test_normalize_locus_token_strips_species_prefix_and_lowercases`) are removed from `tests/test_synthesis.py`. The three compose-level tests (`test_consensus_homologs_dedupe_groups_by_normalized_locus`, `test_consensus_homologs_scoring_prefers_two_source_hits`, `test_consensus_homologs_single_source_degenerates_gracefully`) are rewritten on the new compose signature.
- **Consensus row fields `target_locus_normalized` and the species-inferred-from-locus-token branch of `target_species`** — both removed from `_consensus_homologs_compose` output. Callers reading these fields will see `KeyError` / `None` respectively.

**Changed (signature)**

- **`synthesis._consensus_homologs_compose(gramene_payload, blast_payload, top_n=...)` → `_consensus_homologs_compose(gramene_payload, blast_payload, xref_map, *, top_n)`.** New required positional `xref_map` carries the per-locus UniProt-accession + system-name enrichment from `gramene.fetch_homolog_enrichment_batch`. Pass `xref_map={}` for BLAST-only callers; Gramene-bearing payloads need a real map or every Gramene homolog drops out. Output dict keys: `uniprot_accession`, `target_species`, `n_sources`, `sources`, `mean_identity`, `score`, `gramene_hit`, `blast_hit`.

**Operational impact**

- No HTTP-transport, auth, or registry-metadata change; the breaking surface is a single tool's output schema. Docker tags `:1.2.0` / `:1.2` / `:latest` republish on tag-push; Diun on gt76 auto-redeploys the hosted demo.
- Known coverage gap, **documented not papered-over**: ~half of Gramene v69 entries in fringe organisms (cucurbits, bryophytes — e.g. `Cla97C03G067000`, `Mp4g11910`) have no Swiss-Prot or TrEMBL xref. Without an accession they can't dedup with BLAST, so they're dropped from the consensus rather than diluting it with 1-source rows. Expanding the fallback to species+gene-name on those organisms is a v1.3 candidate.
- BLAST-only consensus rows now report `target_species=None` (was: best-effort parse from defline tokens, often `None` anyway against SwissProt). The NCBI SwissProt defline (`RecName: Full=...`) has no `OS=` species token, and we don't pay for a per-hit UniProt lookup to recover it; if you need species per BLAST hit, fetch the UniProt record from `uniprot_accession` yourself.

## v1.1.1 — 2026-05-24

Bug fix: STRING locus inputs failed for any organism where the locus has multiple valid UniProt accessions and STRING canonicalizes on a different one than our resolver picks. Surfaced as a 404 on rice `Os01g0100100` (live test `test_live_string_rice_locus_resolves_and_returns_partners`): our `uniprot.lookup_locus` returned the Swiss-Prot `Q0JRI1`, but STRING's species-canonical pick for that locus is the TrEMBL `A0A0P0UX28`, so the subsequent STRING call 404'd.

**Fixed**

- **`string_db.lookup_partners` now passes loci to STRING unchanged.** The v1.0.x → v1.1.0 codepath wrapped STRING in a UniProt pre-resolve step: any input that didn't match `_looks_like_accession()` was sent to `uniprot.lookup_locus` first, and the returned UniProt accession was passed to STRING's `/api/json/interaction_partners`. That step was architecturally redundant — STRING's own resolver handles loci directly — and produced wrong-accession 404s whenever a locus had multiple valid UniProt accessions and STRING canonicalized on a different one than UniProt's reviewed-first heuristic. v1.1.1 drops the pre-resolve entirely; loci and accessions both pass through, and `result["accession"]` surfaces STRING's species-canonical pick extracted from `stringId_A` (e.g. `AT1G01010.1` for arabidopsis loci, `A0A0P0UX28` for the rice case above).
- **Removed `string_db._looks_like_accession` helper + `_UNIPROT_RE` regex constant + `uniprot` module import from `string_db.py`** — all only existed to gate the now-deleted pre-resolve branch. Two unit tests covering the helper (`test_looks_like_accession_rejects_trailing_garbage`, the parametrized `test_looks_like_accession`) were removed; `test_lookup_partners_with_locus_input_resolves_first` was rewritten as `test_lookup_partners_with_locus_passes_through`, asserting the locus reaches STRING directly with no intermediate UniProt fetch.
- **`synthesis.biological_context_synth`** — the phase-2 STRING call now receives the original `locus`, not the phase-1 UniProt accession. The envelope still surfaces `uniprot_accession` from phase-1 unchanged.

**Operational impact**

- No schema change; tool signatures and registry metadata unchanged. PyPI republish + Diun-driven Docker retag on gt76 only.
- Live STRING tests against both arabidopsis (`Q0WV96`) and rice (`Os01g0100100`) pass post-fix.

## v1.1.0 — 2026-05-24

Polish bundle — two BREAKING contract tightenings on the multi-organism resolver, one HTTP-transport correctness fix, and a small bag of plan-T1–T4 cleanups. Two backends (KEGG, ATTED-II) that were Arabidopsis-only as of v1.0.4 now thread `organism=` through the same resolver chain as the other 9 backends — both raise `TypeError` if called without `organism=`, matching the rest of the multi-organism surface from v0.9 onward. KEGG resolves the organism to a per-organism KEGG `org_code` (e.g. `ath:`, `osa:`); ATTED-II resolves to a per-organism frozen release ID (e.g. `Ath-u.c4-0`, `Osa-u.c1-0`). Five of the 12 curated organisms (wheat, sorghum, barley, poplar, brachypodium) lack ATTED-II coverage and raise `OrganismNotSupported` before any HTTP fires; live coverage is observable at the `pgmcp://organisms/coverage` resource.

**Breaking — KEGG**

- **`kegg_pathways` + `batch_kegg_pathways` now require `organism=`.** v1.0.x hard-coded an `ath:` prefix on every locus and dropped the caller's organism intent on the floor. v1.1.0 makes the contract explicit: calls passing only `locus=` raise `TypeError`. To preserve prior behavior, add `organism="arabidopsis_thaliana"`. For other plants, pass any of the supported organism forms — slug (`"oryza_sativa"`), scientific name (`"Oryza sativa"`), common name (`"rice"`), or NCBI taxid (`4530`). The resolver picks the per-organism KEGG `org_code` (`osa:` for rice) and splices it onto the locus before the `/link/pathway/` call.
- Live KEGG `/link/pathway` is case-sensitive on the `<org>:<locus>` argument as of KEGG release 118.0 (2026-05-26). v1.0.x callers got lucky because every Arabidopsis locus was already upper-cased after the `ath:` prefix; v1.1.0 preserves the caller's case verbatim.
- KEGG's non-Arabidopsis organism scopes index NCBI Entrez Gene IDs, **not** RAP-DB (`Os…`), MaizeGDB (`Zm…`), or other community locus namespaces. Calling `kegg_pathways` with a RAP-DB rice locus + `organism="oryza_sativa"` will return zero pathways even though KEGG actually has rich rice coverage indexed under Entrez IDs. A locus → Entrez bridge is deferred to a follow-up — see GitHub issues.

**Breaking — ATTED-II**

- **`atted_coexpression` + `batch_atted_coexpression` now require `organism=`.** v1.0.x exposed a module-level `atted.ATTED_RELEASE = "Ath-u.c4-0"` constant and ignored organism intent entirely; v1.1.0 drops the constant and resolves the release per organism via `organisms.atted_release_for(query)`.
- ATTED-II coverage is narrower than the 12-organism matrix. The 7 covered organisms (`arabidopsis_thaliana`, `oryza_sativa`, `zea_mays`, `solanum_lycopersicum`, `glycine_max`, `vitis_vinifera`, `medicago_truncatula`) each have a frozen release ID; the remaining 5 (`triticum_aestivum`, `sorghum_bicolor`, `hordeum_vulgare`, `populus_trichocarpa`, `brachypodium_distachyon`) raise `OrganismNotSupported` before any HTTP fires, with the human-readable list surfaced on the exception.
- Coverage matrix verified by `scripts/verify_organisms.py probe_atted` against `https://atted.jp/api5/` on 2026-05-24 — every populated release ID is the live current ID, not a stale guess.

**Added**

- **`src/plant_genomics_mcp/organisms.py`** — `OrganismRecord` gains `kegg_org_code: str | None` and `atted_release: str | None` fields. Two new module accessors `organisms.kegg_org_code_for(query)` and `organisms.atted_release_for(query)` resolve through the existing alias index and raise `OrganismNotSupported(backend=..., supported=...)` when the resolved record has the slot set to `None`. The matrix is populated for all 12 curated organisms — 12 KEGG codes (every organism is in KEGG), 7 ATTED-II releases (5 unsupported).
- **`scripts/verify_organisms.py`** — extended with `probe_kegg` + `probe_atted` to ground-truth-verify every `kegg_org_code` and `atted_release` against the live upstreams before release. `probe_kegg` rejects 200+HTML error pages from KEGG's intercept HTML (T4 review nit fix); `probe_atted` parses the `Ath-u.c4-0` release identifier shape and confirms `gene/topN` returns a non-empty `result_set`.
- **`pgmcp://organisms/coverage` resource** — adds `kegg` and `atted` columns alongside the existing `ensembl_plants`, `phytozome`, `uniprot`, etc. columns. Clients can now read the full per-backend support matrix from a single resource without instantiating every backend.
- **Codecov badge + CI coverage upload.** README front-page badge + `.github/workflows/ci.yml` uploads `coverage.xml` from the `pytest --cov` step to Codecov on every push. Coverage today: 90% (2026 stmts).

**Changed**

- **`batch.batch_ensembl_plants_lookup_locus`** — now retries 429/5xx via the shared `_http.request_with_retry` helper (Retry-After capped at 60 s, Wave B2 contract). Closes the explicit "scheduled for v1.1" gap left at `batch.py:107-114` when the helper was introduced in v1.0.3. Misses (null record per ID in the upstream batch response) still surface as `[NotFoundError]` entries in the per-locus `errors` map; the whole batch only fails when the upstream call exhausts the retry budget.

**Fixed**

- **HTTP transport: `Starlette(redirect_slashes=False)`.** v1.0.x's default Starlette behavior emitted a 307 on `GET /mcp` (no trailing slash) pointing at `http:///mcp/` — a scheme-downgrade because Starlette generates the `Location` header from the inner request that the reverse proxy has already terminated as `http`. Behind Tailscale Funnel or any HTTPS-terminating reverse proxy, the resulting `Location: http://...` broke HTTPS-only clients. v1.1.0 disables the auto-redirect entirely — clients should register with the trailing-slash form (`/mcp/`); `GET /mcp` now returns a flat 404 from Starlette's route resolver.

**Operational impact**

- Docker images retag `:1.1.0` / `:1.1` / `:latest` on tag-push; Diun on gt76 auto-redeploys the hosted demo. After the redeploy, `curl -sI https://mjarnoldgt76.tail86d19d.ts.net/mcp` returns `404` (was `307` with the broken `Location`); `curl -sI https://mjarnoldgt76.tail86d19d.ts.net/mcp/` with the bearer token returns `200`/`406` per the streamable-HTTP handler.
- MCP registry metadata (short description, `mcp-name` token) unchanged — no `mcp-publisher publish` re-submit needed beyond the PyPI version bump.

## v1.0.4 — 2026-05-24

Registry-publish unblocker. Adds the `mcp-name: io.github.musharna/plant-genomics-mcp` ownership-verification token to the README footer so `mcp-publisher publish` can validate that the PyPI package owner controls the MCP namespace. PyPI versions are immutable, so this is a metadata-only re-release on top of v1.0.3 — no code, schema, or behavior changes. OCI image stays at `ghcr.io/musharna/plant-genomics-mcp:1.0.3` (the registry's ownership check is PyPI-specific).

- **`README.md`** — new `## MCP registry` section above License with the literal `mcp-name:` token in a fenced block.
- **`pyproject.toml`** / **`src/plant_genomics_mcp/__init__.py`** — version bump 1.0.3 → 1.0.4.
- **`server.json`** — top-level `version` and `packages[pypi].version` both → 1.0.4; OCI entry unchanged.
- **No tests affected** — pure documentation/metadata.

## v1.0.3 — 2026-05-24

Internal refactor — extracts the duplicated 429/5xx-retry + `Retry-After`-cap + progress-notification + status→typed-exception loop from 9 backend modules into a single shared helper `plant_genomics_mcp._http.request_with_retry()`. Behavior is preserved: same retry budget (3), same retryable status set (`429, 500, 502, 503, 504`), same 60s `Retry-After` cap from v1.0.0 Wave B2, same exception classes (`NotFoundError`, `RateLimitError`, `UpstreamUnavailableError`, `PlantGenomicsError`). No API changes; no tool-surface changes; no schema changes. Pure code-deduplication ahead of the v1.1 BAR + StringDB + Gramene shape evolution where divergent retry behavior would otherwise drift further.

- **New `src/plant_genomics_mcp/_http.py`** — `request_with_retry(client, method, url, *, service, params=None, data=None, headers=None, timeout, max_retries)` returns the raw `httpx.Response` so each caller retains control of JSON/text parsing and per-backend `cache.TTLCache` write-through. Optional `not_found_returns` parameter accepts a sentinel (used by KEGG, which returns empty-body 404s rather than raising).
- **Migrated callers (9 modules):** `ensembl_plants.py`, `kegg.py`, `bar.py`, `atted.py`, `europe_pmc.py`, `gramene.py`, `quickgo.py`, `string_db.py`, `phytozome.py` (POST variant), `uniprot.py` (3 inline sites — `_search`, `_fetch_by_accession`, `fetch_sequence`; the latter two wrap with `try/except NotFoundError` to preserve the canonical "UniProt has no entry/FASTA for accession=X" message that several tests assert on).
- **Test-only change:** `tests/test_ensembl_plants.py` now patches `_http.asyncio.sleep` instead of `ensembl_plants.asyncio.sleep` to intercept retry backoff (the sleep call moved into the shared helper).
- **Verification:** full suite green (350 passed, 34 skipped — same counts as v1.0.2). Live tests gated by `PLANT_GENOMICS_MCP_LIVE=1` were not re-run; the migration is a pure code move, not a wire-protocol change.
- **No operational impact.** Docker images `:1.0.3` / `:1.0` / `:latest` retag on merge; Diun on gt76 auto-redeploys. Behavior on the hosted demo at `https://mjarnoldgt76.tail86d19d.ts.net/mcp` is unchanged.

## v1.0.2 — 2026-05-23

Hot-fix — repairs the BAR backend, which was DOA in v1.0.0 and v1.0.1. The `bar` module was missing from the `from plant_genomics_mcp import (...)` block in `server.py`, so any dispatch of `bar_gene_summary`, `bar_efp_expression`, or `bar_aiv_interactions` raised `NameError: name 'bar' is not defined` at runtime — three of the 32 tools (plus their batch variants and the silently-aliased `tair_locus_info`) were unusable over stdio/HTTP. Existing BAR unit tests passed because they call `bar.gene_summary(...)` directly and never exercise the server-level dispatcher. Also fixes the related ruff lint failure that has been red on `main` since v1.0.0 (`F821 Undefined name 'bar'` ×3 in `server.py`, `E402 Module level import not at top of file` in `tests/test_organisms.py`).

- **`src/plant_genomics_mcp/server.py`** — add `bar,` to the import block (alphabetic position between `batch,` and `blast,`), restoring the binding the dispatcher relies on.
- **`tests/test_bar.py`** — new regression test `test_dispatch_bar_gene_summary_resolves_bar_module` routes through `server._dispatch("bar_gene_summary", ...)` with mocked HTTPX so any future drop of the `bar,` import fails CI loudly. Module-level direct-call tests stayed green through the bug; this test pins the _dispatch path_ contract.
- **`tests/test_organisms.py`** — move the `from plant_genomics_mcp.errors import (...)` block above the test function defs (E402 cleanup).
- **Operational impact.** Hosted demo at `https://mjarnoldgt76.tail86d19d.ts.net/mcp` has been silently 500-ing on BAR tool calls for the v1.0.0 → v1.0.1 window (~few hours). The Docker pipeline retags `:1.0.2` / `:1.0` / `:latest`; Diun on gt76 auto-redeploys.

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
