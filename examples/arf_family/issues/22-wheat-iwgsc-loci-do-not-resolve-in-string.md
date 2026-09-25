# Every wheat IWGSC locus is a 404 in STRING, though the same loci resolve to UniProt

**Filed as [#155](https://github.com/musharna/plant-genomics-mcp/issues/155) — fixed.** The row is closed at `2fc3f92`. Written from the ARF family dossier in
[`examples/arf_family/`](../), re-run at server commit `052e4ec`
(release `1.21.0`): 64 MCP calls over 114 genes x 16 tools, the family
itself from a 1163-call enumeration through the same tools. Each section
below is one row of [`gaps.jsonl`](../gaps.jsonl), quoted as logged;
`Check` names the captured response to read it back from.

Since #138 every wheat locus resolves to a UniProt accession, and every protein-level tool but one answers for wheat. `string_interactions` sends the IWGSC id to STRING as it is and gets a 404 for all 66. Whether STRING indexes these proteins under another id cannot be told from inside the MCP, so the row's origin is `unverified`.

Covers 1 row of `gaps.jsonl`.

## `wheat-string-unresolvable`

- **Attempted:** string_interactions for the 66 wheat members
- **Returned:** all 66 fail the same way: '[NotFoundError] STRING /api/json/interaction_partners → HTTP 404: [{ "Error" : "not found", "ErrorMessage" : "<p>Sorry, STRING did not find a protein called 'TraesCS...'. The IWGSC id goes to STRING as it is; the same loci resolve to UniProt accessions (resolve_locus_to_uniprot, 66 of 66), which the tool also accepts as input. Whether STRING indexes these proteins under another id cannot be told from the output.
- **Expected:** either a mapping from IWGSC gene ids to the identifier STRING indexes, or an error that says the id form is not indexed
- **Check:** `examples/arf_family/raw/TraesCS3A02G449300__string_interactions.json, examples/arf_family/raw/TraesCS3A02G449300__resolve_locus_to_uniprot.json`
