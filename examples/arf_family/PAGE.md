# One family, 64 calls: a worked run of plant-genomics-mcp

`plant-genomics-mcp` publishes 55 MCP tools over free, public
plant-genomics backends. This page is what happened when 16 of them were
run end to end over a whole gene family — 23 _Arabidopsis thaliana_
members found through the tools alone, and the 25 _Oryza sativa_ and 66
_Triticum aestivum_ members the tools could name and verify — in a
single sitting: every response captured to [`raw/`](raw), and every
place an answer came back unusable, inconsistent or silently short
written down as it was found. It is a showcase and a defect list at the
same time. The recipes below are what the tools do well; the 20 open rows
under **Known gaps** are what they do not, and the 38 closed ones are what
the fix passes between the runs did about it.

Provenance: run 2026-09-26 against server version `1.26.0` at commit
`75576d0`, the release itself — 64 MCP calls over 114 genes × 16 tools,
one call per organism per 50 loci: 8 tools through their own `batch_`
form and 8 through `batch_locus_call`. 49 calls ok, 7 expected refusals,
8 calls carrying 62 locus-level errors, 9 minutes wall. 31 of the errors
are loci outside ATTED-II's co-expression releases; 22 are STRING: 18
wheat proteins it does not carry, and 2 rice and 2 wheat genes with no
partners; 8 are KEGG; 1 is AraGWAS.
OrthoDB, PANTHER and
`gene_report` answer for all 114 genes; on the 2026-09-23 run they lost 87 answers to
OrthoDB refusing the batch's eight-wide fan-out as "too high request
rate", 63 to PANTHER timeouts in an upstream outage, and 18 reports to
an Ensembl failure that emptied them (the closed rows
`batch-fanout-rate-limit` and `failed-step-empties-report` below), and
STRING answered none of the 66 wheat genes (`wheat-string-unresolvable`).
The earlier runs: 2026-09-26 at `6cf5408` (`1.25.0`), 64 calls with 62
locus-level errors; 2026-09-25 at `55dc6ca` (`1.24.0`), 64 calls with 62; 2026-09-25 at `9054886` (`1.23.0`), 64 calls with 63; 2026-09-25 at `2fc3f92`, 64 calls with 62; 2026-09-23 at `052e4ec`, 64 calls with 262; 2026-09-22 at `967bc36`, 400 calls over 48 genes; 2026-09-21
against `1.21.0` as released, 248 calls over 29 genes. The
rows below that the chain does not answer were asked again by
[`probe_gaps.py`](probe_gaps.py) ([`raw/_probe_rerun.json`](raw/_probe_rerun.json)).
The family itself came from a separate
1163-call, 25-minute enumeration on 2026-09-23, not repeated for this run ([`enumerate_family.py`](enumerate_family.py),
[`enumeration_calls.jsonl`](enumeration_calls.jsonl)): the three
starting loci in [`genes.tsv`](genes.tsv), then every candidate the
tools could reach, each decided by `interpro_domains` and recorded in
[`family_candidates.tsv`](family_candidates.tsv) whether kept or not —
577 candidates, none undecided (58 of 560 on the 2026-09-22 run), so the
23 Arabidopsis members are all of those the walk reached, and they are
the 23 that `entry_members` returns for the ARF domain IPR010525 in one
call. Wheat went from 0 to 66 once IWGSC loci resolved (71 queried;
50 more wheat candidates were ids of another namespace and never
queried).
Chain [`chain.py`](chain.py), per-call log [`calls.jsonl`](calls.jsonl),
captured responses [`raw/`](raw).

## Install and first query

Copied from the repository [`README.md`](../../README.md):

```bash
# Zero-install — uv fetches and runs it on demand
claude mcp add plant-genomics --scope local -- uvx plant-genomics-mcp
```

then ask the first recipe's prompt below, or call
`ensembl_plants_lookup_locus` with:

```jsonc
// arguments
{ "locus": "AT1G01010" }

// result (truncated)
{
  "id": "AT1G01010",
  "organism": "arabidopsis_thaliana",
  "display_name": "NAC001",
  "biotype": "protein_coding",
  "seq_region_name": "1",
  "start": 3631,
  "end": 5899,
  "strand": 1,
  "assembly_name": "TAIR10",
  "description": "NAC domain containing protein 1 ..."
}
```

## Three recipes

