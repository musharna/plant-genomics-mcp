# plant-genomics-mcp

> **14 tools** for plant-genomics locus lookup via the Model Context Protocol —
> 8 single-locus + 6 parallel-batch variants.
> Free, public sources: Ensembl Plants + Phytozome BioMart + UniProtKB +
> Europe PMC + QuickGO. TAIR
>
> - PlantCyc are informational stubs that redirect to the free alternatives
>   (both services are paid-subscription-gated, probed 2026-05-21).

[![CI](https://github.com/mjarnold/plant-genomics-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/mjarnold/plant-genomics-mcp/actions/workflows/test.yml)
[![Docker](https://github.com/mjarnold/plant-genomics-mcp/actions/workflows/docker.yml/badge.svg)](https://github.com/mjarnold/plant-genomics-mcp/actions/workflows/docker.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

## Tools at a glance

| #   | Category                | Tool                          | What it does                                                                |
| --- | ----------------------- | ----------------------------- | --------------------------------------------------------------------------- |
| 1   | Gene metadata (live)    | `ensembl_plants_lookup_locus` | Fetches gene record from Ensembl Plants REST (any plant species).           |
| 2   | Cross-references (live) | `get_gene_xrefs`              | Fetches cross-DB references (UniProt, NCBI Gene, TAIR, GO, …) from Ensembl. |
| 3   | Gene metadata (live)    | `phytozome_lookup_locus`      | Fetches gene record from Phytozome BioMart (any Phytozome proteome).        |
| 4   | Protein (live)          | `resolve_locus_to_uniprot`    | Resolves a locus to its UniProtKB record (Swiss-Prot preferred, TrEMBL OK). |
| 5   | Literature (live)       | `locus_literature`            | Searches Europe PMC for papers mentioning the locus (free, no API key).     |
| 6   | GO annotations (live)   | `locus_go_annotations`        | Fetches QuickGO GO annotations (locus → UniProt → QuickGO).                 |
| 7   | Subscription redirect   | `tair_locus_info`             | Returns subscription notice + redirect to live backends. No upstream call.  |
| 8   | Subscription redirect   | `plantcyc_locus_info`         | Returns subscription notice + redirect to live backends. No upstream call.  |
| 9   | Sequence search (live)  | `blast_sequence`              | NCBI BLAST URLAPI — async Put/Get polling with progress notifications.      |
| 10  | Batch (live)            | `batch_*` (six variants)      | Parallel per-locus fanout for tools 1–6. Up to 50 loci per call.            |

Live tools take a TAIR-style locus (e.g. `AT1G01010`) plus optional
`species=` / `organism_id=` and return a structured record. UniProt
expects an NCBI taxonomy ID for `organism_id` (default `3702` =
_Arabidopsis thaliana_); the gene-metadata tools each have their own
species/organism conventions documented below. Subscription tools take
a locus and return a structured redirect record — they do not call the
gated upstream.

Batch variants (`batch_ensembl_plants_lookup_locus`,
`batch_get_gene_xrefs`, `batch_phytozome_lookup_locus`,
`batch_resolve_locus_to_uniprot`, `batch_locus_literature`,
`batch_locus_go_annotations`) take a `loci: string[]` (1–50 items) plus
the same optional `species=` / `organism_id=` arguments. They return
`{tool, count, results, errors}` where `results[locus]` is the same
shape as the single-locus tool and `errors[locus]` is a
`[ClassName] message` string for `PlantGenomicsError` failures
(`[NotFoundError]`, `[RateLimitError]`, …). Ensembl's batch uses
the native `POST /lookup/id` endpoint (one HTTP round-trip);
everything else fans out via `asyncio.gather`.

All fifteen tools publish JSON `outputSchema` for client-side validation
and EDAM ontology tags (`operation_2422` Data retrieval; topic
`topic_0780` Plant biology + `topic_0114` Gene structure) on `_meta`
for registry indexers.

## Resources

The server also advertises three read-only MCP resources (JSON,
`resources/list` + `resources/read`):

| URI                           | What                                                                                                           |
| ----------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `pgmcp://cache/stats`         | Per-backend `TTLCache` rollup — `{hits, misses, size}` for each of the five live backends                      |
| `pgmcp://organisms/phytozome` | Slug → Phytozome `organism_id` map (only `arabidopsis_thaliana=167` is controller-verified; rest are hints)    |
| `pgmcp://backends/status`     | Per-backend liveness rollup — `name`, `base_url`, `kind` (`live` or `stub`), `subscription_gated`, `probed_at` |

Useful for an operator to confirm caching is doing work without
shelling into the process, and for clients that want to enumerate
supported organisms / backends programmatically.

## Prompts

The server exposes two parameterized prompts (`prompts/list` +
`prompts/get`) for one-click multi-tool workflows:

| Name            | Required args | Optional args                              | What it chains                                                                      |
| --------------- | ------------- | ------------------------------------------ | ----------------------------------------------------------------------------------- |
| `analyze_locus` | `locus`       | `species` (default `arabidopsis_thaliana`) | Ensembl annotation → xrefs → UniProt → Europe PMC literature → QuickGO annotations  |
| `find_homologs` | `sequence`    | `program` (default `blastp`)               | `blast_sequence` → per-hit `resolve_locus_to_uniprot` for UniProt-shaped accessions |

Clients populate their slash-command menu from `prompts/list`, so the
workflow is one user selection deep instead of requiring the user to
remember the tool ordering.

Real-execution proof transcripts of both chains (against the live
upstream APIs) live in [`examples/`](examples/) — one JSON + Markdown
pair per prompt.

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

## Install

### pipx (recommended)

```bash
pipx install plant-genomics-mcp
claude mcp add plant-genomics --scope local -- plant-genomics-mcp
```

### Docker (GHCR)

```bash
docker pull ghcr.io/mjarnold/plant-genomics-mcp:latest
claude mcp add plant-genomics --scope local -- \
  docker run --rm -i ghcr.io/mjarnold/plant-genomics-mcp:latest
```

### From source

```bash
git clone https://github.com/mjarnold/plant-genomics-mcp.git
cd plant-genomics-mcp
python -m venv .venv && .venv/bin/pip install -e .
claude mcp add plant-genomics --scope local -- "$(pwd)/.venv/bin/plant-genomics-mcp"
```

## Usage examples

### 1. `ensembl_plants_lookup_locus`

Fetch a gene record from Ensembl Plants. Default species is
`arabidopsis_thaliana`; pass `species=` for any other Ensembl Plants
species (`oryza_sativa`, `zea_mays`, `solanum_lycopersicum`, ...).

```jsonc
// arguments
{ "locus": "AT1G01010" }

// result (truncated)
{
  "id": "AT1G01010",
  "species": "arabidopsis_thaliana",
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
{ "locus": "Os01g0100100", "species": "oryza_sativa" }
```

### 2. `get_gene_xrefs`

Fetch cross-database references from Ensembl Plants. Same host /
species conventions as `ensembl_plants_lookup_locus`. The response
wraps Ensembl's top-level array (so it validates against the MCP
`outputSchema`'s required `type=object` root) and adds a `by_db`
rollup keyed on Ensembl's `dbname` — so a chain consumer can lift
out a single foreign accession without walking the list.

```jsonc
{ "locus": "AT1G01010" }

// result (xrefs[] truncated)
{
  "locus": "AT1G01010",
  "species": "arabidopsis_thaliana",
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
Arabidopsis thaliana TAIR10 (`organism_id=167`, controller-verified
live). Other Phytozome proteome integer IDs are documented as hints
in `src/plant_genomics_mcp/phytozome.py::KNOWN_ORGANISMS` (`275` for
_Glycine max_, `313` for _Sorghum bicolor_, etc.) but only Arabidopsis
is empirically verified.

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
`organism_id` is the NCBI taxonomy ID (`3702` = Arabidopsis, `39947` =
Oryza sativa japonica, `4577` = Zea mays, …; defaults to `3702`).

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
citation count, and abstract. For non-Arabidopsis species the species
common name is appended to the query (`rice`, `maize`, `tomato`, …)
to disambiguate locus IDs that might otherwise collide with unrelated
literature.

```jsonc
{ "locus": "AT1G01010", "size": 3 }

// result (hits[] truncated)
{
  "locus": "AT1G01010",
  "species": "arabidopsis_thaliana",
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

### 7. `tair_locus_info` / 8. `plantcyc_locus_info`

Pure-data redirects — these tools do **not** call upstream. Both TAIR
and PlantCyc gate their free per-locus REST behind paid subscriptions
(Phoenix Bioinformatics for TAIR; SRI/Phoenix for the BioCyc PLANT
orgid). The tools return a structured record so an LLM client can
route to the live backends transparently:

```jsonc
// tair_locus_info { "locus": "AT1G01010" }
{
  "locus": "AT1G01010",
  "tair_web_url": "https://www.arabidopsis.org/locus/AT1G01010",
  "status": "subscription_required",
  "probed_at": "2026-05-21",
  "auth_configured": false,
  "rationale": "TAIR per-locus REST endpoints return 403; Phoenix Bioinformatics requires paid subscription. Set PLANT_GENOMICS_MCP_TAIR_TOKEN once a subscriber-implemented live wiring lands.",
  "alternatives": ["ensembl_plants_lookup_locus", "phytozome_lookup_locus"],
  "alternatives_note": "Both alternatives return the same canonical Arabidopsis annotation; ensembl_plants_lookup_locus also covers other plant species (oryza_sativa, zea_mays, ...).",
}
```

**Subscription-token slots (P2.20).** Both tools read an optional env
var: `PLANT_GENOMICS_MCP_TAIR_TOKEN` and
`PLANT_GENOMICS_MCP_PLANTCYC_TOKEN`. When either is set (non-empty),
the corresponding tool's response flips:

- `status` → `"configured_live_not_implemented"`
- `auth_configured` → `true`
- A new `note_for_subscribers` field appears, pointing at the
  `_call_live_if_configured` hook in the module where a credentialed
  user can drop in the real `httpx` call.

The live HTTP wiring against Phoenix/SRI is **intentionally deferred**:
their auth schemes are undocumented in the public surface, and
shipping an unverifiable client would mislead the first subscriber.
A subscriber-with-credentials PR that includes a real-execution test
against their account is the path forward.

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

1. Call `tair_locus_info { locus }` to detect the gate.
2. Read its `alternatives` field.
3. Call `ensembl_plants_lookup_locus { locus }` (or `phytozome_lookup_locus`).
4. On `[UpstreamUnavailableError]`, fall back to the other backend.

**Cross-species ortholog probe** (manual today, candidate for a built-in
chain in a future release):

1. `ensembl_plants_lookup_locus { locus, species: "arabidopsis_thaliana" }`
   → save the `display_name`.
2. `ensembl_plants_lookup_locus { locus: <ortholog_id>, species: "oryza_sativa" }`
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

## License

MIT — see [`LICENSE`](LICENSE). Underlying services (Ensembl Plants,
Phytozome, TAIR, PlantCyc) have their own terms of use; consult each
before bulk querying.
