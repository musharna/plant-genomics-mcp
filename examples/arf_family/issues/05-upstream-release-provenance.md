# 81% of responses report no upstream release, and three report one under a key nothing reads

**Filed as [#121](https://github.com/musharna/plant-genomics-mcp/issues/121) — closed.** `version-under-another-key` is closed at `967bc36`. `provenance-null-rate` stays open. Written from the ARF family dossier in
[`examples/arf_family/`](../), re-run at server commit `052e4ec`
(release `1.21.0`): 64 MCP calls over 114 genes x 16 tools, the family
itself from a 1163-call enumeration through the same tools. Each section
below is one row of [`gaps.jsonl`](../gaps.jsonl), quoted as logged;
`Check` names the captured response to read it back from.

One issue: both rows are the same field from the two sides — tools that omit it, and tools that name it something else.

Covers 2 rows of `gaps.jsonl`.

## `provenance-null-rate`

- **Attempted:** read `upstream_version` out of every response in calls.jsonl
- **Returned:** 42 of 64 calls carry no upstream_version (66%; 250 of 400, 62%, at 967bc36; 188 of 248, 76%, on the first run). The same 6 of the 16 tools carry the field, on every answer: interpro_domains ('110.0', 114/114), alphafold_structure ('6', 114/114), gramene_homologs ('v69', 114/114), atted_coexpression ('Ath-u.c4-0' / 'Osa-u.c1-0', 17/17), resolve_locus_to_uniprot ('2026_03', 114/114) and gene_report (on the envelope). The other 10 carry the key as null on every call, each stating in its description that its backend states no release on its responses.
- **Expected:** every response carries the upstream release it came from
- **Check:** `examples/arf_family/calls.jsonl`

## `version-under-another-key`

- **Attempted:** collect upstream release ids across tools with one field name
- **Returned:** three tools do report a release but not as `upstream_version`, so a single-key pass reads them as null: atted_coexpression 'atted_release': 'Ath-u.c4-0'; gramene_homologs 'release': 'v69'; alphafold_structure 'latest_version': 6.
- **Expected:** one field name for the upstream release across tools
- **Check:** `raw/AT1G19850__atted_coexpression.json, raw/AT1G19850__gramene_homologs.json, raw/AT1G19850__alphafold_structure.json`
