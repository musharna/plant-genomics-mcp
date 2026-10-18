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
```

The full run cost about $15 in API usage. Raw transcripts go to
`~/plant-genomics-mcp-artifacts/agent_qa_runs` by default (`--runs`), outside
the repository.
