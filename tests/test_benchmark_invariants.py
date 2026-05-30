"""Unit tests for the benchmark's cross-source consistency invariants (v1.7 seed 2).

These exercise the invariant predicates + checks with mocked tool-response dicts
— no live calls. The invariants assert agreement ACROSS backends for one locus:

  INV-1 kegg_entrez_in_ensembl_xrefs — the Entrez ID KEGG's bridge resolved to
        must be one Ensembl /xrefs actually attests for the locus (guards v1.4).
  INV-2 kegg_orgcode_matches_resolver — the kegg_gene_id org-code prefix must
        equal the resolver's kegg_org_code.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from benchmark_annotations import (  # noqa: E402
    Verdict,
    _check_invariants,
)


def _kegg(entrez: str | None, gene_id: str) -> dict:
    out = {"locus": "X", "kegg_gene_id": gene_id, "pathways": [{"id": "p"}], "errors": []}
    if entrez is not None:
        out["entrez_gene_id"] = entrez
    return out


def _xrefs(entrez_ids: list[str]) -> dict:
    return {"locus": "X", "organism": "oryza_sativa", "by_db": {"EntrezGene": entrez_ids}}


def _resolve(kegg_code: str) -> dict:
    return {"canonical": "oryza_sativa", "kegg_org_code": kegg_code}


def _verdicts(record: dict, responses: dict) -> dict[str, Verdict]:
    return {name: ar.verdict for name, ar in _check_invariants(record, responses).items()}


# ---------- INV-1: KEGG entrez ∈ Ensembl xrefs EntrezGene ----------


def test_inv1_pass_when_entrez_attested_by_ensembl() -> None:
    record = {"organism": "oryza_sativa"}
    responses = {
        "kegg.lookup_pathways": _kegg("4326457", "osa:4326457"),
        "ensembl_plants.lookup_xrefs": _xrefs(["4326457"]),
        "organisms.resolve": _resolve("osa"),
    }
    assert _verdicts(record, responses)["kegg_entrez_in_ensembl_xrefs"] == Verdict.PASS


def test_inv1_fail_when_entrez_not_in_xrefs() -> None:
    record = {"organism": "oryza_sativa"}
    responses = {
        "kegg.lookup_pathways": _kegg("4326457", "osa:4326457"),
        "ensembl_plants.lookup_xrefs": _xrefs(["9999999"]),  # different id
        "organisms.resolve": _resolve("osa"),
    }
    assert _verdicts(record, responses)["kegg_entrez_in_ensembl_xrefs"] == Verdict.FAIL


def test_inv1_skipped_for_arabidopsis_native_path() -> None:
    # Arabidopsis kegg is native ath: — no entrez_gene_id, no bridge to check.
    record = {"organism": "arabidopsis_thaliana"}
    responses = {
        "kegg.lookup_pathways": _kegg(None, "ath:AT1G01050"),
        "organisms.resolve": _resolve("ath"),
    }
    assert _verdicts(record, responses)["kegg_entrez_in_ensembl_xrefs"] == Verdict.SKIPPED


def test_inv1_skipped_when_xrefs_absent() -> None:
    record = {"organism": "oryza_sativa"}
    responses = {  # bridge org, kegg succeeded, but xrefs not collected
        "kegg.lookup_pathways": _kegg("4326457", "osa:4326457"),
        "organisms.resolve": _resolve("osa"),
    }
    assert _verdicts(record, responses)["kegg_entrez_in_ensembl_xrefs"] == Verdict.SKIPPED


# ---------- INV-2: kegg_gene_id org-code prefix == resolved kegg_org_code ----------


def test_inv2_pass_when_prefix_matches_resolver() -> None:
    record = {"organism": "oryza_sativa"}
    responses = {
        "kegg.lookup_pathways": _kegg("4326457", "osa:4326457"),
        "organisms.resolve": _resolve("osa"),
    }
    assert _verdicts(record, responses)["kegg_orgcode_matches_resolver"] == Verdict.PASS


def test_inv2_fail_on_prefix_mismatch() -> None:
    record = {"organism": "oryza_sativa"}
    responses = {
        "kegg.lookup_pathways": _kegg("4326457", "zzz:4326457"),  # wrong prefix
        "organisms.resolve": _resolve("osa"),
    }
    assert _verdicts(record, responses)["kegg_orgcode_matches_resolver"] == Verdict.FAIL


def test_inv2_pass_arabidopsis_native() -> None:
    record = {"organism": "arabidopsis_thaliana"}
    responses = {
        "kegg.lookup_pathways": _kegg(None, "ath:AT1G01050"),
        "organisms.resolve": _resolve("ath"),
    }
    assert _verdicts(record, responses)["kegg_orgcode_matches_resolver"] == Verdict.PASS


def test_inv2_skipped_when_kegg_absent() -> None:
    record = {"organism": "oryza_sativa"}
    responses = {"organisms.resolve": _resolve("osa")}
    assert _verdicts(record, responses)["kegg_orgcode_matches_resolver"] == Verdict.SKIPPED