Each one starts from what you actually have — a locus — and shows the
prompt you type to an MCP-connected assistant, the tools it maps to, and
real captured output. Every block below is **trimmed** from a file in
[`raw/`](raw): whole keys and list entries are dropped, nothing is
reworded.

### I have a locus, I want the protein and its domain architecture

> **"What protein does the Arabidopsis locus AT1G19850 make, and what
> domains does it have?"**

`ensembl_plants_lookup_locus` → `resolve_locus_to_uniprot` →
`interpro_domains`. Trimmed from
`raw/AT1G19850__resolve_locus_to_uniprot.json`, 5 of its 12 fields:

```json
{
  "primaryAccession": "P93024",
  "uniProtkbId": "ARFE_ARATH",
  "recommendedName": "Auxin response factor 5",
  "sequenceLength": 902,
  "upstream_version": "2026_03"
}
```

Trimmed from `raw/AT1G19850__interpro_domains.json`, 2 of its 19 domains
and 5 of the 7 keys on each:

```json
{
  "accession": "P93024",
  "domain_count": 19,
  "truncated": false,
  "domains": [
    {
      "accession": "IPR003340",
      "name": "B3 DNA binding domain",
      "type": "domain",
      "source_database": "interpro",
      "locations": [{ "start": 157, "end": 260 }]
    },
    {
      "accession": "IPR010525",
      "name": "Auxin response factor domain",
      "type": "domain",
      "source_database": "interpro",
      "locations": [{ "start": 307, "end": 389 }]
    }
  ]
}
```

The name `Auxin response factor 5` is `resolve_locus_to_uniprot`'s
`recommendedName` for accession `P93024`; the domain names and residue
spans are `interpro_domains`' own entries. Both responses carry an
upstream release id — `2026_03` and `110.0` — which most of the chain
does not (see the figure).

### I have a locus, I want its GO annotations

> **"List the GO annotations for AT1G19850, grouped by aspect."**

`locus_go_annotations` does locus → UniProt → QuickGO in one call.
Trimmed from `raw/AT1G19850__locus_go_annotations.json`: the `by_aspect`
rollup's `molecular_function` in full, with the 50-entry `annotations`
list, `next_cursor` and the other two aspects dropped.

```json
{
  "uniprot_accession": "P93024",
  "numberOfHits": 51,
  "total": 51,
  "returned": 50,
  "truncated": true,
  "by_aspect_deduped_on": "goId",
  "by_aspect": {
    "molecular_function": [
      {
        "goId": "GO:0000976",
        "goName": "transcription cis-regulatory region binding"
      },
      { "goId": "GO:0003677", "goName": "DNA binding" },
      {
        "goId": "GO:0003700",
        "goName": "DNA-binding transcription factor activity"
      },
      { "goId": "GO:0005515", "goName": "protein binding" },
      { "goId": "GO:0042802", "goName": "identical protein binding" }
    ]
  }
}
```

Read the counts before the terms: `total` is 51 and `returned` is 50,
so one annotation did not come back, and `truncated` says so; the
`next_cursor` dropped from the trim above fetches it. On the first run
this tool had no `truncated` field and no cursor — the `silent-truncation`
and `no-pagination` rows below, both closed — and `by_aspect_deduped_on`
now says in the payload that the rollup is deduplicated.

### I have a locus, I want one dossier — and this is where it disappoints

> **"Give me everything you have on AT1G19850 in one go."**

`gene_report` composes eight tools and renders a Markdown report. It
works, and it is the weakest answer on this page. Trimmed from
`raw/AT1G19850__gene_report.json`: 3 of the 7 `result` keys, `steps` cut
to 2 of 8, and `markdown` cut to its first 3 non-blank lines of 5,568
characters.

```json
{
  "elapsed_s": 28.049930927998503,
  "steps": [
    {
      "tool": "ensembl_plants_lookup_locus",
      "elapsed_s": 18.878128401993308,
      "result": null,
      "error": null
    },
    {
      "tool": "resolve_locus_to_uniprot",
      "elapsed_s": 0.0013246830058051273,
      "result": null,
      "error": null
    }
  ],
  "result": {
    "canonical_gene_name": "MP",
    "gene_names": { "canonical": "MP", "ensembl": "MP", "uniprot": ["ARF5"] },
    "markdown": "# MP (UniProt: ARF5) — `AT1G19850`\n\n*Arabidopsis thaliana* · protein_coding · 1:6,886,669–6,891,404 (+) · TAIR10\n\nTranscriptional factor B3 family protein / auxin-responsive factor AUX/IAA-like protein [Source:NCBI gene (formerly Entrezgene);Acc:838573]"
  }
}
```

