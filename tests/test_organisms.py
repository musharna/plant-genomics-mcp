"""Tests for the multi-organism resolver."""

from __future__ import annotations

import pytest

from plant_genomics_mcp import organisms
from plant_genomics_mcp.errors import (
    OrganismNotFound,
    OrganismNotSupported,
    PlantGenomicsError,
)


def test_arabidopsis_record_canonical_lookup() -> None:
    record = organisms.ORGANISMS["arabidopsis_thaliana"]
    assert record.canonical == "arabidopsis_thaliana"
    assert record.scientific == "Arabidopsis thaliana"
    assert record.ncbi_taxid == 3702
    assert record.ensembl_slug == "arabidopsis_thaliana"
    assert record.phytozome_int == 167
    assert record.string_taxid == 3702
    assert record.europe_pmc_slug is None  # AT-prefixed IDs are unambiguous


def test_default_organism_is_arabidopsis() -> None:
    assert organisms.DEFAULT_ORGANISM == "arabidopsis_thaliana"
    assert organisms.DEFAULT_ORGANISM in organisms.ORGANISMS


def test_organism_not_found_is_plant_genomics_error() -> None:
    exc = OrganismNotFound("zucchini", supported=["arabidopsis_thaliana"])
    assert isinstance(exc, PlantGenomicsError)
    rendered = str(exc)
    assert rendered.startswith("[OrganismNotFound]")
    assert "zucchini" in rendered
    assert "arabidopsis_thaliana" in rendered


def test_organism_not_supported_carries_backend_and_supported() -> None:
    exc = OrganismNotSupported(
        backend="phytozome",
        organism="vitis_vinifera",
        supported=["arabidopsis_thaliana", "glycine_max"],
    )
    rendered = str(exc)
    assert rendered.startswith("[OrganismNotSupported]")
    assert "phytozome" in rendered
    assert "vitis_vinifera" in rendered
    assert "arabidopsis_thaliana" in rendered
    assert exc.backend == "phytozome"
    assert exc.organism == "vitis_vinifera"
    assert exc.supported == ["arabidopsis_thaliana", "glycine_max"]


@pytest.mark.parametrize(
    "query",
    [
        "arabidopsis_thaliana",
        "arabidopsis thaliana",
        "arabidopsis-thaliana",
        "Arabidopsis thaliana",
        "ARABIDOPSIS_THALIANA",
        "  arabidopsis_thaliana  ",
        "a. thaliana",
        "A. thaliana",
        "thale cress",
        "thale-cress",
        "at",
        3702,
    ],
)
def test_resolve_arabidopsis_from_all_forms(query) -> None:
    record = organisms.resolve(query)
    assert record.canonical == "arabidopsis_thaliana"


def test_resolve_unknown_raises_organism_not_found() -> None:
    with pytest.raises(OrganismNotFound) as excinfo:
        organisms.resolve("zucchini")
    assert excinfo.value.query == "zucchini"
    assert "arabidopsis_thaliana" in excinfo.value.supported


def test_resolve_unknown_taxid_raises() -> None:
    with pytest.raises(OrganismNotFound) as excinfo:
        organisms.resolve(99999999)
    assert excinfo.value.query == 99999999


def test_ensembl_slug_for_arabidopsis() -> None:
    assert organisms.ensembl_slug_for("arabidopsis_thaliana") == "arabidopsis_thaliana"
    assert organisms.ensembl_slug_for(3702) == "arabidopsis_thaliana"


def test_ncbi_taxid_for_arabidopsis() -> None:
    assert organisms.ncbi_taxid_for("arabidopsis_thaliana") == 3702
    assert organisms.ncbi_taxid_for("A. thaliana") == 3702


def test_phytozome_int_for_arabidopsis() -> None:
    assert organisms.phytozome_int_for("arabidopsis_thaliana") == 167


def test_string_taxid_for_arabidopsis() -> None:
    assert organisms.string_taxid_for("arabidopsis_thaliana") == 3702


def test_europe_pmc_slug_for_arabidopsis_returns_none() -> None:
    # AT-prefixed IDs are already unambiguous — returns None by contract,
    # NOT raising OrganismNotSupported.
    assert organisms.europe_pmc_slug_for("arabidopsis_thaliana") is None


def test_helper_raises_on_unknown_organism() -> None:
    with pytest.raises(OrganismNotFound):
        organisms.ensembl_slug_for("zucchini")


EXPECTED_CANONICAL = {
    "arabidopsis_thaliana",
    "oryza_sativa",
    "zea_mays",
    "triticum_aestivum",
    "solanum_lycopersicum",
    "glycine_max",
    "sorghum_bicolor",
    "hordeum_vulgare",
    "vitis_vinifera",
    "populus_trichocarpa",
    "medicago_truncatula",
    "brachypodium_distachyon",
}


