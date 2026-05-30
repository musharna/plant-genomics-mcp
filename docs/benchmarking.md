# Benchmark annotations

Operator guide for `scripts/benchmark_annotations.py` — the v1.6 scientific-validation + drift detector.

## What it is

A side-channel observability tool that drives ~9 curated canonical loci through every backend module + synthesis pipeline, compares results to `scripts/benchmark_annotations.expected.json`, and emits per-locus-per-tool PASS / DRIFT / FAIL verdicts.

Twin-tier assertions:

- **`stable_facts`** — exact match required. Things that should never change (organism canonical slug, NCBI taxid, KEGG org code, gene_id prefix).
- **`variable_facts`** — tolerance-band match. Counts that drift on upstream release cycles (pathway count, GO count, xref count, partner count). Each entry: `{baseline, tolerance_pct, floor, ceiling?}`.
- **`expects_exception`** — anticipated exception. For matrix-falsified-organism cases (wheat/tomato calling KEGG raises `OrganismNotSupported`), or known-sparse-annotation cases (rice/maize/soybean/barley/poplar/Brachypodium chr1-first-gene KEGG raises `NotFoundError` because the bridge fires but the specific locus has 0 pathway annotations).

## Running

```bash
# Default sweep — ~3-5 min wall, no BLAST
.venv/bin/python scripts/benchmark_annotations.py

# Subset by locus
.venv/bin/python scripts/benchmark_annotations.py --loci AT1G01010,Os01g0100100

# Subset by tool (backend module name)
.venv/bin/python scripts/benchmark_annotations.py --tools kegg,ensembl_plants

# With BLAST (~5-10 min queue per BLAST call)
.venv/bin/python scripts/benchmark_annotations.py --include-blast

# Quiet (JSON sidecar only, no markdown stdout)
.venv/bin/python scripts/benchmark_annotations.py --quiet
```

## Exit codes

