# KEGG and ATTED-II deliver 'nothing here' as a NotFoundError, so an empty result cannot be told from an unknown gene

**Filed as [#140](https://github.com/musharna/plant-genomics-mcp/issues/140) — fixed.** The row is closed at `052e4ec`. Written from the ARF family dossier in
[`examples/arf_family/`](../), re-run at server commit `052e4ec`
(release `1.21.0`): 64 MCP calls over 114 genes x 16 tools, the family
itself from a 1163-call enumeration through the same tools. Each section
below is one row of [`gaps.jsonl`](../gaps.jsonl), quoted as logged;
`Check` names the captured response to read it back from.

16 of the 20 locus-level errors in the full-family run are of this kind. `locus_literature` already returns `hitCount: 0` with an empty list for the same situation; the two tools below raise instead.

Covers 1 row of `gaps.jsonl`.

## `empty-as-error`

- **Attempted:** read an empty answer from kegg_pathways and atted_coexpression
- **Returned:** an empty answer is an error: kegg_pathways answers '[NotFoundError] KEGG: no pathway memberships for Os01g0236300 (queried as osa:4327785)' for 2 rice genes and ATMG00940, and atted_coexpression '[NotFoundError] ATTED-II: no co-expression neighbors for AT1G34170' for 8 Arabidopsis and 5 rice genes. A gene with no pathway or no neighbours is a finding; as an isError result it is indistinguishable in the transport from a gene KEGG or ATTED-II never heard of, and it fills the runner's error log (13 + 3 of the 20 locus-level errors in the run).
- **Expected:** an ok result with an empty list and the count 0, the shape locus_literature already uses for hitCount 0
- **Check:** `raw/Os01g0236300__kegg_pathways.json, raw/AT1G34170__atted_coexpression.json, raw/ATMG00940__kegg_pathways.json`
