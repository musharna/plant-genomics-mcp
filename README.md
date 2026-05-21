# plant-genomics-mcp

Plant-genomics MCP server for Claude Code. Looks up gene metadata by TAIR-style locus identifier from the free public plant-genomics services.

Sibling of the taxon-agnostic [`genomics-mcp`](https://github.com/) (Ensembl + KEGG).

## Tools

| Tool                          | Backend                                              | Status                                                                                                                                                                                       |
| ----------------------------- | ---------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ensembl_plants_lookup_locus` | Ensembl Plants REST (`rest.ensembl.org`)             | live                                                                                                                                                                                         |
| `phytozome_lookup_locus`      | Phytozome BioMart XML (`phytozome-next.jgi.doe.gov`) | live                                                                                                                                                                                         |
| `tair_locus_info`             | TAIR                                                 | informational stub — TAIR's free per-locus REST API was retired (Phoenix Bioinformatics subscription gate, probed 2026-05-21). Returns a structured redirect to the two live backends.       |
| `plantcyc_locus_info`         | PlantCyc                                             | informational stub — BioCyc PLANT orgid is subscription-gated (SRI/Phoenix, probed 2026-05-21). Returns a structured redirect. MetaCyc parent is public but lacks Arabidopsis gene mappings. |

### `ensembl_plants_lookup_locus`

Fetches a gene record from Ensembl Plants. Default species is `arabidopsis_thaliana`.

```jsonc
// arguments
{"locus": "AT1G01010"}
// → {"id": "AT1G01010", "display_name": "NAC001", "biotype": "protein_coding", ...}

{"locus": "Os01g0100100", "species": "oryza_sativa"}
```

### `phytozome_lookup_locus`

Fetches a gene record from Phytozome BioMart. Default organism is Arabidopsis thaliana TAIR10 (`organism_id=167`). Pass `organism_id=` for other proteomes — see `KNOWN_ORGANISMS` in `src/plant_genomics_mcp/phytozome.py` for hints (only Arabidopsis is live-verified; others are unverified hints).

```jsonc
{ "locus": "AT1G01010" }
// → {"organism_name": "Athaliana_TAIR10", "gene_name": "AT1G01010", "chromosome": "Chr1",
//    "gene_start": "3631", "gene_end": "5899", "strand": "1", "description": "..."}
```

### `tair_locus_info` / `plantcyc_locus_info`

Pure-data informational stubs — they do **not** fetch annotation data. Both services gate their free per-locus REST behind paid subscriptions. Returns a structured record with `status: "subscription_required"`, the web URL, and a redirect to `ensembl_plants_lookup_locus` / `phytozome_lookup_locus` which cover the same Arabidopsis annotation.

## Install

From PyPI (once published):

```bash
pipx install plant-genomics-mcp
claude mcp add plant-genomics --scope local -- plant-genomics-mcp
```

From source:

```bash
git clone https://github.com/mjarnold/plant-genomics-mcp.git
cd plant-genomics-mcp
python -m venv .venv && .venv/bin/pip install -e .
claude mcp add plant-genomics --scope local -- "$(pwd)/.venv/bin/plant-genomics-mcp"
```

## Development

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q                                  # mocked tests
PLANT_GENOMICS_MCP_LIVE=1 .venv/bin/pytest -q        # adds live-network probes
.venv/bin/ruff check .
```

The `PLANT_GENOMICS_MCP_LIVE=1` gate runs additional tests that hit the real Ensembl Plants / Phytozome endpoints — useful for catching wire-format drift.

## License

MIT — see [`LICENSE`](LICENSE). Underlying services (Ensembl Plants, Phytozome, TAIR, PlantCyc) have their own terms of use; consult each before bulk querying.
