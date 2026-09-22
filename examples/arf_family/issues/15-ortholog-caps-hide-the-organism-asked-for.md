# Both ortholog tools cap at 100 rows with no target-organism filter, so rice and wheat never come back

**Draft — not filed.** Written from the ARF family dossier run in
[`examples/arf_family/`](../), server version `1.21.0`, 2026-09-21
(29 genes x 16 tools, 248 MCP calls; the family itself from a 516-call
enumeration through the same tools). Each section below is one row of
[`gaps.jsonl`](../gaps.jsonl), quoted as logged; `Check` names the
captured response to read it back from.

From 23 Arabidopsis members the two ortholog tools were asked for rice and wheat orthologs. `gramene_homologs` reports totals of 177-340 per gene and returns 100 rows from other species first; `orthodb_orthologs` returns 100 of 1,986 members alphabetically by organism and never reaches Oryza or Triticum. The rice members in `genes.tsv` are the few that happened to fall inside Gramene's first 100; the wheat set is empty for a second reason (`issues/16`).

Covers 2 rows of `gaps.jsonl`.

## `ortholog-cap-hides-organisms`

- **Attempted:** find the rice and wheat orthologs of each of the 23 Arabidopsis members with gramene_homologs(ortholog) and orthodb_orthologs, as enumerate_family.py stage 4
- **Returned:** gramene_homologs: total 177 / 211 / 340 for the three seeds, 100 returned, and zero rows shaped like a rice or wheat locus among those 100 for any of them; across all 26 queries 12 named a rice locus and 16 a wheat locus, 2,538 returned rows were other species. orthodb_orthologs: member_count 1986 for AT1G19850, 100 returned, the members ordered by organism name from 'Abrus precatorius' to 'Lupinus albus' (13 of organism_count 365) on every query, so Oryza and Triticum never come back - 0 hits in 26 queries. Neither tool has an organism filter for the target side or a page past the cap.
- **Expected:** a target-organism filter on both tools, or pagination, so a cap of 100 cannot silently exclude the organism asked for
- **Check:** `examples/arf_family/ortholog_sources.tsv, raw/AT1G19850__gramene_homologs.json, raw/AT1G19850__orthodb_orthologs.json`

## `ortholog-tools-disagree`

- **Attempted:** reconcile the two ortholog tools' answers per Arabidopsis member
- **Returned:** one disagreement class, not a per-gene one: gramene_homologs names a rice or wheat locus for 16 of 26 queries; orthodb_orthologs names one for 0 of 26. Every disagreement is the cap-by-order effect above, so the union is gramene's answer alone and the table cannot say whether OrthoDB agrees.
- **Expected:** both tools answering for the organism asked, so agreement is measurable
- **Check:** `examples/arf_family/ortholog_sources.tsv`
