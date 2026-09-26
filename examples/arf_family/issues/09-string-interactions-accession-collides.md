# `string_interactions.accession` is a STRING id, not the UniProt accession the other tools mean by that name

**Filed as [#133](https://github.com/musharna/plant-genomics-mcp/issues/133) — fixed.** The row is closed at `55dc6ca`. Written from the ARF family dossier in
[`examples/arf_family/`](../), re-run at server commit `052e4ec`
(release `1.21.0`): 64 MCP calls over 114 genes x 16 tools, the family
itself from a 1163-call enumeration through the same tools. Each section
below is one row of [`gaps.jsonl`](../gaps.jsonl), quoted as logged;
`Check` names the captured response to read it back from.

Covers 1 row of `gaps.jsonl`.

## `misleading-field-name`

- **Attempted:** read string_interactions' `accession` field
- **Returned:** `accession` is byte-identical to `string_id` on every partner ('3702.P93830', '3702.Q38830') - a STRING internal id, not the UniProt accession that `resolve_locus_to_uniprot` and `alphafold_structure` mean by 'accession' ('P93024'). Since #133 the description says so and marks the field deprecated; the value and the name are unchanged.
- **Expected:** either the UniProt accession or a field name that does not collide with the other tools' meaning of 'accession'
- **Check:** `raw/AT1G19850__string_interactions.json`
