# Examples — real-execution proof transcripts

Verbatim captures of the `prompts/get` chains executed against the live
upstream APIs (Ensembl Plants, UniProt, Europe PMC, QuickGO, NCBI BLAST,
Gramene, KEGG, STRING-DB, ATTED-II). Each transcript ships as a pair: a
JSON file with the full payload and a sibling Markdown file that quotes
the load-bearing fields inline.

Outputs may drift on re-run as upstream curates new data; the JSON files are
the durable reference. The chains are driven by `_run_chain.py` (uses the
underlying client functions directly — the MCP envelope is identical to
what the server serializes around these dicts, so going through stdio adds
latency without showing anything new).

## Transcripts

| Prompt               | Query                                     | Files                                                                                                                                                                   |
| -------------------- | ----------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `analyze_locus`      | `AT1G01010` (Arabidopsis NAC001)          | [`analyze_locus_AT1G01010.json`](analyze_locus_AT1G01010.json) · [`analyze_locus_AT1G01010.md`](analyze_locus_AT1G01010.md)                                             |
| `find_homologs`      | NAC domain peptide from AT1G01010 product | [`find_homologs_AT1G01010_NAC_domain.json`](find_homologs_AT1G01010_NAC_domain.json) · [`find_homologs_AT1G01010_NAC_domain.md`](find_homologs_AT1G01010_NAC_domain.md) |
| `biological_context` | `AT1G01010` (Arabidopsis NAC001)          | [`biological_context_AT1G01010.json`](biological_context_AT1G01010.json) · [`biological_context_AT1G01010.md`](biological_context_AT1G01010.md)                         |

## What each chain demonstrates

**`analyze_locus`** — 5-tool walkthrough of a single locus:

1. `ensembl_plants_lookup_locus` → core annotation (display name, biotype, coordinates).
2. `get_gene_xrefs` → 5 cross-references (UniProt + Araport + others).
3. `resolve_locus_to_uniprot` → canonical Swiss-Prot entry **Q0WV96** (NAC1_ARATH).
4. `locus_literature` → 10 Europe PMC hits citing the locus.
5. `locus_go_annotations` → 9 QuickGO annotations for Q0WV96 (TAIR-assigned), `by_aspect` rollup = 3 MF + 1 BP + 2 CC terms.

**`find_homologs`** — BLAST + per-hit enrichment:

1. `blast_sequence` (blastp, swissprot) against the NAC DNA-binding domain
   of NAC001. Returns 10 plant NAC-family hits with bit score + e-value +
   identity%.
2. For each of the top 3 hits, `resolve_locus_to_uniprot` is called with
   the BLAST accession. The tool's input-shape detection routes
   accession-like inputs (`Q9FLJ2.1`) to UniProt's direct-by-accession
   endpoint, returning the full normalized record. Versioned-accession
   suffix (`.1`, `.2`) is stripped before fetch.

**`biological_context`** — 5-tool walkthrough for biological context:

1. `gramene_homologs` → orthologs across plant species (Gramene compara).
2. `kegg_pathways` → KEGG pathway memberships in Arabidopsis.
3. `resolve_locus_to_uniprot` → canonical Swiss-Prot accession (needed for STRING).
4. `string_interactions` → STRING first-neighbor PPI partners with combined + per-channel scores.
5. `atted_coexpression` → ATTED-II coexpression neighbors with locus + Entrez gene ID + z-score (higher = stronger).

Note on partial captures: any step that raises an upstream typed error
(`NotFoundError`, `RateLimitError`, `UpstreamUnavailableError`) records the
error class + message inline and the chain continues with the remaining
steps — a partial transcript is more useful than no transcript. The
captured `biological_context_AT1G01010` happens to show a `kegg_pathways`
miss (NAC001 has no curated KEGG pathway membership), with the other
four steps completing successfully.

## Re-running

```bash
.venv/bin/python examples/_run_chain.py
```

All three chains together take ~3-5 minutes (BLAST polls have a 60s NCBI
etiquette floor). Re-running overwrites the existing JSON + Markdown.
