# P3 CONFIRMATORY on held-out draws: CONFIRMED, 2026-10-26

Pre-registered in `PREREG.md` (58f3fff), with the key's sha256 committed before
any run (463cd8b, `745ce97b…`; the lane checks it before starting).
Sonnet 5.5, 1 rep, 10 questions (`p3c_01`–`p3c_10`, 250 ids none of which was
seen while building the tools). Rows: `results.jsonl`; setup: `versions.json`.

| arm | right | items right | attempted | invalid | cost |
| --- | --- | --- | --- | --- | --- |
| web (WebSearch + WebFetch) | 0/10 | 0/250 | 0 | 10 (no tool call) | $0.06 |
| **mcp, plant-genomics-mcp 1.30.0** | **10/10** | **250/250** | 250 | 0 | $0.45 |

**Primary test:** 10 discordant questions, all for mcp (10:0). The exact
two-sided sign test gives p = 0.00195 < 0.05. **Confirmed** under the
pre-registered rule. Both expectations held: web about 0/10, mcp at least 7/10.

Every mcp run made the same two calls, `maize_gene_model_translation` and then
`uniprot_id_mapping`, in 22–33 s. The format slip seen in the exploratory
p3_06 did not recur; this key has 5 items that split into two v5 genes, against
10 in the pilot key.

**What this does and does not show.** The web arm did not attempt any
question. Each reply said, in effect, that web pages cannot run MaizeGDB's
table or UniProt's ID-mapping job, and answered UNKNOWN. So the result is
that the server makes a batch v4 → v5 + UniProt task answerable, exactly,
where a web-search agent declines it. It is not a comparison against an
agent that tried and got some items right. The key and the tools read the
same sources (MaizeGDB, UniProt 2026_03), so it measures whether the lookups
are done completely and correctly, not whether those databases are right.
