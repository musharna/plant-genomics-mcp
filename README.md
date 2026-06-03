# 🌱 plant-genomics-mcp

> **32 tools** for plant-genomics locus lookup over the Model Context Protocol —
> 16 single-locus + 12 parallel-batch + 4 cross-source synthesis variants.
> Free, public sources: Ensembl Plants, Phytozome BioMart, UniProtKB,
> Europe PMC, QuickGO, NCBI BLAST, Gramene, KEGG, STRING-DB, ATTED-II,
> and BAR (Bio-Analytic Resource for Plant Biology).

[![PyPI](https://img.shields.io/pypi/v/plant-genomics-mcp)](https://pypi.org/project/plant-genomics-mcp/)
[![CI](https://github.com/musharna/plant-genomics-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/musharna/plant-genomics-mcp/actions/workflows/test.yml)
[![Docker](https://github.com/musharna/plant-genomics-mcp/actions/workflows/docker.yml/badge.svg)](https://github.com/musharna/plant-genomics-mcp/actions/workflows/docker.yml)
[![codecov](https://codecov.io/gh/musharna/plant-genomics-mcp/branch/main/graph/badge.svg)](https://codecov.io/gh/musharna/plant-genomics-mcp)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
[![Glama](https://glama.ai/mcp/servers/musharna/plant-genomics-mcp/badges/score.svg)](https://glama.ai/mcp/servers/musharna/plant-genomics-mcp)

<p align="center">
  <img src="examples/assets/demo.svg" alt="plant-genomics-mcp stdio demo — initialize, tools/list (32), and a coverage-matrix resource read" width="780">
</p>

## 📦 Install

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

## 🛠️ Tools

**32 tools across 11 backends** — Ensembl Plants, Phytozome BioMart,
UniProtKB, Europe PMC, QuickGO, NCBI BLAST, Gramene, KEGG, STRING-DB,
ATTED-II, BAR. 16 single-locus + 12 parallel-batch + 4 cross-source
synthesis. All take a TAIR-style locus (e.g. `AT1G01010`) plus
optional `organism=` (slug / scientific name / common name / NCBI taxid
— 12-plant curated coverage matrix at the `pgmcp://organisms/coverage`
MCP resource). All publish JSON `outputSchema` and EDAM ontology tags.

<details>
<summary>Full tool matrix</summary>

| #   | Category                | Tool                                    | What it does                                                                                                                                                                                                                 |
| --- | ----------------------- | --------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | Gene metadata (live)    | `ensembl_plants_lookup_locus`           | Fetches gene record from Ensembl Plants REST (any plant species).                                                                                                                                                            |
| 2   | Cross-references (live) | `get_gene_xrefs`                        | Fetches cross-DB references (UniProt, NCBI Gene, TAIR, GO, …) from Ensembl.                                                                                                                                                  |
| 3   | Gene metadata (live)    | `phytozome_lookup_locus`                | Fetches gene record from Phytozome BioMart (any Phytozome proteome).                                                                                                                                                         |
| 4   | Protein (live)          | `resolve_locus_to_uniprot`              | Resolves a locus to its UniProtKB record (Swiss-Prot preferred, TrEMBL OK).                                                                                                                                                  |
| 5   | Literature (live)       | `locus_literature`                      | Searches Europe PMC for papers mentioning the locus (free, no API key).                                                                                                                                                      |
| 6   | GO annotations (live)   | `locus_go_annotations`                  | Fetches QuickGO GO annotations (locus → UniProt → QuickGO).                                                                                                                                                                  |
| 7   | Sequence search (live)  | `blast_sequence`                        | NCBI BLAST URLAPI — async Put/Get polling with progress notifications.                                                                                                                                                       |
| 8   | Homology (live)         | `gramene_homologs`                      | Fetches Gramene v69 homology entries (ortholog / paralog) with gene_tree_id.                                                                                                                                                 |
| 9   | Pathways (live)         | `kegg_pathways`                         | Fetches KEGG pathway memberships. 7 organisms: Arabidopsis (`ath:`, native AGI), + rice (`osa:`), maize (`zma:`), soybean (`gmx:`), barley (`hvg:`), poplar (`pop:`), brachypodium (`bdi:`) bridged via Ensembl → Entrez ID. |
| 10  | Interactions (live)     | `string_interactions`                   | Fetches STRING-DB first-neighbor interaction partners with per-channel score.                                                                                                                                                |
| 11  | Coexpression (live)     | `atted_coexpression`                    | Fetches ATTED-II Ath-u.c4-0 top-N coexpression neighbors with z-scores.                                                                                                                                                      |
| 12  | Curator summary (live)  | `bar_gene_summary`                      | Fetches BAR ThaleMine + GAIA-aliases curator summary for an Arabidopsis locus.                                                                                                                                               |
| 13  | Expression (live)       | `bar_efp_expression`                    | Fetches BAR eFP-Browser expression profile (mean ± SD per tissue) for a locus.                                                                                                                                               |
| 14  | Interactions (live)     | `bar_aiv_interactions`                  | Fetches BAR AIV interaction partners (Arabidopsis + rice) with confidence + papers.                                                                                                                                          |
| 15  | Curator summary (live)  | `tair_locus_info`                       | Silent upgrade — alias of `bar_gene_summary`. MCP tool name preserved for clients.                                                                                                                                           |
| 16  | Subscription redirect   | `plantcyc_locus_info`                   | Returns subscription notice + redirect to live backends. No upstream call.                                                                                                                                                   |
| 17  | Batch (live)            | `batch_*` (twelve variants)             | Parallel per-locus fanout for tools 1–6, 8–12, 14. Up to 50 loci per call.                                                                                                                                                   |
| 18  | Synthesis (live)        | `*_synth` / `consensus_homologs` (four) | Compose 2–5 backends in parallel, return a `SynthesisEnvelope` with per-step status.                                                                                                                                         |

</details>

## ⚡ Quickstart

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

In Claude Code, the same prompt fans out across Ensembl, UniProtKB, and
Europe PMC in a single turn ([animated demo](examples/assets/cc-demo.gif)):

<p align="center">
  <img src="examples/assets/cc-demo.png" alt="Claude Code (Opus 4.7) calling plant-genomics-mcp 8 times to return the AT1G01010 / NAC1_ARATH record with Ensembl, UniProt Q0WV96, and the top-3 Europe PMC papers" width="820">
</p>

Full per-tool walkthroughs (with real upstream-API transcripts) live in
[`examples/`](examples/):

| Walkthrough                                                                               | Coverage                                                                                |
| ----------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| [`analyze_locus_AT1G01010.md`](examples/analyze_locus_AT1G01010.md)                       | Ensembl → xrefs → UniProt → Europe PMC → QuickGO chain (5 tools).                       |
| [`find_homologs_AT1G01010_NAC_domain.md`](examples/find_homologs_AT1G01010_NAC_domain.md) | BLAST + per-hit UniProt enrichment.                                                     |
| [`biological_context_AT1G01010.md`](examples/biological_context_AT1G01010.md)             | Gramene + KEGG + UniProt + STRING + ATTED-II (5 tools).                                 |
| [`v0.8_synthesis_walkthrough.md`](examples/v0.8_synthesis_walkthrough.md)                 | All 4 v0.8 synthesis tools (`*_synth` + `consensus_homologs`) on the same locus.        |
| [`cross_organism_walkthrough.md`](examples/cross_organism_walkthrough.md)                 | v0.9 multi-organism resolver against rice + maize — per-backend routing on PyPI v1.0.4. |

## 📚 Resources & prompts

<details>
<summary>Four read-only MCP resources + three parameterized prompts</summary>

Clients discover them via `resources/list` and `prompts/list`.

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

</details>

## 🔌 Transports

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

## ⚙️ Configuration

Stdio needs no configuration. The two env vars that matter:

| Variable                        | When                | Effect                                                                                                                |
| ------------------------------- | ------------------- | --------------------------------------------------------------------------------------------------------------------- |
| `PLANT_GENOMICS_MCP_HTTP_TOKEN` | HTTP transport only | Bearer token for `/mcp`; **must be ≥32 chars** or the HTTP server aborts at startup. Generate `openssl rand -hex 32`. |
| `PLANT_GENOMICS_MCP_NCBI_EMAIL` | If you use BLAST    | NCBI etiquette contact. Unset → placeholder + per-call warning; NCBI may throttle.                                    |

<details>
<summary>All env vars (HTTP bind, body cap, cache, BLAST concurrency)</summary>

| Variable                               | Default           | Effect                                                             |
| -------------------------------------- | ----------------- | ------------------------------------------------------------------ |
| `PLANT_GENOMICS_MCP_HTTP_HOST`         | `127.0.0.1`       | HTTP bind address.                                                 |
| `PLANT_GENOMICS_MCP_HTTP_PORT`         | `8765`            | HTTP TCP port.                                                     |
| `PLANT_GENOMICS_MCP_HTTP_MAX_BODY`     | `2097152` (2 MiB) | Reject POSTs with `Content-Length` larger than this.               |
| `PLANT_GENOMICS_MCP_HTTP_STATELESS`    | `1`               | `0` keeps per-client session state (SSE-style).                    |
| `PLANT_GENOMICS_MCP_HTTP_JSON`         | `1`               | `0` switches the response shape to streaming SSE events.           |
| `PLANT_GENOMICS_MCP_BLAST_CONCURRENCY` | `2`               | Max in-flight BLAST searches per process (NCBI per-IP rate limit). |
| `PLANT_GENOMICS_MCP_CACHE_TTL`         | `600`             | Per-backend TTL+LRU cache entry lifetime, in seconds. 200-only.    |
| `PLANT_GENOMICS_MCP_CACHE_SIZE`        | `256`             | Max entries per backend before LRU eviction.                       |
| `PLANT_GENOMICS_MCP_CACHE_DISABLED`    | unset             | Any non-empty value makes every cache a no-op.                     |

The cache is process-local — restart the server to drop all entries.
Long-running calls (retry storms, multi-second Phytozome BioMart POSTs)
emit MCP `notifications/progress` over the active session; clients opt
in via `progressToken` in the request `_meta`.

</details>

## ⚠️ Error model

<details>
<summary>Wire-prefix taxonomy + batch result shape</summary>

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

</details>

## 🧪 Development

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

**Scientific validation / drift detection.** `scripts/benchmark_annotations.py`
drives a curated corpus of canonical loci (27, spanning all 12 organisms)
through every backend + synthesis pipeline and compares results to a frozen
baseline, emitting PASS / DRIFT / FAIL plus cross-source consistency
invariants. It's how upstream data drift is caught. A scheduled GitHub Actions
workflow (`.github/workflows/benchmark.yml`) runs it weekly and pages on a
confirmed regression. Operator guide: [`docs/benchmarking.md`](docs/benchmarking.md).

```bash
.venv/bin/python scripts/benchmark_annotations.py        # full live sweep (~3-5 min)
```

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
