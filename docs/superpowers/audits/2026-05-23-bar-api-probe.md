# BAR / Araport API — Live Probe (Wave A.5a)

**Date:** 2026-05-23
**Trigger:** Pre-1.0 audit Wave A.5 — user pushback on conservative v1.0 versioning ("TAIR is a big resource in plant genomics, let's probe"). BAR (Bio-Analytic Resource for Plant Biology, U Toronto) is the surviving public TAIR-derivative front door after the 2018 Phoenix Bioinformatics paywall.
**Output of:** Wave A.5a only (probe). Wave A.5b (value-add map) and A.5c (GREENLIGHT vs DEFER decision) follow.

---

## TL;DR

BAR is **alive, keyless, free, and well-funded** (Global Core Biodata Resource 2023; NSERC + Genome Canada OGI-162). The published Swagger advertises 55 endpoints across ~12 modules. Live probe finds **~16 of those endpoints actually return useful data**; the rest are decommissioned, rice-only, or empty for any input we tested.

**The framing shift vs the pre-probe hypothesis:**

- _Hypothesis going in:_ "BAR fills the TAIR gap for v1.0, broadens multi-species coverage."
- _Probe reality:_ BAR's strongest value-adds are **Arabidopsis-only**. Multi-species support is genuinely thin — only `/interactions/` works for rice, the rest is single-organism `/thalemine/` and `/microarray_gene_expression/` wrappers. **BAR is an Arabidopsis depth backend, not a multi-organism breadth backend.**

This changes the v1.0 framing question from "ship BAR to broaden organism coverage" to "ship BAR to upgrade `tair_locus_info` from subscription-required stub to a real Curator Summary / Aliases / Publications source for Arabidopsis."

---

## Endpoint surface — what works

All bases: `https://bar.utoronto.ca/api/`, plus the underlying InterMine API at `https://bar.utoronto.ca/thalemine/service/`.

### TAIR-equivalent gene metadata (Arabidopsis only)

| Endpoint                                            | Returns                                                                                                                          |
| --------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `GET /thalemine/gene_information/AT1G01010`         | Symbol, full name, locus_id, Araport11 computed name, **Curator Summary** prose, brief description, type                         |
| `GET /thalemine/publications/AT1G01010`             | ~20 publications: first author, journal, year, vol/issue/pages, PubMed ID                                                        |
| `GET /thalemine/gene_rifs/AT1G01010`                | GeneRIFs (NCBI RIF-style sentence-level functional annotations)                                                                  |
| `GET /gaia/aliases/AT1G01010`                       | Species, NCBI Gene ID, full alias list (`NAC001`, `ANAC001`, RefSeq `NM_099983`, UniProt `Q0WV96`, locus model `T25K16.1`, etc.) |
| `GET /gene_information/single_gene_query/AT1G01010` | Same as `/thalemine/gene_information/` (alternate path)                                                                          |
| `POST /gene_information/gene_query`                 | Batch version of single_gene_query — body `{"species": "arabidopsis", "terms": [...]}`                                           |

**This is the v0.9 TAIR-stub gap closed.** The current `tair_locus_info` returns `subscription_required` and points elsewhere; BAR's `/thalemine/gene_information/` returns the actual Curator Summary + Computational Description for free.

### Expression (Arabidopsis only)

| Endpoint                                                          | Returns                                                                                                                                      |
| ----------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| `GET /microarray_gene_expression/world_efp/arabidopsis/AT1G01010` | eFP browser data — 14 accessions (Bay-0, C24, Col-0, …), lat/lng, probe sets (`261585_at`), sample IDs (`ATGE_111_A/B/C`), expression values |

**No analog in v0.9.** ATTED-II gives co-expression _ranks_; this gives _expression magnitudes per accession_. Distinct primitive.

### Protein-protein interactions

| Endpoint                                       | Returns                                                                                                                  |
| ---------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| `GET /interactions/get_paper_by_agi/AT1G01010` | **Curated** GRN papers — Ikeuchi 2018 (PMID 29462363, wound response), Sparks 2016, plus GRN image URLs and cyjs layouts |
| `GET /interactions/rice/LOC_Os01g01080`        | Rice PPIs — `{protein_1, protein_2, total_hits, Num_species, Quality, pcc}`                                              |
| `POST /interactions/`                          | Batch rice PPI — body `{"species": "rice", "genes": ["LOC_Os01g01080", …]}`                                              |
| `GET /interactions/all_tags`                   | Vocabulary for GRN paper tagging                                                                                         |

The Arabidopsis side is **curated experimental** (AIV — Arabidopsis Interaction Viewer); distinct from STRING's computational predictions. Rice side is the only confirmed multi-species endpoint.

### InterMine direct (Arabidopsis only)

`https://bar.utoronto.ca/thalemine/service/` exposes the standard InterMine REST surface. Three useful path queries beyond the wrapped endpoints above:

