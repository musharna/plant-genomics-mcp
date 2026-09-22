# Six genes with 22-91 papers came back with hitCount 0, ok=true, and nothing in the answer says the source did not respond

**Filed as [#141](https://github.com/musharna/plant-genomics-mcp/issues/141) — open.** Written from the ARF family dossier run in
[`examples/arf_family/`](../), server version `1.21.0`, 2026-09-21
(29 genes x 16 tools, 248 MCP calls; the family itself from a 516-call
enumeration through the same tools). Each section below is one row of
[`gaps.jsonl`](../gaps.jsonl), quoted as logged; `Check` names the
captured response to read it back from.

The run captured empty literature for 6 of 23 Arabidopsis loci; re-asked later, every one has papers. The captured `gene_report` for AT1G19850 carries the same empty step. Whether Europe PMC returned zero or the server's cache served a bad answer cannot be told from the output — origin `unverified` — but the shape is the defect: an empty list with no upstream status is a silent failure.

Covers 1 row of `gaps.jsonl`.

## `silent-empty-result`

- **Attempted:** read the literature for every Arabidopsis member through batch_locus_literature, and again through gene_report's own locus_literature step
- **Returned:** hitCount 0, returned 0 and an empty hits list for 6 of the 23 loci in the run (AT1G19850, AT1G59750, AT2G46530, AT4G23980, AT5G37020, AT5G62000), with ok=true; gene_report's literature step for AT1G19850 in the same run is also empty (150 B). Two rice loci, Os02g0141100 and Os08g0520550, also came back hitCount 0 and were not re-asked. Re-asked 40 minutes later, the single tool answers hitCount 91 / 34 / 22 / 24 / 69 / 69 and the batch tool the same. Nothing in the empty answer distinguishes 'no papers' from 'Europe PMC did not answer this time'; whether the zero came from upstream or from the server's cache cannot be told from the output.
- **Expected:** an upstream status on every answer, so an empty list carries whether the source answered
- **Check:** `raw/AT1G19850__locus_literature.json, raw/AT1G19850__gene_report.json, raw/_probe_literature_recheck.json, raw/Os02g0141100__locus_literature.json`
