# Example chain — `find_homologs` for AT1G01010_NAC_domain

**Query label:** `AT1G01010_NAC_domain` (program `blastp`, sequence length 162)
**Captured:** 2026-05-21T07:34:39Z

Real-execution transcript of the BLAST → per-hit-resolve chain rendered by the `find_homologs` MCP prompt. The query sequence is the NAC DNA-binding domain of Arabidopsis NAC001 (AT1G01010 product); the top BLAST hits should be plant NAC-family proteins. Full payload preserved in the matching `.json` sibling.

---

## Step 1 — `blast_sequence`

**Input:** `{"program": "blastp", "hitlist_size": 10, "sequence_length": 162}`  
**Elapsed:** 64.05s  
**RID:** `0XDCGMUY016`

Top hits (full set in .json):

| # | accession | e-value | bit score | identity | description |
|---|---|---|---|---|---|
| 1 | `Q9FLJ2.1` | 8e-65 | 204.0 | 66% | RecName: Full=NAC domain-containing protein 100; Shor... |
| 2 | `Q9FKA0.1` | 1e-63 | 199.0 | 66% | RecName: Full=NAC domain-containing protein 92; Short... |
| 3 | `Q9FLR3.1` | 2e-63 | 200.0 | 64% | RecName: Full=NAC domain-containing protein 79; Short... |
| 4 | `Q9LJW3.1` | 3e-63 | 199.0 | 76% | RecName: Full=NAC domain-containing protein 59; Short... |
| 5 | `Q9FK44.1` | 3e-60 | 192.0 | 61% | RecName: Full=NAC domain-containing protein 87; Short... |
| 6 | `O04017.1` | 8e-60 | 192.0 | 69% | RecName: Full=Protein CUP-SHAPED COTYLEDON 2; AltName... |
| 7 | `Q7XUV6.2` | 2e-59 | 190.0 | 59% | RecName: Full=NAC domain-containing protein 4; Short=... |
| 8 | `Q9SQQ6.1` | 2e-58 | 187.0 | 61% | RecName: Full=NAC domain-containing protein 46; Short... |
| 9 | `K4BNG7.1` | 1e-55 | 179.0 | 57% | RecName: Full=NAC domain-containing protein 2; Short=... |
| 10 | `Q9S851.1` | 1e-55 | 180.0 | 67% | RecName: Full=Protein CUP-SHAPED COTYLEDON 3; AltName... |

## Step 2 — per-hit `resolve_locus_to_uniprot`

For each of the top 3 BLAST hits we attempt a UniProt lookup if the accession matches the UniProtKB ID pattern. NCBI RefSeq / GenBank accessions are noted but not resolved (out of scope for this tool).

### Hit #1 — `Q9FLJ2.1`

- description: RecName: Full=NAC domain-containing protein 100; Shor...
- e-value: 8e-65
- bit score: 204.0

```json
{
  "locus_query": "Q9FLJ2.1",
  "primaryAccession": "Q9FLJ2",
  "uniProtkbId": "NC100_ARATH",
  "entryType": "UniProtKB reviewed (Swiss-Prot)",
  "reviewed": true,
  "recommendedName": "NAC domain-containing protein 100",
  "geneNames": [
    "NAC100"
  ],
  "organism": "Arabidopsis thaliana",
  "taxonId": 3702,
  "sequenceLength": 336,
  "web_url": "https://www.uniprot.org/uniprotkb/Q9FLJ2"
}
```

### Hit #2 — `Q9FKA0.1`

- description: RecName: Full=NAC domain-containing protein 92; Short...
- e-value: 1e-63
- bit score: 199.0

```json
{
  "locus_query": "Q9FKA0.1",
  "primaryAccession": "Q9FKA0",
  "uniProtkbId": "NAC92_ARATH",
  "entryType": "UniProtKB reviewed (Swiss-Prot)",
  "reviewed": true,
  "recommendedName": "NAC domain-containing protein 92",
  "geneNames": [
    "NAC92"
  ],
  "organism": "Arabidopsis thaliana",
  "taxonId": 3702,
  "sequenceLength": 285,
  "web_url": "https://www.uniprot.org/uniprotkb/Q9FKA0"
}
```

### Hit #3 — `Q9FLR3.1`

- description: RecName: Full=NAC domain-containing protein 79; Short...
- e-value: 2e-63
- bit score: 200.0

```json
{
  "locus_query": "Q9FLR3.1",
  "primaryAccession": "Q9FLR3",
  "uniProtkbId": "NAC79_ARATH",
  "entryType": "UniProtKB reviewed (Swiss-Prot)",
  "reviewed": true,
  "recommendedName": "NAC domain-containing protein 79",
  "geneNames": [
    "NAC079"
  ],
  "organism": "Arabidopsis thaliana",
  "taxonId": 3702,
  "sequenceLength": 329,
  "web_url": "https://www.uniprot.org/uniprotkb/Q9FLR3"
}
```
