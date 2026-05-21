# plant-genomics-mcp

> **4 tools** for plant-genomics locus lookup via the Model Context Protocol.
> Free, public sources: Ensembl Plants + Phytozome BioMart. TAIR + PlantCyc
> are informational stubs that redirect to the free alternatives (both
> services are paid-subscription-gated, probed 2026-05-21).

[![CI](https://github.com/mjarnold/plant-genomics-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/mjarnold/plant-genomics-mcp/actions/workflows/test.yml)
[![Docker](https://github.com/mjarnold/plant-genomics-mcp/actions/workflows/docker.yml/badge.svg)](https://github.com/mjarnold/plant-genomics-mcp/actions/workflows/docker.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

## Tools at a glance

| #   | Category              | Tool                          | What it does                                                           |
| --- | --------------------- | ----------------------------- | ---------------------------------------------------------------------- |
| 1   | Gene metadata (live)  | `ensembl_plants_lookup_locus` | Fetches gene record from Ensembl Plants REST (any plant species).      |
| 2   | Gene metadata (live)  | `phytozome_lookup_locus`      | Fetches gene record from Phytozome BioMart (any Phytozome proteome).   |
| 3   | Subscription redirect | `tair_locus_info`             | Returns subscription notice + redirect to (1) / (2). No upstream call. |
| 4   | Subscription redirect | `plantcyc_locus_info`         | Returns subscription notice + redirect to (1) / (2). No upstream call. |

Live tools take a TAIR-style locus (e.g. `AT1G01010`) plus optional
`species=` / `organism_id=` and return a structured gene record.
Subscription tools take a locus and return a structured redirect
record — they do not call the gated upstream.

All four tools publish JSON `outputSchema` for client-side validation
and EDAM ontology tags (`operation_2422` Data retrieval; topic
`topic_0780` Plant biology + `topic_0114` Gene structure) on `_meta`
for registry indexers.

## Transports

| Transport | Status  | How to launch                                            |
| --------- | ------- | -------------------------------------------------------- |
| stdio     | default | `plant-genomics-mcp` (after install) or via Docker below |
| SSE       | n/a     | Out of scope — stdio is the canonical MCP transport      |

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

### 2. `phytozome_lookup_locus`

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

### 3. `tair_locus_info` / 4. `plantcyc_locus_info`

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
  "rationale": "TAIR per-locus REST endpoints return 403; Phoenix Bioinformatics requires paid subscription.",
  "alternatives": ["ensembl_plants_lookup_locus", "phytozome_lookup_locus"],
  "alternatives_note": "Both alternatives return the same canonical Arabidopsis annotation; ensembl_plants_lookup_locus also covers other plant species (oryza_sativa, zea_mays, ...).",
}
```

## Error model

All four tools raise `PlantGenomicsError` subclasses; the MCP SDK
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

## License

MIT — see [`LICENSE`](LICENSE). Underlying services (Ensembl Plants,
Phytozome, TAIR, PlantCyc) have their own terms of use; consult each
before bulk querying.
