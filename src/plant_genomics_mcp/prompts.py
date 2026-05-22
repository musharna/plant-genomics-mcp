"""Server-defined prompts surfaced over MCP ``prompts/list`` + ``prompts/get``.

These are NOT tool invocations — they're parameterized natural-language
instructions a client can offer the user as one-click workflows. Each
prompt renders to a single ``user`` ``PromptMessage`` whose text walks
the chat model through a deterministic chain of this server's tools.

Two prompts ship in P2.18:

  * ``analyze_locus``  — args: ``locus`` (required), ``species`` (opt,
    default ``arabidopsis_thaliana``). Drives the canonical
    multi-backend gene-profile walkthrough: Ensembl annotation →
    cross-refs → UniProt protein → literature → GO terms.
  * ``find_homologs``  — args: ``sequence`` (required), ``program`` (opt,
    default ``blastp``). Drives a BLAST → top-hit-resolution chain
    using ``blast_sequence`` followed by Ensembl / UniProt lookups on
    the hit accessions.

Why prompts (vs. just describing the chain in a tool docstring):
prompts/list is the discoverable surface clients use to populate a
slash-command menu — bundling the chain into a named prompt makes the
workflow one selection deep instead of forcing the user to remember the
ordering.

Wiring sits in ``server.py``: ``@server.list_prompts()`` returns
``PROMPTS``, ``@server.get_prompt()`` dispatches via ``get_prompt``.
"""

from __future__ import annotations

from mcp import types

from plant_genomics_mcp.errors import NotFoundError

ANALYZE_LOCUS = "analyze_locus"
FIND_HOMOLOGS = "find_homologs"
BIOLOGICAL_CONTEXT = "biological_context"
DEFAULT_TOP_N = 10

DEFAULT_SPECIES = "arabidopsis_thaliana"
DEFAULT_BLAST_PROGRAM = "blastp"
_BLAST_PROGRAMS = {"blastn", "blastp", "blastx", "tblastn", "tblastx"}


PROMPTS: list[types.Prompt] = [
    types.Prompt(
        name=ANALYZE_LOCUS,
        description=(
            "Walk the assistant through a full gene profile for a plant "
            "locus: Ensembl annotation, cross-references, UniProt protein "
            "record, recent literature, and GO term summary. Chains five "
            "tools in a deterministic order."
        ),
        arguments=[
            types.PromptArgument(
                name="locus",
                description="Locus identifier, e.g. AT1G01010 (Arabidopsis NAC001).",
                required=True,
            ),
            types.PromptArgument(
                name="species",
                description=(
                    f"Ensembl species slug (default {DEFAULT_SPECIES}). "
                    "Used for ensembl_plants_lookup_locus, get_gene_xrefs, "
                    "and locus_literature."
                ),
                required=False,
            ),
        ],
    ),
    types.Prompt(
        name=FIND_HOMOLOGS,
        description=(
            "Run a BLAST sequence-similarity search against NCBI and "
            "resolve the top hits against Ensembl Plants / UniProt. "
            "Chains blast_sequence with the per-hit lookup tools."
        ),
        arguments=[
            types.PromptArgument(
                name="sequence",
                description="Raw or FASTA-formatted query sequence (protein or nucleotide).",
                required=True,
            ),
            types.PromptArgument(
                name="program",
                description=(
                    f"BLAST program (default {DEFAULT_BLAST_PROGRAM}). One of "
                    "blastn / blastp / blastx / tblastn / tblastx."
                ),
                required=False,
            ),
        ],
    ),
    types.Prompt(
        name=BIOLOGICAL_CONTEXT,
        description=(
            "Build a biological-context profile for an Arabidopsis locus by "
            "chaining homology (Gramene) → pathways (KEGG) → interactions "
            "(STRING) → coexpression (ATTED-II). Cross-references the result "
            "lists to surface high-confidence functional partners."
        ),
        arguments=[
            types.PromptArgument(
                name="locus",
                description="Arabidopsis AGI locus, e.g. AT1G01010",
                required=True,
            ),
            types.PromptArgument(
                name="top_n",
                description=(
                    "Caps STRING partners and ATTED-II neighbors (default 10). "
                    "MCP prompt args are always strings; we cast to int internally."
                ),
                required=False,
            ),
        ],
    ),
]


def _render_analyze_locus(locus: str, species: str) -> str:
    return (
        f"Build a complete gene profile for plant locus {locus!r} "
        f"(species: {species}). Use this MCP server's tools in this order, "
        "passing the same locus + species to each:\n"
        "\n"
        f"1. `ensembl_plants_lookup_locus` with locus={locus!r}, species={species!r} "
        "— fetch the canonical Ensembl Plants annotation (biotype, location, description).\n"
        f"2. `get_gene_xrefs` with locus={locus!r}, species={species!r} — list "
        "cross-references (UniProt, NCBI Gene, TAIR, ArrayExpress…) and note the "
        "primary UniProt accession.\n"
        f"3. `resolve_locus_to_uniprot` with locus={locus!r} — fetch the canonical "
        "UniProt record (gene names, organism, sequence length, recommended name).\n"
        f"4. `locus_literature` with locus={locus!r}, species={species!r}, size=10 "
        "— pull recent Europe PMC articles citing this locus.\n"
        f"5. `locus_go_annotations` with locus={locus!r} — list GO term annotations "
        "(molecular_function / biological_process / cellular_component).\n"
        "\n"
        "Then summarize: what is this gene, what does its product do, where is it "
        "expressed or active, and what are the 2-3 most-cited papers about it. If "
        "any step returns a `[NotFoundError]`, report which one and continue with "
        "the remaining steps — the locus may exist in one backend but not another."
    )


