# 81% of responses report no upstream release, and three report one under a key nothing reads

**Draft — not filed.** Written from the ARF family dossier run in
[`examples/arf_family/`](../), server version `1.21.0`, 2026-09-21
(29 genes x 16 tools, 248 MCP calls; the family itself from a 516-call
enumeration through the same tools). Each section below is one row of
[`gaps.jsonl`](../gaps.jsonl), quoted as logged; `Check` names the
captured response to read it back from.

One issue: both rows are the same field from the two sides — tools that omit it, and tools that name it something else.

Covers 2 rows of `gaps.jsonl`.

## `provenance-null-rate`

- **Attempted:** read `upstream_version` out of every response in calls.jsonl
- **Returned:** 188 of 248 rows are null (76%). Only 3 of the 16 tools called ever carry the field: interpro_domains ('110.0', 29/29), gene_report ('2026_03', 29/29, inherited from its resolve_locus_to_uniprot step) and batch_resolve_locus_to_uniprot ('2026_03', 2/2). The other 13 are null on every call.
- **Expected:** every response carries the upstream release it came from
- **Check:** `examples/arf_family/calls.jsonl`

## `version-under-another-key`

- **Attempted:** collect upstream release ids across tools with one field name
- **Returned:** three tools do report a release but not as `upstream_version`, so a single-key pass reads them as null: atted_coexpression 'atted_release': 'Ath-u.c4-0'; gramene_homologs 'release': 'v69'; alphafold_structure 'latest_version': 6.
- **Expected:** one field name for the upstream release across tools
- **Check:** `raw/AT1G19850__atted_coexpression.json, raw/AT1G19850__gramene_homologs.json, raw/AT1G19850__alphafold_structure.json`
