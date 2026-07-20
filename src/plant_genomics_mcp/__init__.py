"""Plant genomics MCP server.

Thirty-seven MCP tools across 14 backends for plant gene-record lookup,
biological-context analysis, and cross-source synthesis. Live backends
include Ensembl Plants, Phytozome BioMart, UniProtKB, Europe PMC, QuickGO,
Planteome (PO/TO ontology), PlantCyc/PMN (metabolic pathways), g:Profiler
(GO/KEGG enrichment), NCBI BLAST, Gramene homology, KEGG pathways, STRING-DB
interactions, ATTED-II coexpression, and BAR
(Bio-Analytic Resource for Plant Biology, U Toronto — Global Core Biodata
Resource 2023). v1.13 upgrades ``plantcyc_locus_info`` from a stub to a live
PlantCyc/PMN client — the BioCyc web-services API is free (the earlier
"subscription-gated" classification was wrong, re-probed 2026-07-19). v0.8
adds 4 synthesis tools that compose
the live backends in parallel — see ``synthesis.py``. v0.10 silently
upgrades ``tair_locus_info`` to a direct alias of ``bar_gene_summary``
(BAR ThaleMine mirrors TAIR curator data without the Phoenix
Bioinformatics paid subscription). See ``server.py`` for the full tool
catalog.
"""

__version__ = "1.13.0"
