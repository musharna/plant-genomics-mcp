# plant-genomics-mcp

> **32 tools** for plant-genomics locus lookup via the Model Context Protocol —
> 16 single-locus + 12 parallel-batch + 4 cross-source synthesis variants.
> Free, public sources: Ensembl Plants + Phytozome BioMart + UniProtKB +
> Europe PMC + QuickGO + NCBI BLAST + Gramene + KEGG + STRING-DB + ATTED-II
>
> - BAR (Bio-Analytic Resource for Plant Biology, U Toronto — Global Core
>   Biodata Resource 2023). PlantCyc is an informational stub that redirects
>   to the free alternatives (BioCyc PLANT orgid is paid-subscription-gated,
>   probed 2026-05-21).

[![PyPI](https://img.shields.io/pypi/v/plant-genomics-mcp)](https://pypi.org/project/plant-genomics-mcp/)
[![CI](https://github.com/musharna/plant-genomics-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/musharna/plant-genomics-mcp/actions/workflows/test.yml)
[![Docker](https://github.com/musharna/plant-genomics-mcp/actions/workflows/docker.yml/badge.svg)](https://github.com/musharna/plant-genomics-mcp/actions/workflows/docker.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

## Tools at a glance

| #   | Category                | Tool                                    | What it does                                                                         |
| --- | ----------------------- | --------------------------------------- | ------------------------------------------------------------------------------------ |
| 1   | Gene metadata (live)    | `ensembl_plants_lookup_locus`           | Fetches gene record from Ensembl Plants REST (any plant species).                    |
| 2   | Cross-references (live) | `get_gene_xrefs`                        | Fetches cross-DB references (UniProt, NCBI Gene, TAIR, GO, …) from Ensembl.          |
| 3   | Gene metadata (live)    | `phytozome_lookup_locus`                | Fetches gene record from Phytozome BioMart (any Phytozome proteome).                 |
| 4   | Protein (live)          | `resolve_locus_to_uniprot`              | Resolves a locus to its UniProtKB record (Swiss-Prot preferred, TrEMBL OK).          |
| 5   | Literature (live)       | `locus_literature`                      | Searches Europe PMC for papers mentioning the locus (free, no API key).              |
| 6   | GO annotations (live)   | `locus_go_annotations`                  | Fetches QuickGO GO annotations (locus → UniProt → QuickGO).                          |
| 7   | Sequence search (live)  | `blast_sequence`                        | NCBI BLAST URLAPI — async Put/Get polling with progress notifications.               |
| 8   | Homology (live)         | `gramene_homologs`                      | Fetches Gramene v69 homology entries (ortholog / paralog) with gene_tree_id.         |
| 9   | Pathways (live)         | `kegg_pathways`                         | Fetches KEGG `ath:` pathway memberships for an Arabidopsis locus.                    |
| 10  | Interactions (live)     | `string_interactions`                   | Fetches STRING-DB first-neighbor interaction partners with per-channel score.        |
| 11  | Coexpression (live)     | `atted_coexpression`                    | Fetches ATTED-II Ath-u.c4-0 top-N coexpression neighbors with z-scores.              |
| 12  | Curator summary (live)  | `bar_gene_summary`                      | Fetches BAR ThaleMine + GAIA-aliases curator summary for an Arabidopsis locus.       |
| 13  | Expression (live)       | `bar_efp_expression`                    | Fetches BAR eFP-Browser expression profile (mean ± SD per tissue) for a locus.       |
| 14  | Interactions (live)     | `bar_aiv_interactions`                  | Fetches BAR AIV interaction partners (Arabidopsis + rice) with confidence + papers.  |
| 15  | Curator summary (live)  | `tair_locus_info`                       | Silent upgrade — alias of `bar_gene_summary`. MCP tool name preserved for clients.   |
| 16  | Subscription redirect   | `plantcyc_locus_info`                   | Returns subscription notice + redirect to live backends. No upstream call.           |
| 17  | Batch (live)            | `batch_*` (twelve variants)             | Parallel per-locus fanout for tools 1–6, 8–12, 14. Up to 50 loci per call.           |
| 18  | Synthesis (live)        | `*_synth` / `consensus_homologs` (four) | Compose 2–5 backends in parallel, return a `SynthesisEnvelope` with per-step status. |

Live tools take a TAIR-style locus (e.g. `AT1G01010`) plus an optional
unified `organism=` parameter (default `arabidopsis_thaliana`) and
return a structured record. `organism=` accepts a canonical slug
(`oryza_sativa`), scientific name (`Oryza sativa`), common name
(`rice`), or NCBI taxonomy ID (`39947`) — all resolve to the same
12-plant curated coverage matrix (see `pgmcp://organisms/coverage`).
Subscription tools take a locus and return a structured redirect record —
they do not call the gated upstream.

Batch variants (`batch_ensembl_plants_lookup_locus`,
`batch_get_gene_xrefs`, `batch_phytozome_lookup_locus`,
`batch_resolve_locus_to_uniprot`, `batch_locus_literature`,
`batch_locus_go_annotations`, `batch_gramene_homologs`,
`batch_kegg_pathways`, `batch_string_interactions`,
`batch_atted_coexpression`, `batch_bar_gene_summary`,
`batch_bar_aiv_interactions`) take a `loci: string[]` (1–50 items) plus
the same optional `organism=` argument. They return
`{tool, count, results, errors}` where `results[locus]` is the same
shape as the single-locus tool and `errors[locus]` is a
`[ClassName] message` string for `PlantGenomicsError` failures
(`[NotFoundError]`, `[RateLimitError]`, …). Ensembl's batch uses
the native `POST /lookup/id` endpoint (one HTTP round-trip);
everything else fans out via `asyncio.gather`.

All thirty-two tools publish JSON `outputSchema` for client-side validation
and EDAM ontology tags (`operation_2422` Data retrieval; topic
`topic_0780` Plant biology + `topic_0114` Gene structure) on `_meta`
for registry indexers.

## Resources

The server advertises four read-only MCP resources (`resources/list` +
`resources/read`); JSON unless otherwise noted:

| URI                           | MIME             | What                                                                                                           |
| ----------------------------- | ---------------- | -------------------------------------------------------------------------------------------------------------- |
| `pgmcp://cache/stats`         | application/json | Per-backend `TTLCache` rollup — `{hits, misses, size}` for each of the ten live backends                       |
| `pgmcp://organisms/phytozome` | application/json | Slug → Phytozome `organism_id` map, derived from the curated v0.9 `organisms.ORGANISMS` registry               |
| `pgmcp://backends/status`     | application/json | Per-backend liveness rollup — `name`, `base_url`, `kind` (`live` or `stub`), `subscription_gated`, `probed_at` |
| `pgmcp://organisms/coverage`  | text/markdown    | Markdown table of all 12 supported plants × 5 ID slots (ncbi_taxid, ensembl, phytozome, string, europe_pmc)    |

Useful for an operator to confirm caching is doing work without
shelling into the process, and for clients that want to enumerate
supported organisms / backends programmatically. The coverage matrix
lets a client introspect supported organism coverage in one read
instead of probing `resolve_organism` per organism.

## Prompts

The server exposes three parameterized prompts (`prompts/list` +
`prompts/get`) for one-click multi-tool workflows:

| Name                 | Required args | Optional args                               | What it chains                                                                           |
| -------------------- | ------------- | ------------------------------------------- | ---------------------------------------------------------------------------------------- |
| `analyze_locus`      | `locus`       | `organism` (default `arabidopsis_thaliana`) | Ensembl annotation → xrefs → UniProt → Europe PMC literature → QuickGO annotations       |
| `find_homologs`      | `sequence`    | `program` (default `blastp`)                | `blast_sequence` → per-hit `resolve_locus_to_uniprot` for UniProt-shaped accessions      |
| `biological_context` | `locus`       | `top_n` (default 10)                        | Gramene homologs → KEGG pathways → UniProt → STRING interactions → ATTED-II coexpression |

Clients populate their slash-command menu from `prompts/list`, so the
workflow is one user selection deep instead of requiring the user to
remember the tool ordering.

Real-execution proof transcripts of all three chains (against the live
upstream APIs) live in [`examples/`](examples/) — one JSON + Markdown
pair per prompt. v0.8 adds a 4-tool synthesis walkthrough at
[`examples/v0.8_synthesis_walkthrough.md`](examples/v0.8_synthesis_walkthrough.md),
captured by [`examples/_run_synthesis_chain.py`](examples/_run_synthesis_chain.py).

## Transports

| Transport       | Status  | How to launch                                                       |
| --------------- | ------- | ------------------------------------------------------------------- |
| stdio           | default | `plant-genomics-mcp` (after install) or via Docker below            |
| streamable-HTTP | added   | `plant-genomics-mcp-http` — POST JSON-RPC at `http://host:port/mcp` |

The HTTP transport is stateless and emits JSON responses by default —
the right shape for registry indexers and remote hosting. Env knobs:

| Variable                            | Default     | Effect                                                  |
| ----------------------------------- | ----------- | ------------------------------------------------------- |
| `PLANT_GENOMICS_MCP_HTTP_HOST`      | `127.0.0.1` | Bind address                                            |
| `PLANT_GENOMICS_MCP_HTTP_PORT`      | `8765`      | TCP port                                                |
| `PLANT_GENOMICS_MCP_HTTP_STATELESS` | `1`         | `0` keeps per-client session state (SSE-style)          |
| `PLANT_GENOMICS_MCP_HTTP_JSON`      | `1`         | `0` switches the response shape to streaming SSE events |

## Hosted endpoint

A small **personal demo** of this server runs at:

https://mjarnoldgt76.tail86d19d.ts.net/mcp

This is intended for registry indexers, one-off evaluation, and quick interactive testing — **not for production workloads**. No SLA, no uptime commitment, and the URL may change or disappear without notice. The host is a single laptop on a residential connection.

Liveness probe:

```bash
curl https://mjarnoldgt76.tail86d19d.ts.net/healthz
# {"status":"ok"}
```

Connect from Claude Code:

```bash
claude mcp add --transport http plant-genomics-mcp \
  https://mjarnoldgt76.tail86d19d.ts.net/mcp
```

For anything beyond casual evaluation — **self-host**. The HTTP transport is the same binary (see [Install](#install)). Self-hosting buys you (a) deterministic uptime, (b) the required bearer-token gate via `PLANT_GENOMICS_MCP_HTTP_TOKEN`, and (c) NCBI BLAST etiquette under your own `PLANT_GENOMICS_MCP_NCBI_EMAIL`.

> **v1.0.1 breaking change:** `plant-genomics-mcp-http` now **requires** `PLANT_GENOMICS_MCP_HTTP_TOKEN` to be set to a value of at least 32 characters. The container aborts at startup with a clear error if the env var is missing or too short. Generate one with `openssl rand -hex 32` and pass it via `env_file:` or the compose `environment:` block. v1.0.0 documented this as fail-closed but the code shipped fail-open-on-absent — v1.0.1 closes that gap. Stdio transport is unaffected.

## Install

### pipx (recommended)

```bash
pipx install plant-genomics-mcp
claude mcp add plant-genomics --scope local -- plant-genomics-mcp
```

### Docker (GHCR)

```bash
docker pull ghcr.io/musharna/plant-genomics-mcp:latest
claude mcp add plant-genomics --scope local -- \
  docker run --rm -i ghcr.io/musharna/plant-genomics-mcp:latest
```

### From source

```bash
git clone https://github.com/musharna/plant-genomics-mcp.git
cd plant-genomics-mcp
python -m venv .venv && .venv/bin/pip install -e .
claude mcp add plant-genomics --scope local -- "$(pwd)/.venv/bin/plant-genomics-mcp"
```

## Usage examples

### 1. `ensembl_plants_lookup_locus`

Fetch a gene record from Ensembl Plants. Default organism is
`arabidopsis_thaliana`; pass `organism=` for any other supported plant
(`oryza_sativa`, `zea_mays`, `solanum_lycopersicum`, ...).

```jsonc
// arguments
{ "locus": "AT1G01010" }

// result (truncated)
{
  "id": "AT1G01010",
  "organism": "arabidopsis_thaliana",
  "display_name": "NAC001",
  "biotype": "protein_coding",
  "seq_region_name": "1",
  "start": 3631,
  "end": 5899,
  "strand": 1,
  "assembly_name": "TAIR10",
  "description": "NAC domain containing protein 1 ..."
}
```

Cross-species:

```jsonc
{ "locus": "Os01g0100100", "organism": "oryza_sativa" }
```

### 2. `get_gene_xrefs`

Fetch cross-database references from Ensembl Plants. Same host /
organism conventions as `ensembl_plants_lookup_locus`. The response
wraps Ensembl's top-level array (so it validates against the MCP
`outputSchema`'s required `type=object` root) and adds a `by_db`
rollup keyed on Ensembl's `dbname` — so a chain consumer can lift
out a single foreign accession without walking the list.

```jsonc
{ "locus": "AT1G01010" }

// result (xrefs[] truncated)
{
  "locus": "AT1G01010",
  "organism": "arabidopsis_thaliana",
  "count": 8,
  "xrefs": [
    { "dbname": "Uniprot_gn", "primary_id": "Q0WV96", "info_type": "DEPENDENT" },
    { "dbname": "EntrezGene", "primary_id": "839580", "display_id": "NAC001" },
    { "dbname": "TAIR_LOCUS", "primary_id": "AT1G01010", "info_type": "DIRECT" },
  ],
  "by_db": {
    "Uniprot_gn": ["Q0WV96"],
    "EntrezGene": ["839580"],
    "TAIR_LOCUS": ["AT1G01010"],
  },
}
```

Typical chain pattern — pull the UniProt accession out of `by_db`
without parsing `xrefs[]`:

```python
xrefs = await call("get_gene_xrefs", {"locus": "AT1G01010"})
uniprot_id = xrefs["by_db"].get("Uniprot_gn", [None])[0]  # "Q0WV96"
```

This is the **cross-database pivot** — pair with `resolve_locus_to_uniprot`
for the canonical UniProt entry, or hand `by_db["EntrezGene"]` to an
NCBI sibling MCP.

### 3. `phytozome_lookup_locus`

Fetch a gene record from Phytozome BioMart. Default organism is
`arabidopsis_thaliana` (Phytozome `organism_id=167`,
controller-verified live). Pass `organism=` for any other Phytozome
proteome covered by the curated registry — see
`pgmcp://organisms/phytozome` for the slug → `organism_id` map, or
`pgmcp://organisms/coverage` for the full per-backend coverage matrix.

```jsonc
{ "locus": "AT1G01010" }

// result
{
  "organism_name": "Athaliana_TAIR10",
  "gene_name": "AT1G01010",
  "chromosome": "Chr1",
  "gene_start": "3631",
  "gene_end": "5899",
  "strand": "1",
  "description": "NAC domain containing protein 1 ..."
}
```

### 4. `resolve_locus_to_uniprot`

Resolve a plant locus to its canonical UniProtKB record. Prefers
reviewed (Swiss-Prot) entries; falls back to unreviewed (TrEMBL) when
no curated record exists — the common case for non-Arabidopsis plants.
`organism=` defaults to `arabidopsis_thaliana`; pass a canonical slug,
scientific name, common name, or NCBI taxid for any of the 12 supported
plants.

```jsonc
{ "locus": "AT1G01010" }

// result
{
  "locus_query": "AT1G01010",
  "primaryAccession": "Q0WV96",
  "uniProtkbId": "NAC1_ARATH",
  "entryType": "UniProtKB reviewed (Swiss-Prot)",
  "reviewed": true,
  "recommendedName": "NAC domain-containing protein 1",
  "geneNames": ["NAC001"],
  "organism": "Arabidopsis thaliana",
  "taxonId": 3702,
  "sequenceLength": 429,
  "web_url": "https://www.uniprot.org/uniprotkb/Q0WV96"
}
```

This is the **protein-side entry point** for any downstream workflow:
structure (AlphaFold, RCSB), domains (InterPro, PROSITE), pathways
(Reactome, the subscriber path into PlantCyc), variants (ClinVar via
the human-orthology bridge).

### 5. `locus_literature`

Search Europe PMC for literature mentioning a plant locus. Free,
no API key. Returns up to `size` records (default 10, capped at 25)
with title, authors, journal, year, DOI/PMID/PMCID, open-access status,
citation count, and abstract. For non-Arabidopsis organisms the species
common name is appended to the query (`rice`, `maize`, `tomato`, …)
to disambiguate locus IDs that might otherwise collide with unrelated
literature.

```jsonc
{ "locus": "AT1G01010", "size": 3 }

// result (hits[] truncated)
{
  "locus": "AT1G01010",
  "organism": "arabidopsis_thaliana",
  "query": "AT1G01010",
  "hitCount": 40,
  "returned": 3,
  "hits": [
    {
      "id": "41152268",
      "source": "MED",
      "pmid": "41152268",
      "pmcid": "PMC12569054",
      "doi": "10.1038/s41526-025-00525-5",
      "title": "GLARE: discovering hidden patterns in spaceflight transcriptome ...",
      "authorString": "Seo D, Strickland HF, Zhou M, ...",
      "journalTitle": "npj Microgravity",
      "pubYear": "2025",
      "citedByCount": 0,
      "isOpenAccess": "Y",
      "hasPDF": "Y",
      "web_url": "https://europepmc.org/article/PMC/PMC12569054",
    },
    // ...
  ],
}
```

This is the **literature entry point** — pair it with the protein-side
chain (`resolve_locus_to_uniprot` → AlphaFold) or the cross-DB pivot
(`get_gene_xrefs` → NCBI Gene) to ground a locus first, then fan out
to the most-cited or most-recent papers.

### 6. `locus_go_annotations`

Fetch Gene Ontology annotations for a plant locus from QuickGO (EBI).
Free, no API key. The locus is first resolved to a UniProt accession
(same logic as `resolve_locus_to_uniprot`), then QuickGO is queried
by `geneProductId`. Returns raw `annotations[]` plus a `by_aspect`
rollup (`{molecular_function: [{goId, goName}, ...], biological_process:
[...], cellular_component: [...]}`) deduped on `goId` so the high-level
term set is one read away.

```jsonc
{ "locus": "AT1G01010", "limit": 5 }

// result
{
  "locus": "AT1G01010",
  "uniprot_accession": "Q0WV96",
  "numberOfHits": 9,
  "returned": 5,
  "annotations": [
    {
      "geneProductId": "UniProtKB:Q0WV96",
      "symbol": "NAC001",
      "qualifier": "enables",
      "goId": "GO:0000976",
      "goName": "transcription cis-regulatory region binding",
      "goAspect": "molecular_function",
      "goEvidence": "IPI",
      "reference": "PMID:30356219",
      "assignedBy": "TAIR",
      "taxonId": 3702,
      "taxonName": "Arabidopsis thaliana",
      // ...
    },
    // ...
  ],
  "by_aspect": {
    "molecular_function": [
      { "goId": "GO:0000976", "goName": "transcription cis-regulatory region binding" },
    ],
    "biological_process": [
      { "goId": "GO:0006355", "goName": "regulation of DNA-templated transcription" },
    ],
    "cellular_component": [{ "goId": "GO:0005634", "goName": "nucleus" }],
  },
}
```

`[NotFoundError]` propagates from either step — a locus with no UniProt
entry can't be queried in QuickGO, so the caller gets a typed error
rather than an empty result that hides the resolution failure.

### 7. `tair_locus_info` (BAR alias) / 8. `plantcyc_locus_info`

`tair_locus_info` is a **silent upgrade** (v0.10.0) — the MCP tool name
is preserved for client compatibility, but the body now delegates to
`bar_gene_summary` instead of returning a subscription-required redirect.
BAR (Bio-Analytic Resource for Plant Biology, U Toronto — Global Core
Biodata Resource 2023) mirrors the TAIR curator data through ThaleMine +
GAIA aliases without the Phoenix Bioinformatics paid subscription.
Returned shape is `BarGeneSummary` (same as `bar_gene_summary`).

`plantcyc_locus_info` remains a pure-data redirect — BioCyc PLANT orgid
gates its free per-locus REST behind a paid subscription (SRI/Phoenix)
and no free mirror covers the metabolic-pathway membership data:

```jsonc
// plantcyc_locus_info { "locus": "AT1G01010" }
{
  "locus": "AT1G01010",
  "plantcyc_web_url": "https://pmn.plantcyc.org/gene?orgid=ARA&id=AT1G01010",
  "status": "subscription_required",
  "probed_at": "2026-05-21",
  "rationale": "PlantCyc per-locus REST endpoints require a paid BioCyc subscription. This MCP does not ship a live PlantCyc client — use the alternatives below for partial coverage.",
  "alternatives": ["kegg_pathways", "ensembl_plants_lookup_locus"],
  "alternatives_note": "kegg_pathways covers pathway membership (no metabolic-network depth); ensembl_plants_lookup_locus covers gene annotation.",
}
```

`plantcyc_locus_info` exists for **discoverability** — a caller who
knows of PlantCyc gets a structured pointer to the closest free
backends rather than a 404. PlantCyc's metabolic-network value-add is
not currently substituted by the alternatives.

### 9. Batch variants

Each of tools 1–6 has a `batch_*` variant that takes `loci: string[]`
(1–50) and the same optional arguments, returning a unified envelope:

```jsonc
// batch_ensembl_plants_lookup_locus { "loci": ["AT1G01010", "AT1G01020", "AT9G99999"] }
{
  "tool": "ensembl_plants_lookup_locus",
  "count": 3,
  "results": {
    "AT1G01010": { "id": "AT1G01010", "display_name": "NAC001", "biotype": "protein_coding", ... },
    "AT1G01020": { "id": "AT1G01020", "display_name": "ARV1", ... }
  },
  "errors": {
    "AT9G99999": "[NotFoundError] Ensembl Plants /lookup/id: no record for AT9G99999"
  }
}
```

`batch_ensembl_plants_lookup_locus` uses Ensembl's native
`POST /lookup/id` endpoint — one HTTP round-trip handles all loci.
The other five batch tools fan out single-locus calls via
`asyncio.gather`, so the wall-clock time is `~max(per_locus_latency) +
gather overhead` rather than `N × per_locus_latency`. The wire shape is
the same for both implementations.

## Error model

All live tools raise `PlantGenomicsError` subclasses; the MCP SDK
stringifies them into the wire `content` with a `[ClassName]` prefix
so clients can route on failure kind without parsing the message:

| Wire prefix                  | When                                                               |
| ---------------------------- | ------------------------------------------------------------------ |
| `[NotFoundError]`            | 404 / empty BioMart row / invalid locus identifier                 |
| `[RateLimitError]`           | 429 retry budget exhausted — back off and retry                    |
| `[UpstreamUnavailableError]` | 5xx past retry budget — service outage, try a peer backend         |
| `[PlantGenomicsError]`       | Other (BioMart `Query ERROR:` body, unexpected column count, etc.) |

## Chain recipes

**Annotation fallback chain** — TAIR locus to canonical record, with
graceful degradation:

1. Call `tair_locus_info { locus }` (alias of `bar_gene_summary`) for the
   curator-vetted summary + alias set.
2. On `[UpstreamUnavailableError]` (BAR ThaleMine outage), fall back to
   `ensembl_plants_lookup_locus { locus }` for the Ensembl Plants record.
3. On a second `[UpstreamUnavailableError]`, fall back to
   `phytozome_lookup_locus { locus }` for the Phytozome BioMart record.

**Cross-species ortholog probe** (manual today, candidate for a built-in
chain in a future release):

1. `ensembl_plants_lookup_locus { locus, organism: "arabidopsis_thaliana" }`
   → save the `display_name`.
2. `ensembl_plants_lookup_locus { locus: <ortholog_id>, organism: "oryza_sativa" }`
   → compare biotype + description.

**Locus → protein → structure** (gene to AlphaFold model in two MCP
hops, expecting an external AlphaFold / RCSB tool downstream):

1. `resolve_locus_to_uniprot { locus: "AT1G01010" }`
   → save `primaryAccession` (e.g. `Q0WV96`).
2. Hand the accession to AlphaFold (`https://alphafold.ebi.ac.uk/api/prediction/{accession}`)
   or RCSB (`https://search.rcsb.org/`) via a sibling MCP.
3. On `[NotFoundError]`, the locus has no UniProt entry — usually a
   non-coding or recently-annotated gene; fall back to
   `ensembl_plants_lookup_locus` for the `biotype` to confirm.

**Biological context** — homology + pathways + interactions +
coexpression for one locus, cross-referenced into a high-confidence
functional-partner shortlist:

```python
homologs = await gramene_homologs(locus="AT1G01010")
pathways = await kegg_pathways(locus="AT1G01010")
uniprot = await resolve_locus_to_uniprot(locus="AT1G01010")
interactions = await string_interactions(
    locus_or_accession=uniprot["primaryAccession"], limit=10
)
coex = await atted_coexpression(locus="AT1G01010", top_n=10)
# Cross-reference: interactors ∩ coex_neighbors = high-confidence functional partners.
```

Or as one parameterized prompt:

```
prompts/get biological_context locus=AT1G01010 top_n=10
```

## Development

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q                                       # unit tests
PLANT_GENOMICS_MCP_LIVE=1 .venv/bin/pytest -q             # adds live network probes
PLANT_GENOMICS_MCP_STDIO_SMOKE=1 .venv/bin/pytest -q      # adds stdio smoke
.venv/bin/ruff check .
```

The `_LIVE=1` gate runs additional tests that hit real Ensembl Plants /
Phytozome endpoints — useful for catching wire-format drift. The
`_STDIO_SMOKE=1` gate spawns the MCP server over stdio and round-trips
real `initialize` / `list_tools` / `call_tool` requests.

CI runs the unit suite + the stdio smoke on every push/PR (matrix:
Python 3.11, 3.12). The live-network gate is **not** run in CI to avoid
flakes from upstream availability.

### Cache

Each backend keeps a small in-memory TTL+LRU cache around its HTTP
helper (`_get` / `_post`). Identical requests within the TTL window
return the cached payload without touching the network. Only 200
responses are cached; 4xx/5xx still raise. The cache is process-local —
restart the server to drop all entries.

Env knobs (read once at import):

| Variable                            | Default | Purpose                                               |
| ----------------------------------- | ------- | ----------------------------------------------------- |
| `PLANT_GENOMICS_MCP_CACHE_TTL`      | `600`   | Entry lifetime in seconds.                            |
| `PLANT_GENOMICS_MCP_CACHE_SIZE`     | `256`   | Max entries per backend before LRU eviction kicks in. |
| `PLANT_GENOMICS_MCP_CACHE_DISABLED` | unset   | Any non-empty value makes every cache a no-op.        |

### Progress notifications

For long-running calls (retry storms, the multi-second Phytozome BioMart
POST), the server emits MCP `notifications/progress` messages over the
active session. Clients opt in by passing a `progressToken` in the
request `_meta`; without one, every notification is dropped.

What gets reported:

- **Retry sleeps** in every backend retry loop (`Ensembl Plants /lookup/id/...: HTTP 429, retrying in 1.0s (attempt 2/3)`).
- **BioMart query bookends** — `Phytozome BioMart: submitting query` before the POST and `Phytozome BioMart: query complete` after a 200.

`progress` is a monotonically increasing step counter (not a percentage);
`total` is omitted because retry budgets aren't a useful denominator.

### NCBI BLAST contact email

NCBI's BLAST URLAPI policy expects every caller to identify itself with a
real contact email so abusive clients can be reached before they get
throttled or blocked. `blast_sequence` reads the operator address from
`PLANT_GENOMICS_MCP_NCBI_EMAIL` and sends it as the `email=` parameter on
every `Put`/`Get`. **Set this in production** — when the variable is
unset, the server sends an unmistakable placeholder
(`plant-genomics-mcp-unconfigured@example.invalid`) and emits a one-shot
progress warning per call, but NCBI may still throttle or block requests
that look anonymous.

To prevent the same process from running away with BLAST submissions
(NCBI rate-limits per-IP, not per-email), `blast_sequence` is wrapped in
a module-level `asyncio.Semaphore` capped at 2 in-flight searches.
Excess callers wait their turn rather than racing to upstream.

## Migrating from v0.8 to v0.9

Every backend tool that previously accepted `species=` (Ensembl slug) or
`organism_id=` (NCBI taxid) now accepts a single `organism=` parameter:

```python
# v0.8 (deprecated)
await ensembl_plants.lookup_locus(client, "AT1G01010", species="arabidopsis_thaliana")
await uniprot.lookup_locus(client, "AT1G01010", organism_id=3702)

# v0.9
await ensembl_plants.lookup_locus(client, "AT1G01010", organism="arabidopsis_thaliana")
await uniprot.lookup_locus(client, "AT1G01010", organism="arabidopsis_thaliana")
# All of these also work:
await ensembl_plants.lookup_locus(client, "AT1G01010", organism="Arabidopsis thaliana")
await ensembl_plants.lookup_locus(client, "AT1G01010", organism="thale cress")
await ensembl_plants.lookup_locus(client, "AT1G01010", organism=3702)
```

`sed` migration for downstream callers:

```bash
sed -i 's/species=/organism=/g; s/organism_id=/organism=/g' your_code.py
```

The default value (`arabidopsis_thaliana`) is unchanged, so calls that never
passed `species=` or `organism_id=` continue to work without code changes.

Output Pydantic models also rename `species` → `organism` and
`organism_taxid` → `organism` (the string slug, not the int). See
`CHANGELOG.md` for the full breaking-change list.

### Supported organisms in v0.9

12 plants across the major Ensembl / STRING / Phytozome / Europe PMC
backends. See the `pgmcp://organisms/coverage` MCP resource for the live
coverage matrix, or `organisms.ORGANISMS` in the source.

## License

MIT — see [`LICENSE`](LICENSE). Underlying services (Ensembl Plants,
Phytozome, TAIR, PlantCyc) have their own terms of use; consult each
before bulk querying.
