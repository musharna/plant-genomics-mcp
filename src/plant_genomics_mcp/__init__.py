"""Plant genomics MCP server.

Twenty-seven MCP tools across 13 backends for plant gene-record lookup,
biological-context analysis, and cross-source synthesis. Live backends
include Ensembl Plants, Phytozome BioMart, UniProtKB, Europe PMC, QuickGO,
NCBI BLAST, Gramene homology, KEGG pathways, STRING-DB interactions, and
ATTED-II coexpression; TAIR / PlantCyc ship as informational-redirect
stubs that point at the free backends above (their per-locus REST is
paid-only, controller-verified 2026-05-21). v0.8 adds 4 synthesis tools that compose the live
backends in parallel — see ``synthesis.py``. See ``server.py`` for the
full tool catalog.
"""

__version__ = "0.8.0"
