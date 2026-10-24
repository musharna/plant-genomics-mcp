# plant-genomics-mcp

plant-genomics-mcp is an MCP server that lets AI agents look up plant genes in 23
free public databases, from Ensembl Plants and UniProt to KEGG, STRING and
AlphaFold, with curated coverage of 12 plants.

[![CI](https://img.shields.io/github/actions/workflow/status/musharna/plant-genomics-mcp/test.yml?branch=main&label=CI)](https://github.com/musharna/plant-genomics-mcp/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/plant-genomics-mcp)](https://pypi.org/project/plant-genomics-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/plant-genomics-mcp)](https://pypi.org/project/plant-genomics-mcp/)
[![License](https://img.shields.io/pypi/l/plant-genomics-mcp)](https://github.com/musharna/plant-genomics-mcp/blob/main/LICENSE)
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.21636352.svg)](https://doi.org/10.5281/zenodo.21636352)
[![Glama](https://glama.ai/mcp/servers/musharna/plant-genomics-mcp/badges/score.svg)](https://glama.ai/mcp/servers/musharna/plant-genomics-mcp)
<!-- mcp-name: io.github.musharna/plant-genomics-mcp -->

<p align="center"><img src="https://raw.githubusercontent.com/musharna/plant-genomics-mcp/main/examples/assets/cc-answer.png" alt="Claude Code (Opus 5.5) with plant-genomics-mcp 1.29.1 answering a prompt about Arabidopsis NAC001 (AT1G01010): the Ensembl Plants record (Chr 1:3,631-5,899, protein_coding) and UniProtKB entry Q0WV96 (NAC1_ARATH, reviewed, 429 aa). Real output, trimmed" width="780"></p>

## Install

```bash
# uv fetches and runs it on demand
claude mcp add plant-genomics --scope local -- uvx plant-genomics-mcp
```

<details>
<summary>Other install paths (pipx, Docker, from source)</summary>

```bash
# pipx — installs the CLI onto your PATH
pipx install plant-genomics-mcp
claude mcp add plant-genomics --scope local -- plant-genomics-mcp

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

## Try it

Ask in plain language; the client picks the tools:

> **"Tell me everything about the Arabidopsis gene AT1G01010 — its
> function, GO terms, KEGG pathways, protein-interaction partners, and
> recent papers."**

For another species, name it or pass `organism=` (e.g. rice `Os01g0100100`,
`oryza_sativa`). A recorded ortholog run, where Claude checked three sources
and said which one does not back the rice match:

<p align="center"><img src="https://raw.githubusercontent.com/musharna/plant-genomics-mcp/main/examples/assets/cc-orthologs.png" alt="Claude Code (Opus 5.5) after 6 plant-genomics-mcp calls: AT1G01010's rice ortholog is Os07g0683200 (Gramene v69 ortholog_one2many, the only rice member of Ensembl gene tree EPlGT01160000455820); no maize ortholog in either; OrthoDB's NAC group has no rice or maize member, so it does not back the rice match" width="780"></p>

Prompts and full answers for both screenshots: [`examples/assets/cards/RUN.md`](examples/assets/cards/RUN.md).

A worked 64-call run over 114 genes in three organisms, with the 51 gaps it
logged (39 since closed), is in [`examples/arf_family/PAGE.md`](examples/arf_family/PAGE.md).
Per-tool walkthroughs, including the one-call gene dossier
[`gene_report_AT1G01010.md`](examples/gene_report_AT1G01010.md), are in
[`examples/`](examples/).

## What the benchmark shows

Claude was compared with itself using web search. On 36 multi-step gene questions, 3 runs
each, Haiku 5.5 got 31 right in every run with the server and 9 with web search: a
pre-registered win (Holm-adjusted McNemar p = 2e-5), at 0.07 times the cost. Sonnet 5.5
tied (35 and 35; 0.65× the time, 1.49× the cost). With Opus 5.5 (the re-run 2026-10-18)
both got all 108 right; the server was faster at the median (0.77×, 95% CI 0.64 to 0.96)
and dearer (1.72×, 1.46 to 2.24).

<p align="center"><img src="https://raw.githubusercontent.com/musharna/plant-genomics-mcp/main/benchmarks/agent_qa/agent_qa.png" alt="Stacked bar chart of right, wrong and declined answers, 156 per setup. Claude alone: 36 right, 3 wrong, 117 declined. Claude with web search: 151 right, 4 wrong, 1 declined. Claude with plant-genomics-mcp: 156 right. Two no-model baselines: one constant answer per kind of question, 3 of 52 right; chromosome read from the gene ID, 0 of 12 right" width="780"></p>

The chart: Opus 5.5 on 52 plant gene-lookup questions, 151 of 156 right with web search and
156 of 156 with the server, not separated (McNemar p = 0.125). The key comes from the
databases the server reads, so this tests the lookups, not whether the databases are right.
We wrote the questions; an outside re-run is welcome. [Method](benchmarks/agent_qa/README.md);
[Haiku and Sonnet](benchmarks/agent_qa/README.md#haiku-and-sonnet-re-run-2026-10-20).

## Tools

**56 tools across 23 backends**:

| Area | Tools |
| --- | --- |
| Gene records | `ensembl_plants_lookup_locus`, `phytozome_lookup_locus`, `get_gene_xrefs`, `bar_gene_summary`, `tair_locus_info` |
| Sequence | `get_sequence`, `blast_sequence` |
| Protein and structure | `resolve_locus_to_uniprot`, `interpro_domains`, `alphafold_structure`, `experimental_structures` |
| Function | `locus_go_annotations`, `locus_plant_ontology`, `locus_gene_rifs`, `go_enrichment`, `locus_literature` |
| Pathways | `kegg_pathways`, `plantcyc_locus_info` |
| Interactions and expression | `string_interactions`, `experimental_interactions`, `bar_aiv_interactions`, `atted_coexpression`, `bar_efp_expression` |
| Homology and families | `gramene_homologs`, `ensembl_plants_paralogs`, `orthodb_orthologs`, `panther_family`, `gene_tree_members`, `entry_members` |
| TF motifs | `tf_binding_motifs`, `jaspar_motif` |
| Variation | `locus_variants`, `vep_annotate`, `aragwas_associations`, `arabidopsis_natural_variation` |
| Regions and assemblies | `ensembl_region_query`, `ensembl_plants_assembly` |
| Provenance | `upstream_release` |
| Batch and synthesis | `batch_*` (12), `batch_locus_call`, `*_synth` (3), `consensus_homologs`, `gene_report` |

Most take a locus (e.g. `AT1G01010`) and an optional `organism=`. Every tool is
marked read-only. What each tool does, the list of backends, the MCP resources
and prompts, all environment variables and the error model:
[`docs/reference.md`](docs/reference.md).

## Configuration

stdio (`plant-genomics-mcp`, the default) needs no configuration.
`plant-genomics-mcp-http` serves streamable HTTP at `http://host:port/mcp`;
there is no public endpoint, so you run it yourself, behind your own token.

| Variable                        | When                | Effect                                                                                                                |
| ------------------------------- | ------------------- | --------------------------------------------------------------------------------------------------------------------- |
| `PLANT_GENOMICS_MCP_HTTP_TOKEN` | HTTP transport only | Bearer token for `/mcp`; **must be ≥32 chars** or the HTTP server aborts at startup. Generate `openssl rand -hex 32`. |
| `PLANT_GENOMICS_MCP_NCBI_EMAIL` | If you use BLAST    | NCBI etiquette contact. Unset → placeholder + per-call warning; NCBI may throttle.                                    |

## Development

```bash
.venv/bin/pip install -e '.[dev]'                         # or: uv sync --extra dev
.venv/bin/pytest -q                                       # unit tests
PLANT_GENOMICS_MCP_LIVE=1 .venv/bin/pytest -q             # adds live network probes
PLANT_GENOMICS_MCP_STDIO_SMOKE=1 .venv/bin/pytest -q      # adds stdio smoke
.venv/bin/ruff check .
```

A bare `uv sync` omits the test dependencies. CI runs the unit suite and the
stdio smoke on Python 3.11–3.14 with the network blocked. A weekly job checks
27 loci against a frozen snapshot and pages on drift
([docs/benchmarking.md](docs/benchmarking.md)). Setup: [CONTRIBUTING.md](CONTRIBUTING.md#dev-setup).
Release notes: [`CHANGELOG.md`](CHANGELOG.md).

## License

MIT, see [`LICENSE`](LICENSE). The underlying services (Ensembl Plants,
Phytozome, TAIR, PlantCyc, BAR) have their own terms of use; check them before
bulk querying.
