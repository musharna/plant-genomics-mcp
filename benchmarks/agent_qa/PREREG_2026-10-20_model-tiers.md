# Pre-registration: model tiers, 2026-10-20

Committed and pushed before either confirmatory sweep. Anything run after a
sweep is exploratory and is labelled so.

## Question

On Claude Opus 5.5 the server tied web search at the ceiling (36 of 36
questions right in all 3 runs each, 2026-10-18). Opus fetches the same REST
APIs itself through WebFetch. Does the server lift the accuracy of a weaker
model?

## Tiers

- **Haiku 5.5** (`--model claude-haiku-5-5`), then
- **Sonnet 5.5** (`--model claude-sonnet-5-5`), in this session, right after
  the Haiku sweep, with the same design.

No Opus row is re-run or reused in the primary test.

## Server, key, harness

- Server: `plant-genomics-mcp==1.29.3` from PyPI (the current release),
  launched by `uvx --exclude-newer 2026-10-17T01:00:00Z`. 1.29.3 differs from
  the 10-18 run's 1.29.2 only in its version string (`git diff v1.29.2 v1.29.3
  -- src`: one line of `__init__.py`). 1.29.3 was uploaded at 00:21 UTC on
  10-17, after the 10-18 cutoff of 00:00, so the cutoff moves one hour; at
  either cutoff `uv pip compile` resolves the same 32 dependencies.
- Key: the 10-18 key, unchanged:
  `~/plant-genomics-mcp-artifacts/agent_qa/rerun-2026-10-18/questions.json`,
  sha256 `f8a5e2b6b7b7782c2f46fc9bc3e40a6e7ff6fe0f83e00b930335d7c1437ea957`,
  36 questions, built on Ensembl 116 / Ensembl Genomes 63 and UniProt 2026_03.
- Is it still valid (checked 2026-10-20, 18:55 EDT): Ensembl REST serves 116 /
  EG 63, UniProt serves 2026_03 (02 September 2026), and the 3 NCBI Gene
  answers (h22–h24) are current, unreplaced records in E-utilities. Nothing
  the key depends on has moved, so it is not rebuilt. The oracle passed in
  `run.py`'s preflight: 36 of 36 key answers right, 0 of 36 shifted answers.
- Harness: `run.py` on branch `bench/model-tiers`, which adds `--model`,
  `--server`, `--exclude-newer`, `--stop-at` and `--mcp-timeout-ms` (defaults
  unchanged). Each row records the requested model and the model the API
  served (the init event's `model`); `versions.json` records the spec, the
  Claude Code version, the key's sha256 and `models_served`.
- `--mcp-timeout-ms 180000` in every arm (see Pilots): Claude Code waits up to
  180 s for the server to start. It does nothing in the bare and web arms.

## Arms, reps, order

- `bare`: no tools. `web`: WebSearch and WebFetch only. `mcp`: the server
  only. Same system prompt, answer format, isolation and validity rule as on
  10-18; each run gets its own `TMPDIR`.
- 3 reps, in rep, then question, then arm order (bare, web, mcp), so the arms
  are interleaved by question. `--workers 2` under
  `flock … sweep.lock env HEAVY_RUN_MEM=6G heavy-run`: the runs wait on the
  API and on HTTP, not on this host's CPU.
- Output, outside the repository:
  `~/plant-genomics-mcp-artifacts/agent_qa/tiers-2026-10-20/<tier>/`
  (`results.jsonl`, `versions.json`, `pauses.jsonl`, transcripts in `runs/`).

## Primary endpoint and decision

Per tier: questions right in all 3 runs, mcp against web, out of 36 (an
invalid run is not right). Exact two-sided McNemar test on the discordant
questions, and the bootstrap 95% CI (10,000 resamples of questions, seed
20261018) of (mcp − web) / 36, both as on 10-18 (`analyze_rerun.win_rule`).

The two tiers' McNemar p-values are Holm-adjusted together.

- **Lift** at a tier: Holm p < 0.05, more questions only mcp got right than
  only web, and the CI wholly above 0.
- **Loss** at a tier: Holm p < 0.05, more only-web questions, CI wholly
  below 0.
- **WIN** = a lift at ≥ 1 tier and no loss at the other. **LOSS** = a loss at
  either tier and no lift. Anything else is a **TIE** (including a lift at one
  tier with a loss at the other). Implemented as `tiers_verdict.verdict` and
  tested in `tests/test_agent_qa_rerun.py`. Until both tiers are in, the raw p
  is reported and the verdict is pending.

Secondary (reported, not deciding): right per run; the bare floor (mcp
against bare, same statistic); accuracy by kind; the mechanism readout (server
calls per run, runs per tool, mcp runs with 0 server calls, refused calls,
error results); cost, seconds and tool calls per answer; the 10-18 per-tier
win rule (non-inferiority margin 2).

**Substitute claim (secondary, non-concurrent):** each tier's mcp rows against
the 2026-10-18 Opus web rows (same key, statistic and CI), labelled
non-concurrent: different day, Claude Code version and host load.

Expectation, written down so it can be wrong: Haiku's web arm will lose
answers on interval counts and position-to-gene, where it must read long
Ensembl JSON through WebFetch's summarizer, and on the two-step ortholog
kinds; the server should hold most of them. Sonnet may sit near Opus.

## Controls

- Oracle and the Ensembl/UniProt release check, in `run.py`'s preflight
  before each sweep.
- Baselines: the no-model constant baseline (3 of 36), and the bare arm.
- Validity rule (10-18, unchanged): exit 0, a result, exactly the arm's tools,
  for mcp a connected server, at least one tool call in a tool arm, no leak.
  An mcp run whose server failed to connect is invalid and counts as not
  right; it is reported, not re-run.
- Leak scan: `run.py --audit` on every transcript after each sweep.
- Usage: `run.py` pauses until `resetsAt` whenever a transcript shows
  five-hour utilization ≥ 0.85 or a `rejected` status (each pause in
  `pauses.jsonl`), and stops at seven-day utilization ≥ 0.90 (`--stop-at
  0.90`, raised from 0.80 by the coordinator's amendment of 10-20 18:50 EDT).
  The gate is read after every run. Seven-day read 0.70 at 18:58 EDT. If the
  stop is reached, the rows are kept and reported, and the tier is incomplete.
- Harness failures: a row the API ended before the model answered
  (`api_failure`: a rate-limit rejection with no result, or terminal reason
  `api_error`) is moved aside with `--drop-api-failures` after the sweep and
  re-run once on resume; a second such row stays in, invalid. Timeouts are not
  re-run. An interrupted sweep is resumed, never restarted, and never stopped
  early on interim results.

## Analysis

```bash
R=~/plant-genomics-mcp-artifacts/agent_qa/tiers-2026-10-20
K=~/plant-genomics-mcp-artifacts/agent_qa/rerun-2026-10-18/questions.json
O=~/plant-genomics-mcp-artifacts/agent_qa/rerun-2026-10-18/results.jsonl
for t in haiku sonnet; do
  uv run python benchmarks/agent_qa/run.py --key $K --results $R/$t/results.jsonl --runs $R/$t/runs --audit
  uv run python benchmarks/agent_qa/analyze_rerun.py --key $K --results $R/$t/results.jsonl \
    --runs $R/$t/runs --outdir benchmarks/agent_qa/tiers_2026-10-20/$t \
    --reference-results $O --reference-label "Opus 2026-10-18"
