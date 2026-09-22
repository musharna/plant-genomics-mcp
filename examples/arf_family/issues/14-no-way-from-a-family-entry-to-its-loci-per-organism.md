# No way from an InterPro entry to its loci in an organism: the walk that stands in costs 52 region calls, 325 domain calls and 19 minutes

**Filed as [#124](https://github.com/musharna/plant-genomics-mcp/issues/124) — open.** Written from the ARF family dossier run in
[`examples/arf_family/`](../), server version `1.21.0`, 2026-09-21
(29 genes x 16 tools, 248 MCP calls; the family itself from a 516-call
enumeration through the same tools). Each section below is one row of
[`gaps.jsonl`](../gaps.jsonl), quoted as logged; `Check` names the
captured response to read it back from.

Task 7 of the dossier scaled the three starting loci to the whole family through the tools alone. The brief's route — close over `gramene_homologs` paralogs — stops at 6 loci because the paralog projection is empty for two of the three seeds (`paralog-closure-empty`, origin unverified from inside the MCP). What did work is a genome walk with `ensembl_region_query`, a free-text candidate filter, and `interpro_domains` as the arbiter on every candidate; the two rows below are what that route costs and what it needs that no tool provides. The free-text filter cannot be repeated in rice or wheat, where the description field is null (`free-text-null-outside-arabidopsis`, an upstream value).

Covers 5 rows of `gaps.jsonl`.

## `family-enumeration-cost`

- **Attempted:** list every Arabidopsis gene carrying InterPro entry IPR010525 with no tool that takes an entry accession (see no-family-enumeration), by walking the genome with ensembl_region_query and asking interpro_domains about every protein-coding gene whose free-text description or external_name mentions auxin, ARF or B3
- **Returned:** 52 ensembl_region_query calls (5 chromosomes in 4 Mb windows, 8 of the 52 failed: HTTP 500 or ReadTimeout after the tool's own three retries, a 5 Mb window never answers) and 325 interpro_domains calls, 19 minutes and 30.6 MB on the wire, to find 23 members among 325 candidates; each 4 Mb window is 1.1-1.2 MB on the wire. The candidate filter is Ensembl's free text (see free-text-not-a-label), so a member whose description names neither term is not found and nothing in the output says how many those are.
- **Expected:** a tool that takes IPR010525 (or PTHR31384) and an organism and returns the loci
- **Check:** `examples/arf_family/enumeration_calls.jsonl, examples/arf_family/family_candidates.tsv`

## `no-assembly-metadata`

- **Attempted:** learn the chromosome names and lengths of an assembly before walking it with ensembl_region_query
- **Returned:** no tool reports seq-region names or lengths. The walk learns them from error text: region '6' answers HTTP 400 'No slice found for location 6:1-4000000', and a window past the end answers HTTP 400 'Cannot request a slice whose start (32000001) is greater than 30427671 for 1.' - the length is only in that sentence.
- **Expected:** a tool, or a field on ensembl_plants_lookup_locus, that lists an assembly's seq-regions and their lengths
- **Check:** `examples/arf_family/enumeration_calls.jsonl`

## `paralog-closure-empty`

- **Attempted:** enumerate the family by closing over gramene_homologs(homology_type='paralog') from the three seed loci, as enumerate_family.py stage 1
- **Returned:** total 0 and an empty homologs list for AT1G19850 and AT1G59750; 3 hits for AT2G28350 (AT4G30080, AT1G77850, ATMG00940), whose own paralog lists point back at each other. The closure from the three seeds stops at 6 loci. The same locus with homology_type='all' reports total 177 with every one of the 100 returned rows an ortholog. src/plant_genomics_mcp/gramene.py:66 keeps rows typed within_species_paralog / between_species_paralog; whether Gramene's fl=homology projection for these loci carries no such rows, or carries them past the 100-row cap, cannot be told from the tool's output.
- **Expected:** the in-species paralogs of a locus, or a statement that the projection has none
- **Check:** `examples/arf_family/enumeration_calls.jsonl, examples/arf_family/family_candidates.tsv, raw/AT1G19850__gramene_homologs.json`

## `free-text-null-outside-arabidopsis`

- **Attempted:** repeat the region walk's candidate filter in rice and wheat
- **Returned:** ensembl_region_query 1:1-1000000 on oryza_sativa returns 165 protein-coding genes with description on 1; 1A:1-1000000 on triticum_aestivum returns 17 protein-coding genes with description on 0. The free-text candidate filter that found 17 of the 23 Arabidopsis members (the other 6 came from the seeds and the paralog closure) has nothing to read in either genome, so the walk cannot be repeated there without asking interpro_domains about every gene.
- **Expected:** a structured family or domain field per gene, the same in every organism
- **Check:** `raw/_probe_region_descriptions.json`

## `candidate-undecidable`

- **Attempted:** decide 560 family candidates with interpro_domains, as enumerate_family.py stage 3
- **Returned:** 58 of the 560 could not be decided, 56 wheat and 2 Arabidopsis (first run: 10 of 325, 8 wheat and 2 Arabidopsis). The 2 Arabidopsis, AT1G01335 and AT2G36920, answer 'InterPro entry/protein → HTTP 204: ' - an empty body from InterPro, surfaced as a failed call with no entry list. They are recorded as kept=undecided in family_candidates.tsv, not as rejected, and the 23-member count carries that margin; whether InterPro has no record for these proteins or answered empty this once cannot be told from the output. The 56 wheat loci are undecided for a different reason: they never resolved to a protein (wheat-locus-unresolvable).
- **Expected:** a found=false answer with a stated reason, so 'no record' and 'no answer' are different results
- **Check:** `examples/arf_family/family_candidates.tsv, examples/arf_family/enumeration_calls.jsonl`
