# `gene_report`: payloads carried twice, per-step timings always null, two names for one gene, GO bullets repeated

**Draft — not filed.** Written from the 48-call ARF dossier run in
[`examples/arf_family/`](../), server version `1.21.0`, 2026-09-21
(3 genes x 16 tools). Each section below is one row of
[`gaps.jsonl`](../gaps.jsonl), quoted as logged; `Check` names the
captured response to read it back from.

One issue: four defects in one envelope, all readable from the same captured response, and the first is why the tool trips the runner's size threshold.

Covers 4 rows of `gaps.jsonl`.

## `payload-duplicated`

- **Attempted:** read gene_report's answer
- **Returned:** every sub-tool payload appears twice in one envelope: the eight steps[].result payloads total 58,099 B and the eight result.sections payloads total the same 58,099 B, byte-identical per tool. The description documents result.sections as 'a structured result.sections mirror' of the markdown, but not the third copy under steps[]. The human-readable result.markdown is 5,695 B (5,642 characters). On the wire the three gene_report calls were 252,044 / 211,826 / 208,165 bytes - the only three of 48 calls over the 200 kB threshold.
- **Expected:** the sub-payloads once, or a flag to drop the raw steps
- **Check:** `raw/AT1G19850__gene_report.json`

## `null-field`

- **Attempted:** read gene_report's per-step timings
- **Returned:** steps[].elapsed_s is null for all 8 steps of all 3 genes (24/24), while the envelope's own top-level elapsed_s is populated (2.92 / 2.50 / 3.77). steps[].error is likewise null throughout, which here is correct.
- **Expected:** a per-step number or no field at all
- **Check:** `raw/AT1G19850__gene_report.json, raw/AT1G59750__gene_report.json, raw/AT2G28350__gene_report.json`

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

The dossier runner logs any response over its 200,000-byte threshold. `gene_report` is the only tool that crosses it, and it crosses on every gene: AT1G19850 252044 bytes, AT1G59750 211826 bytes, AT2G28350 208165 bytes. The largest of the other 45 calls is `aragwas_associations` on AT1G19850 at 133,374 bytes; the smallest is `experimental_structures` on AT2G28350 at 359 bytes.

Every byte figure in this draft is the size of the JSON-RPC response line, which carries each payload as both `content[].text` and `structuredContent`; the files in `raw/` are the parsed payload, re-indented. The payload-level totals above (58,099 B) are `json.dumps` of the parsed payload's own entries, and `result.markdown`'s 5,695 B is its UTF-8 byte length (5,642 characters), both read back from `raw/AT1G19850__gene_report.json`.
