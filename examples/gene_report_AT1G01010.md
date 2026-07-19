# `gene_report` walkthrough — AT1G01010 (NAC001)

`gene_report` is the one-shot "just tell me about this gene" tool. A single
call fans out across **seven** live backends and returns a
`SynthesisEnvelope` whose `result.markdown` is a rendered Markdown gene
dossier (the headline deliverable), alongside a structured
`result.sections` mirror for programmatic use.

```jsonc
// arguments
{ "locus": "AT1G01010", "top_n": 5 }
```

## Per-step status

This is a **real-execution transcript** (live upstream APIs, `top_n=5`). Note
that KEGG legitimately has no pathway membership for this locus — the tool
degrades that one section to an "Unavailable" note and still composes the
rest of the dossier. That partial-failure isolation is the point: one dead
backend never sinks the whole report.

| Step | Tool                          | Status                         |
| ---- | ----------------------------- | ------------------------------ |
| 1    | `ensembl_plants_lookup_locus` | ok                             |
| 2    | `resolve_locus_to_uniprot`    | ok                             |
| 3    | `get_gene_xrefs`              | ok                             |
| 4    | `kegg_pathways`               | error (no pathway memberships) |
| 5    | `string_interactions`         | ok                             |
| 6    | `locus_literature`            | ok                             |
| 7    | `locus_go_annotations`        | ok                             |

## Rendered dossier (`result.markdown`)

> # NAC001 — `AT1G01010`
>
> _Arabidopsis thaliana_ · protein_coding · 1:3,631–5,899 (+) · TAIR10
>
> NAC domain containing protein 1 [Source:NCBI gene (formerly Entrezgene);Acc:839580]
>
> ## Protein
>
> **NAC domain-containing protein 1**
> NAC1_ARATH · UniProtKB reviewed (Swiss-Prot) · 429 aa
> UniProt: [Q0WV96](https://www.uniprot.org/uniprotkb/Q0WV96)
>
> ## GO annotations
>
> **Molecular Function**
>
> - [GO:0000976] transcription cis-regulatory region binding (IPI)
> - [GO:0003677] DNA binding (IEA)
> - [GO:0003700] DNA-binding transcription factor activity (ISS)
>
> **Biological Process**
>
> - [GO:0006355] regulation of DNA-templated transcription (IBA)
>
> **Cellular Component**
>
> - [GO:0005634] nucleus (ISS)
> - [GO:0016020] membrane (ISS)
>
> ## Pathways (KEGG)
>
> _Unavailable — [NotFoundError] KEGG: no pathway memberships for AT1G01010 (queried as ath:AT1G01010)_
>
> ## Interaction partners (STRING, top 5)
>
> | Partner      | Score |
> | ------------ | ----- |
> | ARV1         | 0.957 |
> | F4J030_ARATH | 0.784 |
> | T4M8.10      | 0.719 |
> | T22J18.8     | 0.718 |
> | HULK2        | 0.687 |
>
> ## Cross-references
>
> - NASC Gene ID: AT1G01010
> - TAIR: AT1G01010
> - TAIR Gene Name: ANAC001
> - Expression Atlas: AT1G01010
>
> ## Literature
>
> 40 hits total; showing top 5.
>
> - **GLARE: discovering hidden patterns in spaceflight transcriptome using representation learning** — Seo D, Strickland HF, Zhou M, … (PMID:41152268; doi:10.1038/s41526-025-00525-5)
> - **Guidelines for gene and genome assembly nomenclature** — Cannon EKS, Molik DC, … (PMID:39813136; doi:10.1093/genetics/iyaf006)

## Structured mirror (`result.sections`)

Alongside the Markdown, `result` carries `locus`, `organism`,
`canonical_gene_name`, `uniprot_accession`, and a `sections` dict keyed by
`annotation` / `protein` / `xrefs` / `pathways` / `interactions` /
`literature` / `go_annotations` — each holding the raw backend record (or
`null` when that step failed), so a client can drive UI off the structured
data while showing the prose dossier to the user.

## In Claude Code

Just ask in plain language — Claude picks `gene_report` and renders the
dossier inline:

> **"Give me a full report on the Arabidopsis gene AT1G01010."**
