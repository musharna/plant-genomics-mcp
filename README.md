# plant-genomics-mcp

Plant-specific genomics MCP server for Claude Code. Wraps **Ensembl Plants** (`rest.ensembl.org`, species-scoped to plants such as `arabidopsis_thaliana`) — gene lookup by TAIR-style locus identifier.

Sibling of the taxon-agnostic `genomics-mcp` (Ensembl + KEGG). Phytozome, TAIR, and PlantCyc backends are roadmapped as separate follow-up tasks.

## Install

```bash
claude mcp add plant-genomics --scope local -- /home/mjarnold/plant-genomics-mcp/.venv/bin/plant-genomics-mcp
```

## Tools

### Ensembl Plants

- `ensembl_plants_lookup_locus` — fetch metadata for a plant locus (e.g. `AT1G01010` in `arabidopsis_thaliana`)

## Roadmap

- Phytozome (BioMart XML API)
- TAIR REST (live-shape probe required first)
- PlantCyc (likely requires registration — stub-then-ship if so)

## License

This MCP wrapper: MIT. Underlying services have their own terms.
