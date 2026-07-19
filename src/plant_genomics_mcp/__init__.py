"""Plant genomics MCP server.

Thirty-two MCP tools across 11 backends for plant gene-record lookup,
biological-context analysis, and cross-source synthesis. Live backends
include Ensembl Plants, Phytozome BioMart, UniProtKB, Europe PMC, QuickGO,
NCBI BLAST, Gramene homology, KEGG pathways, STRING-DB interactions,
ATTED-II coexpression, and BAR (Bio-Analytic Resource for Plant Biology,
U Toronto — Global Core Biodata Resource 2023). PlantCyc ships as an
informational-redirect stub (BioCyc PLANT orgid is paid-only,
controller-verified 2026-05-21). v0.8 adds 4 synthesis tools that compose
the live backends in parallel — see ``synthesis.py``. v0.10 silently
upgrades ``tair_locus_info`` to a direct alias of ``bar_gene_summary``
(BAR ThaleMine mirrors TAIR curator data without the Phoenix
Bioinformatics paid subscription). See ``server.py`` for the full tool
catalog.
"""

__version__ = "1.10.0"
