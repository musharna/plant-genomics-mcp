"""Tests for the multi-organism resolver."""

from __future__ import annotations

import pytest

from plant_genomics_mcp import organisms


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


from plant_genomics_mcp.errors import (
    OrganismNotFound,
    OrganismNotSupported,
    PlantGenomicsError,
)


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
