# 8 of the 16 chain tools have no `batch_` form

**Draft — not filed.** Written from the 48-call ARF dossier run in
[`examples/arf_family/`](../), server version `1.21.0`, 2026-09-21
(3 genes x 16 tools). Each section below is one row of
[`gaps.jsonl`](../gaps.jsonl), quoted as logged; `Check` names the
captured response to read it back from.

Covers 1 row of `gaps.jsonl`.

## `no-batch-form`

- **Attempted:** run the 16-tool chain over 3 genes without 48 separate round trips
- **Returned:** 8 of the 16 chain tools have no batch\_ form on the live server: interpro_domains, alphafold_structure, experimental_structures, tf_binding_motifs, panther_family, orthodb_orthologs, aragwas_associations, gene_report. The other 8 do (batch_ensembl_plants_lookup_locus, batch_resolve_locus_to_uniprot, batch_gramene_homologs, batch_atted_coexpression, batch_string_interactions, batch_locus_go_annotations, batch_kegg_pathways, batch_locus_literature).
- **Expected:** a batch\_ form for every tool that takes a single locus, so a multi-locus call list collapses
- **Check:** `examples/arf_family/calls.jsonl, live tools/list`
