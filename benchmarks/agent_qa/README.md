# Agent benchmark: plant gene lookups with and without the server

Does Claude answer plant gene-lookup questions better with plant-genomics-mcp
than without it? 52 questions, asked 3 times each to Claude Opus 5.5 in
Claude Code under three setups:

- **Claude alone**: no tools.
- **Claude + web search**: WebSearch and WebFetch only.
- **Claude + plant-genomics-mcp**: this server (1.29.2 from PyPI) only.

Run on 2026-10-16 with Claude Code 2.1.302.

![Right, wrong or declined per setup](agent_qa.png)

| Setup | Right | Wrong | Declined | Questions right in all 3 runs | Cost / answer, median (mean) | Seconds / answer, median (mean) | Slowest |
|---|---|---|---|---|---|---|---|
| Claude alone | 36 | 3 | 117 | 12 of 52 | $0.011 ($0.011) | 5.5 (5.8) | 13 s |
| Claude + web search | 151 | 4 | 1 | 48 of 52 | $0.015 ($0.058) | 13.4 (40.0) | 705 s |
| Claude + plant-genomics-mcp | 156 | 0 | 0 | 52 of 52 | $0.023 ($0.026) | 10.1 (14.5) | 86 s |

Out of 156 answers per setup. The three runs of a question are not
independent, so the questions column is the fairer count. The numbers come
from [`summary.tsv`](summary.tsv), which `plot.R` writes from
[`results.jsonl`](results.jsonl).

## What it shows

- **Alone, Claude mostly declines rather than guesses.** It knew 24 of 30
  gene-name → locus answers (Arabidopsis and rice) and 9 of 30 UniProt
  accessions. It declined every location and ortholog question. Its 3 wrong
  answers are one question answered the same way in all three runs. It gave
  F4HYE4, a real but unreviewed UniProt entry for the gene, when the question
  asked for the reviewed one.
- **With web access, Claude mostly calls the same databases itself.** In 127
  of 156 runs it fetched `rest.ensembl.org`, `rest.uniprot.org` or NCBI
  E-utilities through WebFetch, and it got 48 of 52 questions right every
  time. Its 4 wrong answers were all gene locations, each read faithfully from
  a different annotation:
  - Three runs used NCBI's gene model, whose coordinates differ from
    Ensembl's.
  - In one run Ensembl returned an error for the wheat lookup. The retry
    without a species reached another wheat cultivar's assembly, where the
    same gene ID sits about 100 Mb away.

  It also declined one protein-length question.
- **With the server, every answer was right.** The typical answer took a
  little less time than with web search (median 10.1 s against 13.4 s) but
  cost more ($0.023 against $0.015). The server's 56 tool descriptions are
  sent with every request. Web runs had a long tail, which the server's did
  not: the slowest took 705 s and 72 calls, against 86 s. So the server's
  total cost for the 156 answers was $4.06, against $9.05 for web search.

![Cost and time per answer, every run](agent_qa_cost_time.png)

![Right, wrong or declined by kind of question](agent_qa_by_category.png)

## What it does not show

- **The answer key comes from the databases the server reads**: Ensembl
  Plants 63 (Ensembl 116) and UniProt. So the benchmark measures whether a lookup
  is carried out correctly. It does not measure whether the databases are
  right. An answer from another annotation (NCBI, Phytozome, MSU for rice)
  counts as wrong even where that annotation is defensible.
- **These are single lookups.** Questions that chain several lookups, or
  that need judgement, are not covered.
- **One model, one date.** Results for other models, or for later versions of
  the databases, may differ.

## Questions

[`questions.json`](questions.json) holds the 52 questions with their answers.
`build_questions.py` drew them at random (seed 20261016) from 12 plant
genomes: Arabidopsis, rice, maize, wheat, tomato, soybean, sorghum, barley,
grapevine, poplar, Medicago and Brachypodium. Every question names the ID
system it expects (AGI, RAP-DB, B73 v5, ITAG).

| Kind | Count | Answer key |
|---|---|---|
| Gene location (chromosome and start) | 12 | Ensembl `/overlap/region` |
| Protein length (canonical transcript) | 12 | Ensembl `/lookup/id`, canonical translation |
| Gene name → locus ID | 10 | UniProt reviewed entries |
| Locus → reviewed UniProt accession | 10 | UniProt reviewed entries |
| Arabidopsis → rice, maize or tomato one-to-one ortholog | 8 | Ensembl Compara |

`build_questions.py` fetches the answers over plain HTTP and never imports
`plant_genomics_mcp`, so a bug in the server cannot also be in the key.

## Scoring

Each reply ends with `ANSWER: <value>` or `ANSWER: UNKNOWN`. `score.py`
decides the verdict from that line alone:

- **right**: the value matches the key.
- **wrong**: the value does not match.
- **declined**: `UNKNOWN`.
- **no answer**: there is no `ANSWER:` line. This never happened in this run.

Matching rules by kind:
- **Locations** are right if the chromosome name matches and the coordinate
  is within 1 of either end of the gene, since on the minus strand the gene's
  5′ end is its highest coordinate.
- **IDs** are compared without case, version suffixes or transcript suffixes.
- **Protein lengths** must match exactly.

A wrong answer shaped like a real ID is also flagged `wrong_id`. The flag
checks the shape only, not whether the ID exists.
Tests: `tests/test_agent_qa_score.py`.

## Reproduce

```bash
uv run python benchmarks/agent_qa/build_questions.py        # re-fetch the key (writes questions.json)
uv run python benchmarks/agent_qa/run.py --reps 3 --workers 5   # 468 claude -p runs; resumable
uv run python benchmarks/agent_qa/run.py --rescore          # re-score stored replies after a key fix
Rscript benchmarks/agent_qa/plot.R                          # summary.tsv + figures
```

Each run is an isolated `claude -p`:
- `--setting-sources ""` and an empty working directory.
- `--strict-mcp-config`, so no other MCP servers.
- The same short system prompt in every setup.

The full run cost about $15 in API usage. Raw transcripts go to `runs/`,
which is gitignored.
