# Pre-registration amendment: field families P1 and P3, 2026-10-24

Written and committed before any run. Anything run after the pilot that
this file does not name is exploratory, and is labelled so.

## Why

On the 2026-10-18 key, web search and the server tied at the ceiling on every
tier: each question asked for one fact. These two families ask for a set, and
score abstention, as orthology (QfO, Orthobench) and database QA (LAB-Bench
DbQA) score their own tools. Design: `fieldbench_genomics_data_jobs.md`, rows
P1, P3 and P5 (mcp-bench-api state, outside the repository).

## Key

- `~/plant-genomics-mcp-artifacts/agent_qa/field-2026-10-24/questions.json`,
  sha256 `664b8ba3143ce0a743ca598953b2400c71c8ab87b5c3dcf48d045e5e63a916e4`,
  built 2026-10-25 00:07 UTC (2026-10-24 20:07 EDT) by
  [`build_field.py`](build_field.py), seed 20261024. A second build from the
  same code gave the same key in every field but `built`.
- Releases: Ensembl REST 116 / Ensembl Genomes 63 (plants.ensembl.org now
  redirects to the "release 63" archive, and REST serves 63), UniProt 2026_03
  (02 September 2026), and MaizeGDB's
  `Pan-genes/B73_gene_xref/B73v4_to_B73v5.tsv` (Last-Modified 02 Dec 2025,
  sha256 `7bd5ed26…924cee`, 38,356 v4 ids, 5,116 with no v5 gene).
  `run.py`'s preflight refuses a sweep if any of the three has moved.
- The builder never imports the server. Every answer comes over plain HTTP.

## Families (n = 10 questions each)

**P1, family roster** (`family_roster`, `p1_01`–`p1_10`). The question names an
Arabidopsis gene drawn uniformly from the genome and asks for every rice
(IRGSP-1.0) and maize (B73 NAM-5.0) member of its Ensembl Compara gene tree,
paralogs included, or NONE. The key is `/genetree/member/id`, pruned to the
three species. The query gene must be a leaf of its own tree.

- Retention: 10 draws, 10 kept, 0 discarded. A draw is discarded if its tree
  holds exactly 1 rice or maize gene (a single lookup would then be the whole
  answer), more than 40, or a tree already asked about.
- True negatives: 3 of 10, all trees with no rice or maize gene. They are
  kept at the rate the draws gave. A 40-draw pilot (seed 7) gave 14 of 40
  (35%), so the natural rate is above the design's "about 20%".
- The 7 positive rosters hold 3 to 20 genes (median 6, mean 9.9).

**P3, batch v4 → v5 + UniProt** (`id_batch`, `p3_01`–`p3_10`, 25 ids each). The
question lists 25 maize B73 RefGen_v4 ids, drawn uniformly from MaizeGDB's
table. For each id it asks for:

- the v5 gene or genes, per MaizeGDB's v4→v5 translation;
- every UniProtKB accession that UniProt's ID mapping service (Ensembl Genomes
  gene → UniProtKB) returns for those genes;
- or UNMAPPABLE, or UNKNOWN.

An id is kept only where the Ensembl Plants ID history (`/archive/id`) gives
the same answer as MaizeGDB, and every v5 gene is served by EG 63.

- Retention: 290 ids screened, 250 kept (216 mapped in both, 34 unmappable in
  both). 40 were discarded: 31 where the two sources name different v5 genes,
  and 9 where MaizeGDB maps but Ensembl has no record.
- True negatives: 34 of 250 items (13.6%), against MaizeGDB's own 13.3%.
  Every question holds 1 to 5.
- 10 items map to two or more v5 genes. 84 have two or more accessions.

## Scoring (fixed now, from the data's own structure)

The answer key is exact database state, so there is no measurement
tolerance. The scoring has no free parameter.

- P1: set F1 of the rice and maize gene ids in the ANSWER line against the
  tree. Ids are compared after case and `_T`/`_P` suffixes are dropped; NONE
  is the empty set, and two empty sets give F1 = 1. A question is **right only
  at F1 = 1**. Mean F1 is reported too.
- P3, LAB-Bench style. For each item:
  - attempted = mapped, or UNMAPPABLE;
  - declined = UNKNOWN, or the id left out;
  - right = the exact v5 set and the exact accession set, or UNMAPPABLE where
    the key has no v5 gene. An id given two different answers is wrong.

  The question is right only if all 25 items are right. Reported: item
  accuracy (right/total), precision (right/attempted) and coverage
  (attempted/total), plus v5-only item accuracy.
- An invalid run (the 10-18 validity rule, unchanged) is not right.

## Pilot (now)

- Web arm only (`--arms web`: WebSearch and WebFetch, as in every earlier
  sweep; web search is this repo's comparison arm by design, so it stays
  on).
- `--model claude-sonnet-5-5`, 1 rep, 20 runs.
- Expected cost: about $0.005 per tool call (10-20 Sonnet web rows). P1 needs
  1–3 REST fetches; P3 needs about 26 (25 ID-history GETs and 1 UniProt
  search), so roughly $2–4 in all.

```bash
A=~/plant-genomics-mcp-artifacts/agent_qa/field-2026-10-24
uv run python benchmarks/agent_qa/run.py --key $A/questions.json --model claude-sonnet-5-5 \
  --arms web --reps 1 --workers 2 --results $A/pilot/results.jsonl --runs $A/pilot/runs
uv run python benchmarks/agent_qa/run.py --key $A/questions.json --results $A/pilot/results.jsonl \
  --runs $A/pilot/runs --audit
uv run python benchmarks/agent_qa/field_2026-10-24/summarize.py $A/pilot/results.jsonl
```

**Headroom rule.** A family has headroom if the web arm is not right
(wrong, declined, no answer or invalid) on at least 3 of its 10 questions
(`summarize.py`). Only a family with headroom gets an mcp arm, within the
remaining pilot budget: `plant-genomics-mcp==1.29.3`,
`--exclude-newer 2026-10-17T01:00:00Z --mcp-timeout-ms 180000`, Sonnet, 1 rep.
A full run, if any, gets its own amendment first. A tie on a full run is an
acceptable result and is reported as one.

## Known before any run

- **The server cannot map v4 to v5.** Driven directly on 2026-10-24 (no
  model):
  - `ensembl_plants_lookup_locus` and `get_gene_xrefs` answer NotFound for
    v4 ids;
  - `resolve_locus_to_uniprot` finds a v4 id only where UniProt lists it as
    an ORF name;
  - no tool reads Ensembl's ID history or MaizeGDB.

  The feature was not added. On P3 the server is expected to lose, honestly.
- `gene_tree_members` covers P1.
- A 25-id P3 run may near the 900 s run timeout. A timeout stays in as an
  invalid row.

Every result is reported, whatever it is.
