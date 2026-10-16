# The run behind the README cards

`cc-answer.png` and `cc-literature.png` are stills of one real Claude Code
answer, trimmed and laid out by `render.py` (values verbatim; the second card's
grey "4 calls" line summarises the tool trace below, not model text).

- Recorded 2026-10-16 with Claude Code 2.1.301, model `claude-opus-5-5`,
  plant-genomics-mcp 1.29.1 from PyPI (56 tools), no other MCP server, no
  user settings or hooks.
- Command (empty working directory; `mcp.json` runs `uvx plant-genomics-mcp==1.29.1`):

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

## The answer, verbatim

## NAC001 (AT1G01010), *Arabidopsis thaliana*

**Ensembl Plants (TAIR10 / Araport11)**
| Field | Value |
|---|---|
| Name | NAC001: NAC domain containing protein 1 (NCBI Gene 839580) |
| Biotype | protein_coding |
| Location | Chr 1:3,631–5,899 (+ strand), 2,269 bp |
| Canonical transcript | AT1G01010.1 |

**UniProtKB (release 2026_03)**
| Field | Value |
|---|---|
| Accession | [Q0WV96](https://www.uniprot.org/uniprotkb/Q0WV96) (NAC1_ARATH) |
| Status | Reviewed (Swiss-Prot) |
| Name | NAC domain-containing protein 1 |
| Length | 429 aa |

The Ensembl and UniProt names agree.

**Three most-cited Europe PMC papers** (ranked from all 40 hits)
1. **205 citations.** Weeda S *et al.* (2014) "Arabidopsis transcriptome analysis reveals key roles of melatonin in plant defense systems." *PLoS ONE*. PMID [24682084](https://europepmc.org/article/PMC/PMC3969325)
2. **151 citations.** Hass C *et al.* (2004) "The response regulator 2 mediates ethylene signalling and hormone signal integration in Arabidopsis." *EMBO J*. PMID [15282545](https://europepmc.org/article/PMC/PMC514511)
3. **125 citations.** Vrbsky J *et al.* (2010) "siRNA-mediated methylation of Arabidopsis telomeres." *PLoS Genet*. PMID [20548962](https://europepmc.org/article/PMC/PMC2883606)

None of these three is mainly about NAC001. Based on their titles, the gene is probably just mentioned in their data. The first two look like genome-wide expression studies. AT1G01010 is the first annotated gene on chromosome 1, next to the telomere, which likely explains why it shows up in the telomere paper.
