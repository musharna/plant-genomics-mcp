"""Tests for the multi-organism resolver."""

from __future__ import annotations


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
