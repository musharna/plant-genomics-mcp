# Every wheat IWGSC locus fails the locus-to-UniProt step, so no protein-level tool can answer for wheat

**Filed as [#138](https://github.com/musharna/plant-genomics-mcp/issues/138) — open.** Written from the ARF family dossier run in
[`examples/arf_family/`](../), server version `1.21.0`, 2026-09-21
(29 genes x 16 tools, 248 MCP calls; the family itself from a 516-call
enumeration through the same tools). Each section below is one row of
[`gaps.jsonl`](../gaps.jsonl), quoted as logged; `Check` names the
captured response to read it back from.

`interpro_domains` on all 8 wheat loci the ortholog tool named fails with `UniProt has no entry for gene=TraesCS... organism_id=4565`. Six chain tools share that resolution step. Whether the fix is on the query side or the ids are simply not indexed by UniProt under that name cannot be told from inside the MCP, so the row's origin is `unverified`.

Covers 1 row of `gaps.jsonl`.

## `wheat-locus-unresolvable`

- **Attempted:** verify each wheat locus the ortholog tool named with interpro_domains(locus, organism='triticum_aestivum')
- **Returned:** all 56 queried wheat loci fail the same way (8 of 8 on the first run): '[NotFoundError] UniProt has no entry for gene=TraesCS3A02G159200 organism_id=4565'. interpro_domains, alphafold_structure, experimental_structures, tf_binding_motifs, locus_go_annotations and string_interactions all go through the same locus -> UniProt step, so no protein-level tool in the chain can answer for a wheat IWGSC locus. Whether UniProt indexes these genes under another name, or the query needs a different field, cannot be told from the output.
- **Expected:** either a resolution path for IWGSC gene ids or an error that says the id form is not indexed
- **Check:** `examples/arf_family/family_candidates.tsv, examples/arf_family/enumeration_calls.jsonl`
