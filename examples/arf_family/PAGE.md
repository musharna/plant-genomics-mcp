# One family, 248 calls: a worked run of plant-genomics-mcp

`plant-genomics-mcp` publishes 50 MCP tools over 23 free, public
plant-genomics backends. This page is what happened when 16 of them were
run end to end over a whole gene family — 23 _Arabidopsis thaliana_
members found through the tools alone, and the 6 _Oryza sativa_ and 0
_Triticum aestivum_ members the tools could name and verify — in a
single sitting: every response captured to [`raw/`](raw), and every
place an answer came back unusable, inconsistent or silently short
written down as it was found. It is a showcase and a defect list at the
same time. The recipes below are what the tools do well; the 49 rows
under **Known gaps** are what they do not.

Provenance: run 2026-09-21 against server version `1.21.0` — 248 MCP
calls over 29 genes × 16 tools (8 tools through their `batch_` form, 50
loci per call; 8 per locus), 237 ok, 6 expected refusals
(`aragwas_associations` on rice), 5 calls carrying 20 locus-level
errors, 3.9 minutes wall. The family itself came from a separate
516-call, 19-minute enumeration ([`enumerate_family.py`](enumerate_family.py),
[`enumeration_calls.jsonl`](enumeration_calls.jsonl)): the three
starting loci in [`genes.tsv`](genes.tsv), then every candidate the
tools could reach, each decided by `interpro_domains` and recorded in
[`family_candidates.tsv`](family_candidates.tsv) whether kept or not —
325 candidates, of which 10 could not be decided — 8 wheat whose loci
never resolved to a protein (`wheat-locus-unresolvable` below) and 2
Arabidopsis for which InterPro answered an empty body
(`candidate-undecidable` below) — so the 23 is 23 of a possible 25 in
Arabidopsis and the 0 for wheat is 0 decided of 8.
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
list and the other two aspects dropped.

