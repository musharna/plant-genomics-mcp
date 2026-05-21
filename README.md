# genomics-mcp

Taxon-agnostic genomics MCP server for Claude Code. Wraps two well-established public REST APIs:

- **Ensembl REST** (`rest.ensembl.org`) — gene lookup, sequence retrieval, cross-references, homology
- **KEGG REST** (`rest.kegg.jp`) — pathway / compound / gene lookup; rate-limited to 3 req/sec per KEGG TOS (academic-only license)

For plant-specific resources (TAIR, Phytozome, Ensembl Plants), see the roadmapped sibling `plant-genomics-mcp`.

## Install

```bash
claude mcp add genomics --scope local -- uvx --from /path/to/genomics-mcp genomics-mcp
```

Or once published to PyPI:

```bash
claude mcp add genomics --scope local -- uvx genomics-mcp
```

## Tools

### Ensembl

- `ensembl_lookup_id` — fetch metadata for an Ensembl ID
- `ensembl_lookup_symbol` — resolve a gene symbol to Ensembl IDs
- `ensembl_sequence_by_id` — retrieve sequence (cdna/cds/protein/genomic) for an ID
- `ensembl_xrefs_by_id` — list cross-references in external databases
- `ensembl_homology_by_id` — orthologs/paralogs across species

### KEGG

- `kegg_find` — search across a KEGG database (genes, pathways, compounds, etc.)
- `kegg_get` — fetch a KEGG entry by ID
- `kegg_link` — find linked entries between two KEGG databases
- `kegg_conv` — convert between KEGG and external IDs (NCBI, UniProt, etc.)

## License

This MCP wrapper: MIT. Underlying services have their own terms — KEGG in particular is **academic-use-only** without a commercial EULA.
