"""Tests for the server-defined MCP prompts surface (P2.18)."""

from __future__ import annotations

import pytest

from plant_genomics_mcp import prompts
from plant_genomics_mcp.errors import NotFoundError


def test_prompt_catalog_has_three_entries() -> None:
    names = {p.name for p in prompts.PROMPTS}
    assert names == {
        prompts.ANALYZE_LOCUS,
        prompts.FIND_HOMOLOGS,
        prompts.BIOLOGICAL_CONTEXT,
    }


def test_each_prompt_has_description_and_arguments() -> None:
    for p in prompts.PROMPTS:
        assert p.description
        assert p.arguments
        for arg in p.arguments:
            assert arg.name
            assert arg.description


def test_analyze_locus_marks_locus_required_and_organism_optional() -> None:
    p = next(p for p in prompts.PROMPTS if p.name == prompts.ANALYZE_LOCUS)
    by_name = {a.name: a for a in p.arguments or []}
    assert by_name["locus"].required is True
    # required is None or False is acceptable for optional args
    assert not by_name["organism"].required


def test_find_homologs_marks_sequence_required_and_program_optional() -> None:
    p = next(p for p in prompts.PROMPTS if p.name == prompts.FIND_HOMOLOGS)
    by_name = {a.name: a for a in p.arguments or []}
    assert by_name["sequence"].required is True
    assert not by_name["program"].required


@pytest.mark.asyncio
async def test_get_analyze_locus_renders_chain_with_all_five_tools() -> None:
    result = await prompts.get_prompt(prompts.ANALYZE_LOCUS, {"locus": "AT1G01010"})
    assert result.description
    assert "AT1G01010" in result.description
    assert len(result.messages) == 1
    msg = result.messages[0]
    assert msg.role == "user"
    text = msg.content.text
    assert "AT1G01010" in text
    assert "arabidopsis_thaliana" in text  # default species applied
    for tool in (
        "ensembl_plants_lookup_locus",
        "get_gene_xrefs",
        "resolve_locus_to_uniprot",
        "locus_literature",
        "locus_go_annotations",
    ):
        assert tool in text


@pytest.mark.asyncio
async def test_get_analyze_locus_honors_organism_arg() -> None:
    result = await prompts.get_prompt(
        prompts.ANALYZE_LOCUS,
        {"locus": "Os01g0100100", "organism": "oryza_sativa"},
    )
    text = result.messages[0].content.text
    assert "Os01g0100100" in text
    assert "oryza_sativa" in text
    assert "arabidopsis_thaliana" not in text


@pytest.mark.asyncio
async def test_get_analyze_locus_accepts_organism_alias() -> None:
    """organism='thale cress' resolves to arabidopsis_thaliana; scientific name displayed."""
    result = await prompts.get_prompt(
        prompts.ANALYZE_LOCUS,
        {"locus": "AT1G01010", "organism": "thale cress"},
    )
    text = result.messages[0].content.text
    assert "AT1G01010" in text
    assert "Arabidopsis thaliana" in text  # scientific display name
    assert "arabidopsis_thaliana" in text  # canonical slug in tool calls


@pytest.mark.asyncio
async def test_get_analyze_locus_unknown_organism_raises_typed() -> None:
    from plant_genomics_mcp.errors import NotFoundError as _NotFound

    with pytest.raises(_NotFound, match="OrganismNotFound"):
        await prompts.get_prompt(
            prompts.ANALYZE_LOCUS,
            {"locus": "AT1G01010", "organism": "zucchini"},
        )


@pytest.mark.asyncio
async def test_get_analyze_locus_missing_locus_raises_typed() -> None:
    with pytest.raises(NotFoundError, match="missing required argument 'locus'"):
        await prompts.get_prompt(prompts.ANALYZE_LOCUS, {})


@pytest.mark.asyncio
async def test_get_find_homologs_renders_with_blastp_default() -> None:
    seq = "MEDQVGFGFRPNDEELVGHYLRNKIESQTSRSAIEVDLNK"
    result = await prompts.get_prompt(prompts.FIND_HOMOLOGS, {"sequence": seq})
    assert "blastp" in result.description
    text = result.messages[0].content.text
    assert seq in text
    assert "blast_sequence" in text
    assert "resolve_locus_to_uniprot" in text


@pytest.mark.asyncio
async def test_get_find_homologs_unknown_program_raises_typed() -> None:
    with pytest.raises(NotFoundError, match="program 'blastz' must be one of"):
        await prompts.get_prompt(prompts.FIND_HOMOLOGS, {"sequence": "MNSAKQ", "program": "blastz"})


@pytest.mark.asyncio
async def test_get_unknown_prompt_raises_typed() -> None:
    with pytest.raises(NotFoundError, match="unknown prompt"):
        await prompts.get_prompt("nonexistent", {})


