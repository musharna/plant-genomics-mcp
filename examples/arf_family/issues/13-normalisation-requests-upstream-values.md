# Normalisation requests: six values this server passes through from upstream unchanged

**Draft — not filed.** Written from the 48-call ARF dossier run in
[`examples/arf_family/`](../), server version `1.21.0`, 2026-09-21
(3 genes x 16 tools). Each section below is one row of
[`gaps.jsonl`](../gaps.jsonl), quoted as logged; `Check` names the
captured response to read it back from.

These are **not** defects this server introduced — each value is what the upstream API returned, and the gap log records all six as `upstream-passthrough`. They are filed together as one request: normalise at the boundary, or say in the tool description that the field is upstream's raw value.

Covers 6 rows of `gaps.jsonl`.

## `free-text-not-a-label`

- **Attempted:** ensembl_plants_lookup_locus on all three genes; read `description` as a family label
- **Returned:** AT1G19850: 'Transcriptional factor B3 family protein / auxin-responsive factor AUX/IAA-like protein [Source:NCBI gene ...]'. AT1G59750: 'auxin response factor 1 [...]'. AT2G28350: 'auxin response factor 10 [...]'. Three members of one family, two phrasings; the first does not contain the family name the other two use. `display_name` is 'MP' for the first and 'ARF1'/'ARF10' for the others.
- **Expected:** a structured family field; the free text cannot be matched or grepped consistently even within one family
- **Check:** `raw/AT1G19850__ensembl_plants_lookup_locus.json, raw/AT1G59750__ensembl_plants_lookup_locus.json, raw/AT2G28350__ensembl_plants_lookup_locus.json`

## `locus-case`

- **Attempted:** feed atted_coexpression's neighbours back into another chain tool
- **Returned:** all 75 neighbour loci across the three genes come back mixed-case ('At2g44830', 'At2g21050', 'At5g50090'). Every other tool in the chain, and genes.tsv, use upper case ('AT1G19850'). The caller has to know to normalise.
- **Expected:** one locus spelling across tools
- **Check:** `raw/AT1G19850__atted_coexpression.json, raw/AT1G59750__atted_coexpression.json, raw/AT2G28350__atted_coexpression.json`

## `enrichment-ask`

- **Attempted:** identify atted_coexpression's neighbours
- **Returned:** each of the 25 neighbours is {locus, entrez_gene_id, z_score} - no symbol, no description. The shape is documented ('target locus + NCBI Entrez gene ID + z-score'), and ATTED-II's own row carries no more (src/plant_genomics_mcp/atted.py:86), so naming them costs 25 further lookups per gene. string_interactions, by contrast, returns preferred_name inline.
- **Expected:** a symbol per neighbour, as string_interactions does
- **Check:** `raw/AT1G19850__atted_coexpression.json, raw/AT1G19850__string_interactions.json`

## `normalise-upstream-repr`

- **Attempted:** read aragwas_associations' study identifier
- **Returned:** study.name is "('As75_raw_Full imputed genotype_amm',)" - a Python tuple repr, parentheses, quotes and trailing comma included. This is AraGWAS's own value passed through untouched: src/plant_genomics_mcp/aragwas.py:99 is a bare `study.get("name")`. The neighbouring study.method ('amm'), study.phenotype ('As75') and study.phenotype_description are clean.
- **Expected:** the tool to normalise a value it knows is a repr, or to say in its description that study.name is upstream's raw string
- **Check:** `raw/AT1G19850__aragwas_associations.json`

## `thresholds-undocumented`

- **Attempted:** read aragwas_associations' score and the three significance booleans
- **Returned:** score is 33.078925771081366 on the top association, beside maf 0.0058 and mac 2. The tool description does name it ('effect size (score)'), but neither the description nor the payload gives its scale or direction, and neither says what thresholds over_bonferroni, over_fdr and over_permutation were taken against. src/plant_genomics_mcp/aragwas.py:80 passes assoc.get('score') through unchanged.
- **Expected:** the scale of score and the thresholds behind the three booleans
- **Check:** `raw/AT1G19850__aragwas_associations.json`

## `normalise-upstream-id`

- **Attempted:** use ensembl_plants_lookup_locus' canonical_transcript as a transcript id
- **Returned:** 'AT1G19850.1.' - a trailing period, on all three genes ('AT1G59750.1.', 'AT2G28350.1.'). This is Ensembl's own value: src/plant_genomics_mcp/ensembl_plants.py:110 returns `{\*\*raw}` with only species renamed to organism. The same id appears WITHOUT the trailing period inside aragwas_associations ('AT1G19850.1'), so the two tools disagree on the spelling of one identifier.
- **Expected:** one transcript-id spelling across tools, or a note that this field is Ensembl's raw value
- **Check:** `raw/AT1G19850__ensembl_plants_lookup_locus.json, raw/AT1G19850__aragwas_associations.json`