```json
{
  "uniprot_accession": "P93024",
  "numberOfHits": 51,
  "returned": 50,
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

Read the two counts before the terms: `numberOfHits` is 51 and
`returned` is 50, so one annotation did not come back — and this tool has
no `truncated` field to say so, though five other tools in the chain
carry one. That is the `silent-truncation` row below.

### I have a locus, I want one dossier — and this is where it disappoints

> **"Give me everything you have on AT1G19850 in one go."**

`gene_report` composes eight tools and renders a Markdown report. It
works, and it is the weakest answer on this page. Trimmed from
`raw/AT1G19850__gene_report.json`: 3 of the 6 `result` keys, `steps` cut
to 2 of 8, and `markdown` cut to its first 3 non-blank lines of 3,122
characters.

```json
{
  "elapsed_s": 0.8738203659995634,
  "steps": [
    { "tool": "ensembl_plants_lookup_locus", "elapsed_s": null, "error": null },
    { "tool": "resolve_locus_to_uniprot", "elapsed_s": null, "error": null }
  ],
  "result": {
    "canonical_gene_name": "MP",
    "uniprot_accession": "P93024",
    "markdown": "# MP — `AT1G19850`\n\n*Arabidopsis thaliana* · protein_coding · 1:6,886,669–6,891,404 (+) · TAIR10\n\nTranscriptional factor B3 family protein / auxin-responsive factor AUX/IAA-like protein [Source:NCBI gene (formerly Entrezgene);Acc:838573]"
  }
}
```

Three things are wrong with that answer, all visible in the trim above.
The report is titled `MP`, taken from Ensembl's `display_name`, while its
own protein section — from `resolve_locus_to_uniprot`, the same call as
in the first recipe — reads `Auxin response factor 5`: one report, two
names, neither labelled by source. `steps[].elapsed_s` is `null` for all
8 steps on all 29 genes, so the per-step field that does exist tells you
nothing. And the response is 158,103 bytes on the wire for this gene, of
which the payload is 77 kB; within it every sub-tool payload is carried
twice — the eight `steps[].result` payloads total 36,021 B and the eight
`result.sections` payloads total the same 36,021 B, byte-identical. On 6
of the 29 genes the response is over 200 kB. Use `gene_report` for the
rendered `result.markdown`; go to the individual tools for anything you
intend to parse. One more thing this capture shows: its literature step
is empty (150 B, `hitCount` 0) for a gene that has 91 papers when asked
again — the `silent-empty-result` row below.

Sizes are bytes of the JSON-RPC response line, which carries each payload
as both text and `structuredContent`; the files in `raw/` are the parsed
payload, re-indented. The 77 kB and the two 36,021 B totals are
`json.dumps` of the parsed payload, and of its eight `steps[].result` and
eight `result.sections` entries, read back from
`raw/AT1G19850__gene_report.json`.

## What 248 responses cost, and what they say about their source

![Response size on the wire for each of the 248 MCP calls on a log10 axis, one row per tool called ordered by median size, offset and shaped by organism with exact ties drawn on one point, coloured by whether the tool reports an upstream release under upstream_version, under another key, or not at all; hollow points are documented organism refusals; a dashed line marks the 200 kB oversize threshold, crossed by gene_report on 6 genes and by three batch calls](coverage.png)

Response sizes on the wire span 3,744×, from 174 B
(`aragwas_associations` on ATMG00940) to 651 kB
(`batch_locus_go_annotations` over the 23 Arabidopsis loci). The eight
tools with a `batch_` form draw two points each (one per organism); the
eight called per locus draw one point per distinct (organism, byte
count), since exact ties draw on one point — from 6 positions for
`experimental_structures` to 29 for `gene_report`. A 100-gene family is
not readable one point per gene, so the vertical offset is by organism,
not by gene. Only 3 of the 16 tools called put the upstream
release in `upstream_version`; 3 more report one under a different key
(`atted_release`, `release`, `latest_version`), so a single-key read
scores those null too, and the remaining 10 carry no release at all.
`gene_report` is over the runner's 200 kB oversize threshold on 6 of 29
genes; three batch calls over the 23 Arabidopsis loci are over it too
(`batch_gramene_homologs`, `batch_locus_go_annotations`,
`batch_locus_literature`). Exact ties draw on one point: the hollow
square on the `aragwas_associations` row is six 210 B refusals of rice,
which its description says it will make. Per-tool table: [`coverage.tsv`](coverage.tsv).

Every row below is one line of [`gaps.jsonl`](gaps.jsonl), rendered by
[`render_gaps.py`](render_gaps.py) so the page cannot drift from the log.
Drafted issue text, grouped by theme, is in [`issues/`](issues) — filed
nowhere yet.

## Known gaps

All 49 rows the run logged, one line each: what was attempted, what came back, what was expected instead. Full text and the raw response each row was read from are in [`gaps.jsonl`](gaps.jsonl) and [`gaps_auto.jsonl`](gaps_auto.jsonl).

### Defects in this tool

- **`resolve_locus_to_uniprot`** (shared-symbol) — ok=true and one scalar answer: primaryAccession Q8L7G0 / uniProtkbId ARFA_ARATH / recommendedName 'Auxin response… — expected: either the loci the symbol matches, or a refusal; a single ok=true accession is indistinguishable from an unambiguous hit
- **several tools** (symbol-rejected-elsewhere) — three reject it, verbatim: [NotFoundError] Ensembl Plants /lookup/id/ARF1 → HTTP 400 (not found): {"error":"ID 'ARF1'… — expected: one behaviour for a symbol in a `locus` argument across tools that share the argument name
- **`string_interactions`** (argument-name) — the call works, but string_interactions is the only one of the chain's 16 tools whose locus argument is not called… — expected: one argument name for the same kind of value across the tool surface
- **several tools** (no-family-enumeration) — no tool on the live server takes a family or entry accession as input. Of 50 tools, every input is a locus, a list of… — expected: a tool that takes PTHR31384 or IPR010525 and returns its members
- **several tools** (no-batch-form) — 8 of the 16 chain tools have no batch\_ form on the live server: interpro_domains, alphafold_structure… — expected: a batch\_ form for every tool that takes a single locus, so a multi-locus call list collapses
- **several tools** (provenance-null-rate) — 188 of 248 rows are null (76%). Only 3 of the 16 tools called ever carry the field: interpro_domains ('110.0', 29/29)… — expected: every response carries the upstream release it came from
- **several tools** (version-under-another-key) — three tools do report a release but not as `upstream_version`, so a single-key pass reads them as null… — expected: one field name for the upstream release across tools
- **`gene_report`** (payload-duplicated) — every sub-tool payload appears twice in one envelope: for AT1G19850 the eight steps[].result payloads total 36,021 B… — expected: the sub-payloads once, or a flag to drop the raw steps
- **`gene_report`** (null-field) — steps[].elapsed_s is null for all 8 steps of all 29 genes (232/232), while the envelope's own top-level elapsed_s is… — expected: a per-step number or no field at all
- **`gene_report`** (inconsistent-name) — result.canonical_gene_name is 'MP' for AT1G19850 (taken from Ensembl display_name) and the markdown title reads '# MP… — expected: one name, or both names labelled by source
- **`gene_report`** (duplicate-rendering) — 23 GO bullets, 20 distinct lines, 14 distinct GO ids. '- [GO:0005515] protein binding (IPI)' appears 3 times… — expected: one bullet per (term, evidence) pair, or the reference that makes the repeats distinct
- **several tools** (silent-truncation) — locus_go_annotations reports numberOfHits 51 and returned 50, and the annotations list holds 50. There is no… — expected: a truncation flag wherever the returned count is below the upstream count
- **`locus_go_annotations`** (dedup-not-in-payload) — by_aspect holds 16 entries (biological_process 10, molecular_function 5, cellular_component 1) beside 50 annotations… — expected: the payload to mark by_aspect as deduplicated, as the description already does
- **several tools** (count-field-names) — nine different field names for the upstream total across ten tools - domain_count (interpro_domains), structure_count… — expected: one pair of field names for 'how many exist upstream' and 'how many came back'
- **several tools** (no-pagination) — orthodb_orthologs returns 100 of member_count 1986; gramene_homologs 100 of total 177; aragwas_associations 100 of… — expected: an offset or cursor on any tool that reports more rows upstream than it returns
- **`string_interactions`** (misleading-field-name) — `accession` is byte-identical to `string_id` on every partner ('3702.P93830', '3702.Q38830') - a STRING internal id… — expected: either the UniProt accession or a field name that does not collide with the other tools' meaning of 'accession'
- **`locus_literature`** (promised-field-always-null) — journalTitle is JSON null in all 160 hits across the 29 genes - never a string… — expected: the journal the description promises, and one type per kind of value
- **`experimental_structures`** (count-counts-rows) — AT1G19850: structure_count 10, structures list 10 rows, 3 distinct pdb_ids (4chk, 4ldu, 6l5k) - 4chk appears as chain… — expected: a name that matches what is counted (entry_count / chain_count), or a distinct pdb count beside it
- **`alphafold_structure`** (undefined-bands) — four fractions named very_low / low / confident / very_high (0.516 / 0.027 / 0.120 / 0.338 for AT1G19850) beside… — expected: the numeric cutoffs alongside the fractions
- **`gramene_homologs`** (identifier-with-no-tool) — each homolog is {target_locus, type, gene_tree_id} - e.g. {'C5167_014531', 'ortholog_one2many'… — expected: a tool that takes a gene_tree_id, and the species enrichment the server already computes surfaced on this tool
- **several tools** (browser-needed-assets) — the only pointers to a structure, a PAE plot or a motif logo are URLs the tools cannot dereference: cif_url / pdb_url… — expected: a tool that fetches the asset, or an explicit statement that these are browser-only
- **`interpro_domains`** (family-enumeration-cost) — 52 ensembl_region_query calls (5 chromosomes in 4 Mb windows, 8 of the 52 failed: HTTP 500 or ReadTimeout after the… — expected: a tool that takes IPR010525 (or PTHR31384) and an organism and returns the loci
- **several tools** (no-assembly-metadata) — no tool reports seq-region names or lengths. The walk learns them from error text: region '6' answers HTTP 400 'No… — expected: a tool, or a field on ensembl_plants_lookup_locus, that lists an assembly's seq-regions and their lengths
- **`gramene_homologs`** (ortholog-cap-hides-organisms) — gramene_homologs: total 177 / 211 / 340 for the three seeds, 100 returned, and zero rows shaped like a rice or wheat… — expected: a target-organism filter on both tools, or pagination, so a cap of 100 cannot silently exclude the organism asked for
- **several tools** (ortholog-tools-disagree) — one disagreement class, not a per-gene one: gramene_homologs names a rice or wheat locus for 16 of 26 queries… — expected: both tools answering for the organism asked, so agreement is measurable
- **`kegg_pathways`** (batch-refusal-shape) — the single tool is an isError result: '[OrganismNotSupported] backend kegg has no ID for triticum_aestivum'. The batch… — expected: one refusal shape for the two forms of one tool, and descriptions that match the organism list the error prints
- **`kegg_pathways`** (empty-as-error) — an empty answer is an error: kegg_pathways answers '[NotFoundError] KEGG: no pathway memberships for Os01g0236300… — expected: an ok result with an empty list and the count 0, the shape locus_literature already uses for hitCount 0
- **`gramene_homologs`** (oversize) — 23 loci: over 200 kB on the wire (batch), largest 506333 bytes — expected: a usable answer within 200 kB
- **`atted_coexpression`** (error) — 13 loci: [NotFoundError] ATTED-II: no co-expression neighbors for <locus> — expected: a usable answer
- **`aragwas_associations`** (error) — ATMG00940: [UpstreamUnavailableError] AraGWAS associations exhausted N retries (last HTTP N) — expected: a usable answer
- **`locus_go_annotations`** (oversize) — 23 loci: over 200 kB on the wire (batch), largest 651444 bytes — expected: a usable answer within 200 kB
- **`kegg_pathways`** (error) — ATMG00940: [NotFoundError] KEGG: no pathway memberships for <locus> (queried as ath:<locus>) — expected: a usable answer
- **`locus_literature`** (oversize) — 23 loci: over 200 kB on the wire (batch), largest 585924 bytes — expected: a usable answer within 200 kB
- **`gene_report`** (oversize) — 6 loci: over 200 kB on the wire, largest 247929 bytes — expected: a usable answer within 200 kB
- **`kegg_pathways`** (error) — 2 loci: [NotFoundError] KEGG: no pathway memberships for <locus> (queried as osa:N) — expected: a usable answer
- **`kegg_pathways`** (error) — 3 loci: [NotFoundError] KEGG bridge (Ensembl Plants /xrefs): [NotFoundError] KEGG: no Entrez Gene ID for <locus>… — expected: a usable answer

### Upstream values passed through unchanged

- **`ensembl_plants_lookup_locus`** (free-text-not-a-label) — AT1G19850: 'Transcriptional factor B3 family protein / auxin-responsive factor AUX/IAA-like protein [Source:NCBI gene… — expected: a structured family field; the free text cannot be matched or grepped consistently even within one family
- **`atted_coexpression`** (locus-case) — 396 of the 400 neighbour loci across the 16 genes ATTED-II answers for come back mixed-case ('At2g44830', 'At2g21050'… — expected: one locus spelling across tools
- **`atted_coexpression`** (enrichment-ask) — each of the 25 neighbours is {locus, entrez_gene_id, z_score} - no symbol, no description. The shape is documented… — expected: a symbol per neighbour, as string_interactions does
- **`aragwas_associations`** (normalise-upstream-repr) — study.name is "('As75_raw_Full imputed genotype_amm',)" - a Python tuple repr, parentheses, quotes and trailing comma… — expected: the tool to normalise a value it knows is a repr, or to say in its description that study.name is upstream's raw string
- **`aragwas_associations`** (thresholds-undocumented) — score is 33.078925771081366 on the top association, beside maf 0.0058 and mac 2. The tool description does name it… — expected: the scale of score and the thresholds behind the three booleans
- **`ensembl_plants_lookup_locus`** (normalise-upstream-id) — 'AT1G19850.1.' - a trailing period, on all 29 genes in both organisms ('AT1G59750.1.', 'Os01t0236300-01.'). This is… — expected: one transcript-id spelling across tools, or a note that this field is Ensembl's raw value
- **several tools** (free-text-null-outside-arabidopsis) — ensembl_region_query 1:1-1000000 on oryza_sativa returns 165 protein-coding genes with description on 1; 1A:1-1000000… — expected: a structured family or domain field per gene, the same in every organism
- **`panther_family`** (unclassified-member) — 3 of the 29 members have no subfamily_id: ATMG00940 (found true, subfamily_id null), Os02g0141100 and Os08g0520500… — expected: a null that the schema documents, so an empty cell in a manifest is a defined value
- **`atted_coexpression`** (null-score) — every one of the 25 neighbours of Os08g0520550 has z_score null, while all 375 Arabidopsis neighbours carry a number… — expected: the score the description promises, or a per-organism note that ATTED-II's rice table has none

### Not attributable from inside the MCP

- **`gramene_homologs`** (paralog-closure-empty) — total 0 and an empty homologs list for AT1G19850 and AT1G59750; 3 hits for AT2G28350 (AT4G30080, AT1G77850… — expected: the in-species paralogs of a locus, or a statement that the projection has none
- **`interpro_domains`** (wheat-locus-unresolvable) — all 8 fail the same way: '[NotFoundError] UniProt has no entry for gene=TraesCS3A02G159200 organism_id=4565'… — expected: either a resolution path for IWGSC gene ids or an error that says the id form is not indexed
- **`locus_literature`** (silent-empty-result) — hitCount 0, returned 0 and an empty hits list for 6 of the 23 loci in the run (AT1G19850, AT1G59750, AT2G46530… — expected: an upstream status on every answer, so an empty list carries whether the source answered
- **`interpro_domains`** (candidate-undecidable) — 10 of the 325 could not be decided, 8 wheat and 2 Arabidopsis. The 2 Arabidopsis, AT1G01335 and AT2G36920, answer… — expected: a found=false answer with a stated reason, so 'no record' and 'no answer' are different results

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