Three things were wrong with this answer on the first run, and the trim
above shows what the fix pass (#122, closed at `967bc36`) did to each.
The report was titled `MP`, taken from Ensembl's `display_name`, while
its own protein section — from `resolve_locus_to_uniprot`, the same call
as in the first recipe — read `Auxin response factor 5`: one report, two
names, neither labelled by source; it now carries `gene_names` with each
name by source, and the title shows both when they differ.
`steps[].elapsed_s` was `null` for all 8 steps on all 29 genes; it is a
number on all 912 steps in this run, none of them skipped. And every
sub-tool payload was carried twice — under `steps[].result` and again
under `result.sections` — so 6 of 29 responses were over 200 kB;
`steps[].result` is now `null` and `steps` is the audit trail. It is
still the weakest answer here for parsing: use `gene_report` for the
rendered `result.markdown`; go to the individual tools for anything you
intend to parse. Its literature step answered (`hitCount` 91); on the
first run it was empty (150 B, `hitCount` 0) for the same gene, with
nothing in the answer to say the source had not responded — the
`silent-empty-result` row below, now closed. The 2026-09-23 run found a
weakness: where the first step failed (18 genes, in an Ensembl outage),
the answer was ok with an empty `result`, not the degraded dossier the
description promises; since #154 an Ensembl failure alone leaves the
rest of the report rendered — `failed-step-empties-report` below, closed.

Sizes are bytes of the JSON-RPC response line, which carries each payload
as both text and `structuredContent`; the files in `raw/` are the parsed
payload, re-indented. A batch call's line carries every locus in it —
2,251,688 B for the 23 Arabidopsis `gene_report`s — so one gene's size is
its parsed payload: 66,037 B by `json.dumps` for this gene, of which
`result.sections` is 58,811 B, read back from
`raw/AT1G19850__gene_report.json`.

## What 64 responses cost, and what they say about their source

![Response size on the wire for each of the 64 MCP calls on a log10 axis, one row per chain tool and the batch form that ran it, ordered by median size, offset and shaped by organism with exact ties drawn on one point, coloured by whether the tool reports an upstream release under upstream_version or not at all; hollow points are documented organism refusals; a dashed line marks the 200 kB oversize threshold, crossed by 22 calls of 7 tools](coverage.png)

Response sizes on the wire span 11,070×, from 318 B
(`batch_atted_coexpression` refusing the wheat loci) to 3,520 kB
(`batch_locus_call` running `aragwas_associations` over the 23
Arabidopsis loci). Every chain tool is called through a batch form, one
call per organism per 50 loci — four calls, since wheat's 66 loci take
two — and exact ties draw on one point: 3 positions for
`batch_atted_coexpression` and `batch_kegg_pathways`, 4 for the other 14
(each of the two has a pair of wheat refusals that tie). The
vertical offset is by organism. Six of the 16 tools called put the upstream release in
`upstream_version`: `interpro_domains`, `resolve_locus_to_uniprot`,
`gene_report`, and — since the fix pass (#121) — `gramene_homologs`,
`atted_coexpression` and `alphafold_structure`, which on the first run
reported it under `release`, `atted_release` and `latest_version`. The
remaining 10 carry the key as `null`, each saying in its description
that its backend states no release on its responses. A batch answer
carries up to 50 payloads, so its size grows with the loci in it: 22
calls of 7 tools are over the runner's 200 kB oversize threshold, all 4
of `gene_report`, `batch_gramene_homologs`,
`batch_locus_go_annotations` and `orthodb_orthologs` among them. The hollow points are the
refusals the descriptions say the tools will make:
`aragwas_associations` on rice and wheat, `atted_coexpression` and
`kegg_pathways` on wheat. Per-tool table: [`coverage.tsv`](coverage.tsv).

Every row below is one line of [`gaps.jsonl`](gaps.jsonl), rendered by
[`render_gaps.py`](render_gaps.py) so the page cannot drift from the log.
The hand-logged rows were written against the first run (29 genes,
`1.21.0` as released) or, the last three, against the 2026-09-23 run,
and read again against each later run; those this run no
longer reproduces are listed last, with what it returned instead.
Drafted issue text, grouped by theme, is in [`issues/`](issues): drafts
01–19 were filed as #121–#141, and each says which of its rows are
closed; 20–22, from the 2026-09-23 run's new rows, were filed as
#153–#155 and closed at `2fc3f92`; 09 and 12 (#133, #136) lost their
last open rows at this run.

## Known gaps

All 20 open rows, one line each: what was attempted, what came back, what was expected instead. The 38 rows logged against an earlier run that a later run no longer reproduces are listed last, with what it returned instead. Full text and the raw response each row was read from are in [`gaps.jsonl`](gaps.jsonl) and [`gaps_auto.jsonl`](gaps_auto.jsonl).

### Defects in this tool

- **several tools** (symbol-rejected-elsewhere) — all four refuse ARF1 now, for two different reasons: ensembl_plants_lookup_locus ('[NotFoundError] Ensembl Plants… — expected: one behaviour for a symbol in a `locus` argument across tools that share the argument name
- **several tools** (provenance-null-rate) — 42 of 64 calls carry no upstream_version (66%; 250 of 400, 62%, at 967bc36; 188 of 248, 76%, on the first run). The… — expected: every response carries the upstream release it came from
- **`interpro_domains`** (oversize) — 98 loci: over 200 kB on the wire (batch), largest 466038 bytes — expected: a usable answer within 200 kB
- **`orthodb_orthologs`** (oversize) — 114 loci: over 200 kB on the wire (batch), largest 1132998 bytes — expected: a usable answer within 200 kB
- **`gramene_homologs`** (oversize) — 114 loci: over 200 kB on the wire (batch), largest 1121720 bytes — expected: a usable answer within 200 kB
- **`atted_coexpression`** (error) — 8 loci: [NotFoundError] ATTED-II: <locus> is not in the Ath-u.N-N co-expression release (no neighbour ranking exists… — expected: a usable answer
- **`aragwas_associations`** (error) — ATMG00940: [UpstreamUnavailableError] AraGWAS associations exhausted N retries (last HTTP N) — expected: a usable answer
- **`aragwas_associations`** (oversize) — 23 loci: over 200 kB on the wire (batch), largest 3520158 bytes — expected: a usable answer within 200 kB
- **`locus_go_annotations`** (oversize) — 114 loci: over 200 kB on the wire (batch), largest 657583 bytes — expected: a usable answer within 200 kB
- **`kegg_pathways`** (error) — ATMG00940: [NotFoundError] KEGG: no gene record for <locus> (queried as ath:<locus>); /list/ath:<locus> is empty — expected: a usable answer
- **`locus_literature`** (oversize) — 48 loci: over 200 kB on the wire (batch), largest 869175 bytes — expected: a usable answer within 200 kB
- **`gene_report`** (oversize) — 114 loci: over 200 kB on the wire (batch), largest 2251688 bytes — expected: a usable answer within 200 kB
- **`atted_coexpression`** (error) — 23 loci: [NotFoundError] ATTED-II: <locus> is not in the Osa-u.N-N co-expression release (no neighbour ranking exists… — expected: a usable answer
- **`string_interactions`** (error) — 2 loci: [NotFoundError] STRING: no interaction partners for <locus> (queried as <locus>) — expected: a usable answer
- **`kegg_pathways`** (error) — 7 loci: [NotFoundError] KEGG bridge (Ensembl Plants /xrefs): [NotFoundError] KEGG: no Entrez Gene ID for <locus>… — expected: a usable answer
- **`string_interactions`** (error) — 18 loci: [NotFoundError] STRING has no protein for <locus> in triticum_aestivum (queried as N): STRING… — expected: a usable answer
- **`string_interactions`** (error) — 2 loci: [NotFoundError] STRING: no interaction partners for <locus> (queried as N) — expected: a usable answer

### Upstream values passed through unchanged

- **`ensembl_plants_lookup_locus`** (free-text-not-a-label) — AT1G19850: 'Transcriptional factor B3 family protein / auxin-responsive factor AUX/IAA-like protein [Source:NCBI gene… — expected: a structured family field; the free text cannot be matched or grepped consistently even within one family
- **`atted_coexpression`** (enrichment-ask) — each of the 25 neighbours is {locus, entrez_gene_id, z_score} - no symbol, no description. The shape is documented… — expected: a symbol per neighbour, as string_interactions does
- **several tools** (free-text-null-outside-arabidopsis) — ensembl_region_query 1:1-1000000 on oryza_sativa returns 165 protein-coding genes with description on 1; 1A:1-1000000… — expected: a structured family or domain field per gene, the same in every organism

### Closed by a later run

- **`resolve_locus_to_uniprot`** (shared-symbol) — was: ok=true and one scalar answer: primaryAccession Q8L7G0 / uniProtkbId ARFA_ARATH / recommendedName 'Auxin response… — now, at `052e4ec`: resolve_locus_to_uniprot('ARF1') is an isError refusal naming the loci: "[InvalidArguments] 'ARF1' is not a locus id…
- **`string_interactions`** (argument-name) — was: the call works, but string_interactions is the only one of the chain's 16 tools whose locus argument is not called… — now, at `052e4ec`: string_interactions requires `locus` and batch_string_interactions `loci`, the names every other chain tool uses (live…
- **several tools** (no-family-enumeration) — was: no tool on the live server takes a family or entry accession as input. Of 50 tools, every input is a locus, a list of… — now, at `052e4ec`: entry_members takes an InterPro, Pfam or PANTHER accession: entry_members('IPR010525', arabidopsis_thaliana) answers…
- **several tools** (no-batch-form) — was: 8 of the 16 chain tools have no batch\_ form on the live server: interpro_domains, alphafold_structure… — now, at `052e4ec`: every chain tool went through a batch form: 64 calls for 114 genes in three organisms, one per organism per 50 loci…
- **several tools** (version-under-another-key) — was: three tools do report a release but not as `upstream_version`, so a single-key pass reads them as null… — now, at `967bc36`: atted_coexpression, gramene_homologs and alphafold_structure carry the release as upstream_version ('Ath-u.c4-0'…
- **`gene_report`** (payload-duplicated) — was: every sub-tool payload appears twice in one envelope: for AT1G19850 the eight steps[].result payloads total 36,021 B… — now, at `967bc36`: steps[].result is null on all 384 steps of the 48 genes; each sub-tool payload appears once, under result.sections…
- **`gene_report`** (null-field) — was: steps[].elapsed_s is null for all 8 steps of all 29 genes (232/232), while the envelope's own top-level elapsed_s is… — now, at `967bc36`: steps[].elapsed_s is a number on all 384 steps of the 48 genes (0/384 null); 3.36 s for the ensembl step and 0.002 s…
- **`gene_report`** (inconsistent-name) — was: result.canonical_gene_name is 'MP' for AT1G19850 (taken from Ensembl display_name) and the markdown title reads '# MP… — now, at `967bc36`: result.gene_names labels each name by source ({'canonical': 'MP', 'ensembl': 'MP', 'uniprot': ['ARF5']} for AT1G19850)…
- **`gene_report`** (duplicate-rendering) — was: 23 GO bullets, 20 distinct lines, 14 distinct GO ids. '- [GO:0005515] protein binding (IPI)' appears 3 times… — now, at `967bc36`: AT1G19850 renders 21 GO bullets, 21 distinct; 0 repeated bullets across the 48 genes
- **several tools** (silent-truncation) — was: locus_go_annotations reports numberOfHits 51 and returned 50, and the annotations list holds 50. There is no… — now, at `052e4ec`: locus_go_annotations carries truncated: AT1G19850 answers total 51, returned 50, truncated true and a next_cursor for…
- **`locus_go_annotations`** (dedup-not-in-payload) — was: by_aspect holds 16 entries (biological_process 10, molecular_function 5, cellular_component 1) beside 50 annotations… — now, at `052e4ec`: the payload says so itself: by_aspect_deduped_on 'goId' beside by_aspect on every locus_go_annotations answer
- **several tools** (count-field-names) — was: nine different field names for the upstream total across ten tools - domain_count (interpro_domains), structure_count… — now, at `052e4ec`: every list-returning chain tool carries total / returned / truncated beside its own count field: interpro_domains 19 /…
- **several tools** (no-pagination) — was: orthodb_orthologs returns 100 of member_count 1986; gramene_homologs 100 of total 177; aragwas_associations 100 of… — now, at `052e4ec`: a capped list carries next_cursor, and passing it back returns the rest: gramene_homologs AT1G19850 answers 100 of 177…
- **`atted_coexpression`** (locus-case) — was: 396 of the 400 neighbour loci across the 16 genes ATTED-II answers for come back mixed-case ('At2g44830', 'At2g21050'… — now, at `052e4ec`: the Arabidopsis neighbours come back upper case (0 of the 375 across the 15 Arabidopsis genes ATTED-II answers for are…
- **`string_interactions`** (misleading-field-name) — was: `accession` is byte-identical to `string_id` on every partner ('3702.P93830', '3702.Q38830') - a STRING internal id… — now, at `55dc6ca`: string_interactions partners no longer carry `accession` (v1.24.0, removed after the 1.22.0 deprecation): none of the…
- **`aragwas_associations`** (normalise-upstream-repr) — was: study.name is "('As75_raw_Full imputed genotype_amm',)" - a Python tuple repr, parentheses, quotes and trailing comma… — now, at `052e4ec`: study.name is 'As75_raw_Full imputed genotype_amm', the tuple repr unwrapped
- **`aragwas_associations`** (thresholds-undocumented) — was: score is 33.078925771081366 on the top association, beside maf 0.0058 and mac 2. The tool description does name it… — now, at `052e4ec`: the description gives score as -log10 p, and each association's study carries study.thresholds on that scale, which…
- **`locus_literature`** (promised-field-always-null) — was: journalTitle is a string on 350 of the 351 hits across the 114 genes (null on all 160 on the first run), so the… — now, at `9054886`: locus_literature hits carry one type per kind of value (#134): across the 358 hits for the 114 genes pubYear is an int…
- **`ensembl_plants_lookup_locus`** (normalise-upstream-id) — was: 'AT1G19850.1.' - a trailing period, on all 29 genes in both organisms ('AT1G59750.1.', 'Os01t0236300-01.'). This is… — now, at `052e4ec`: canonical_transcript has no trailing period on any of the 114 genes ('AT1G19850.1', the spelling aragwas_associations…
- **`experimental_structures`** (count-counts-rows) — was: AT1G19850: structure_count 10, structures list 10 rows, 3 distinct pdb_ids (4chk, 4ldu, 6l5k) - 4chk appears as chain… — now, at `052e4ec`: experimental_structures carries entry_count beside structure_count: AT1G19850 answers structure_count 10 and…
- **`alphafold_structure`** (undefined-bands) — was: four fractions named very_low / low / confident / very_high (0.516 / 0.027 / 0.120 / 0.338 for AT1G19850) beside… — now, at `052e4ec`: plddt_band_ranges gives the cutoffs beside the fractions: very_low [0, 50], low [50, 70], confident [70, 90]…
- **`gramene_homologs`** (identifier-with-no-tool) — was: each homolog is {target_locus, type, gene_tree_id} - e.g. {'C5167_014531', 'ortholog_one2many'… — now, at `9054886`: gene_tree_members takes the gene_tree_id gramene_homologs hands back (#130): EPlGT00940000167082 lists 187 members…
- **several tools** (browser-needed-assets) — was: the only pointers to a structure, a PAE plot or a motif logo are URLs the tools cannot dereference: cif_url / pdb_url… — now, at `55dc6ca`: every tool description that offers a URL, logo or image now says it is a link no tool fetches: alphafold_structure…
- **`gramene_homologs`** (paralog-closure-empty) — was: total 0 and an empty homologs list for AT1G19850 and AT1G59750; 3 hits for AT2G28350 (AT4G30080, AT1G77850… — now, at `6cf5408`: ensembl_plants_paralogs(AT1G19850) answers 24 paralogues, all other_paralog (Ensembl Compara's ancient paralogues…
- **`interpro_domains`** (family-enumeration-cost) — was: 52 ensembl_region_query calls (5 chromosomes in 4 Mb windows, 8 of the 52 failed: HTTP 500 or ReadTimeout after the… — now, at `052e4ec`: entry_members answers it in one call: 23 loci for IPR010525 in Arabidopsis, set-equal to the 23 the walk finds. The…
- **several tools** (no-assembly-metadata) — was: no tool reports seq-region names or lengths. The walk learns them from error text: region '6' answers HTTP 400 'No… — now, at `75576d0`: ensembl_plants_assembly(arabidopsis_thaliana) lists TAIR10 (GCA_000001735.1) with its 7 top-level regions and their…
- **`gramene_homologs`** (ortholog-cap-hides-organisms) — was: gramene_homologs: total 177 / 211 / 340 for the three seeds, 100 returned, and zero rows shaped like a rice or wheat… — now, at `967bc36`: with target_organism on both tools the filter runs before the cap: over the 41 member queries gramene_homologs names a…
- **several tools** (ortholog-tools-disagree) — was: one disagreement class, not a per-gene one: gramene_homologs names a rice or wheat locus for 16 of 26 queries… — now, at `052e4ec`: both tools answer for the organism asked: gramene_homologs names a rice or wheat locus for 88 of 93 queries…
- **`interpro_domains`** (wheat-locus-unresolvable) — was: all 56 queried wheat loci fail the same way (8 of 8 on the first run): '[NotFoundError] UniProt has no entry for… — now, at `052e4ec`: every wheat locus resolves: all 71 queried are decided (66 kept, 5 not), and resolve_locus_to_uniprot answers all 66…
- **`kegg_pathways`** (batch-refusal-shape) — was: the single tool is an isError result: '[OrganismNotSupported] backend kegg has no ID for triticum_aestivum'. The batch… — now, at `052e4ec`: both forms are the same isError refusal before any request, "[OrganismNotSupported] backend 'kegg' has no ID for…
- **`kegg_pathways`** (empty-as-error) — was: an empty answer is an error: kegg_pathways answers '[NotFoundError] KEGG: no pathway memberships for Os01g0236300… — now, at `052e4ec`: kegg_pathways answers a gene with no pathway as ok with pathways [] (5 rice genes, Os01g0236300 among them) and keeps…
- **`locus_literature`** (silent-empty-result) — was: hitCount 0, returned 0 and an empty hits list for 6 of the 23 loci in the run (AT1G19850, AT1G59750, AT2G46530… — now, at `052e4ec`: a Europe PMC answer with no hit count is an error that says so ('[UpstreamUnavailableError] Europe PMC /search…
- **`panther_family`** (unclassified-member) — was: 3 of the 29 members have no subfamily_id: ATMG00940 (found true, subfamily_id null), Os02g0141100 and Os08g0520500… — now, at `55dc6ca`: panther_family's schema documents the nulls (v1.24.0): found is 'True if PANTHER mapped the locus; the family fields…
- **`atted_coexpression`** (null-score) — was: every one of the 25 neighbours of Os08g0520550 has z_score null, while all 375 Arabidopsis neighbours carry a number… — now, at `55dc6ca`: atted_coexpression reads each neighbour's score under the index ATTED-II declares and names it in score_type…
- **`interpro_domains`** (candidate-undecidable) — was: 58 of the 560 could not be decided, 56 wheat and 2 Arabidopsis (first run: 10 of 325, 8 wheat and 2 Arabidopsis). The… — now, at `052e4ec`: 0 of the 577 candidates are undecided (58 of 560 at 967bc36): AT1G01335 and AT2G36920 decide as carrying no ARF entry…
- **`orthodb_orthologs`** (batch-fanout-rate-limit) — was: 87 of the 114 loci fail with '[...] OrthoDB /current/search → HTTP 403: Your query was rejected. The reason can be any… — now, at `2fc3f92`: batch_locus_call(tool='orthodb_orthologs') at the default width of 8 answers 114 of 114 loci, 0 errors (23…
- **`gene_report`** (failed-step-empties-report) — was: for 17 rice loci and 1 wheat locus the first step (ensembl_plants_lookup_locus) failed with an upstream HTTP 500… — now, at `2fc3f92`: all 114 gene_reports render, 0 with an empty result; Ensembl did not fail during this run, so the path is not…
- **`string_interactions`** (wheat-string-unresolvable) — was: all 66 fail the same way: '[NotFoundError] STRING /api/json/interaction_partners → HTTP 404: [{ "Error" : "not found"… — now, at `2fc3f92`: a wheat locus goes to STRING as the UniProt accession it resolves to (#155): 46 of the 66 wheat members return…

## Cite

From [`CITATION.cff`](../../CITATION.cff):

```yaml
message: "If you use plant-genomics-mcp in your work, please cite it as below."
title: plant-genomics-mcp
authors:
  - family-names: Arnold
    given-names: Jaret
    orcid: "https://orcid.org/0009-0003-4055-5238"
doi: "10.5281/zenodo.21636352"
version: 1.21.0
date-released: "2026-08-06"
```
