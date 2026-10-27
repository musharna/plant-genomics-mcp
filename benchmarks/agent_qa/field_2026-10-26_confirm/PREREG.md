# Pre-registration: P3 CONFIRMATORY, held-out draws, 2026-10-26

Written before the key is built and before any run. User go: "go confirm"
(2026-10-26). This follows the exploratory P3 re-run of 2026-10-26
(`field_2026-10-24/rerun_1300/RESULTS.md`: 9/10 right vs web 0/10). That
run's questions were seen while the tools were built, so it could not
confirm.

## Hypothesis

On P3 questions drawn from ids never seen while building the tools,
Claude Sonnet 5.5 with `plant-genomics-mcp==1.30.0` is right on more
questions than Claude Sonnet 5.5 with web search.

## Key (held out)

- Builder: `field_2026-10-24/build_field.py`, the pilot's procedure
  unchanged. Retention rule, item count (25 per question), question text and
  answer format all as before. A rebuild with default flags reproduced the
  pilot key's 250 draws exactly (positive control, 2026-10-26).
- `--families P3 --seed 20261026 --id-prefix c`, so P3 draws
  `Random(20261029)` and the questions are `p3c_01`–`p3c_10`.
- `--exclude exclude_v4.txt` (254 ids), removed from the pool before the
  shuffle:
  - the 250 pilot P3 ids;
  - every `Zm00001d` id in `src/` and `tests/` on origin/main (08b7cee
    era), i.e. the ids used while building and testing the tools.
- Out of the repo: `~/plant-genomics-mcp-artifacts/agent_qa/field-2026-10-26-confirm/questions.json`.
  Its sha256 is committed in an addendum below before any run.
- Nobody (model or me) drives either tool on the new ids before the run.
  The key's builder reads MaizeGDB, Ensembl and UniProt directly and never
  imports the server.

## Arms

One invocation, `--arms web,mcp`, Sonnet 5.5 (`claude-sonnet-5-5`), 1 rep,
`--workers 2`:

- **web**: WebSearch + WebFetch, as in the pilot.
- **mcp**: `plant-genomics-mcp==1.30.0`, `--exclude-newer 2026-10-27T00:05:00Z`,
  `--mcp-timeout-ms 180000`.

Lane: `lane6_field_pilot.sh pgmcp-confirm`, results in
`~/plant-genomics-mcp-artifacts/agent_qa/field-2026-10-26-confirm/`.

## Scoring and test (fixed now)

- The pilot's P3 scoring and validity rule, unchanged (`score.py`
  `_score_id_batch`). A question is right only if all 25 items are right. An
  invalid run is not right. The `;`-inside-brackets slip seen in the
  exploratory run's p3_06 is scored wrong here too. The format is not changed
  after the fact.
- **Primary test:** the exact two-sided paired sign test (exact McNemar)
  over discordant questions, mcp-only right vs web-only right, at α = 0.05.
  **Confirmed** if mcp is right on more questions and p < 0.05. With
  n = 10 that needs, for example, 6:0 (p = 0.031) or 8:1 (p = 0.039); 7:1
  gives p = 0.070 and is NOT confirmed.
- Secondary, descriptive only: item accuracy, precision, coverage, v5-only
  accuracy, cost and seconds per arm.
- A tie or a loss is reported as one. Nothing is re-run to change a result.
  An API failure or rate-limit row is re-run once, and only that row, with
  the reason logged.

## Expectations (before the run)

- web: about 0/10 right. The pilot declined 10/10 (UNKNOWN, no tool call).
- mcp: at least 7/10 right; exploratory gave 9/10. The known risk is the
  format slip on items that split into two or more v5 genes. About 1 in 25
  items does (10/250 in the pilot).
- Cost: about $0.5 (mcp) + about $0.1 (web). The LAUNCH_STOP spend gate (160.23)
  is unchanged.

## Addendum: the key, built 2026-10-26 (before any run)

- `questions.json` sha256 `745ce97b29098ed75d56abb9453870614a167fc93baa57c1cb564fbd8e6caa6c`.
- 10 questions, `p3c_01`–`p3c_10`, 250 distinct ids, 0 in `exclude_v4.txt`
  (252 of whose ids were in MaizeGDB's table and left the pool).
- Screened 286. Kept 204 same-v5 and 46 unmappable in both. Discarded 18
  where the sources name different v5 genes and 18 where MaizeGDB maps but
  Ensembl has no record.
- 46 true negatives (18.4%). 5 items split into two or more v5 genes.
- UniProt 2026_03, MaizeGDB table sha256 7bd5ed26…, Ensembl 116 / EG 63, the
  same releases as the pilot key.
