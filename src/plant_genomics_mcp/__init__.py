"""Plant genomics MCP server.

Forty-eight MCP tools across 22 backends for plant gene-record lookup,
biological-context analysis, and cross-source synthesis. Live backends
include Ensembl Plants, Phytozome BioMart, UniProtKB, Europe PMC, QuickGO,
Planteome (PO/TO ontology), PlantCyc/PMN (metabolic pathways), g:Profiler
(GO/KEGG enrichment), AlphaFold DB (predicted structure), PDBe (experimental
structures), InterPro (protein
domains), JASPAR (TF binding motifs), PANTHER (protein families), OrthoDB (orthology), AraGWAS
(Arabidopsis GWAS), 1001 Genomes (Arabidopsis natural variation), NCBI BLAST,
Gramene homology, KEGG pathways, STRING-DB
interactions, ATTED-II coexpression, and BAR
(Bio-Analytic Resource for Plant Biology, U Toronto — Global Core Biodata
Resource 2023). v1.17 adds ``tf_binding_motifs`` + ``jaspar_motif`` — JASPAR
curated transcription-factor DNA-binding profiles per locus, the cis-regulatory
axis, UniProt-confirmed against JASPAR's fuzzy name search. v1.16 adds ``experimental_structures`` — PDBe deposited
X-ray/cryo-EM/NMR structures per locus (the experimentally-solved companion to
``alphafold_structure``), UniProt-keyed. v1.15 adds the variation, orthology, and Arabidopsis-diversity
tier: ``locus_variants`` + ``vep_annotate`` (Ensembl variation/VEP),
``panther_family``, ``orthodb_orthologs``, ``aragwas_associations``, and
``arabidopsis_natural_variation``. v1.14 adds ``alphafold_structure`` + ``interpro_domains`` —
the protein structure + domain-architecture view, both keyed on the
locus→UniProt resolution the server already performs; InterPro domains also
feed ``gene_report``. v1.13 upgrades ``plantcyc_locus_info`` from a stub to a
live PlantCyc/PMN client — the BioCyc web-services API is free (the earlier
"subscription-gated" classification was wrong, re-probed 2026-07-19). v0.8
adds 4 synthesis tools that compose
the live backends in parallel — see ``synthesis.py``. v0.10 silently
upgrades ``tair_locus_info`` to a direct alias of ``bar_gene_summary``
(BAR ThaleMine mirrors TAIR curator data without the Phoenix
Bioinformatics paid subscription). See ``server.py`` for the full tool
catalog.
"""

__version__ = "1.17.0"
