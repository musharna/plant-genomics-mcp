"""Pinned contracts for ``_consensus_homologs_compose`` and ``_parse_blast_identity_pct``.

The existing compose tests check one property each (a key, a count). The
nightly mutation run showed what that leaves open: the tie-break order, the
``top_n`` cut, the "no identity, no row" rule, the second-copy dedup, the
rounding, and the ``> 1.0`` percent-vs-fraction boundary all survived. Each
test below asserts the whole output row list, so a change to any field is a
change to the expected value.
"""

from __future__ import annotations

import pytest

from plant_genomics_mcp.synthesis import _consensus_homologs_compose, _parse_blast_identity_pct


def _hit(acc: str, identity, **extra) -> dict:
    return {
        "accession": acc,
        "description": "x",
        "bit_score": 1.0,
        "evalue": 1e-5,
        "identity": identity,
        **extra,
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        (80, 0.8),  # int percent
        (80.0, 0.8),  # float percent
        (1.0, 1.0),  # exactly 1.0 is a fraction, not 1 %
        (0.5, 0.5),  # fraction passes through
        (1.5, 0.015),  # anything above 1.0 is a percent
        ("78%", 0.78),
        (" 78 % ", 0.78),  # strip + rstrip leave "78 "; float() tolerates that
        ("78", 0.78),
        ("abc", None),
        ("", None),
        ("100%", 1.0),
    ],
)
def test_parse_blast_identity_pct_table(raw, expected):
    assert _parse_blast_identity_pct(raw) == expected


def test_compose_full_rows_sorted_by_sources_then_score_then_accession():
    # The rows that must be skipped come FIRST: a `continue` that became a
    # `break` would then drop every real row after them, which the expected
    # list below cannot miss.
    gramene = {
        "homologs": [
            {"target_locus": ""},  # empty locus: skipped
            {"target_locus": "L_NOXREF"},  # no xref entry at all: dropped
            {"target_locus": "L_NOACC"},  # xref without accession: dropped
            {"target_locus": "L1"},
            {"target_locus": "L2"},
            {"target_locus": "L3"},
            {"target_locus": "L1"},  # duplicate locus: must not double-count gramene
        ]
    }
    xref_map = {
        "L1": {"uniprot_acc": "P00001", "system_name": "sp_a"},
        "L2": {"uniprot_acc": "P00002", "system_name": "sp_b"},
        "L3": {"uniprot_acc": "P00003", "system_name": "sp_c"},
        "L_NOACC": {"uniprot_acc": None, "system_name": "sp_x"},
    }
    blast = {
        "hits": [
            _hit("sp|P00006|X", None),  # no identity: dropped entirely (first, see above)
            _hit("", "50%"),  # unparseable accession: dropped
            _hit("sp|P00002.1|X", "60%"),  # joins gramene P00002 -> 2 sources, mean 0.8
            _hit("sp|P00001.2|X", "40%"),  # joins gramene P00001 -> 2 sources, mean 0.7
            _hit("sp|P00001.3|X", "99%"),  # second blast copy of P00001: ignored
            _hit("sp|P00004|X", "90%"),  # blast-only, identity 0.9
            _hit("sp|P00005|X", "90%"),  # blast-only, same score: accession breaks the tie
        ]
    }
    out = _consensus_homologs_compose(gramene, blast, xref_map=xref_map, top_n=10)
    assert out == [
        {
            "uniprot_accession": "P00002",
            "target_species": "sp_b",
            "n_sources": 2,
            "sources": ["gramene", "blast"],
            "mean_identity": 0.8,
            "score": 1.6,
            "gramene_hit": {"target_locus": "L2"},
            "blast_hit": _hit("sp|P00002.1|X", "60%"),
        },
        {
            "uniprot_accession": "P00001",
            "target_species": "sp_a",
            "n_sources": 2,
            "sources": ["gramene", "blast"],
            "mean_identity": 0.7,
            "score": 1.4,
            "gramene_hit": {"target_locus": "L1"},
            "blast_hit": _hit("sp|P00001.2|X", "40%"),
        },
        # single-source rows: gramene identity is a fixed 1.0, so P00003 (score 1.0)
        # outranks the 0.9 blast-only pair; P00004 before P00005 on accession.
        {
            "uniprot_accession": "P00003",
            "target_species": "sp_c",
            "n_sources": 1,
            "sources": ["gramene"],
            "mean_identity": 1.0,
            "score": 1.0,
            "gramene_hit": {"target_locus": "L3"},
            "blast_hit": None,
        },
        {
            "uniprot_accession": "P00004",
            "target_species": None,
            "n_sources": 1,
            "sources": ["blast"],
            "mean_identity": 0.9,
            "score": 0.9,
            "gramene_hit": None,
            "blast_hit": _hit("sp|P00004|X", "90%"),
        },
        {
            "uniprot_accession": "P00005",
            "target_species": None,
            "n_sources": 1,
            "sources": ["blast"],
            "mean_identity": 0.9,
            "score": 0.9,
            "gramene_hit": None,
            "blast_hit": _hit("sp|P00005|X", "90%"),
        },
    ]


def test_compose_top_n_cuts_after_sorting():
    """top_n keeps the best rows, not the first-seen ones."""
    blast = {
        "hits": [_hit("sp|P00009|X", "10%"), _hit("sp|P00007|X", "90%"), _hit("sp|P00008|X", "50%")]
    }
    out = _consensus_homologs_compose(None, blast, xref_map=None, top_n=2)
    assert [r["uniprot_accession"] for r in out] == ["P00007", "P00008"]
    assert (
        _consensus_homologs_compose(None, blast, xref_map=None, top_n=1)[0]["uniprot_accession"]
        == "P00007"
    )


def test_compose_rounds_to_four_places():
    blast = {"hits": [_hit("sp|Q9XYZ1|X", "33.33333%")]}
    gramene = {"homologs": [{"target_locus": "L"}]}
    out = _consensus_homologs_compose(
        gramene, blast, xref_map={"L": {"uniprot_acc": "Q9XYZ1"}}, top_n=5
    )
    assert out == [
        {
            "uniprot_accession": "Q9XYZ1",
            "target_species": None,  # xref carries no system_name
            "n_sources": 2,
            "sources": ["gramene", "blast"],
            "mean_identity": 0.6667,
            "score": 1.3333,
            "gramene_hit": {"target_locus": "L"},
            "blast_hit": _hit("sp|Q9XYZ1|X", "33.33333%"),
        }
    ]


def test_compose_empty_and_none_payloads_yield_nothing():
    assert _consensus_homologs_compose(None, None, xref_map=None, top_n=5) == []
    assert _consensus_homologs_compose({}, {}, xref_map={}, top_n=5) == []
    assert (
        _consensus_homologs_compose({"homologs": None}, {"hits": None}, xref_map={}, top_n=5) == []
    )
    # A gramene homolog whose xref exists but BLAST never saw it is still a row
    # (positive control: the empties above are empty because of the data, not
    # because compose is broken).
    out = _consensus_homologs_compose(
        {"homologs": [{"target_locus": "L"}]},
        None,
        xref_map={"L": {"uniprot_acc": "Q9XYZ1"}},
        top_n=5,
    )
    assert [r["uniprot_accession"] for r in out] == ["Q9XYZ1"]
