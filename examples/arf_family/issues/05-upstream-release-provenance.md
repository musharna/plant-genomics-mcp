# 81% of responses report no upstream release, and three report one under a key nothing reads

**Filed as [#121](https://github.com/musharna/plant-genomics-mcp/issues/121) — fixed.** `version-under-another-key` is closed at `967bc36`; `provenance-null-rate` stays open (250/400 rows null, down from 188/248). Written from the ARF family dossier run in
[`examples/arf_family/`](../), server version `1.21.0`, 2026-09-21
(29 genes x 16 tools, 248 MCP calls; the family itself from a 516-call
enumeration through the same tools). Each section below is one row of
[`gaps.jsonl`](../gaps.jsonl), quoted as logged; `Check` names the
captured response to read it back from.

One issue: both rows are the same field from the two sides — tools that omit it, and tools that name it something else.

Covers 2 rows of `gaps.jsonl`.

## `provenance-null-rate`

- **Attempted:** read `upstream_version` out of every response in calls.jsonl
- **Returned:** 250 of 400 rows are null (62%; 188 of 248, 76%, on the first run). 6 of the 16 tools called carry the field: interpro_domains ('110.0', 48/48), alphafold_structure ('6', 48/48), gramene_homologs ('v69', 48/48), atted_coexpression ('Ath-u.c4-0' / 'Osa-u.c1-0', 17/17), resolve_locus_to_uniprot and gene_report ('2026_03', 48/48). The other 10 carry the key as null on every call, each stating in its description that its backend states no release on its responses.
- **Expected:** every response carries the upstream release it came from
- **Check:** `examples/arf_family/calls.jsonl`

## `version-under-another-key`

- **Attempted:** collect upstream release ids across tools with one field name
- **Returned:** three tools do report a release but not as `upstream_version`, so a single-key pass reads them as null: atted_coexpression 'atted_release': 'Ath-u.c4-0'; gramene_homologs 'release': 'v69'; alphafold_structure 'latest_version': 6.
- **Expected:** one field name for the upstream release across tools
- **Check:** `raw/AT1G19850__atted_coexpression.json, raw/AT1G19850__gramene_homologs.json, raw/AT1G19850__alphafold_structure.json`
