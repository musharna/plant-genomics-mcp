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

## Files

| Path                                          | Purpose                                                                   |
| --------------------------------------------- | ------------------------------------------------------------------------- |
| `scripts/benchmark_annotations.py`            | Driver                                                                    |
| `scripts/benchmark_annotations.expected.json` | Frozen baseline (committed, hand-curated stable + auto-captured variable) |
| `scripts/benchmark_annotations.last_run.json` | Most-recent output (committed for diff visibility)                        |

## Corpus shape (v1.6 baseline)

9 loci × 12 tools = 92 assertions. Coverage:

| #   | Locus                       | Organism            | Coverage                                                                                                                                                                  |
| --- | --------------------------- | ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | `AT1G01010`                 | Arabidopsis         | BAR (gene_summary / efp / aiv), ATTED, KEGG `ath:` (no annotation → expects NotFoundError), Europe PMC, UniProt, Ensembl                                                  |
| 2   | `Os01g0100100`              | Rice                | Ensembl, UniProt, Europe PMC, STRING; KEGG bridge fires but 0 pathways (expects NotFoundError); Phytozome data drift (expects NotFoundError); Gramene (no assertions yet) |
| 3   | `Zm00001eb000010`           | Maize               | Ensembl, KEGG bridge fires but 0 pathways (expects NotFoundError)                                                                                                         |
| 4   | `GLYMA_01G001700`           | Soybean             | Ensembl, KEGG bridge fires but 0 pathways (expects NotFoundError); Phytozome data drift (expects NotFoundError)                                                           |
| 5   | `HORVU.MOREX.r3.1HG0000090` | Barley (v1.5)       | Ensembl, KEGG bridge fires but 0 pathways (expects NotFoundError)                                                                                                         |
| 6   | `Potri.001G006600.v4.1`     | Poplar (v1.5)       | Ensembl, KEGG bridge fires but 0 pathways (expects NotFoundError)                                                                                                         |
| 7   | `BRADI_1g00485v3`           | Brachypodium (v1.5) | Ensembl, KEGG bridge fires but 0 pathways (expects NotFoundError)                                                                                                         |
| 8   | `TraesCS1A02G000300`        | Wheat (falsified)   | Ensembl; KEGG raises OrganismNotSupported (matrix guard)                                                                                                                  |
| 9   | `Solyc01g005610.3`          | Tomato (falsified)  | Ensembl, Europe PMC; KEGG raises OrganismNotSupported (matrix guard)                                                                                                      |

**Known sparse coverage:** No KEGG happy-path is currently validated. Every non-Arabidopsis KEGG call in the corpus raises NotFoundError (because the chr1-first-gene loci happen to have 0 pathway annotations) or OrganismNotSupported (matrix-falsified). The bridge mechanism is still validated — the resolved Entrez ID appears in the NotFoundError message. To add a KEGG happy-path assertion, swap in a known pathway-annotated locus per organism (operator-determined; not currently in scope).

**Known data drift:** Phytozome for rice / soybean returns NotFoundError for the canonical Wave A2 loci. Possibly upstream BioMart data drift. Tracked separately; not addressed in v1.6.

## Adding a new organism

When `organisms.py` gains a new entry:

1. Add a locus block to `expected.json` with `stable_facts` populated by hand (canonical, taxid, KEGG org code if any).
2. Run `--regenerate-baseline-all` (or `--loci <new>` first to isolate).
3. Review the captured `variable_facts`; sanity-check counts.
4. Commit.

Estimated effort: ~1 hour per organism.

## Out of scope (v1.7+ candidates)

- Continuous monitoring (cron, GH Actions weekly).
- MCP-server-layer dispatch testing.
- Cross-source consistency invariants (Ensembl xref UniProt-acc == UniProt primary).
- Annotation-quality scoring.
- KEGG happy-path coverage (need pathway-annotated loci per organism).
- Phytozome rice/soybean data drift investigation.
