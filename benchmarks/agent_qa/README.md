# Agent benchmark: plant gene lookups with and without the server

We asked Claude Opus 5.5, in Claude Code, 52 plant gene-lookup questions, 3
times each, under three setups: no tools, web search only, and this server
only. Web search got 151 of 156 answers right and the server 156 of 156. On
accuracy the two are not separated: counting questions right in all 3 runs,
the server led on 4 and web search on none (paired exact McNemar p = 0.125).
What separates them is the work: the server needed 2.7 fewer tool calls per
answer (95% CI 0.6 to 5.8), and its median answer was 3.3 s faster (95% CI 0.3
to 7.0). Median cost is not separated. The main limitation is that the answer
key comes from the databases the server reads, so this measures whether a
lookup is carried out correctly, not whether the databases are right.

A pre-registered re-run on 2026-10-18 asked 36 harder, multi-step questions:
both setups got all 108 answers right, a tie. The server was faster at the
median and cost more ([Re-run 2026-10-18](#re-run-2026-10-18-server-1292-from-pypi)).

![Right, wrong or declined per setup, with two no-model baselines](agent_qa.png)

## Results

| Setup | Right | Wrong | Declined | Invalid runs | Questions right in all 3 runs | Share right (95% CI) | Cost / answer, median (95% CI) | Seconds / answer, median (95% CI) | Tool calls / answer, mean (95% CI) |
|---|---|---|---|---|---|---|---|---|---|
| Baseline: one constant answer per kind | 3 | 49 | 0 | – | 3 of 52 (1 answer each) | 6% | – | – | – |
| Baseline: chromosome read from the gene ID (locations only) | 0 | 10 | 2 | – | 0 of 12 (1 answer each) | 0% | – | – | – |
| Claude alone | 36 | 3 | 117 | 0 | 12 of 52 | 23% (12–35) | $0.011 (0.010–0.011) | 5.5 (5.2–5.8) | 0 |
| Claude + web search | 151 | 4 | 1 | 14 | 48 of 52 | 96.8% (93.0–99.4) | $0.015 (0.013–0.026) | 13.4 (11.1–16.9) | 3.9 (1.9–7.0) |
| Claude + plant-genomics-mcp | 156 | 0 | 0 | 2 | 52 of 52 | 100% (100–100) | $0.023 (0.022–0.024) | 10.1 (8.5–13.0) | 1.25 (1.11–1.41) |

Each setup gave 156 answers. Intervals are bootstrap 95% intervals over
questions (2,000 resamples, each question with all 3 of its runs), because the
runs of one question are not independent. [`summary.tsv`](summary.tsv) and
[`stats.tsv`](stats.tsv) hold every number; [`plot.R`](plot.R) writes both from
[`results.jsonl`](results.jsonl) and [`baselines.jsonl`](baselines.jsonl).

- Web search missed 5 answers: 4 wrong locations and 1 declined protein
  length. Three of the locations read NCBI's gene model and are right against
  NCBI's record (see Limitations). In the fourth, Ensembl returned an error for
  a wheat gene, and the retry without a species reached another cultivar's
  assembly, about 100 Mb away. Per answer the server leads by 3.2 points (95%
  CI 0.6 to 7.1), an interval that excludes 0; accepting NCBI's model, the gap
  is 2 answers of 156.
- In 127 of 156 runs web search fetched the Ensembl, UniProt or NCBI
  E-utilities REST APIs through WebFetch (counted from the transcripts).
- The median server answer cost $0.0075 more (95% CI −$0.0028 to +$0.0100);
  its 56 tool descriptions go with every request. Totals were $4.06 for the
  server and $9.05 for web search, whose mean ($0.058) is far above its median
  ($0.015) because of a few long runs. We have no interval on the mean.
- Single-run maxima, not a tested difference: the slowest web-search run took
  705 s and 72 tool calls and ended by declining; the slowest server run took
  86 s.
- Against Claude alone, the server was right in all runs of 40 more questions
  (p = 1.8e-12) and web search of 36 more (p = 2.9e-11). Alone, Claude mostly
  declined: it knew 24 of 30 gene-name → locus answers and 9 of 30 UniProt
  accessions. Its 3 wrong answers are one question, answered F4HYE4 each time,
  a real but unreviewed entry where the reviewed one was asked for.

![Cost and time per answer, every run](agent_qa_cost_time.png)

![Right, wrong or declined by kind of question](agent_qa_by_category.png)

## Re-run 2026-10-18 (server 1.29.2 from PyPI)

Pre-registered in [`PREREG_2026-10-18.md`](PREREG_2026-10-18.md) (committed
de86d4f, before the sweep). Primary endpoint: questions right in all 3 runs,
server against web search. No server code changed, so this tests the same
1.29.2 release as above, not an unreleased commit.

**Why harder questions, and why no server change.** On the 10-16 questions
both setups were near the ceiling. [`diagnose.py`](diagnose.py) split every
10-16 run's tokens and dollars (prices fitted to each model's cost, exact):
the server sent no bare tool name that was refused (0 of 156), pasted no
payloads (median tool input 54 characters) and returned short outputs (median
385 characters). Its cost is its 56 tool definitions, about 27,300 tokens with
every API call (first-call context 28,015 tokens against web search's 2,069),
47% of its spend. Shortening them could change accuracy, the endpoint here,
so it was left for a separate test.

**Questions.** [`build_hard.py`](build_hard.py) drew 36 (seed 20261018) from
Ensembl 116 / Ensembl Genomes 63, UniProt 2026_03 and NCBI Gene over plain
HTTP, never importing the server, and froze them outside the repository
(sha256 in the pre-registration). Each names the database its key holds.

| Kind (6 each) | What it asks |
|---|---|
| ortholog_location | an Arabidopsis gene's one-to-one Ensembl Compara ortholog, then where it starts |
| ortholog_protein_length | the same ortholog, then its canonical protein length |
| symbol_ortholog | a gene name, to its locus, to its one-to-one ortholog |
| id_mapping | rice RAP-DB → MSU (UniProt); Arabidopsis AGI → NCBI Gene ID |
| position_to_gene | the only gene at a position of a named assembly |
| interval_count | protein-coding genes overlapping a 150–400 kb interval |

**Result: TIE.** Web search and the server were interleaved by question, 3
reps, one worker (16:12–17:34 EDT).

| Setup | Right | Wrong | Invalid | Questions right in all 3 | Cost / answer, median | Seconds / answer, median | Tool calls / answer, mean |
|---|---|---|---|---|---|---|---|
| Baseline: one constant answer per kind | 3 of 36 | 33 | – | 3 | – | – | – |
| Claude + web search | 108 | 0 | 0 | 36 of 36 | $0.021 | 20.0 | 2.22 |
| Claude + plant-genomics-mcp | 108 | 0 | 0 | 36 of 36 | $0.037 | 15.3 | 2.19 |

- Win rule: non-inferior (36 ≥ 36 − 2) but not better (0 discordant
  questions, McNemar p = 1; difference 0, 95% CI 0 to 0). Every kind was 6 of
  6 for both.
- The server's median answer cost 1.72× web search's (95% CI 1.46 to 2.24;
  dearer on 31 of 36 questions, sign test p = 1.3e-5) and took 0.77× the time
  (0.64 to 0.96; faster on 25 of 36, p = 0.029). Tool calls were even (−0.03
  per answer, −0.78 to 0.66). Totals: $5.79 server, $2.93 web search.
- Where the server's money went: its context was 28,050 tokens on the first
  call against 2,104, and its outputs were longer (median 1,090 characters,
  mean 4,887, against 431 and 899), most of them full Ensembl region rows. Web
  search fetched the Ensembl, UniProt or NCBI REST APIs itself in 105 of 108
  runs.
- Mechanism: the server's runs called `gramene_homologs` in 54,
  `ensembl_region_query` in 36, `resolve_locus_to_uniprot` in 27; no call was
  refused, 12 of 237 calls returned an error the model recovered from
  ([`rerun_2026-10-18/mechanism.tsv`](rerun_2026-10-18/mechanism.tsv)).
  Every ortholog answer it read from Gramene matched the Ensembl Compara key.
- Controls: oracle 36 of 36 right and 0 of 36 shifted before the sweep; 0
  invalid runs; leak scan 0 hits in 216 transcripts; 0 usage pauses and 0
  rate-limited runs. Two pilots (one 10-16 question, one new question, both
  setups, all right) are kept apart and not counted.
- Limitations: the harder set is still at the ceiling for both setups, so it
  cannot show an accuracy difference either; web search could reach every
  answer through the same public REST APIs.

Tables: [`rerun_2026-10-18/`](rerun_2026-10-18/) (`summary.tsv`, `stats.tsv`,
`by_kind.tsv`, `mechanism.tsv`), written by
[`analyze_rerun.py`](analyze_rerun.py). The sweep cost $8.71, plus $0.37 of
pilots.

## Haiku 5.5 re-run 2026-10-20

Pre-registered in
[`PREREG_2026-10-20_model-tiers.md`](PREREG_2026-10-20_model-tiers.md). The
question is whether the server lifts a weaker model. The 10-18 key and
scorer were used, with server 1.29.3 from PyPI and all three setups on
`claude-haiku-5-5`, interleaved, 3 reps. Sonnet 5.5 is the second tier and
has not run yet. The verdict across tiers (Holm over both) waits for it.

| Setup (Haiku 5.5) | Right of 108 | Questions right in all 3 | Cost / answer, median | Seconds, median | Tool calls, mean |
|---|---|---|---|---|---|
| Claude alone | 0 (108 declined) | 0 of 36 | $0.0003 | 5.7 | 0 |
| Claude + web search | 46 (1 wrong) | 9 of 36 | $0.026 | 40.1 | 7.1 |
| Claude + plant-genomics-mcp | 96 (12 declined) | 31 of 36 | $0.0017 | 19.0 | 3.6 |

- Primary endpoint: the server was right in all 3 runs on 24 questions where
  web search was not, and web search on 2 where the server was not. Exact
  McNemar p = 1.0e-5 (raw); the difference is 0.61 of the questions (95% CI
  0.42 to 0.81). Holm over the two tiers at most doubles this p.
- By kind, the server was right in 18 of 18 runs on each of the ortholog,
  position and interval kinds. Web search was right in 0 of 36 runs on the
  two ortholog kinds. Web search led on ID mapping (17 of 18 runs against 9
  of 18).
- Cost and time: the server's median answer cost 0.066× web search's (95% CI
  0.060 to 0.106) and took 0.47× the time (0.39 to 0.57).
- Mechanism: every server run called the server (3.6 calls per run; 0 runs
  without a call, 1 call refused by the client). The most-used tools were
  `ensembl_plants_lookup_locus` and `gramene_homologs` (51 runs each).
- Against Opus with web search on 10-18 (not run at the same time, labelled
  non-concurrent): Haiku with the server 31, Opus 36 of 36. Opus led on 5
  questions and Haiku on none (p = 0.0625).
- Controls: oracle 36 of 36 and shifted 0 before each start; 0 invalid of
  324; 0 leak hits; 0 API failures. Deviations are in the pre-registration:
  a server-start timeout raised for a loaded host, two usage stops (the
  second a spurious reading, now checked by a fresh probe), and Claude Code
  2.1.306 on the first 51 rows and 2.1.307 on the rest. The sweep cost $3.29.

Tables: [`tiers_2026-10-20/haiku/`](tiers_2026-10-20/haiku/).

## Controls

- Oracle (positive control). The key's own answers, written as replies, score
  52 of 52. The same answers moved to the next question of the same kind score
  0 of 52. A CI test checks this, and `run.py` checks it before every sweep.
- Baselines (negative controls), scored by the same scorer
  ([`baselines.py`](baselines.py)). One constant answer per kind of question
  gets 3 of 52, the 1-in-n hit of a constant ID in each of the three ID kinds.
  Reading the chromosome from the gene ID with the median start gets 0 of 12
  locations.
- Validity, one rule for every setup (`run.py`, `validity`). A run is invalid
  if it did not exit 0, wrote no result, started without exactly its setup's
  tools, had tools but called none, or touched the key. No run is retried; a
  timeout stays as an invalid row. 16 of 468 runs are invalid, all answering a
  gene-name → locus question from memory: 14 web-search runs (q25, q26, q28,
  q29, q30) and 2 server runs (q30). All 16 were right and stay in the table.
  On valid runs only, web search got 137 of 142 and the server 154 of 154; the
  paired test is unchanged (4 against 0, p = 0.125, over 48 questions).
- Leak scan. `run.py --audit` reads every tool input and output in all 468
  transcripts for the key and benchmark file names, the transcript
  directories, and this directory on GitHub. It found 0 hits, and no
  transcript mentions `musharna` or `plant-genomics-mcp`. The benchmark was
  first committed at 20:34 UTC, after the sweep (19:47–20:17 UTC).

## Limitations

- The answer key comes from the databases the server reads (Ensembl Plants 63
  and UniProt), so whether those databases are right is not tested.
- The questions do not say which annotation they mean. A location question
  names the assembly (for example "TAIR10"), not Ensembl, so an answer from
  NCBI's gene model on the same assembly is a fair reading that the primary
  key scores wrong. `summary.tsv` also scores locations against NCBI
  (`location_right_ncbi`, `location_right_either`). A rebuilt key should name
  Ensembl in the question.
- These are single lookups. Questions that chain several lookups, or that need
  judgment, are not covered.
- One model, one date. Other models, or later database releases, may differ.
- The sweep's dependency versions were not recorded (see Method).

## Method

- Claude alone: no tools.
- Claude + web search: WebSearch and WebFetch only. With no shell there is no
  `curl` and no scripts; it reaches REST APIs only by fetching URLs.
- Claude + plant-genomics-mcp: this server only, version 1.29.2 from PyPI,
  launched by `uvx`.

We ran it on 2026-10-16 with Claude Code 2.1.302. Each run is an isolated
`claude -p` with `--setting-sources ""`, an empty working directory,
`--strict-mcp-config` and the same short system prompt in every setup.
The sweep pinned the server but not its dependencies, and `uvx` did not record
them. The `--exclude-newer 2026-10-17T00:00:00Z` pin, added afterwards,
resolves the same 33 packages as the sweep's start did, so the sweep most
likely ran those; that is inferred, not observed. `run.py` refuses a new sweep
if Ensembl serves a release other than the key's (116 / Ensembl Genomes 63).

### Questions

[`questions.json`](questions.json) holds the 52 questions with their answers.
[`build_questions.py`](build_questions.py) drew them at random (seed 20261016)
from 12 plant genomes: Arabidopsis, rice, maize, wheat, tomato, soybean,
sorghum, barley, grapevine, poplar, Medicago and Brachypodium. Every question
names the ID system it expects (AGI, RAP-DB, B73 v5, ITAG).

| Kind | Count | Answer key |
|---|---|---|
| Gene location (chromosome and start) | 12 | Ensembl `/overlap/region` (secondary: NCBI Gene, 6 of 12) |
| Protein length (canonical transcript) | 12 | Ensembl `/lookup/id`, canonical translation |
| Gene name → locus ID | 10 | UniProt reviewed entries |
| Locus → reviewed UniProt accession | 10 | UniProt reviewed entries |
| Arabidopsis → rice, maize or tomato one-to-one ortholog | 8 | Ensembl Compara |

`build_questions.py` and [`ncbi_locations.py`](ncbi_locations.py) fetch the
answers over plain HTTP and never import `plant_genomics_mcp`, so a bug in the
server cannot also be in the key.

### Scoring

Each reply ends with `ANSWER: <value>` or `ANSWER: UNKNOWN`, and
[`score.py`](score.py) scores that line alone: right, wrong, declined
(`UNKNOWN`) or no answer (no `ANSWER:` line, which never happened). A location
is right if the chromosome matches and the coordinate is within 1 of either
end of the gene, since a minus-strand gene starts at its highest coordinate.
IDs are compared without case, version or transcript suffixes. Protein lengths
must match exactly. A wrong answer shaped like a real ID is flagged
`wrong_id`; the flag checks the shape only, not whether the ID exists.

Locations are also scored against NCBI's gene model (`verdict_ncbi`), for
the 6 of 12 genes where `ncbi_locations.py` finds the NCBI Gene record
mechanically (a record whose name or alias is the ID, else a single search hit). Against
NCBI the server is right 3 of 18 times, since it matches Ensembl and only
q01's two gene models agree; web search is right 6 of 18.
Tests: [`tests/test_agent_qa_score.py`](../../tests/test_agent_qa_score.py).

## History

- Pilot: the no-tools setup wrote fake tool calls as text, because Claude
  Code's own system prompt describes tools even when all are removed, so every
  setup now gets one short system prompt. The pilot was not archived.
- Barley key fix (#237, before publication): the key held only `5H`, so a
  correct `5:…` scored wrong. The bare number is now an alias where no other
  chromosome shares it; re-scoring turned 3 web-search runs from wrong to
  right.
- `wrong_id` fix (#237): the flag now reads the accession without its isoform
  suffix. Re-scoring changed nothing.
- Controls added (#239, 2026-10-17): the validity rule, leak scan, NCBI
  verdict, intervals and paired tests, baselines, oracle check and the
  `--exclude-newer` pin. Nothing was re-run; each is a re-score or audit of
  the same 468 transcripts, archived at
  `~/plant-genomics-mcp-artifacts/agent_qa_runs_2026-10-16`, and no primary
  verdict changed. The leak scan's first pattern also matched each run's own
  empty working directory, where Claude Code had saved 2 large WebFetch
  bodies; it now excludes that, and a test pins both cases.

## Reproduce

```bash
uv run python benchmarks/agent_qa/build_questions.py   # re-fetch the key (writes questions.json)
uv run python benchmarks/agent_qa/ncbi_locations.py    # add NCBI's coordinates to it
uv run python benchmarks/agent_qa/baselines.py         # no-model baselines -> baselines.jsonl
uv run python benchmarks/agent_qa/run.py --reps 3 --workers 5   # 468 claude -p runs; resumable
uv run python benchmarks/agent_qa/run.py --rescore     # re-score stored replies after a key fix
uv run python benchmarks/agent_qa/run.py --audit --runs DIR   # validity + leak scan from transcripts
Rscript benchmarks/agent_qa/plot.R                     # summary.tsv, stats.tsv, figures

# 2026-10-18 re-run (R=~/plant-genomics-mcp-artifacts/agent_qa/rerun-2026-10-18)
uv run python benchmarks/agent_qa/build_hard.py --out $R/questions.json
uv run python benchmarks/agent_qa/run.py --key $R/questions.json --arms web,mcp --reps 3 \
  --workers 1 --results $R/results.jsonl --runs $R/runs   # pauses on usage limits
uv run python benchmarks/agent_qa/analyze_rerun.py --key $R/questions.json \
  --results $R/results.jsonl --runs $R/runs --outdir benchmarks/agent_qa/rerun_2026-10-18
uv run python benchmarks/agent_qa/diagnose.py --runs $R/runs   # where tokens and dollars go
```

The full run cost about $15 in API usage. Raw transcripts go to
`~/plant-genomics-mcp-artifacts/agent_qa_runs` by default (`--runs`), outside
the repository.