| InterMine path                                       | Returns                                                                                                  |
| ---------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| `Gene.proteins.proteinDomainRegions.proteinDomain.*` | InterPro domains with start/end coordinates (e.g. `IPR003441 NAC domain 3–156`, `IPR036093 superfamily`) |
| `Gene.goAnnotation.ontologyTerm.identifier`          | GO terms (covered by QuickGO already)                                                                    |
| `Gene.publications.pubMedId`                         | PubMed IDs (covered by Europe PMC)                                                                       |

ThaleMine instance is version 5.1.0-20250704; runs InterMine REST v35. Three organisms loaded: taxon 3702 (A. thaliana, primary), 4932 (yeast), 9606 (human) — yeast/human are present for ortholog cross-reference, not as plant data.

---

## Endpoint surface — what's broken or empty

Documenting these so a future implementer doesn't waste time:

| Endpoint pattern                          | Status                                                                                                                                                                                                                                                                                                        |
| ----------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GET /loc/{species}/{gene_id}`            | Returns `"Invalid species or gene ID"` for **every** species variant tried: `arabidopsis`, `Arabidopsis`, `athaliana`, `Athaliana`, `ARABIDOPSIS_THALIANA`, `arabidopsis_thaliana`, `Arabidopsis_thaliana`, `rice`, `soybean`. SUBA subcellular-localization wrapper appears decommissioned at the API layer. |
| `GET /sequence/{species}/{gene_id}`       | `"Invalid species"` for all variants                                                                                                                                                                                                                                                                          |
| `GET /snps/arabidopsis/{gene_id}`         | `"Invalid gene id"` even for valid AT-loci                                                                                                                                                                                                                                                                    |
| `GET /interactions/arabidopsis/{gene_id}` | Fails — only `/interactions/rice/` works for GET despite Swagger not specifying                                                                                                                                                                                                                               |
| `GET /thalemine/templates`                | 0 templates returned; 500 on XML variants                                                                                                                                                                                                                                                                     |
| `Gene.alleles`, `Gene.homologues`         | Valid InterMine paths but empty for AT1G01010 (data not loaded)                                                                                                                                                                                                                                               |

**Reachability map (HTTP status check across base endpoints):**

```
bar root: 200
thalemine root: 302 → 200
thalemine /service: 200
thalemine /service/version: 200
iodocs (Araport): 000   ← Araport.org legacy front-end is dead
araport api endpoint: 000   ← Araport.org legacy front-end is dead
eplant: 200
efp: 403 (browser-gated, but API at /microarray_gene_expression/ works)
api root: 200
webservices index: 200
```

The legacy `araport.org` front-end (the iodocs / api.araport.org host) is fully offline. The "Araport" name in BAR's Swagger refers to the **Araport11 annotation release**, not the api.araport.org service. Anything we ship has to point at `bar.utoronto.ca`, not `araport.org`.

---

## Auth, rate limit, latency

- **No authentication.** No `Authorization` header, no `X-API-Key`, no cookie required. The InterMine service sets `JSESSIONID` for its own session tracking but the client doesn't have to do anything with it.
- **No rate-limit headers.** None of `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `Retry-After` appear in 200 responses or in the few errors we saw.
- **CORS open** (`Access-Control-Allow-Origin: *`).
- **HTTPS only**, HTTP/2, Apache front-end, HSTS `max-age=63072000`.
- **Latency:** 10 sequential `/thalemine/gene_information/` calls completed in 2.83 s (~283 ms/call), all 200 OK. No throttling observed; reasonable to assume modest-batch usage (≤50 loci) is fine; the existing `RateLimitedClient` (10 req/s) is already conservative for this host.

We document this in our backend module the same way we document Phytozome's `Retry-After` semantics, but we don't need to special-case anything.

---

## Sustainability + governance

This was the decisive question for "is BAR a 1.0-grade dependency or a 1.1-experimental one":

- **Global Core Biodata Resource (GCBR) designation, 2023.** GCBR is the same tier as UniProt / Ensembl / PDB. The designation comes with a sustainability review and a published commitment to long-term operation.
- **Funded by NSERC + Genome Canada (OGI-162).** Long-term federal funding lines.
- **Run by the Provart lab at the University of Toronto**, established Arabidopsis bioinformatics group since ~2005. AIV, eFP browser, ThaleMine, ePlant all originated here.
- **Public commitment:** "excellent operational stability" stated in their 2024 NAR Database Issue paper (PMC11701662).

**This is a 1.0-grade dependency by the same standards we use for Ensembl Plants and UniProt.** No concerns on the sustainability axis.

---

## Multi-species reality check

This is where the pre-probe hypothesis didn't survive contact:

