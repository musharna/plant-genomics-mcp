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
