# 🌱 plant-genomics-mcp

> **50 tools** for plant-genomics locus lookup over the Model Context Protocol —
> 28 single-locus + 1 motif lookup + 1 region query + 1 variant annotator + 1 gene-set enrichment + 1 BLAST search + 12 parallel-batch + 5 cross-source synthesis variants.
> Free, public sources: Ensembl Plants, Phytozome BioMart, UniProtKB,
> Europe PMC, QuickGO, Planteome, PlantCyc/PMN, g:Profiler, NCBI BLAST,
> Gramene, JASPAR, KEGG, STRING-DB, ATTED-II, ThaleMine, and BAR (Bio-Analytic Resource for
> Plant Biology).

[![PyPI](https://img.shields.io/pypi/v/plant-genomics-mcp)](https://pypi.org/project/plant-genomics-mcp/)
[![CI](https://github.com/musharna/plant-genomics-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/musharna/plant-genomics-mcp/actions/workflows/test.yml)
[![Docker](https://github.com/musharna/plant-genomics-mcp/actions/workflows/docker.yml/badge.svg)](https://github.com/musharna/plant-genomics-mcp/actions/workflows/docker.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
[![Glama](https://glama.ai/mcp/servers/musharna/plant-genomics-mcp/badges/score.svg)](https://glama.ai/mcp/servers/musharna/plant-genomics-mcp)

<p align="center">
  <img src="examples/assets/cc-demo.gif" alt="Claude Code answering a plant-genomics question live — calling plant-genomics-mcp across Ensembl Plants, UniProt, and Europe PMC and synthesizing the AT1G01010 / NAC1_ARATH gene profile in a single turn" width="780">
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

## 💬 Try it

Once connected, ask Claude a plain-language question — you don't have to
name any tool or remember the chain:

> **"Tell me everything about the Arabidopsis gene AT1G01010 — its
> function, GO terms, KEGG pathways, protein-interaction partners, and
> recent papers."**

Claude fans out across Ensembl Plants, UniProt, QuickGO, KEGG, STRING-DB,
and Europe PMC in a single turn and hands back one synthesized answer.
Swap in any locus and pass `organism=` for cross-species — e.g. rice
`Os01g0100100` (`oryza_sativa`) — and it routes to the right backends
automatically.

## 🛠️ Tools

**50 tools across 23 backends** — Ensembl Plants, Phytozome BioMart,
UniProtKB, Europe PMC, QuickGO, Planteome, PlantCyc/PMN, g:Profiler,
AlphaFold DB, PDBe, InterPro, JASPAR, PANTHER, OrthoDB, AraGWAS, 1001 Genomes, NCBI BLAST,
Gramene, KEGG, STRING-DB, ATTED-II, ThaleMine, BAR.
28 single-locus + 1 motif lookup + 1 region query + 1 variant annotator + 1 gene-set
enrichment + 1 BLAST search + 12 parallel-batch + 5 cross-source synthesis. Most take a
TAIR-style locus (e.g. `AT1G01010`) plus
optional `organism=` (slug / scientific name / common name / NCBI taxid
— 12-plant curated coverage matrix at the `pgmcp://organisms/coverage`
MCP resource). All publish JSON `outputSchema` and EDAM ontology tags.

<details>
<summary>Full tool matrix</summary>

| #   | Category                | Tool                                    | What it does                                                                                                                                                                                                                                                                                                                                                          |
| --- | ----------------------- | --------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | Gene metadata (live)    | `ensembl_plants_lookup_locus`           | Fetches gene record from Ensembl Plants REST (any plant species).                                                                                                                                                                                                                                                                                                     |
| 2   | Cross-references (live) | `get_gene_xrefs`                        | Fetches cross-DB references (UniProt, NCBI Gene, TAIR, GO, …) from Ensembl.                                                                                                                                                                                                                                                                                           |
| 3   | Gene metadata (live)    | `phytozome_lookup_locus`                | Fetches gene record from Phytozome BioMart (any Phytozome proteome).                                                                                                                                                                                                                                                                                                  |
| 4   | Protein (live)          | `resolve_locus_to_uniprot`              | Resolves a locus to its UniProtKB record (Swiss-Prot preferred, TrEMBL OK).                                                                                                                                                                                                                                                                                           |
| 5   | Literature (live)       | `locus_literature`                      | Searches Europe PMC for papers mentioning the locus (free, no API key).                                                                                                                                                                                                                                                                                               |
| 6   | GO annotations (live)   | `locus_go_annotations`                  | Fetches QuickGO GO annotations (locus → UniProt → QuickGO).                                                                                                                                                                                                                                                                                                           |
| 7   | Sequence search (live)  | `blast_sequence`                        | NCBI BLAST URLAPI — async Put/Get polling with progress notifications.                                                                                                                                                                                                                                                                                                |
| 8   | Homology (live)         | `gramene_homologs`                      | Fetches Gramene v69 homology entries (ortholog / paralog) with gene_tree_id.                                                                                                                                                                                                                                                                                          |
| 9   | Pathways (live)         | `kegg_pathways`                         | Fetches KEGG pathway memberships. 7 organisms: Arabidopsis (`ath:`, native AGI), + rice (`osa:`), maize (`zma:`), soybean (`gmx:`), barley (`hvg:`), poplar (`pop:`), brachypodium (`bdi:`) bridged via Ensembl → Entrez ID.                                                                                                                                          |
| 10  | Interactions (live)     | `string_interactions`                   | Fetches STRING-DB first-neighbor interaction partners with per-channel score.                                                                                                                                                                                                                                                                                         |
| 11  | Coexpression (live)     | `atted_coexpression`                    | Fetches ATTED-II Ath-u.c4-0 top-N coexpression neighbors with z-scores.                                                                                                                                                                                                                                                                                               |
| 12  | Curator summary (live)  | `bar_gene_summary`                      | Fetches BAR ThaleMine + GAIA-aliases curator summary for an Arabidopsis locus.                                                                                                                                                                                                                                                                                        |
| 13  | Expression (live)       | `bar_efp_expression`                    | Fetches BAR eFP-Browser expression profile (mean ± SD per tissue) for a locus.                                                                                                                                                                                                                                                                                        |
| 14  | Interactions (live)     | `bar_aiv_interactions`                  | Fetches BAR AIV interaction partners (Arabidopsis + rice) with confidence + papers.                                                                                                                                                                                                                                                                                   |
| 15  | Curator summary (live)  | `tair_locus_info`                       | Silent upgrade — alias of `bar_gene_summary`. MCP tool name preserved for clients.                                                                                                                                                                                                                                                                                    |
| 16  | Metabolism (live)       | `plantcyc_locus_info`                   | Walks gene → enzyme → reactions → PlantCyc/PMN pathways (free BioCyc web-services API). The metabolic-pathway view KEGG/GO lack; found=false for non-enzymatic genes. 11 species have a PGDB.                                                                                                                                                                         |
| 17  | Sequence (live)         | `get_sequence`                          | Fetches a locus's sequence (genomic / cds / cdna / protein) from Ensembl `/sequence/id` — the fetch half of lookup → fetch → BLAST; feed `sequence` to `blast_sequence`.                                                                                                                                                                                              |
| 18  | Region query (live)     | `ensembl_region_query`                  | Lists gene/transcript/cds/exon features overlapping a genomic interval (chr:start-end) via Ensembl `/overlap/region` — "what's in this QTL interval" without a per-locus lookup.                                                                                                                                                                                      |
| 19  | Enrichment (live)       | `go_enrichment`                         | GO + KEGG over-representation for a gene **list** via g:Profiler g:GOSt — "what is my DE / co-expression set enriched for?" Reports unmapped loci; optional custom background. All 12 organisms.                                                                                                                                                                      |
| 20  | Plant ontology (live)   | `locus_plant_ontology`                  | Plant Ontology (anatomy / dev-stage) + Trait Ontology annotations for a locus via Planteome (Solr) — the plant-specific ontologies GO doesn't cover. by_ontology rollup; taxon-filtered. Strong for 6 species.                                                                                                                                                        |
| 21  | Structure (live)        | `alphafold_structure`                   | AlphaFold DB predicted 3D model for a locus (locus → UniProt → model): global mean pLDDT, per-band confidence, modelled span, and mmCIF / PDB / PAE URLs. found=false when no model is deposited. All 12 organisms.                                                                                                                                                   |
| 22  | Structure (live)        | `experimental_structures`               | PDBe experimentally-solved (X-ray / cryo-EM / NMR) structures for a locus (locus → UniProt): best-first PDB id, chain, method, resolution, coverage, residue span. found=false when none deposited (common for plants). All 12 organisms.                                                                                                                             |
| 23  | Domains (live)          | `interpro_domains`                      | InterPro domain / family architecture (locus → UniProt): each entry's accession, name, type, source_database (Pfam included), integrated InterPro id, and residue spans, plus a count_by_type rollup. All 12 organisms.                                                                                                                                               |
| 24  | TF motifs (live)        | `tf_binding_motifs`                     | JASPAR curated TF DNA-binding profiles for a locus (locus → UniProt → symbol search, then UniProt-confirmed): matrix id, TF class/family, assay type (SELEX / ChIP-seq / PBM / DAP-seq), IUPAC consensus, PubMed refs, logo URL. Fuzzy name hits for _other_ genes are quarantined in `name_only_matches`. Arabidopsis-heavy coverage.                                |
| 25  | TF motifs (live)        | `jaspar_motif`                          | One JASPAR profile by matrix id (e.g. `MA0570.1`, or `MA0570` for the newest version) including the raw position-frequency matrix — the drill-down companion to `tf_binding_motifs`.                                                                                                                                                                                  |
| 26  | Interactions (live)     | `experimental_interactions`             | ThaleMine CURATED EXPERIMENTAL interaction partners (BioGRID / IntAct / PSI-MI) for an Arabidopsis locus — per partner: detection method (two hybrid, pull down, ...), PSI-MI relationship type, physical vs genetic, source DB, PubMed IDs, and an evidence count. The experimental counterpart to `string_interactions` (predicted / text-mined). Arabidopsis only. |
| 27  | Function (live)         | `locus_gene_rifs`                       | ThaleMine curated GeneRIF statements — one-sentence, manually curated descriptions of what the gene does, each tied to a PubMed ID (HY5 has 114). Citable functional context that GO terms and raw abstracts don't provide. Arabidopsis only.                                                                                                                         |
| 28  | Variation (live)        | `locus_variants`                        | Natural (EVA/dbSNP) variants overlapping a locus's genomic span via Ensembl `/overlap/region` — id, source, consequence class, alleles, clinical significance. variant_count + truncated. All 12 organisms.                                                                                                                                                           |
| 29  | Variation (live)        | `vep_annotate`                          | Ensembl VEP consequence prediction for a variant (region + allele, not locus) — most-severe consequence + per-transcript SO terms, IMPACT, SIFT/PolyPhen. All 12 organisms.                                                                                                                                                                                           |
| 30  | Orthology (live)        | `panther_family`                        | PANTHER protein family + subfamily (id + name), GO terms by aspect, protein class, and pathways. found=false when unclassified. All 12 organisms.                                                                                                                                                                                                                     |
| 31  | Orthology (live)        | `orthodb_orthologs`                     | OrthoDB ortholog group (name, evolutionary rate) + cross-species member genes at the Viridiplantae level. organism_count + truncated. All 12 organisms.                                                                                                                                                                                                               |
| 32  | Diversity (live)        | `aragwas_associations`                  | AraGWAS genome-wide association hits per locus — score, MAF, SNP effect, phenotype/study. Arabidopsis-only.                                                                                                                                                                                                                                                           |
| 33  | Diversity (live)        | `arabidopsis_natural_variation`         | 1001 Genomes natural-variation SNP effects across 1135 accessions — chr, position, effect, impact, amino-acid change, transcript + gene span. Arabidopsis-only.                                                                                                                                                                                                       |
| 34  | Batch (live)            | `batch_*` (twelve variants)             | Parallel per-locus fanout for tools 1–6, 8–12, 14. Up to 50 loci per call.                                                                                                                                                                                                                                                                                            |
| 35  | Synthesis (live)        | `*_synth` / `consensus_homologs` (four) | Compose 2–5 backends in parallel, return a `SynthesisEnvelope` with per-step status.                                                                                                                                                                                                                                                                                  |
| 36  | Synthesis (live)        | `gene_report`                           | One-shot "tell me about this gene" dossier — annotation + xrefs + protein + domains + GO + KEGG + STRING + literature composed into a rendered Markdown `result.markdown` (+ structured `result.sections`).                                                                                                                                                           |

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
| [`gene_report_AT1G01010.md`](examples/gene_report_AT1G01010.md)                           | One-shot Markdown gene dossier — 7 backends composed, with graceful KEGG degradation.   |
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

| URI                           | What                                                                                                                                                   |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `pgmcp://cache/stats`         | Per-backend `TTLCache` rollup — `{hits, misses, size}` for each live backend.                                                                          |
| `pgmcp://organisms/phytozome` | Slug → Phytozome `organism_id` map.                                                                                                                    |
| `pgmcp://backends/status`     | Per-backend liveness rollup — `name`, `base_url`, `kind`, `subscription_gated`.                                                                        |
| `pgmcp://organisms/coverage`  | Markdown table of all 12 supported plants × 9 ID slots (ncbi_taxid / ensembl / phytozome / string / europe_pmc / kegg / atted / gprofiler / plantcyc). |

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
.venv/bin/pip install -e '.[dev]'                         # or: uv sync --extra dev
.venv/bin/pytest -q                                       # unit tests
PLANT_GENOMICS_MCP_LIVE=1 .venv/bin/pytest -q             # adds live network probes
PLANT_GENOMICS_MCP_STDIO_SMOKE=1 .venv/bin/pytest -q      # adds stdio smoke
.venv/bin/ruff check .
```

With `uv`, pass `--extra dev` — a bare `uv sync` omits (and removes) the test
dependencies. See [CONTRIBUTING.md](CONTRIBUTING.md#dev-setup).

CI runs the unit suite + the stdio smoke on every push/PR (matrix:
Python 3.11, 3.12, 3.13, 3.14 — the full `requires-python` range). The
live-network gate is **not** run in CI to avoid flakes from upstream
availability.

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
