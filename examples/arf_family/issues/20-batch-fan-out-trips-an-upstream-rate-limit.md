# `batch_locus_call` fans out 8 wide, and OrthoDB refuses the excess as a rate limit

**Filed as [#153](https://github.com/musharna/plant-genomics-mcp/issues/153).** Written from the ARF family dossier in
[`examples/arf_family/`](../), re-run at server commit `052e4ec`
(release `1.21.0`): 64 MCP calls over 114 genes x 16 tools, the family
itself from a 1163-call enumeration through the same tools. Each section
below is one row of [`gaps.jsonl`](../gaps.jsonl), quoted as logged;
`Check` names the captured response to read it back from.

The generic batch form added for #131 runs every tool at the server's one concurrency width. OrthoDB answers the excess with HTTP 403 "too high request rate", which the retry loop does not treat as retryable, so the refusals come back as per-locus errors inside an ok envelope. The width decides it: the same batch answers in full at width 1, three times out of three.

Covers 1 row of `gaps.jsonl`.

## `batch-fanout-rate-limit`

- **Attempted:** batch_locus_call(tool='orthodb_orthologs') over the 23 Arabidopsis, 25 rice and 66 wheat members, as the chain's orthodb_orthologs step
- **Returned:** 87 of the 114 loci fail with '[...] OrthoDB /current/search → HTTP 403: Your query was rejected. The reason can be any of the ... below: too high request rate ...', inside an ok envelope. The fan-out width decides it: the same 23-locus Arabidopsis batch answers 23 of 23 with the server started with PLANT_GENOMICS_MCP_BATCH_CONCURRENCY=1 and 7 of 23 at the default 8 (raw/_probe_batch_concurrency.json; 8 of 23 and 6 of 23 in two earlier repeats). panther_family and gene_report, which lost answers in the same run to an upstream outage, answer in full at both widths. A 403 is not in the retried statuses, so nothing backs off.
- **Expected:** a fan-out that stays under each backend's rate limit, or a rate-limit refusal that is retried rather than returned as a per-locus error
- **Check:** `examples/arf_family/raw/_probe_batch_concurrency.json, examples/arf_family/raw/_batch__batch_locus_call__orthodb_orthologs__arabidopsis_thaliana__1.json`
