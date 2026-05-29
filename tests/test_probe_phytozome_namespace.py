"""Unit tests for the Phytozome namespace probe's pure TSV parser (v1.7 seed 3).

Exercises ``_parse_discovery_rows`` with synthetic BioMart bodies — no live
calls. The parser turns a streamed org-only discovery TSV into native gene-name
row dicts, defending against BioMart's quirks (200-with-Query-ERROR, header-only
bodies, ragged lines) and enforcing the row cap.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from probe_phytozome_namespace import (  # noqa: E402
    BioMartQueryError,
    _parse_discovery_rows,
    _start_key,
)

_HEADER = "Organism\tGene Name\tChromosome\tStart"


def _tsv(*rows: str) -> str:
    return "\n".join([_HEADER, *rows])


def test_parses_normal_multi_row() -> None:
    body = _tsv(
        "Osativa\tLOC_Os01g01010\tChr01\t2903",
        "Osativa\tLOC_Os01g01019\tChr01\t11218",
    )
    rows = _parse_discovery_rows(body, cap=40)
    assert len(rows) == 2
    assert rows[0] == {
        "organism_name": "Osativa",
        "gene_name": "LOC_Os01g01010",
        "chromosome": "Chr01",
        "gene_start": "2903",
    }


def test_query_error_body_raises() -> None:
    with pytest.raises(BioMartQueryError):
        _parse_discovery_rows("Query ERROR: Filter organism_id NOT FOUND", cap=40)


def test_header_only_body_yields_empty() -> None:
    assert _parse_discovery_rows(_HEADER, cap=40) == []


def test_empty_body_yields_empty() -> None:
    assert _parse_discovery_rows("", cap=40) == []


def test_ragged_rows_are_skipped() -> None:
    body = _tsv(
        "Osativa\tLOC_Os01g01010\tChr01\t2903",
        "broken\trow",  # too few columns
        "Osativa\tLOC_Os01g01019\tChr01\t11218\textra",  # too many columns
        "Osativa\tLOC_Os01g01030\tChr01\t12000",
    )
    rows = _parse_discovery_rows(body, cap=40)
    assert [r["gene_name"] for r in rows] == ["LOC_Os01g01010", "LOC_Os01g01030"]


def test_cap_is_enforced() -> None:
    body = _tsv(*[f"Osativa\tLOC_Os01g{i:05d}\tChr01\t{i}" for i in range(100)])
    rows = _parse_discovery_rows(body, cap=10)
    assert len(rows) == 10


def test_start_key_sorts_chr1_lowest_start_first() -> None:
    rows = [
        {"chromosome": "Chr02", "gene_start": "5"},
        {"chromosome": "Chr01", "gene_start": "900"},
        {"chromosome": "Chr01", "gene_start": "100"},
    ]
    first = sorted(rows, key=_start_key)[0]
    assert first == {"chromosome": "Chr01", "gene_start": "100"}


def test_start_key_tolerates_nonnumeric_start() -> None:
    rows = [
        {"chromosome": "Chr01", "gene_start": "n/a"},
        {"chromosome": "Chr01", "gene_start": "50"},
    ]
    first = sorted(rows, key=_start_key)[0]
    assert first["gene_start"] == "50"
