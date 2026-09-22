# `string_interactions.accession` is a STRING id, not the UniProt accession the other tools mean by that name

**Draft — not filed.** Written from the ARF family dossier run in
[`examples/arf_family/`](../), server version `1.21.0`, 2026-09-21
(29 genes x 16 tools, 248 MCP calls; the family itself from a 516-call
enumeration through the same tools). Each section below is one row of
[`gaps.jsonl`](../gaps.jsonl), quoted as logged; `Check` names the
captured response to read it back from.

Covers 1 row of `gaps.jsonl`.

## `misleading-field-name`

- **Attempted:** read string_interactions' `accession` field
- **Returned:** `accession` is byte-identical to `string_id` on every partner ('3702.P93830', '3702.Q38830') - a STRING internal id, not the UniProt accession that `resolve_locus_to_uniprot` and `alphafold_structure` mean by 'accession' ('P93024').
- **Expected:** either the UniProt accession or a field name that does not collide with the other tools' meaning of 'accession'
- **Check:** `raw/AT1G19850__string_interactions.json`
