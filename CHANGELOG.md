# Changelog

## v0.4.0 — 2026-05-21

- Add `plantcyc_locus_info` tool — informational stub. BioCyc PLANT orgid returns 404 for per-locus REST (live probe 2026-05-21); SRI/Phoenix subscription required. Returns a structured redirect to `ensembl_plants_lookup_locus` and `phytozome_lookup_locus`. MetaCyc parent (`META` orgid) is publicly accessible but lacks Arabidopsis gene mappings.

## v0.3.0 — 2026-05-21

- Add `tair_locus_info` tool — informational stub. TAIR's free per-locus REST API was retired (live probe 2026-05-21: public `arabidopsis.org` is a Vue SPA shell; `/api/*` endpoints return 403, gated by Phoenix Bioinformatics subscription). Returns a structured redirect to the live Ensembl Plants and Phytozome backends, which cover the same Arabidopsis annotation.

## v0.2.0 — 2026-05-21

- Add `phytozome_lookup_locus` tool — async Phytozome BioMart XML POST client (`phytozome-next.jgi.doe.gov`). Default `organism_id=167` (Arabidopsis thaliana TAIR10, live-verified). `KNOWN_ORGANISMS` dict ships 9 additional unverified hints (Glycine max, Sorghum bicolor, Brachypodium distachyon, Manihot esculenta, Eucalyptus grandis, Populus trichocarpa, Phaseolus vulgaris, Chlamydomonas reinhardtii, Daucus carota). Detects BioMart's `Query ERROR:` and empty-results idioms.

## v0.1.0 — 2026-05-21

- Initial release. `ensembl_plants_lookup_locus` tool — async httpx client for `rest.ensembl.org/lookup/id/{locus}?species={species}` with 429/5xx retry and exponential backoff. Default species `arabidopsis_thaliana`.
