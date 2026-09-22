# `string_interactions` is the only chain tool whose locus argument is not called `locus`

**Filed as [#129](https://github.com/musharna/plant-genomics-mcp/issues/129) — open.** Written from the ARF family dossier run in
[`examples/arf_family/`](../), server version `1.21.0`, 2026-09-21
(29 genes x 16 tools, 248 MCP calls; the family itself from a 516-call
enumeration through the same tools). Each section below is one row of
[`gaps.jsonl`](../gaps.jsonl), quoted as logged; `Check` names the
captured response to read it back from.

Covers 1 row of `gaps.jsonl`.

## `argument-name`

- **Attempted:** string_interactions({'locus_or_accession': 'AT1G19850', 'organism': 'arabidopsis_thaliana'})
- **Returned:** the call works, but string_interactions is the only one of the chain's 16 tools whose locus argument is not called `locus` (live tools/list: `locus_or_accession`, required). batch_string_interactions likewise takes `loci_or_accessions` where every other batch\_* tool takes `loci`.
- **Expected:** one argument name for the same kind of value across the tool surface
- **Check:** `examples/arf_family/chain.py, live tools/list`
