# `locus_go_annotations` drops a row without a flag and ships a dedup the payload does not label

**Filed as [#132](https://github.com/musharna/plant-genomics-mcp/issues/132) — open.** Written from the ARF family dossier run in
[`examples/arf_family/`](../), server version `1.21.0`, 2026-09-21
(29 genes x 16 tools, 248 MCP calls; the family itself from a 516-call
enumeration through the same tools). Each section below is one row of
[`gaps.jsonl`](../gaps.jsonl), quoted as logged; `Check` names the
captured response to read it back from.

One issue: both rows are about the same three numbers in the same payload — `numberOfHits` 51, `returned` 50, `by_aspect` 16 — and a reader holding only the response cannot tell which difference is a cap and which is a dedup.

Covers 2 rows of `gaps.jsonl`.

## `silent-truncation`

- **Attempted:** read every GO annotation for a gene
- **Returned:** locus_go_annotations reports numberOfHits 51 and returned 50, and the annotations list holds 50. There is no `truncated` field, unlike orthodb_orthologs, gramene_homologs, experimental_structures, interpro_domains and tf_binding_motifs which all carry one. One annotation is dropped with no flag.
- **Expected:** a truncation flag wherever the returned count is below the upstream count
- **Check:** `raw/AT1G19850__locus_go_annotations.json`

## `dedup-not-in-payload`

- **Attempted:** reconcile locus_go_annotations' by_aspect rollup with its annotations list from the payload alone
- **Returned:** by_aspect holds 16 entries (biological_process 10, molecular_function 5, cellular_component 1) beside 50 annotations and numberOfHits 51. The 16 goIds are set-equal to the 50 annotations' distinct goIds, so by_aspect is a dedup - which the tool description does state ('a by_aspect rollup ... deduped on goId'). Nothing in the payload itself says so, so a reader working from a captured response alone cannot tell a dedup from a truncation.
- **Expected:** the payload to mark by_aspect as deduplicated, as the description already does
- **Check:** `raw/AT1G19850__locus_go_annotations.json`