| Backend module path                      | Organism support                                               |
| ---------------------------------------- | -------------------------------------------------------------- |
| `/thalemine/*` (all wrappers)            | Arabidopsis only (taxon 3702)                                  |
| `/gene_information/*`                    | Arabidopsis only                                               |
| `/microarray_gene_expression/world_efp/` | Arabidopsis only (also one tomato endpoint)                    |
| `/gaia/aliases/`                         | Arabidopsis only                                               |
| `/interactions/` (POST batch)            | **rice only** per Swagger description                          |
| `/interactions/rice/` (GET)              | rice                                                           |
| `/interactions/get_paper_by_agi/`        | Arabidopsis only ("AGI" = Arabidopsis Genome Initiative locus) |
| `/loc/`, `/sequence/`, `/snps/`          | None — endpoints broken                                        |

**Net:** Arabidopsis-heavy, with one rice PPI lane. The Wave A5 work that wired `organism=` through 9 backends does **not** find new BAR endpoints to broaden — the multi-organism coverage stays at the 12 organisms we already support via Ensembl + Phytozome + UniProt + STRING.

What BAR _does_ add is **depth on Arabidopsis** that no other backend gives us for free.

---

## Preview of value-add map (full version in Wave A.5b)

Non-redundant capabilities BAR exposes vs the existing 9 backends:

1. **TAIR Curator Summary / Computational Description / Brief Description** — currently `tair_locus_info` returns `subscription_required`. BAR provides this for free via `/thalemine/gene_information/`. **High value.**
2. **eFP Browser expression magnitudes** — currently no backend has accession-resolved expression data; ATTED-II gives co-expression _ranks_, not magnitudes. **Distinct primitive.**
3. **AIV curated PPIs + GRN paper provenance** — STRING gives computational predictions; AIV gives experimentally-curated Arabidopsis PPIs with paper-level provenance. **Complementary to STRING.**
4. **GAIA comprehensive alias table** — overlap with `get_gene_xrefs`, but BAR's table includes locus-model IDs (`T25K16.1`) and curated synonyms that Ensembl's xref list doesn't.
5. **InterPro domain regions with positions** — `resolve_locus_to_uniprot` exposes the UniProt accession; BAR exposes the protein domain breakdown with start/end coordinates.
6. **Curated Arabidopsis publication list** — overlap with Europe PMC, but TAIR's list is curator-vetted and includes some pre-2000 references not well-indexed by Europe PMC.

Lower-value (redundant with existing backends): GO annotations (QuickGO covers), PubMed IDs (Europe PMC covers), homology (Gramene covers).

---

## Implications for Wave A.5c (GREENLIGHT vs DEFER decision)

Three live decision options for the user, surfaced cleanly:

**Option G1 — GREENLIGHT `bar.py` for v1.0 (Arabidopsis-deep framing).** Ship one backend module with three tools: `bar_gene_summary` (the TAIR-substitute), `bar_efp_expression`, `bar_aiv_interactions`. Reframe `tair_locus_info` from "subscription_required stub" to "→ see `bar_gene_summary`" or alias it directly. Estimated effort: ~1.5 days backend + tests + docs.

**Option G2 — GREENLIGHT minimal (TAIR-substitute only) for v1.0.** Just `bar_gene_summary` to close the TAIR-stub gap. eFP and AIV deferred to v1.1. Estimated effort: ~0.5 days.

**Option D — DEFER all of BAR to v1.1.** Keep v1.0 scope as it is (security hardening + API polish). Add `bar.py` post-1.0 as its own minor release. The case for this: the v0.9 multi-organism story is the headline feature; adding an Arabidopsis-only backend in v1.0 may muddle the framing.

**Asymmetry to flag:** the user's original motivation was "TAIR is a big resource, let's probe." The probe shows we _can_ ship a TAIR-equivalent for free via BAR, but it's strictly an Arabidopsis-only addition. If the v1.0 narrative is "first plant-genomics MCP with multi-organism support," adding an Arabidopsis-only backend is orthogonal to that narrative — but it _does_ remove an embarrassing stub.

Recommendation deferred to Wave A.5c with user input.

---

## Probe methodology (for replication)

- Swagger spec downloaded from `https://bar.utoronto.ca/api/swagger.json` (55 endpoints, basePath `/api`).
- 25+ live GET/POST probes against representative endpoints with `AT1G01010` (Arabidopsis) and `LOC_Os01g01080` (rice MSU format) as test loci.
- Schema mismatches debugged via Swagger `$ref` definitions (`GeneIsoforms`, `ItrnsRiceGenes`, `GeneInformation`) — POST bodies require `species` + `genes` (not `loci`), rice loci must be MSU format not RAP-DB.
- HTTP header survey: no rate-limit, no auth, CORS open, HTTP/2 + HSTS.
- Sustainability source: `PMC11701662` (BAR's 2024 NAR Database Issue paper).
