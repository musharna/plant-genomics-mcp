# Structures, PAE plots and motif logos are URLs no tool can dereference

**Filed as [#136](https://github.com/musharna/plant-genomics-mcp/issues/136) — open.** Written from the ARF family dossier in
[`examples/arf_family/`](../), re-run at server commit `967bc36`
(release `1.21.0`): 400 MCP calls over 48 genes x 16 tools, the family
itself from a 742-call enumeration through the same tools. Each section
below is one row of [`gaps.jsonl`](../gaps.jsonl), quoted as logged;
`Check` names the captured response to read it back from.

Covers 1 row of `gaps.jsonl`.

## `browser-needed-assets`

- **Attempted:** look at the artefacts the payloads point to
- **Returned:** the only pointers to a structure, a PAE plot or a motif logo are URLs the tools cannot dereference: cif_url / pdb_url / pae_image_url (alphafold_structure), sequence_logo and web_url (tf_binding_motifs), web_url (resolve_locus_to_uniprot, locus_literature). Every one is a browser step.
- **Expected:** a tool that fetches the asset, or an explicit statement that these are browser-only
- **Check:** `raw/AT1G19850__alphafold_structure.json, raw/AT1G19850__tf_binding_motifs.json`
