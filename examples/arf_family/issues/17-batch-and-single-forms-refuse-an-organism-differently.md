# A refused organism is an isError on the single tool and an ok envelope on its batch form; both descriptions understate what KEGG covers

**Filed as [#139](https://github.com/musharna/plant-genomics-mcp/issues/139) — fixed.** The row is closed at `052e4ec`. Written from the ARF family dossier in
[`examples/arf_family/`](../), re-run at server commit `052e4ec`
(release `1.21.0`): 64 MCP calls over 114 genes x 16 tools, the family
itself from a 1163-call enumeration through the same tools. Each section
below is one row of [`gaps.jsonl`](../gaps.jsonl), quoted as logged;
`Check` names the captured response to read it back from.

The runner classifies a documented organism refusal as `expected`, not a gap — but it had to learn two shapes for one refusal. The descriptions also name Arabidopsis as the only organism KEGG resolves, while the error text lists seven, and the rice run did resolve.

Covers 1 row of `gaps.jsonl`.

## `batch-refusal-shape`

- **Attempted:** call kegg_pathways and batch_kegg_pathways with organism='triticum_aestivum', which KEGG does not cover
- **Returned:** the single tool is an isError result: '[OrganismNotSupported] backend kegg has no ID for triticum_aestivum'. The batch tool is ok=true with an envelope whose results is empty and whose errors carries that same message per locus - although its description says a non-supported organism 'raises OrganismNotSupported before any HTTP fan-out'. Both descriptions also say only arabidopsis_thaliana resolves, while the error lists seven supported organisms including oryza_sativa, and the rice run did resolve (queried as osa:4327785).
- **Expected:** one refusal shape for the two forms of one tool, and descriptions that match the organism list the error prints
- **Check:** `raw/_probe_batch_kegg_wheat.json, raw/Os01g0236300__kegg_pathways.json`