@pytest.mark.asyncio
async def test_get_prompt_accepts_none_arguments() -> None:
    """MCP spec allows arguments to be omitted — should still raise for required args."""
    with pytest.raises(NotFoundError, match="missing required argument"):
        await prompts.get_prompt(prompts.ANALYZE_LOCUS, None)


@pytest.mark.asyncio
async def test_biological_context_renders_chain():
    result = await prompts.get_prompt(prompts.BIOLOGICAL_CONTEXT, {"locus": "AT1G01010"})
    assert "AT1G01010" in result.description
    assert len(result.messages) == 1
    text = result.messages[0].content.text
    for tool in (
        "gramene_homologs",
        "kegg_pathways",
        "resolve_locus_to_uniprot",
        "string_interactions",
        "atted_coexpression",
    ):
        assert tool in text, f"chain missing {tool}"


@pytest.mark.asyncio
async def test_biological_context_top_n_propagates():
    result = await prompts.get_prompt(
        prompts.BIOLOGICAL_CONTEXT, {"locus": "AT1G01010", "top_n": "30"}
    )
    text = result.messages[0].content.text
    assert "limit=30" in text or "top_n=30" in text


@pytest.mark.asyncio
async def test_biological_context_missing_locus_raises():
    with pytest.raises(NotFoundError) as exc:
        await prompts.get_prompt(prompts.BIOLOGICAL_CONTEXT, {})
    assert "[NotFoundError]" in str(exc.value)


@pytest.mark.asyncio
async def test_biological_context_uses_default_top_n_when_omitted():
    """Default DEFAULT_TOP_N (int 10) renders as ``limit=10`` / ``top_n=10`` in the chain."""
    result = await prompts.get_prompt(prompts.BIOLOGICAL_CONTEXT, {"locus": "AT1G01010"})
    text = result.messages[0].content.text
    assert "limit=10" in text
    assert "top_n=10" in text
    assert result.description.endswith("top_n=10)")


@pytest.mark.asyncio
async def test_biological_context_bad_top_n_raises_typed():
    with pytest.raises(NotFoundError, match="top_n 'abc' must be parseable as int"):
        await prompts.get_prompt(prompts.BIOLOGICAL_CONTEXT, {"locus": "AT1G01010", "top_n": "abc"})


@pytest.mark.asyncio
async def test_biological_context_marks_locus_required_and_organism_optional() -> None:
    p = next(p for p in prompts.PROMPTS if p.name == prompts.BIOLOGICAL_CONTEXT)
    by_name = {a.name: a for a in p.arguments or []}
    assert by_name["locus"].required is True
    assert not by_name["organism"].required


@pytest.mark.asyncio
async def test_biological_context_arabidopsis_passes_canonical_to_organism_aware_tools() -> None:
    """Default arabidopsis renders the full 5-step chain with organism= on the
    tools that take it (gramene/uniprot/string); kegg + atted are organism-fixed."""
    result = await prompts.get_prompt(prompts.BIOLOGICAL_CONTEXT, {"locus": "AT1G01010"})
    text = result.messages[0].content.text
    assert "organism='arabidopsis_thaliana'" in text
    # Full 5-step chain still present for arabidopsis.
    for tool in ("gramene_homologs", "kegg_pathways", "string_interactions", "atted_coexpression"):
        assert tool in text


@pytest.mark.asyncio
async def test_biological_context_non_arabidopsis_skips_kegg_and_atted() -> None:
    """Non-Arabidopsis renders Gramene+UniProt+STRING only; KEGG and ATTED are
    skipped because those backends only ship Arabidopsis data (audit C5)."""
    result = await prompts.get_prompt(
        prompts.BIOLOGICAL_CONTEXT,
        {"locus": "Os01g0100100", "organism": "oryza_sativa"},
    )
    text = result.messages[0].content.text
    assert "oryza_sativa" in text
    assert "arabidopsis_thaliana" not in text
    # Three steps that DO run.
    for tool in ("gramene_homologs", "resolve_locus_to_uniprot", "string_interactions"):
        assert tool in text
    # Two steps that are skipped.
    assert "kegg_pathways" not in text
    assert "atted_coexpression" not in text
    # Synthesis note explains the omission.
    assert "KEGG and ATTED-II are omitted" in text


@pytest.mark.asyncio
async def test_biological_context_organism_alias_resolves() -> None:
    """Common name 'rice' resolves to oryza_sativa; scientific name displayed."""
    result = await prompts.get_prompt(
        prompts.BIOLOGICAL_CONTEXT,
        {"locus": "Os01g0100100", "organism": "rice"},
    )
    text = result.messages[0].content.text
    assert "Oryza sativa" in text  # scientific display
    assert "organism='oryza_sativa'" in text  # canonical slug in tool calls


@pytest.mark.asyncio
async def test_biological_context_unknown_organism_raises_typed() -> None:
    with pytest.raises(NotFoundError, match="OrganismNotFound"):
        await prompts.get_prompt(
            prompts.BIOLOGICAL_CONTEXT,
            {"locus": "AT1G01010", "organism": "zucchini"},
        )