done
uv run python benchmarks/agent_qa/tiers_verdict.py haiku=benchmarks/agent_qa/tiers_2026-10-20/haiku \
  sonnet=benchmarks/agent_qa/tiers_2026-10-20/sonnet
```

Every result is reported, whatever it is.

Both tiers run from [`tiers_sweep.sh`](tiers_sweep.sh), Haiku first, under
one hold of the sweep lock.

## Pilots (before this file)

Feasibility only; no result was used for a decision other than the harness
fix below. Kept apart in `tiers-2026-10-20/pilot-haiku*/`, not analysed.

- Pilot 1, Haiku, h01 and h31, all three arms, 1 rep: bare and web declined
  both (web made 8 and 6 calls, $0.029 and $0.013, 159 s and 48 s; bare
  $0.0003). Both mcp runs were invalid: the server's status at init was
  `failed`, so the model had no tools. Started by hand, `uvx` took 18–27 s to
  answer `initialize` at a load average near 78 on 22 cores (other sessions'
  work). (Correction 23:55 EDT: an earlier version of this line added "against
  2 s idle"; no idle start was measured.)
- Fix: `--mcp-timeout-ms 180000` (Claude Code's `MCP_TIMEOUT`). Pilot 2, mcp
  only, same two questions: the server connected in both, 1 and 3 server
  calls, $0.003 and $0.007, 133 s and 190 s.
- Usage at pilot 2 (19:20 EDT): five-hour 0.01, seven-day 0.71.

## Amendment, 2026-10-20 23:00 EDT (before either sweep started)

The coordinator reordered the shared sweep queue at seven-day 0.84 (22:50
EDT): every server's Haiku sweep runs before any server's Sonnet sweep. So
`tiers_sweep.sh haiku` runs now, under its own hold of the lock, and
`tiers_sweep.sh sonnet` runs later when the coordinator releases it, still
this week and still before the analysis. The queued both-tier sweep was
cancelled before it took the lock; no row of either tier existed. Design,
endpoint, rule and the 0.90 stop are unchanged. If the stop or the queue
means Sonnet does not run, the verdict stays pending and the Haiku result is
reported with its raw p.

## Amendment 2, 2026-10-20 23:55 EDT (before either sweep started)

The coordinator moved this sweep to a second lock, `sweep2.lock`, because
phylokit holds `sweep.lock` until about 04:00–05:00 EDT on CPU-bound tree
inference, and the lock guards disk and memory (179 GB disk, 28 GB RAM free
at 23:50 EDT). So the Haiku sweep runs concurrently with phylokit's sweep.
The arms are interleaved by question, so host load falls on all three arms
alike; it changes seconds per run, a secondary measure, and could slow the
server's start, which `--mcp-timeout-ms 180000` covers. `tiers_sweep.sh`
refuses to start with less than 5 GB of disk free. Nothing else changes.
