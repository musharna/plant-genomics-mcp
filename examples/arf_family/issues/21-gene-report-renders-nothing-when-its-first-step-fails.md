# `gene_report` answers ok with an empty result when its Ensembl step fails, where the description promises a degraded dossier

**Filed as [#154](https://github.com/musharna/plant-genomics-mcp/issues/154) — fixed.** The row is closed at `2fc3f92`. Written from the ARF family dossier in
[`examples/arf_family/`](../), re-run at server commit `052e4ec`
(release `1.21.0`): 64 MCP calls over 114 genes x 16 tools, the family
itself from a 1163-call enumeration through the same tools. Each section
below is one row of [`gaps.jsonl`](../gaps.jsonl), quoted as logged;
`Check` names the captured response to read it back from.

The Ensembl failures themselves were an upstream outage and did not reproduce ten minutes later. What the tool did with them is the defect: an ok answer with no markdown and no sections, six steps skipped, and a payload under `steps[]`, which the description says carries status and timing only.

Covers 1 row of `gaps.jsonl`.

## `failed-step-empties-report`

- **Attempted:** read gene_report for the 25 rice members
- **Returned:** for 17 rice loci and 1 wheat locus the first step (ensembl_plants_lookup_locus) failed with an upstream HTTP 500 during the run, and the answer is ok with result {} - no markdown, no sections - and the six later steps 'skipped' ('phase-1 ensembl lookup failed; skipped'), while the UniProt step's payload sits under steps[1].result. The description says 'Any single backend failure degrades that section to an Unavailable note; the rest of the dossier still renders' and that steps[] 'carries status and per-step timing only'. Asked again 10 minutes later, all 25 rice reports render (raw/_probe_batch_concurrency.json), so the failure was upstream; the empty answer to it is the tool's.
- **Expected:** the documented degraded dossier: the Ensembl section marked Unavailable and the rest rendered from the steps that can run without it, or an error rather than ok with an empty result
- **Check:** `examples/arf_family/raw/Os01g0236300__gene_report.json, examples/arf_family/raw/_probe_batch_concurrency.json`
