"""The tool chain the dossier runner walks for every gene in `genes.tsv`.

Each entry is `(tool_name, build_args)`, where `build_args(locus, organism)`
returns the `arguments` object for one `tools/call`. Names and argument
names are checked against the server's live `tools/list` by
`tests/test_arf_chain.py` — this table is hand-written and nothing else
stops it drifting.

Two rows do not take the `{"locus", "organism"}` pair that the other
fourteen take, and both are recorded as gaps rather than smoothed over
here:

- `gramene_homologs` has no `organism` property at all.
- `string_interactions` calls its locus argument `locus_or_accession`.
"""

from __future__ import annotations

from collections.abc import Callable


def _lo(locus: str, organism: str) -> dict:
    return {"locus": locus, "organism": organism}


CHAIN: list[tuple[str, Callable[[str, str], dict]]] = [
    ("ensembl_plants_lookup_locus", _lo),
    ("resolve_locus_to_uniprot", _lo),
    ("interpro_domains", _lo),
    ("alphafold_structure", _lo),
    ("experimental_structures", _lo),
    ("tf_binding_motifs", _lo),
    ("panther_family", _lo),
    ("orthodb_orthologs", _lo),
    ("gramene_homologs", lambda locus, organism: {"locus": locus}),
    ("atted_coexpression", _lo),
    (
        "string_interactions",
        lambda locus, organism: {"locus_or_accession": locus, "organism": organism},
    ),
    ("aragwas_associations", _lo),
    ("locus_go_annotations", _lo),
    ("kegg_pathways", _lo),
    ("locus_literature", _lo),
    ("gene_report", _lo),
]
