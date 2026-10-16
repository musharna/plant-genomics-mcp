# The runs behind the README cards

Each card is a still of one real Claude Code answer, trimmed and laid out by
`render.py`. The values are verbatim, and an ellipsis marks each cut. On the
orthologs card, the grey "6 calls" line summarises the tool trace below; it is
not model text. Both runs: model `claude-opus-5-5`, plant-genomics-mcp from PyPI
(56 tools), no other MCP server, no user settings or hooks, run from an empty
working directory. `mcp.json` runs `uvx plant-genomics-mcp==<version>`.

## 1. `cc-answer.png`: NAC001's records

- Recorded 2026-10-16 with Claude Code 2.1.301 and plant-genomics-mcp 1.29.1.
  The card shows the Ensembl and UniProt part; the papers part is below.
- Command:

```bash
claude -p "Use the plant-genomics-mcp server. Show me Arabidopsis NAC001 (AT1G01010): pull the Ensembl record, then enrich it with UniProtKB and the three most-cited Europe PMC papers mentioning the locus. Keep it concise." \
  --model claude-opus-5-5 --strict-mcp-config --mcp-config mcp.json \
  --setting-sources "" --allowedTools mcp__plant-genomics --tools ""
```

- Tool calls (4): `ensembl_plants_lookup_locus`, `resolve_locus_to_uniprot`,
  `locus_literature` (size 25), `locus_literature` (next page by cursor).
- Citation counts are Europe PMC's `citedByCount` at the time of the run;
  other indexes count differently (OpenAlex, same day: 380, 217, 155). The
  three papers' first authors, years, journals and titles were checked against
  OpenAlex by DOI (10.1371/journal.pone.0093462, 10.1038/sj.emboj.7600337,
  10.1371/journal.pgen.1000986).

### The answer, verbatim

> ## NAC001 (AT1G01010), *Arabidopsis thaliana*
>
> **Ensembl Plants (TAIR10 / Araport11)**
> | Field | Value |
> |---|---|
> | Name | NAC001: NAC domain containing protein 1 (NCBI Gene 839580) |
> | Biotype | protein_coding |
> | Location | Chr 1:3,631–5,899 (+ strand), 2,269 bp |
> | Canonical transcript | AT1G01010.1 |
>
> **UniProtKB (release 2026_03)**
> | Field | Value |
> |---|---|
> | Accession | [Q0WV96](https://www.uniprot.org/uniprotkb/Q0WV96) (NAC1_ARATH) |
> | Status | Reviewed (Swiss-Prot) |
> | Name | NAC domain-containing protein 1 |
> | Length | 429 aa |
>
> The Ensembl and UniProt names agree.
>
> **Three most-cited Europe PMC papers** (ranked from all 40 hits)
> 1. **205 citations.** Weeda S *et al.* (2014) "Arabidopsis transcriptome analysis reveals key roles of melatonin in plant defense systems." *PLoS ONE*. PMID [24682084](https://europepmc.org/article/PMC/PMC3969325)
> 2. **151 citations.** Hass C *et al.* (2004) "The response regulator 2 mediates ethylene signalling and hormone signal integration in Arabidopsis." *EMBO J*. PMID [15282545](https://europepmc.org/article/PMC/PMC514511)
> 3. **125 citations.** Vrbsky J *et al.* (2010) "siRNA-mediated methylation of Arabidopsis telomeres." *PLoS Genet*. PMID [20548962](https://europepmc.org/article/PMC/PMC2883606)
>
> None of these three is mainly about NAC001. Based on their titles, the gene is probably just mentioned in their data. The first two look like genome-wide expression studies. AT1G01010 is the first annotated gene on chromosome 1, next to the telomere, which likely explains why it shows up in the telomere paper.

## 2. `cc-orthologs.png`: NAC001's orthologs in rice and maize

- Recorded 2026-10-16 with Claude Code 2.1.302 and plant-genomics-mcp 1.29.2.
  The card cuts the OrthoDB group's size and most of the last paragraph
  (the family's size and the BLAST suggestion); the full answer is below.
- Command:

```bash
claude -p "Use the plant-genomics-mcp server. Find the orthologs of Arabidopsis NAC001 (AT1G01010) in rice and maize. Keep it concise." \
  --model claude-opus-5-5 --strict-mcp-config --mcp-config mcp.json \
  --setting-sources "" --allowedTools mcp__plant-genomics --tools ""
```

- Tool calls (6): `gramene_homologs`, `orthodb_orthologs` and
  `gene_tree_members` (by `locus`), each once for rice and once for maize.
- Every number in the answer is in a tool result: Gramene v69 gives
  Os07g0683200 as `ortholog_one2many` and no maize row (22 within-species
  paralogs left out). Ensembl tree EPlGT01160000455820 has 1 rice member and
  0 maize. OrthoDB v12 group 261582at33090 has 70 genes in 36 species, none
  of them rice or maize.
- This run is on 1.29.2. On 1.29.1, `gene_tree_members` took only Gramene's
  tree id (EPlGT01130000406172). rest.ensembl.org no longer has that id, so
  the call was not found (#235).

### The answer, verbatim

> **Rice: one ortholog, Os07g0683200.** Gramene (v69) classes it as `ortholog_one2many`, and the current Ensembl Compara gene tree (EPlGT01160000455820) has it as the only rice member.
>
> **Maize: none found.** Neither Gramene nor the current Ensembl gene tree lists a maize member for AT1G01010.
>
> **What OrthoDB adds:** OrthoDB (v12) puts NAC001 in the group "NAC domain-containing protein" (261582at33090), which has 70 genes across 36 species. That group has no rice or maize members. So the rice match rests on Gramene and Ensembl, which use the same Compara pipeline, and OrthoDB does not back it up.
>
> NAC is a large family. Arabidopsis alone has 22 within-species paralogs of NAC001. Maize clearly has NAC genes, but none falls in NAC001's tree. To look for a rough maize counterpart anyway, the next step would be a BLAST search of the NAC001 protein against the maize proteome.
