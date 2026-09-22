# `locus_literature` promises a journal and returns null in all 30 hits

**Draft — not filed.** Written from the 48-call ARF dossier run in
[`examples/arf_family/`](../), server version `1.21.0`, 2026-09-21
(3 genes x 16 tools). Each section below is one row of
[`gaps.jsonl`](../gaps.jsonl), quoted as logged; `Check` names the
captured response to read it back from.

Covers 1 row of `gaps.jsonl`.

## `promised-field-always-null`

- **Attempted:** read the journal of each literature hit, which the locus_literature description promises: 'Returns up to `size` results ... with title, authors, journal, year, DOI, PMID, open-access status, citation count, and abstract'
- **Returned:** journalTitle is JSON null in all 30 hits across the three genes - never a string. src/plant_genomics_mcp/europe_pmc.py:91 projects the hit with a flat `{k: hit.get(k) for k in \_HIT_FIELDS}` including the key 'journalTitle'; \_normalize's docstring says it 'Keeps null fields explicit', so the null reads as a deliberate absent-value rather than as a key that never matched. Separately, pmid ('40093823'), pubYear ('2025'), isOpenAccess ('Y') and hasPDF ('Y') come back as strings while citedByCount is an int and the top-level hitCount and returned are ints.
- **Expected:** the journal the description promises, and one type per kind of value
- **Check:** `raw/AT1G19850__locus_literature.json, raw/AT1G59750__locus_literature.json, raw/AT2G28350__locus_literature.json`
