# `locus_literature` promises a journal and returns null in all 160 hits

**Filed as [#134](https://github.com/musharna/plant-genomics-mcp/issues/134) — open.** Written from the ARF family dossier in
[`examples/arf_family/`](../), re-run at server commit `052e4ec`
(release `1.21.0`): 64 MCP calls over 114 genes x 16 tools, the family
itself from a 1163-call enumeration through the same tools. Each section
below is one row of [`gaps.jsonl`](../gaps.jsonl), quoted as logged;
`Check` names the captured response to read it back from.

Covers 1 row of `gaps.jsonl`.

## `promised-field-always-null`

- **Attempted:** read the journal of each literature hit, which the locus_literature description promises: 'Returns up to `size` results ... with title, authors, journal, year, DOI, PMID, open-access status, citation count, and abstract'
- **Returned:** journalTitle is a string on 350 of the 351 hits across the 114 genes (null on all 160 on the first run), so the journal the description promises comes back. The types do not: pmid is a string on 349 hits and null on 2, pubYear, isOpenAccess and hasPDF are strings on all 351, while citedByCount is an int, as are the top-level counts.
- **Expected:** the journal the description promises, and one type per kind of value
- **Check:** `raw/AT1G59750__locus_literature.json, raw/AT2G28350__locus_literature.json, raw/Os01g0236300__locus_literature.json`
