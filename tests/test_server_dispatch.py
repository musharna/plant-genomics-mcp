"""End-to-end coverage of the ``server._dispatch`` arg-routing table (audit I2).

Before this, only the offline ``plantcyc_locus_info`` stub was driven through
the dispatcher; every network-backed arm's argument-key read (``args["locus"]``
vs ``args["loci"]``), its default-organism literal, and the synthesis
``env.model_dump()`` calls were validated only at the backend-module level — so
a wrong arg key or default (the exact class of bug the v0.9 audit caught for
``string_interactions``) would pass the entire mocked + CI stdio suite and
surface only at live invocation.

This test monkeypatches each backend entrypoint to a recording stub and drives
every tool name through ``server._dispatch``, asserting (a) the identifier
(locus / loci / sequence) reaches the backend as a positional arg, and (b) the
resolved default organism is forwarded when ``organism`` is omitted. A
coverage-lock test asserts the spec table matches ``server.TOOLS`` exactly, so
a newly-added tool without a dispatch spec fails loudly here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from plant_genomics_mcp import (
    alphafold,
    aragwas,
    atted,
    bar,
    batch,
    blast,
    ensembl_plants,
    ensembl_variation,
    europe_pmc,
    gprofiler,
    gramene,
    interpro,
    kegg,
    onekg,
    orthodb,
    panther,
    phytozome,
    plantcyc,
    planteome,
    server,
    string_db,
    synthesis,
    tair,
    uniprot,
)

_DEFAULT_ORG = "arabidopsis_thaliana"
L = "AT1G01010"
LOCI = ["AT1G01010", "AT2G02010"]
SEQ = "MKVLAA"


@dataclass
class Spec:
    tool: str
    module: Any
    attr: str
    args: dict[str, Any]
    expected_id: Any
    expected_org: str | None  # None => tool must not forward organism=
    sync: bool = False  # a pure-sync dispatch arm (no client, no await); none currently
    synth: bool = False  # synthesis arms call env.model_dump() on the return


DISPATCH_SPECS: list[Spec] = [
    Spec(
        "ensembl_plants_lookup_locus", ensembl_plants, "lookup_locus", {"locus": L}, L, _DEFAULT_ORG
    ),
    Spec("get_gene_xrefs", ensembl_plants, "lookup_xrefs", {"locus": L}, L, _DEFAULT_ORG),
    Spec("get_sequence", ensembl_plants, "get_sequence", {"locus": L}, L, _DEFAULT_ORG),
    Spec(
        "ensembl_region_query",
        ensembl_plants,
        "region_query",
        {"region": "1", "start": 3000, "end": 10000},
        "1",
        _DEFAULT_ORG,
    ),
    Spec("phytozome_lookup_locus", phytozome, "lookup_locus", {"locus": L}, L, _DEFAULT_ORG),
    Spec("resolve_locus_to_uniprot", uniprot, "lookup_locus", {"locus": L}, L, _DEFAULT_ORG),
    Spec("locus_literature", europe_pmc, "lookup_locus", {"locus": L}, L, _DEFAULT_ORG),
    Spec(
        "locus_go_annotations",
        server,
        "_resolve_then_go_annotations",
        {"locus": L},
        L,
        _DEFAULT_ORG,
    ),
    Spec("go_enrichment", gprofiler, "go_enrichment", {"loci": LOCI}, LOCI, _DEFAULT_ORG),
    Spec("locus_plant_ontology", planteome, "lookup_locus", {"locus": L}, L, _DEFAULT_ORG),
    Spec("tair_locus_info", tair, "lookup_locus", {"locus": L}, L, None),
    Spec("plantcyc_locus_info", plantcyc, "lookup_locus", {"locus": L}, L, _DEFAULT_ORG),
    Spec("alphafold_structure", alphafold, "lookup_locus", {"locus": L}, L, _DEFAULT_ORG),
    Spec("interpro_domains", interpro, "lookup_locus", {"locus": L}, L, _DEFAULT_ORG),
    Spec("locus_variants", ensembl_variation, "locus_variants", {"locus": L}, L, _DEFAULT_ORG),
    Spec(
        "vep_annotate",
        ensembl_variation,
        "vep_annotate",
        {"region": "1:100-100:1", "allele": "C"},
        "1:100-100:1",
        _DEFAULT_ORG,
    ),
    Spec("panther_family", panther, "lookup_locus", {"locus": L}, L, _DEFAULT_ORG),
    Spec("orthodb_orthologs", orthodb, "lookup_locus", {"locus": L}, L, _DEFAULT_ORG),
    Spec("aragwas_associations", aragwas, "lookup_locus", {"locus": L}, L, _DEFAULT_ORG),
    Spec("arabidopsis_natural_variation", onekg, "lookup_locus", {"locus": L}, L, _DEFAULT_ORG),
    Spec(
        "batch_ensembl_plants_lookup_locus",
        batch,
        "batch_ensembl_plants_lookup_locus",
        {"loci": LOCI},
        LOCI,
        _DEFAULT_ORG,
    ),
    Spec("batch_get_gene_xrefs", batch, "batch_get_gene_xrefs", {"loci": LOCI}, LOCI, _DEFAULT_ORG),
    Spec(
        "batch_phytozome_lookup_locus",
        batch,
        "batch_phytozome_lookup_locus",
        {"loci": LOCI},
        LOCI,
        _DEFAULT_ORG,
    ),
    Spec(
        "batch_resolve_locus_to_uniprot",
        batch,
        "batch_resolve_locus_to_uniprot",
        {"loci": LOCI},
        LOCI,
        _DEFAULT_ORG,
    ),
    Spec(
        "batch_locus_literature",
        batch,
        "batch_locus_literature",
        {"loci": LOCI},
        LOCI,
        _DEFAULT_ORG,
    ),
    Spec("blast_sequence", blast, "blast_sequence", {"sequence": SEQ}, SEQ, None),
    Spec(
        "batch_locus_go_annotations",
        batch,
        "batch_locus_go_annotations",
        {"loci": LOCI},
        LOCI,
        _DEFAULT_ORG,
    ),
    Spec("batch_kegg_pathways", batch, "batch_kegg_pathways", {"loci": LOCI}, LOCI, _DEFAULT_ORG),
    Spec("bar_gene_summary", bar, "gene_summary", {"locus": L}, L, None),
    Spec("batch_bar_gene_summary", batch, "batch_bar_gene_summary", {"loci": LOCI}, LOCI, None),
    Spec("bar_efp_expression", bar, "efp_expression", {"locus": L}, L, None),
    Spec("bar_aiv_interactions", bar, "aiv_interactions", {"locus": L}, L, _DEFAULT_ORG),
    Spec(
        "batch_bar_aiv_interactions",
        batch,
        "batch_bar_aiv_interactions",
        {"loci": LOCI},
        LOCI,
        _DEFAULT_ORG,
    ),
    Spec(
        "batch_string_interactions",
        batch,
        "batch_string_interactions",
        {"loci_or_accessions": LOCI},
        LOCI,
        _DEFAULT_ORG,
    ),
    Spec("batch_gramene_homologs", batch, "batch_gramene_homologs", {"loci": LOCI}, LOCI, None),
    Spec("kegg_pathways", kegg, "lookup_pathways", {"locus": L}, L, _DEFAULT_ORG),
    Spec(
        "string_interactions",
        string_db,
        "lookup_partners",
        {"locus_or_accession": L},
        L,
        _DEFAULT_ORG,
    ),
    Spec("atted_coexpression", atted, "lookup_coexpression", {"locus": L}, L, _DEFAULT_ORG),
    Spec(
        "batch_atted_coexpression",
        batch,
        "batch_atted_coexpression",
        {"loci": LOCI},
        LOCI,
        _DEFAULT_ORG,
    ),
    Spec("gramene_homologs", gramene, "lookup_homologs", {"locus": L}, L, None),
    Spec(
        "analyze_locus_synth",
        synthesis,
        "analyze_locus_synth",
        {"locus": L},
        L,
        _DEFAULT_ORG,
        synth=True,
    ),
    Spec(
        "find_homologs_synth",
        synthesis,
        "find_homologs_synth",
        {"sequence": SEQ},
        SEQ,
        None,
        synth=True,
    ),
    Spec(
        "biological_context_synth",
        synthesis,
        "biological_context_synth",
        {"locus": L},
        L,
        _DEFAULT_ORG,
        synth=True,
    ),
    Spec(
        "consensus_homologs",
        synthesis,
        "consensus_homologs",
        {"locus": L},
        L,
        _DEFAULT_ORG,
        synth=True,
    ),
    Spec(
        "gene_report",
        synthesis,
        "gene_report",
        {"locus": L},
        L,
        _DEFAULT_ORG,
        synth=True,
    ),
]


class _Env:
    """Stand-in for a SynthesisEnvelope — the dispatcher calls .model_dump()."""

    def model_dump(self) -> dict[str, Any]:
        return {"stub": True}


def _make_recorder(rv: Any, *, sync: bool):
    calls: list[tuple[tuple, dict]] = []

    if sync:

        def rec(*a: Any, **k: Any) -> Any:
            calls.append((a, k))
            return rv
    else:

        async def rec(*a: Any, **k: Any) -> Any:  # type: ignore[misc]
            calls.append((a, k))
            return rv

    rec.calls = calls  # type: ignore[attr-defined]
    return rec


@pytest.mark.parametrize("spec", DISPATCH_SPECS, ids=lambda s: s.tool)
@pytest.mark.asyncio
async def test_dispatch_routes_identifier_and_default_organism(spec: Spec, monkeypatch) -> None:
    rv: Any = _Env() if spec.synth else {"stub": True}
    rec = _make_recorder(rv, sync=spec.sync)
    monkeypatch.setattr(spec.module, spec.attr, rec)

    await server._dispatch(spec.tool, dict(spec.args))

    assert rec.calls, f"{spec.tool}: backend stub was never called"
    a, k = rec.calls[0]
    assert spec.expected_id in a, (
        f"{spec.tool}: identifier {spec.expected_id!r} not passed positionally; got {a!r}"
    )
    if spec.expected_org is None:
        assert "organism" not in k, (
            f"{spec.tool}: forwarded an unexpected organism={k.get('organism')!r}"
        )
    else:
        assert k.get("organism") == spec.expected_org, (
            f"{spec.tool}: expected default organism={spec.expected_org!r}, got {k.get('organism')!r}"
        )


def test_dispatch_specs_cover_every_tool() -> None:
    """Lock the spec table to the live catalog so a new tool can't slip in
    without a dispatch-routing assertion."""
    spec_names = {s.tool for s in DISPATCH_SPECS}
    tool_names = {t.name for t in server.TOOLS}
    assert spec_names == tool_names, (
        f"dispatch specs out of sync with server.TOOLS — "
        f"missing specs: {sorted(tool_names - spec_names)}; "
        f"stale specs: {sorted(spec_names - tool_names)}"
    )


@pytest.mark.asyncio
async def test_dispatch_unknown_tool_raises() -> None:
    with pytest.raises(ValueError, match="unknown tool: not_a_real_tool"):
        await server._dispatch("not_a_real_tool", {})
