# plant-genomics-mcp

> **32 tools** for plant-genomics locus lookup over the Model Context Protocol —
> 16 single-locus + 12 parallel-batch + 4 cross-source synthesis variants.
> Free, public sources: Ensembl Plants, Phytozome BioMart, UniProtKB,
> Europe PMC, QuickGO, NCBI BLAST, Gramene, KEGG, STRING-DB, ATTED-II,
> and BAR (Bio-Analytic Resource for Plant Biology).

[![PyPI](https://img.shields.io/pypi/v/plant-genomics-mcp)](https://pypi.org/project/plant-genomics-mcp/)
[![CI](https://github.com/musharna/plant-genomics-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/musharna/plant-genomics-mcp/actions/workflows/test.yml)
[![Docker](https://github.com/musharna/plant-genomics-mcp/actions/workflows/docker.yml/badge.svg)](https://github.com/musharna/plant-genomics-mcp/actions/workflows/docker.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

## Install

```bash
pipx install plant-genomics-mcp
claude mcp add plant-genomics --scope local -- plant-genomics-mcp
```

<details>
<summary>Other install paths (Docker, from source)</summary>

```bash
# GHCR Docker image
docker pull ghcr.io/musharna/plant-genomics-mcp:latest
claude mcp add plant-genomics --scope local -- \
  docker run --rm -i ghcr.io/musharna/plant-genomics-mcp:latest

# From source
git clone https://github.com/musharna/plant-genomics-mcp.git
cd plant-genomics-mcp
python -m venv .venv && .venv/bin/pip install -e .
claude mcp add plant-genomics --scope local -- "$(pwd)/.venv/bin/plant-genomics-mcp"
```

</details>

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

All live tools take a TAIR-style locus (e.g. `AT1G01010`) plus an
optional `organism=` parameter (default `arabidopsis_thaliana`).
`organism=` accepts a canonical slug (`oryza_sativa`), scientific name
(`Oryza sativa`), common name (`rice`), or NCBI taxonomy ID (`39947`) —
all resolve to the same 12-plant curated coverage matrix. See the
`pgmcp://organisms/coverage` MCP resource for the live matrix.

All 32 tools publish JSON `outputSchema` for client-side validation and
EDAM ontology tags on `_meta` for registry indexers.

## Quickstart

After install, the simplest call returns the Ensembl Plants record for
NAC001 — the canonical worked example used throughout `examples/`:

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

Cross-species — pass `organism=`:

```jsonc
{ "locus": "Os01g0100100", "organism": "oryza_sativa" }
```

Full per-tool walkthroughs (with real upstream-API transcripts) live in
[`examples/`](examples/):

| Walkthrough                                                                               | Coverage                                                                                |
| ----------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| [`analyze_locus_AT1G01010.md`](examples/analyze_locus_AT1G01010.md)                       | Ensembl → xrefs → UniProt → Europe PMC → QuickGO chain (5 tools).                       |
| [`find_homologs_AT1G01010_NAC_domain.md`](examples/find_homologs_AT1G01010_NAC_domain.md) | BLAST + per-hit UniProt enrichment.                                                     |
| [`biological_context_AT1G01010.md`](examples/biological_context_AT1G01010.md)             | Gramene + KEGG + UniProt + STRING + ATTED-II (5 tools).                                 |
| [`v0.8_synthesis_walkthrough.md`](examples/v0.8_synthesis_walkthrough.md)                 | All 4 v0.8 synthesis tools (`*_synth` + `consensus_homologs`) on the same locus.        |
| [`cross_organism_walkthrough.md`](examples/cross_organism_walkthrough.md)                 | v0.9 multi-organism resolver against rice + maize — per-backend routing on PyPI v1.0.4. |

## Resources & prompts

The server advertises four read-only MCP resources and three
parameterized prompts. Clients discover them via `resources/list` and
`prompts/list`.

**Resources** (`resources/read`):

| URI                           | What                                                                                           |
| ----------------------------- | ---------------------------------------------------------------------------------------------- |
| `pgmcp://cache/stats`         | Per-backend `TTLCache` rollup — `{hits, misses, size}` for each live backend.                  |
| `pgmcp://organisms/phytozome` | Slug → Phytozome `organism_id` map.                                                            |
| `pgmcp://backends/status`     | Per-backend liveness rollup — `name`, `base_url`, `kind`, `subscription_gated`, `probed_at`.   |
| `pgmcp://organisms/coverage`  | Markdown table of all 12 supported plants × 5 ID slots (ncbi_taxid / ensembl / phytozome / …). |

**Prompts** (`prompts/get`):

| Name                 | Required   | Optional                                    | Chains                                                                               |
| -------------------- | ---------- | ------------------------------------------- | ------------------------------------------------------------------------------------ |
| `analyze_locus`      | `locus`    | `organism` (default `arabidopsis_thaliana`) | Ensembl → xrefs → UniProt → Europe PMC → QuickGO.                                    |
| `find_homologs`      | `sequence` | `program` (default `blastp`)                | `blast_sequence` → per-hit `resolve_locus_to_uniprot` for UniProt-shaped accessions. |
| `biological_context` | `locus`    | `top_n` (default 10)                        | Gramene → KEGG → UniProt → STRING → ATTED-II.                                        |

## Transports

| Transport       | How to launch                                                       |
| --------------- | ------------------------------------------------------------------- |
| stdio (default) | `plant-genomics-mcp` (after install) or via Docker above            |
| streamable-HTTP | `plant-genomics-mcp-http` — POST JSON-RPC at `http://host:port/mcp` |

The HTTP transport is stateless and emits JSON responses by default —
the right shape for registry indexers and remote hosting.

### Hosted endpoint

A small **personal demo** runs at:

```
https://mjarnoldgt76.tail86d19d.ts.net/mcp
```

Intended for registry indexers, one-off evaluation, and quick
interactive testing — **not for production workloads**. No SLA, no
uptime commitment, URL may change without notice (single laptop on a
residential connection).

```bash
# liveness probe
curl https://mjarnoldgt76.tail86d19d.ts.net/healthz
# {"status":"ok"}

# connect from Claude Code
claude mcp add --transport http plant-genomics-mcp \
  https://mjarnoldgt76.tail86d19d.ts.net/mcp
```

For anything beyond casual evaluation, **self-host**. The HTTP transport
is the same binary; self-hosting buys deterministic uptime, your own
bearer-token gate (`PLANT_GENOMICS_MCP_HTTP_TOKEN`), and NCBI BLAST
etiquette under your own contact email.

## Configuration

All runtime knobs read from environment variables at startup or first
use. None are required for stdio. The HTTP transport requires
`PLANT_GENOMICS_MCP_HTTP_TOKEN`; BLAST requests should set
`PLANT_GENOMICS_MCP_NCBI_EMAIL`.

| Variable                               | Default                | Effect                                                                                                                                        |
| -------------------------------------- | ---------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `PLANT_GENOMICS_MCP_HTTP_HOST`         | `127.0.0.1`            | HTTP bind address.                                                                                                                            |
| `PLANT_GENOMICS_MCP_HTTP_PORT`         | `8765`                 | HTTP TCP port.                                                                                                                                |
| `PLANT_GENOMICS_MCP_HTTP_TOKEN`        | _(none, **required**)_ | Bearer token for `/mcp`; **must be ≥32 chars** or the HTTP server aborts at startup. `/healthz` exempt. Generate with `openssl rand -hex 32`. |
| `PLANT_GENOMICS_MCP_HTTP_MAX_BODY`     | `2097152` (2 MiB)      | Reject POSTs with `Content-Length` larger than this.                                                                                          |
| `PLANT_GENOMICS_MCP_HTTP_STATELESS`    | `1`                    | `0` keeps per-client session state (SSE-style).                                                                                               |
| `PLANT_GENOMICS_MCP_HTTP_JSON`         | `1`                    | `0` switches the response shape to streaming SSE events.                                                                                      |
| `PLANT_GENOMICS_MCP_NCBI_EMAIL`        | placeholder            | NCBI BLAST etiquette contact. Unset → unmistakable placeholder + one-shot per-call warning; NCBI may throttle.                                |
| `PLANT_GENOMICS_MCP_BLAST_CONCURRENCY` | `2`                    | Max in-flight BLAST searches per process (NCBI per-IP rate limit).                                                                            |
| `PLANT_GENOMICS_MCP_CACHE_TTL`         | `600`                  | Per-backend TTL+LRU cache entry lifetime, in seconds. 200-only.                                                                               |
| `PLANT_GENOMICS_MCP_CACHE_SIZE`        | `256`                  | Max entries per backend before LRU eviction.                                                                                                  |
| `PLANT_GENOMICS_MCP_CACHE_DISABLED`    | unset                  | Any non-empty value makes every cache a no-op.                                                                                                |

The cache is process-local — restart the server to drop all entries.
Long-running calls (retry storms, multi-second Phytozome BioMart POSTs)
emit MCP `notifications/progress` over the active session; clients opt
in via `progressToken` in the request `_meta`.

## Error model

All live tools raise `PlantGenomicsError` subclasses; the MCP SDK
stringifies them into the wire `content` with a `[ClassName]` prefix so
clients can route on failure kind without parsing the message:

| Wire prefix                  | When                                                               |
| ---------------------------- | ------------------------------------------------------------------ |
| `[NotFoundError]`            | 404 / empty BioMart row / invalid locus identifier                 |
| `[RateLimitError]`           | 429 retry budget exhausted — back off and retry                    |
| `[UpstreamUnavailableError]` | 5xx past retry budget — service outage, try a peer backend         |
| `[PlantGenomicsError]`       | Other (BioMart `Query ERROR:` body, unexpected column count, etc.) |

Batch tools return `{tool, count, results, errors}` where
`results[locus]` is the same shape as the single-locus tool and
`errors[locus]` is the same `[ClassName] message` string. Ensembl's
batch uses the native `POST /lookup/id` endpoint (one HTTP round-trip);
everything else fans out via `asyncio.gather`.

## Development

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q                                       # unit tests
PLANT_GENOMICS_MCP_LIVE=1 .venv/bin/pytest -q             # adds live network probes
PLANT_GENOMICS_MCP_STDIO_SMOKE=1 .venv/bin/pytest -q      # adds stdio smoke
.venv/bin/ruff check .
```

CI runs the unit suite + the stdio smoke on every push/PR (matrix:
Python 3.11, 3.12). The live-network gate is **not** run in CI to avoid
flakes from upstream availability.

See [`CHANGELOG.md`](CHANGELOG.md) for release notes, including the
v0.8 → v0.9 `species=`/`organism_id=` → `organism=` migration and the
v1.0.1 HTTP-token enforcement change.

## MCP registry

Listed in the [official MCP registry](https://registry.modelcontextprotocol.io)
under the namespace below (ownership-verification token for `mcp-publisher`):

```
mcp-name: io.github.musharna/plant-genomics-mcp
```

## License

MIT — see [`LICENSE`](LICENSE). Underlying services (Ensembl Plants,
Phytozome, TAIR, PlantCyc, BAR) have their own terms of use; consult
each before bulk querying.
