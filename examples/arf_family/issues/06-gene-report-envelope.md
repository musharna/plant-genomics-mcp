# `gene_report`: payloads carried twice, per-step timings always null, two names for one gene, GO bullets repeated

**Filed as [#122](https://github.com/musharna/plant-genomics-mcp/issues/122) — fixed.** All 4 rows are closed at `967bc36`. Written from the ARF family dossier in
[`examples/arf_family/`](../), re-run at server commit `052e4ec`
(release `1.21.0`): 64 MCP calls over 114 genes x 16 tools, the family
itself from a 1163-call enumeration through the same tools. Each section
below is one row of [`gaps.jsonl`](../gaps.jsonl), quoted as logged;
`Check` names the captured response to read it back from.

One issue: four defects in one envelope, all readable from the same captured response, and the first is why the tool trips the runner's size threshold.

Covers 4 rows of `gaps.jsonl`.

## `payload-duplicated`

- **Attempted:** read gene_report's answer
- **Returned:** every sub-tool payload appears twice in one envelope: for AT1G19850 the eight steps[].result payloads total 36,021 B and the eight result.sections payloads total the same 36,021 B, byte-identical per tool, and the equality holds on all 29 genes. The description documents result.sections as 'a structured result.sections mirror' of the markdown, but not the third copy under steps[]. The human-readable result.markdown is 3,153 B (3,122 characters) for that gene. On the wire gene_report was 61,513-247,929 bytes across the 29 genes and over the 200 kB threshold on 6 of them.
- **Expected:** the sub-payloads once, or a flag to drop the raw steps
- **Check:** `raw/AT1G19850__gene_report.json`

## `null-field`

- **Attempted:** read gene_report's per-step timings
- **Returned:** steps[].elapsed_s is null for all 8 steps of all 29 genes (232/232), while the envelope's own top-level elapsed_s is populated (0.87 for AT1G19850). steps[].error is likewise null throughout, which here is correct.
- **Expected:** a per-step number or no field at all
- **Check:** `raw/AT1G19850__gene_report.json, raw/Os01g0236300__gene_report.json`

## `inconsistent-name`

- **Attempted:** read the gene's name out of gene_report
- **Returned:** result.canonical_gene_name is 'MP' for AT1G19850 (taken from Ensembl display_name) and the markdown title reads '# MP — `AT1G19850`'. The same report's own protein section, from resolve_locus_to_uniprot, carries recommendedName 'Auxin response factor 5' and geneNames ['ARF5'].
- **Expected:** one name, or both names labelled by source
- **Check:** `raw/AT1G19850__gene_report.json`

## `duplicate-rendering`

- **Attempted:** count GO terms from gene_report's markdown
- **Returned:** 23 GO bullets, 20 distinct lines, 14 distinct GO ids. '- [GO:0005515] protein binding (IPI)' appears 3 times byte-identically and '- [GO:0003700] DNA-binding transcription factor activity (ISS)' twice; the repeats carry no distinguishing reference or source.
- **Expected:** one bullet per (term, evidence) pair, or the reference that makes the repeats distinct
- **Check:** `raw/AT1G19850__gene_report.json`

## The runner's own flag (`gaps_auto.jsonl`)

The dossier runner logs any response over its 200,000-byte threshold. `gene_report` crosses it on 6 of the 29 genes (largest AT3G61830 at 247,929 bytes; AT1G19850 is 158,103 bytes in this run, with an empty literature step — see `silent-empty-result` in the gap log). Three batch calls over the 23 Arabidopsis loci cross it too: `batch_gramene_homologs` 506,333 bytes, `batch_locus_go_annotations` 651,444 bytes, `batch_locus_literature` 585,924 bytes. The smallest of the 248 calls is `aragwas_associations` on ATMG00940 at 174 bytes.

Every byte figure in this draft is the size of the JSON-RPC response line, which carries each payload as both `content[].text` and `structuredContent`; the files in `raw/` are the parsed payload, re-indented. The payload-level totals above (36,021 B) are `json.dumps` of the parsed payload's own entries, and `result.markdown`'s 3,153 B is its UTF-8 byte length (3,122 characters), both read back from `raw/AT1G19850__gene_report.json`.