def _render_find_homologs(sequence: str, program: str) -> str:
    return (
        f"Find homologs for the supplied query sequence using NCBI BLAST "
        f"(program: {program}).\n"
        "\n"
        f"1. Call `blast_sequence` with program={program!r}, hitlist_size=10, and "
        f"the sequence below. Wait for the search to complete (the tool emits "
        "`notifications/progress` per poll). If the call raises `[NotFoundError]` "
        "with an RID, the search exceeded `max_wait`; retry once with a larger "
        "`max_wait` and report the RID.\n"
        "2. For each of the top 3 hits, extract the accession and description. If "
        "the accession looks like a UniProt entry (e.g. `P12345`, `Q9LIV2`), call "
        "`resolve_locus_to_uniprot` with `locus=<accession>` to enrich it. Otherwise "
        "note it for the user as a non-UniProt hit (RefSeq, GenBank, …).\n"
        "3. Summarize: what protein/sequence family does the query belong to, what "
        "species are represented in the top hits, and how strong is the best hit "
        "(bit score + e-value).\n"
        "\n"
        "Query sequence:\n"
        f"```\n{sequence}\n```"
    )


def _render_biological_context(locus: str, top_n: int) -> str:
    return (
        f"Build a biological-context profile for Arabidopsis locus {locus!r}. "
        "Use this MCP server's tools in this order:\n"
        "\n"
        f"1. `gramene_homologs` with locus={locus!r}, homology_type='ortholog' "
        "— retrieve orthologs across plant species from Gramene compara.\n"
        f"2. `kegg_pathways` with locus={locus!r} — list KEGG pathway "
        "memberships in Arabidopsis.\n"
        f"3. `resolve_locus_to_uniprot` with locus={locus!r} — fetch the "
        "canonical UniProt accession (needed for STRING).\n"
        f"4. `string_interactions` with locus_or_accession=<accession from step 3>, "
        f"limit={top_n} — fetch first-neighbor protein-protein interactors "
        "(STRING combined + per-channel sub-scores).\n"
        f"5. `atted_coexpression` with locus={locus!r}, top_n={top_n} — "
        "fetch co-expression neighbors from ATTED-II.\n"
        "\n"
        "Then synthesize: cross-reference the three sets (orthologs, "
        "interactors, coexpression neighbors). Interactors that are ALSO "
        "in the coexpression neighbor set are higher-confidence functional "
        "partners. Orthologs that recur as interactors across plant species "
        "suggest a conserved functional module. Flag any pathway from step 2 "
        "that contains multiple interactors or coexpression partners — that "
        "is a candidate functional context for the locus. If any step "
        "returns a [NotFoundError], report which one and continue with the "
        "remaining steps."
    )


async def get_prompt(name: str, arguments: dict[str, str] | None) -> types.GetPromptResult:
    """Render one of the named prompts to a single user-role message.

    Raises ``NotFoundError`` for unknown names so the server's standard
    typed-error prefix lands on the wire. Argument validation is strict
    on required fields and lenient on optional fields (unknown extras
    are ignored — MCP clients sometimes pass them).
    """
    args = arguments or {}

    if name == ANALYZE_LOCUS:
        locus = args.get("locus")
        if not locus:
            raise NotFoundError(f"prompt {name!r}: missing required argument 'locus'")
        species = args.get("species") or DEFAULT_SPECIES
        text = _render_analyze_locus(locus, species)
        description = f"Full gene profile for {locus} ({species})"
    elif name == FIND_HOMOLOGS:
        sequence = args.get("sequence")
        if not sequence:
            raise NotFoundError(f"prompt {name!r}: missing required argument 'sequence'")
        program = args.get("program") or DEFAULT_BLAST_PROGRAM
        if program not in _BLAST_PROGRAMS:
            raise NotFoundError(
                f"prompt {name!r}: program {program!r} must be one of {sorted(_BLAST_PROGRAMS)}"
            )
        text = _render_find_homologs(sequence, program)
        description = f"BLAST homolog search ({program}, {len(sequence)} chars)"
    elif name == BIOLOGICAL_CONTEXT:
        locus = args.get("locus")
        if not locus:
            raise NotFoundError(f"prompt {name!r}: missing required argument 'locus'")
        top_n_raw = args.get("top_n")
        if top_n_raw is None or top_n_raw == "":
            top_n = DEFAULT_TOP_N
        else:
            try:
                top_n = int(top_n_raw)
            except ValueError:
                raise NotFoundError(
                    f"prompt {name!r}: top_n {top_n_raw!r} must be parseable as int"
                )
        text = _render_biological_context(locus, top_n)
        description = f"Biological-context profile for {locus} (top_n={top_n})"
    else:
        raise NotFoundError(f"unknown prompt: {name!r}")

    return types.GetPromptResult(
        description=description,
        messages=[
            types.PromptMessage(
                role="user",
                content=types.TextContent(type="text", text=text),
            ),
        ],
    )