| Code | Meaning                                                                               |
| ---- | ------------------------------------------------------------------------------------- |
| 0    | All PASS + DRIFT + SKIPPED + EXCEPTION_OK. Safe to ship.                              |
| 1    | Any FAIL / EXCEPTION_BAD / EXCEPTION_DIFFERENT / TIMEOUT. Block release; investigate. |
| 2    | Script-level error (couldn't import, malformed expected.json).                        |

## Reading the table

```
LEGEND:  ✓ PASS   ~ DRIFT   ✗ FAIL   ! EXCEPTION_BAD/DIFFERENT   T TIMEOUT   - SKIPPED
```

Pivot rows are loci; columns are tools (truncated module names). Each cell shows the WORST verdict across that locus×tool's assertion set. A `~` cell means at least one assertion drifted; check the `WORST OFFENDERS (DRIFT)` block below the table for specifics.

## Cross-source invariants (v1.7)

Below the per-tool table, a `CROSS-SOURCE INVARIANTS` block reports assertions that check **agreement across backends** for the same locus — beyond what any single tool's stable/variable facts can see. They are reuse-only: each invariant reads tool responses already collected during the run (near-zero extra HTTP), runs as a post-pass per locus, and folds its verdict (PASS / FAIL / SKIPPED) into the summary counts and exit code. A FAIL here blocks release exactly like a stable_fact FAIL.

Each invariant has an `applies()` gate (→ SKIPPED when the locus lacks the needed responses or is on a different code path) and a `check()` (→ PASS / FAIL). Current invariants:

| Invariant                       | Applies when                                                                                                             | Checks                                                                        | Guards                                                                                                     |
| ------------------------------- | ------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| `kegg_entrez_in_ensembl_xrefs`  | bridge organism (non-Arabidopsis), KEGG succeeded with an `entrez_gene_id`, and `ensembl_plants.lookup_xrefs` is present | the Entrez ID KEGG's bridge resolved to ∈ Ensembl `/xrefs` `by_db.EntrezGene` | the v1.4 KEGG↔Entrez bridge — proves KEGG resolved to an Entrez ID Ensembl actually attests, not a phantom |
| `kegg_orgcode_matches_resolver` | `kegg.lookup_pathways` and `organisms.resolve` both succeeded                                                            | `kegg_gene_id` org-code prefix == resolver `kegg_org_code`                    | the org-code wiring between the resolver and live KEGG gene id                                             |

Arabidopsis uses the native `ath:` KEGG path (no Entrez bridge), so `kegg_entrez_in_ensembl_xrefs` is SKIPPED there; `kegg_orgcode_matches_resolver` still applies. The invariant registry lives in `scripts/benchmark_annotations.py` (`INVARIANTS`); add one by appending an `Invariant(name, applies, check)`. Excluded as flaky: Ensembl-xref-UniProt-acc vs `uniprot.primaryAccession` (legitimate SwissProt/TrEMBL divergence).

## Triaging DRIFT

DRIFT means a `variable_facts` actual was outside the tolerance band but still within floor/ceiling. Not a failure — surfaces for review.

| Cause                                              | Action                                                                                                                                                |
| -------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| Real upstream drift (Ensembl shipped, IDs rotated) | `--regenerate-baseline <locus> <module>.<fn>.<key>` per-key, OR `--regenerate-baseline-all` (typed confirmation required). Re-commit `expected.json`. |
| Tolerance band too tight                           | Bump `tolerance_pct` for that key in `expected.json`; re-commit.                                                                                      |
| Real regression sneaking in as DRIFT               | Investigate as bug; don't bump thresholds.                                                                                                            |

## Triaging FAIL

FAIL means a `stable_facts` actual ≠ expected, OR a `variable_facts` actual is below floor / above ceiling, OR an unanticipated exception was raised.

| Cause                                              | Action                                                                                     |
| -------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| Upstream rotation past the floor                   | Bump floor down or tolerance up + regen baseline. Document the rotation in project memory. |
| Real regression — your code change broke a tool    | Fix the code; don't bump thresholds.                                                       |
| Network flake (Europe PMC, BLAST, sometimes ATTED) | Re-run. If green, file as flake; if stays red, treat as real.                              |

## Re-baselining

```bash
# Full re-capture (interactive confirmation required)
.venv/bin/python scripts/benchmark_annotations.py --regenerate-baseline-all
# Type 'regenerate' at prompt

# Per-key
.venv/bin/python scripts/benchmark_annotations.py --regenerate-baseline AT1G01010 atted.lookup_coexpression.neighbors.len
```

The script deep-copies the existing `expected.json`, overwrites only the targeted variable_facts, and writes it back. `stable_facts` is never touched by regen — those are hand-curated forever.

## Pre-release ritual

Run `.venv/bin/python scripts/benchmark_annotations.py` after T7 of the release plan (release scaffolding) and before T8 (tag/push). Pin the summary line counts in the deploy memo:

```
Benchmark baseline (v1.X.0): 92 assertions / 81 PASS / 0 DRIFT / 0 FAIL / 11 EXCEPTION_OK
```

If FAIL count > 0 at this point, decide before tagging: re-baseline + ship, or investigate + delay.

## Continuous monitoring (v1.7)

`.github/workflows/benchmark.yml` runs the benchmark on a schedule so upstream drift is caught between releases, not just at release time.

- **When:** weekly, `cron: '0 11 * * 1'` (Mon 11:00 UTC ≈ 6–7am ET) + manual `workflow_dispatch`. NOT run on push/PR — that CI (`test.yml`) is mocked and offline; this is the only live-calling workflow.
- **Two-strikes (anti-flake):** run 1 writes `last_run.ci.json`. On a non-zero exit, `scripts/benchmark_failing_loci.py` extracts just the failing `locus_id`s (classified by `benchmark_annotations.EXIT_TRIGGERING_VERDICTS` — the same set the exit code uses) and the workflow re-runs only those (`--loci`). It pages **only if the same loci fail twice**, so a transient ATTED / Europe PMC blip self-heals on the retry. A non-zero exit with _no_ failing locus (a script-level crash) is treated as a confirmed failure — never swallowed.
- **Surfacing:** on a confirmed failure the workflow pages **Telegram** (`api.telegram.org` `sendMessage`) **and** a **public ntfy topic** (priority high) with the failing loci + run URL, then exits non-zero so GitHub's red ✗ and scheduled-failure email fire too. This mirrors the homelab `notify.sh` fan-out (Telegram bot + unified ntfy topic) but hits both public endpoints directly, so the runner needs no tailnet access. Each notification step is best-effort: a missing/empty secret emits a workflow `::warning::` and is skipped rather than failing the job. Every run uploads `last_run.ci.json` (+ `rerun.ci.json` if a retry happened) as the `benchmark-sidecars` artifact for diffing.
- **Triage:** a page means run the FAIL/DRIFT triage tables above. Download the artifact to see which assertion moved.

**Operator setup — three repo secrets** (the workflow references them; they are not in the repo):

```bash
gh secret set TELEGRAM_BOT_TOKEN   # BotFather token for the homelab bot (api.telegram.org)
gh secret set TELEGRAM_CHAT_ID     # destination chat id for that bot
gh secret set BENCHMARK_NTFY_URL   # full topic URL, e.g. https://ntfy.sh/mjarnold-homelab-<id>
```

After merge, trigger one manual run (`gh workflow run benchmark.yml` or the Actions tab) to validate end-to-end — the schedule alone won't fire until its next slot. A healthy run is green and pages nothing; the notification path only fires on a confirmed two-strikes failure.

## Files

| Path                                          | Purpose                                                                        |
| --------------------------------------------- | ------------------------------------------------------------------------------ |
| `scripts/benchmark_annotations.py`            | Driver                                                                         |
| `scripts/benchmark_annotations.expected.json` | Frozen baseline (committed, hand-curated stable + auto-captured variable)      |
| `scripts/benchmark_annotations.last_run.json` | Most-recent output (committed for diff visibility)                             |
| `scripts/benchmark_failing_loci.py`           | Classifier: failing `locus_id`s from a sidecar (monitoring two-strikes re-run) |
| `.github/workflows/benchmark.yml`             | Weekly + manual scheduled monitor (see Continuous monitoring)                  |

## Corpus shape (v1.7 baseline)

27 loci. The original 9 (below) + 7 KEGG happy-path loci + 11 Phytozome native-ID happy-path loci (one per supported organism), all added across v1.7/v1.8 (see the happy-path tables). Coverage:

| #   | Locus                       | Organism            | Coverage                                                                                                                                                                                                                                        |
| --- | --------------------------- | ------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | `AT1G01010`                 | Arabidopsis         | BAR (gene_summary / efp / aiv), ATTED, KEGG `ath:` (no annotation → expects NotFoundError), Europe PMC, UniProt, Ensembl                                                                                                                        |
| 2   | `Os01g0100100`              | Rice                | Ensembl, UniProt, Europe PMC, STRING; KEGG bridge fires but 0 pathways (expects NotFoundError); Phytozome namespace guard (RAP-DB id → expects NotFoundError; native `LOC_Os01g01307` happy-path added separately); Gramene (no assertions yet) |
| 3   | `Zm00001eb000010`           | Maize               | Ensembl, KEGG bridge fires but 0 pathways (expects NotFoundError)                                                                                                                                                                               |
| 4   | `GLYMA_01G001700`           | Soybean             | Ensembl, KEGG bridge fires but 0 pathways (expects NotFoundError); Phytozome namespace guard (`GLYMA_` id → expects NotFoundError; native `Glyma.02G140400` happy-path added separately)                                                        |
| 5   | `HORVU.MOREX.r3.1HG0000090` | Barley (v1.5)       | Ensembl, KEGG bridge fires but 0 pathways (expects NotFoundError)                                                                                                                                                                               |
| 6   | `Potri.001G006600.v4.1`     | Poplar (v1.5)       | Ensembl, KEGG bridge fires but 0 pathways (expects NotFoundError)                                                                                                                                                                               |
| 7   | `BRADI_1g00485v3`           | Brachypodium (v1.5) | Ensembl, KEGG bridge fires but 0 pathways (expects NotFoundError)                                                                                                                                                                               |
| 8   | `TraesCS1A02G000300`        | Wheat (falsified)   | Ensembl; KEGG raises OrganismNotSupported (matrix guard)                                                                                                                                                                                        |
| 9   | `Solyc01g005610.3`          | Tomato (falsified)  | Ensembl, Europe PMC; KEGG raises OrganismNotSupported (matrix guard)                                                                                                                                                                            |

### KEGG happy-path loci (v1.7)

Seven loci that DO carry pathway annotations — one per supported organism — discovered by `scripts/probe_kegg_happy_path.py` (scans the first ~5 Mb of chr1 per genome for a pathway-annotated gene). Each asserts the live `kegg.lookup_pathways` success path (stable `kegg_gene_id` + `organism`; variable `pathways.len`). The 6 bridge loci also carry `ensembl_plants.lookup_xrefs` (a within-run cache hit — the bridge already fetched it) so the `kegg_entrez_in_ensembl_xrefs` cross-source invariant can run.

| Organism     | Locus                       | `kegg_gene_id`           | pathways |
| ------------ | --------------------------- | ------------------------ | -------- |
| Arabidopsis  | `AT1G01050`                 | `ath:AT1G01050` (native) | 1        |
| Rice         | `Os01g0100700`              | `osa:4326457`            | 1        |
| Maize        | `Zm00001eb000210`           | `zma:100383860`          | 3        |
| Soybean      | `GLYMA_01G001300`           | `gmx:548054`             | 3        |
| Barley       | `HORVU.MOREX.r3.1HG0000040` | `hvg:123394901`          | 2        |
| Poplar       | `Potri.001G000500.v4.1`     | `pop:7483226`            | 2        |
| Brachypodium | `BRADI_1g00460v3`           | `bdi:100836389`          | 1        |

The original 9 corpus loci still validate the KEGG bridge's _failure_ path: non-Arabidopsis chr1-first-gene loci raise NotFoundError (0 annotations; resolved Entrez ID still appears in the message), and matrix-falsified organisms raise OrganismNotSupported.

### Phytozome happy-path loci (v1.7 diagnosis, v1.8 full coverage)

The rice/soybean Phytozome NotFoundError was **diagnosed in v1.7 as an ID-namespace mismatch, not data drift.** Phytozome's `gene_name_filter` indexes each genome's NATIVE gene names, not the Ensembl-style IDs the corpus used: rice wants MSU `LOC_Os...` (not RAP-DB `Os01g...`), soybean wants `Glyma.NNg...` dot-format (not `GLYMA_` underscore). `scripts/probe_phytozome_namespace.py` swept all 12 Phytozome organisms (org-id-only BioMart query, stream-capped, round-trip-confirmed through production `phytozome.lookup_locus`) and found a working native gene for **every** one; the two originally-flagged organisms' canonical IDs raise while the native IDs succeed (verdict `namespace_mismatch_confirmed`; `organism_name` echo confirms the `phytozome_int` is correct). Findings: `scripts/probe_phytozome_namespace.json`.

v1.8 rolled the probe's validated native IDs into the corpus as happy-path loci for **all 12 organisms** (was rice/maize/soybean only). The two originally-flagged organisms' `expects_exception` entries are **kept** as namespace-mismatch regression guards.

| Organism     | Native happy-path locus     | `organism_name` prefix asserted | Namespace guard (still expects NotFoundError) |
| ------------ | --------------------------- | ------------------------------- | --------------------------------------------- |
| Arabidopsis  | `AT1G07270`                 | `Athaliana`                     | —                                             |
| Rice         | `LOC_Os01g01307`            | `Osativa`                       | `Os01g0100100` (RAP-DB)                       |
| Maize        | `Zm00001eb000010`           | (native == Ensembl id)          | —                                             |
| Wheat        | `TraesCS5A03G0137600`       | `Taestivum`                     | —                                             |
| Tomato       | `Solyc01g080240`            | `Slycopersicum`                 | —                                             |
| Soybean      | `Glyma.02G140400`           | `Gmax`                          | `GLYMA_01G001700` (underscore)                |
| Sorghum      | `Sobic.003G000100`          | `Sbicolor`                      | —                                             |
| Barley       | `HORVU.MOREX.r3.UnG0785490` | `Hvulgare`                      | —                                             |
| Grape        | `VIT_201s0010g00130`        | `Vvinifera`                     | —                                             |
| Poplar       | `Potri.001G226000`          | `Ptrichocarpa`                  | —                                             |
| Medicago     | `Medtr1g014230`             | `Mtruncatula`                   | —                                             |
| Brachypodium | `Bradi1g72735`              | `Bdistachyon`                   | —                                             |

The happy-path `organism_name` assertion uses a `startswith` prefix (the genus-species token, e.g. `Osativa`) so a Phytozome assembly-version bump (e.g. `v7.0`→`v7.1`) does not spuriously FAIL the build. Maize needed no namespace fix because its native Phytozome id (`Zm00001eb...`) coincides with the Ensembl id. Wheat and tomato are KEGG-matrix-falsified (no KEGG path) but Phytozome-supported, so these are their first happy-path assertions in the corpus.

## Adding a new organism

When `organisms.py` gains a new entry:

1. Add a locus block to `expected.json` with `stable_facts` populated by hand (canonical, taxid, KEGG org code if any).
2. Run `--regenerate-baseline-all` (or `--loci <new>` first to isolate).
3. Review the captured `variable_facts`; sanity-check counts.
4. Commit.

Estimated effort: ~1 hour per organism.

## Out of scope (v1.7+ candidates)

- MCP-server-layer dispatch testing.
- Annotation-quality scoring.
- More cross-source invariants (e.g. INV-3 organism-echo agreement; the Ensembl-xref-UniProt-acc invariant is deliberately excluded as flaky).

**Done (v1.7 diagnosis → v1.8 release):** KEGG happy-path coverage (7 loci) · cross-source consistency invariants (`kegg_entrez_in_ensembl_xrefs`, `kegg_orgcode_matches_resolver`) · Phytozome namespace diagnosis (rice/soybean drift was a namespace mismatch, not data drift) + native-ID happy-path for **all 12** organisms · continuous monitoring (weekly scheduled GH Actions workflow, two-strikes Telegram + ntfy paging).