def test_full_coverage_matrix() -> None:
    assert set(organisms.ORGANISMS.keys()) == EXPECTED_CANONICAL


@pytest.mark.parametrize("canonical", sorted(EXPECTED_CANONICAL))
def test_every_record_has_ncbi_taxid_and_ensembl_slug(canonical: str) -> None:
    record = organisms.ORGANISMS[canonical]
    assert isinstance(record.ncbi_taxid, int)
    assert record.ncbi_taxid > 0
    # Every record in the v0.9 matrix has Ensembl Plants coverage.
    assert record.ensembl_slug is not None


def test_phytozome_int_carries_over_from_known_organisms() -> None:
    # The 5 records that already had verified phytozome_ints in
    # phytozome.KNOWN_ORGANISMS reuse those values exactly.
    assert organisms.ORGANISMS["arabidopsis_thaliana"].phytozome_int == 167
    assert organisms.ORGANISMS["glycine_max"].phytozome_int == 275
    assert organisms.ORGANISMS["sorghum_bicolor"].phytozome_int == 454
    assert organisms.ORGANISMS["brachypodium_distachyon"].phytozome_int == 314
    assert organisms.ORGANISMS["populus_trichocarpa"].phytozome_int == 210


def test_all_records_have_phytozome_int() -> None:
    # Post-Wave-A2 (pre-1.0): every organism in the registry must carry
    # a verified Phytozome organism_id. Wave A2 (2026-05-23) live-probed
    # BioMart for the 7 records that previously shipped as None.
    for canonical, record in organisms.ORGANISMS.items():
        assert record.phytozome_int is not None, (
            f"{canonical} has phytozome_int=None; Wave A2 populates all 12"
        )


def test_resolve_rice_by_common_name() -> None:
    assert organisms.resolve("rice").canonical == "oryza_sativa"


def test_resolve_maize_by_common_name() -> None:
    assert organisms.resolve("maize").canonical == "zea_mays"
    assert organisms.resolve("corn").canonical == "zea_mays"


def test_ncbi_taxid_for_rice() -> None:
    assert organisms.ncbi_taxid_for("oryza_sativa") == 39947
    assert organisms.ncbi_taxid_for("rice") == 39947


def test_phytozome_unsupported_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # Wave A2 populated every record's phytozome_int, so to exercise the
    # OrganismNotSupported branch we shadow the registry with a None slot
    # for one organism. The accessor must still raise the same way for any
    # future record that ships as None.
    from dataclasses import replace

    record = organisms.ORGANISMS["vitis_vinifera"]
    shadowed = dict(organisms.ORGANISMS)
    shadowed["vitis_vinifera"] = replace(record, phytozome_int=None)
    monkeypatch.setattr(organisms, "ORGANISMS", shadowed)
    with pytest.raises(OrganismNotSupported) as excinfo:
        organisms.phytozome_int_for("vitis_vinifera")
    assert excinfo.value.backend == "phytozome"
    assert excinfo.value.organism == "vitis_vinifera"
    assert "arabidopsis_thaliana" in excinfo.value.supported


# --- v1.1.0 T4: kegg_org_code + atted_release schema migration ---------------


def test_organism_record_has_kegg_and_atted_fields() -> None:
    """v1.1.0 schema: OrganismRecord exposes per-backend kegg + atted slots."""
    arab = organisms.resolve("arabidopsis_thaliana")
    assert hasattr(arab, "kegg_org_code")
    assert hasattr(arab, "atted_release")


def test_kegg_org_code_for_arabidopsis_returns_ath() -> None:
    assert organisms.kegg_org_code_for("arabidopsis_thaliana") == "ath"


def test_atted_release_for_arabidopsis_returns_known_release() -> None:
    # Spec sentinel — the populated value MUST be the live ATTED-II release id
    # for Arabidopsis confirmed via scripts/verify_organisms.py. Pinned today
    # at the v1.0.x release ("Ath-u.c4-0"); update if the probe disagrees.
    assert organisms.atted_release_for("arabidopsis_thaliana") == "Ath-u.c4-0"


def test_kegg_org_code_for_unsupported_raises_organism_not_supported() -> None:
    for canonical in organisms.ORGANISMS:
        if organisms.ORGANISMS[canonical].kegg_org_code is None:
            with pytest.raises(OrganismNotSupported):
                organisms.kegg_org_code_for(canonical)
            return
    pytest.skip("KEGG covers all 12 organisms — no negative case to assert")


def test_atted_release_for_unsupported_raises_organism_not_supported() -> None:
    for canonical in organisms.ORGANISMS:
        if organisms.ORGANISMS[canonical].atted_release is None:
            with pytest.raises(OrganismNotSupported):
                organisms.atted_release_for(canonical)
            return
    pytest.skip("ATTED covers all 12 organisms — no negative case to assert")
