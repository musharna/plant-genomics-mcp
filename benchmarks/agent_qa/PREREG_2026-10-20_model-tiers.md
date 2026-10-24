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
  the Haiku sweep, with the same design. *(Timing superseded by the amendments
  below: Sonnet runs when the coordinator releases it; the design is
  unchanged.)*

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
one hold of the sweep lock. *(Superseded by the two amendments below: each
tier runs as its own hold, Sonnet when the coordinator releases it, and Haiku
on the second lock.)*

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

## Deviation, 2026-10-21 00:17 EDT: usage stop

The Haiku sweep started 23:55 EDT (seven-day 0.88 on its first run) and
stopped at the registered gate, seven-day 0.90, after 51 of 324 runs (rep 1,
h01–h18: 18 bare, 17 web, 16 mcp). The rows are kept; `run.py --audit` finds
0 invalid and 0 leak hits. Not analysed: the tier is incomplete. The sweep
resumes after the weekly reset (Sun 10-25 12:00 EDT), on the coordinator's
word, with the same command; `run.py` skips finished (arm, question, rep)
triples. `versions.json` pins Claude Code 2.1.306: if it has updated by then,
`run.py` refuses to resume, and the choice (run 2.1.306, or accept and record
the new version) is made and written here before resuming.

## Deviation, 2026-10-21 (before any row after run 51): early resume, CLI version, leak rule

- **Resume now.** The coordinator's live probe at 18:12 EDT read seven-day
  0.57 (the weekly window rolls; `resetsAt` Sat 10-24 23:00 EDT) and
  five-hour 0.49, so the Haiku sweep resumes from run 52 on `sweep2.lock`
  instead of waiting for Sunday. Stop at seven-day ≥ 0.90, unchanged. Sonnet
  still waits for the coordinator.
- **Claude Code version.** The first 51 rows ran on 2.1.306; the current CLI is
  2.1.307. Per the coordinator's decision for all tracks, the resume accepts
  the current version rather than pinning the old one (`run.py
  --accept-cli-change`). Every row records its own `claude_code`, and
  `versions.json` counts rows per version (`claude_code_served`). The arms
  are interleaved by question, so a version change falls on every arm alike;
  rep 1 h01–h18 ran on 2.1.306, the rest on the version current when each run
  started. Every other `versions.json` field must still match, or `run.py`
  refuses.
- **Leak rule.** The coordinator's narrowed rule is primary for every row and
  arm: a leak-scan hit counts only if it is key or answer material (the key
  file, the benchmark's own files, its transcripts or results); a hit that is
  only a tool binary path (`which`, `command -v`, PATH) is not a leak. Every
  hit is classified by hand before the analysis. The as-written rule (any
  match of `run.LEAK`) is reported as secondary. Here no arm has a shell, and
  the first 51 rows have 0 hits under either rule.

## Deviation, 2026-10-21 22:31 EDT: second usage stop

The resumed sweep (from 20:20 EDT, seven-day 0.58) paused once for the
five-hour window (0.87 after web h14 rep 3, until 22:21 EDT, recorded in
`pauses.jsonl`). The first run after the pause read seven-day 0.98 (five-hour
0.01), and the gate stopped the sweep: 261 of 324 runs in, reps 1 and 2
complete for every question and arm, rep 3 through h16 (16 bare, 15 web, 14
mcp). Rows kept; `run.py --audit`: 0 invalid, 0 leak hits in 261 transcripts,
0 API failures. Claude Code 2.1.306 on 51 rows, 2.1.307 on 210. The tier is
incomplete and not analysed; it resumes with the same command when the
coordinator says so.

The CLI-version acceptance is set per resume, not baked in: `tiers_sweep.sh`
passes `--accept-cli-change` only when `ACCEPT_CLI_CHANGE=1` is set for that
run, so a later Claude Code update still stops a resume until the choice is
made and written here (review of ad66fcd). The 20:20 EDT resume ran with the
flag; no resume since.

## Deviation, 2026-10-21 (before the last 63 runs): probe-confirmed seven-day stop

The coordinator judged the 22:31 EDT stop spurious: the user's `/usage`
showed 66% weekly, and the 0.98 reading carried a `resetsAt` of Sun 12:00
where the account's window resets Sat 23:00. A probe from this harness at
resume time read seven-day 0.67 (`resetsAt` Sat 23:00 EDT). Gate change, for
the rest of this tier and for Sonnet: a run's seven-day reading at or above
0.90 triggers a fresh probe (`claude -p "say ok" --model claude-haiku-5-5
--output-format stream-json --verbose --setting-sources ""`, its newest
`unifiedWindows.seven_day`). The sweep stops only if the probe also reads at
or above 0.90, or gives no reading; otherwise both readings are logged to
`pauses.jsonl` (`kind: seven_day_probe`) and the sweep goes on. The
five-hour pause is unchanged. Test: `test_a_high_seven_day_reading_stops_only_if_a_fresh_probe_confirms_it`
(a 0.98 run with a 0.66 probe continues; with a 0.95 probe, or none, it
stops), seen to fail on the old gate. The resume runs the 63 missing runs
(rep 3: bare h17–h36, web h16–h36, mcp h15–h36) on `sweep2.lock`, Claude Code 2.1.307 as
before, so `ACCEPT_CLI_CHANGE` is not set.

## Amendment, 2026-10-23 ~19:20 EDT: API billing (before any further run)

- **Billing.** The user's decision: every remaining run in this design (both
  tiers) bills an Anthropic API key, not the Claude subscription. Same model
  IDs, prompts, arms, reps, harness and controls. Claude Code is 2.1.309 now
  (recorded per row).
- **How.** A benchmark-only `claude` wrapper, first on `PATH` for the sweep,
  loads the key and then `exec`s the real CLI (so the harness's timeout still
  kills Claude itself). It records each run's cost (Claude Code's
  `total_cost_usd`; for a killed run, estimated from its token counts) and
  refuses to start a run once the total reaches $171 of the user's $175 cap
  (exit 75, no transcript). A run whose `init` event does not show
  `apiKeySource: ANTHROPIC_API_KEY` stops every later launch.
- **Gate.** API calls report no subscription windows, so the seven-day stop
  and the five-hour pause no longer fire. `run.py` instead asks the spend gate
  (`BENCH_SPEND_GATE`) before every run and, on a refusal, stops cleanly as
  at the seven-day stop: finished rows stay, nothing new starts. Test added,
  seen to fail with the check disabled (`mutate-run`).
- **Set aside by exit code, not outcome.** A run the wrapper refuses (exit
  75) is a harness failure, not a result: it is set aside and re-run once.
- **Concurrency.** Up to 4 servers' sweeps run at once (2 before). The
  workers per sweep are unchanged.
