# P3 mcp re-run on plant-genomics-mcp 1.30.0: EXPLORATORY, 2026-10-26

Pre-registered in the amendment of 2026-10-26 (c55c822), written before the
run. Sonnet 5.5, 1 rep, `plant-genomics-mcp==1.30.0`,
`--exclude-newer 2026-10-27T00:05:00Z`. Cost $0.52. Rows: `results.jsonl`;
setup: `versions.json`.

| arm | right | items right | attempted | coverage |
| --- | --- | --- | --- | --- |
| web (pilot) | 0/10 | 0/250 | 0 | 0 |
| mcp 1.29.3 (pilot) | 0/10 | 0/250 | 0 | 0 |
| **mcp 1.30.0** | **9/10** | **248/250** | 250 | 1.0 |

On the 1.30.0 arm, every run called `maize_gene_model_translation` and then
`uniprot_id_mapping`, two calls in all. Runs took 17–35 s; none was invalid
and none timed out.

**Versus the web arm** (10/10 UNKNOWN, no tool call): 9 discordant questions,
all for mcp. The exact two-sided sign test gives p = 0.0039. The prediction was
at least 7/10, and it held. This is exploratory: the tools were built after
the 1.29.3 arm declined these questions, and the key came from the same
sources they read.

**The one miss, p3_06.** All 25 v5 sets are right. Two items have the wrong
accession set, and they are this question's only two v4 ids that split into two
v5 genes (Zm00001d008862, Zm00001d003620). In both, the agent gave exactly the
key's accessions but wrote one gene's apart from the other's with `;`
(`[A0A804UFR9; A0A804QLF0]`). The stated format separates items with
semicolons, so the scorer keeps only the first accession. That is a format
violation, and it is scored wrong as pre-registered. None of risks 1–3 in the
amendment occurred.

**Not shown.** Whether this holds on ids not seen while building the tools.
That is the confirmatory run on fresh draws, under its own amendment.
