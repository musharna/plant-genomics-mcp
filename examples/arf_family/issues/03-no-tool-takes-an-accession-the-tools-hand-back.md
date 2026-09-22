# Family, entry and gene-tree accessions come back from tools that nothing can take them into

**Filed as [#130](https://github.com/musharna/plant-genomics-mcp/issues/130) — open.** Written from the ARF family dossier in
[`examples/arf_family/`](../), re-run at server commit `967bc36`
(release `1.21.0`): 400 MCP calls over 48 genes x 16 tools, the family
itself from a 742-call enumeration through the same tools. Each section
below is one row of [`gaps.jsonl`](../gaps.jsonl), quoted as logged;
`Check` names the captured response to read it back from.

One issue: every identifier in these rows is produced by a tool on this server and consumed by none of the other 49.

Covers 2 rows of `gaps.jsonl`.

## `no-family-enumeration`

- **Attempted:** go from a family/entry accession the tools return back to loci
- **Returned:** no tool on the live server takes a family or entry accession as input. Of 50 tools, every input is a locus, a list of loci, a region, a sequence, or a JASPAR matrix_id; `panther_family` and `interpro_domains` are the only two whose names mention a family or domain and both go locus -> family, never family -> loci.
- **Expected:** a tool that takes PTHR31384 or IPR010525 and returns its members
- **Check:** `raw/AT1G19850__panther_family.json, raw/AT1G19850__interpro_domains.json, live tools/list`

## `identifier-with-no-tool`

- **Attempted:** follow a gramene_homologs gene_tree_id without leaving the tools
- **Returned:** each homolog is {target_locus, type, gene_tree_id} - e.g. {'C5167_014531', 'ortholog_one2many', 'EPlGT00940000167082'}. The missing taxon is documented ('The fl=homology projection does not carry per-row taxon, identity, or protein ID; pair with resolve_locus_to_uniprot'), and the server already has fetch_homolog_enrichment_batch (src/plant_genomics_mcp/gramene.py:131) returning a species slug per locus, but only synthesis.consensus_homologs uses it. The gene_tree_id has no dereferencing tool at all: no input in the live 50-tool list takes one.
- **Expected:** a tool that takes a gene_tree_id, and the species enrichment the server already computes surfaced on this tool
- **Check:** `raw/AT1G19850__gramene_homologs.json, raw/AT1G19850__orthodb_orthologs.json`
