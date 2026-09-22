# Nine names for “how many exist upstream”, no cursor to reach the rest, and one count that counts chains

**Draft — not filed.** Written from the 48-call ARF dossier run in
[`examples/arf_family/`](../), server version `1.21.0`, 2026-09-21
(3 genes x 16 tools). Each section below is one row of
[`gaps.jsonl`](../gaps.jsonl), quoted as logged; `Check` names the
captured response to read it back from.

One issue: writing a single “N of M, fetch the rest” helper against this surface is blocked by all three rows at once.

Covers 3 rows of `gaps.jsonl`.

## `count-field-names`

- **Attempted:** write one function that reports 'N of M' for every tool in the chain
- **Returned:** nine different field names for the upstream total across ten tools - domain_count (interpro_domains), structure_count (experimental_structures), motif_count (tf_binding_motifs), member_count and organism_count (orthodb_orthologs), total (gramene_homologs), association_count (aragwas_associations), numberOfHits (locus_go_annotations), hitCount (locus_literature) - with a `returned` companion on only three of them, and no count at all on atted_coexpression (25 rows) or string_interactions (20 rows).
- **Expected:** one pair of field names for 'how many exist upstream' and 'how many came back'
- **Check:** `raw/AT1G19850__orthodb_orthologs.json, raw/AT1G19850__gramene_homologs.json, raw/AT1G19850__locus_literature.json, raw/AT1G19850__atted_coexpression.json, raw/AT1G19850__interpro_domains.json`

## `no-pagination`

- **Attempted:** fetch the rows past the first page of a truncated list
- **Returned:** orthodb_orthologs returns 100 of member_count 1986; gramene_homologs 100 of total 177; aragwas_associations 100 of association_count 126; locus_literature 10 of hitCount 91. No tool has an offset, page or cursor argument, so the rows past the cap are unreachable. The caps are not uniform either: orthodb_orthologs and gramene_homologs take `limit`, locus_literature takes `size` (its description caps it at 25, below the 91 hits), and aragwas_associations takes neither - its live schema is locus and organism only.
- **Expected:** an offset or cursor on any tool that reports more rows upstream than it returns
- **Check:** `raw/AT1G19850__orthodb_orthologs.json, raw/AT1G19850__gramene_homologs.json, raw/AT1G19850__aragwas_associations.json, raw/AT1G19850__locus_literature.json`

## `count-counts-rows`

- **Attempted:** read experimental_structures' structure_count as a number of structures
- **Returned:** AT1G19850: structure_count 10, structures list 10 rows, 3 distinct pdb_ids (4chk, 4ldu, 6l5k) - 4chk appears as chain A and chain B, and so on. AT1G59750: structure_count 9, 5 distinct pdb_ids. src/plant_genomics_mcp/pdbe.py:109 sets total = len(valid), counting PDBe best_structures entries, which are per-chain. The description does say 'per entry the PDB id, chain, ...', so the shape is documented; the field name is not.
- **Expected:** a name that matches what is counted (entry_count / chain_count), or a distinct pdb count beside it
- **Check:** `raw/AT1G19850__experimental_structures.json, raw/AT1G59750__experimental_structures.json`
