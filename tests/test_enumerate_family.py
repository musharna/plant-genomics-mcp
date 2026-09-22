"""`examples/arf_family/enumerate_family.py` against the fake stdio server.

The real server's answers are whatever Gramene, Ensembl and InterPro say
today, so the closure logic is pinned here on the fake server's `family`
mode (`tests/_fake_mcp_server.py`, `FAMILY_*` tables): a toy genome
whose paralog graph is deliberately incomplete, so that a member found
by the region scan and NOT reachable by paralogs, a paralog-reachable
member NOT on any scanned window, a candidate the InterPro arbiter must
reject, and an ortholog hit whose id is not a locus of any target
organism are all distinguishable outcomes — each of which the assertions
below name individually. The run goes over the real stdio transport
through the real `McpClient`.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import re
import sys
from pathlib import Path

import pytest

from examples.arf_family import enumerate_family as enumerate_family_module
from examples.arf_family.enumerate_family import (
    EnumerationError,
    enumerate_family,
    interpro_entries,
    organism_of_locus,
)
from examples.arf_family.mcp_client import McpClient

FAKE_SERVER = Path(__file__).parent / "_fake_mcp_server.py"
FAKE_FAMILY = [sys.executable, str(FAKE_SERVER), "family"]

SEED = {
    "locus": "AT1G00010",
    "symbol": "SEED",
    "organism": "arabidopsis_thaliana",
    "panther_subfamily": "PTHR0:seed",
    "has_pb1_domain": "true",
}


def _run(seeds: list[dict], **kw):
    enumerate_family_module.RETRY_PAUSE_S = 0.0
    log = io.StringIO()

    async def go():
        c = McpClient(FAKE_FAMILY)
        await c.start()
        try:
            return await enumerate_family(seeds, c, log, **kw)
        finally:
            await c.close()

    rows, candidates, sources = asyncio.run(go())
    calls = [json.loads(line) for line in log.getvalue().splitlines()]
    return rows, candidates, sources, calls


def test_closure_reaches_members_by_paralog_and_by_scan_and_rejects_by_interpro():
    rows, candidates, sources, calls = _run([SEED])

    by_locus = {r["locus"]: r for r in rows}
    cand = {r["locus"]: r for r in candidates}

    # The seed row is returned first and untouched.
    assert rows[0] == SEED

    # Positive cases, each by a different route: AT1G00050 is only a
    # paralog of the seed (never on a scanned window); AT2G00010 is only on
    # chromosome 2 of the scan (no paralog edge reaches it).
    assert cand["AT1G00050"]["source"] == "paralog:AT1G00010"
    assert cand["AT1G00060"]["source"] == "paralog:AT1G00050"  # second hop: the closure iterated
    assert cand["AT2G00010"]["source"] == "region:2:1"
    assert by_locus["AT1G00050"]["has_pb1_domain"] == "false"
    assert by_locus["AT2G00010"]["symbol"] == "SYM_AT2G00010"
    assert by_locus["AT2G00010"]["panther_subfamily"] == "PTHR0:AT2G00010"

    # Negative case, same run: a scanned candidate whose description
    # matched but whose InterPro entries lack the family entry is
    # recorded as rejected WITH the entries it does carry, and is not a row.
    assert cand["AT1G00030"] == {
        "locus": "AT1G00030",
        "organism": "arabidopsis_thaliana",
        "source": "region:1:1",
        "kept": "false",
        "interpro_entries": "IPR003340",
    }
    assert "AT1G00030" not in by_locus
    # Third state, same run: a candidate whose interpro_domains call FAILED
    # is neither kept nor rejected — `undecided`, with the error as its
    # reason — beside the kept (AT1G00010) and rejected (AT1G00030) rows
    # above, so the three values are shown to be distinct outcomes.
    assert cand["AT1G00070"]["kept"] == "undecided"
    assert cand["AT1G00070"]["interpro_entries"].startswith("call failed: [NotFoundError]")
    assert cand["AT1G00010"]["kept"] == "true"
    assert "AT1G00070" not in by_locus
    assert {r["kept"] for r in candidates} == {"true", "false", "undecided"}
    # ...and a gene the candidate filter never matched, or that is not
    # protein-coding, was never sent to InterPro at all.
    assert "AT1G00020" not in cand
    assert "AT1G00040" not in cand
    interpro_loci = [c["args"]["locus"] for c in calls if c["tool"] == "interpro_domains"]
    assert "AT1G00020" not in interpro_loci

    # Orthologs: the rice- and wheat-shaped hits are verified in their own
    # organism; the maize-shaped hit is recorded, not queried; the rice
    # paralog closure ran and its non-member paralog was rejected.
    assert by_locus["Os01g0000100"]["organism"] == "oryza_sativa"
    assert by_locus["TraesCS1A02G000100"]["organism"] == "triticum_aestivum"
    assert by_locus["TraesCS1A02G000100"]["has_pb1_domain"] == "true"
    assert "Zm00001d000001" not in cand  # not a locus of any target organism: never queried
    assert cand["Os01g0000200"]["kept"] == "false"
    assert cand["Os01g0000200"]["source"] == "paralog:Os01g0000100"
    wheat_calls = [c for c in calls if c["args"].get("locus") == "TraesCS1A02G000100"]
    assert [c["args"]["organism"] for c in wheat_calls if "organism" in c["args"]] == [
        "triticum_aestivum"
    ] * 5  # interpro, orthodb ×2 (one per target organism), panther, ensembl — never the seed's
    # Both ortholog tools are asked once per target organism, filtered by the tool.
    assert sorted(
        (c["tool"], c["args"]["target_organism"])
        for c in wheat_calls
        if "target_organism" in c["args"]
    ) == [
        ("gramene_homologs", "oryza_sativa"),
        ("gramene_homologs", "triticum_aestivum"),
        ("orthodb_orthologs", "oryza_sativa"),
        ("orthodb_orthologs", "triticum_aestivum"),
    ]

    # The ortholog-source table keeps both tools' answers side by side, so
    # a disagreement is visible rather than reconciled.
    gram = {
        (s["query_locus"], s["organism"]): s["hits"]
        for s in sources
        if s["tool"] == "gramene_homologs"
    }
    odb = {
        (s["query_locus"], s["organism"]): s["hits"]
        for s in sources
        if s["tool"] == "orthodb_orthologs"
    }
    assert gram[("AT1G00010", "oryza_sativa")] == "Os01g0000100"
    assert odb[("AT1G00010", "oryza_sativa")] == "none"
    # ...and the hit that belongs to neither organism is counted, not lost.
    elsewhere = {
        (s["query_locus"], s["tool"]): s["hits_elsewhere"]
        for s in sources
        if s["organism"] == "oryza_sativa"
    }
    assert elsewhere[("AT1G00010", "gramene_homologs")] == 1
    assert elsewhere[("AT1G00050", "gramene_homologs")] == 0

    # Order and completeness of the output rows.
    assert [r["locus"] for r in rows] == [
        "AT1G00010",
        "AT1G00050",
        "AT1G00060",
        "AT2G00010",
        "Os01g0000100",
        "TraesCS1A02G000100",
    ]
    # The scan walked off the end of both chromosomes and stopped at the
    # first unknown one, so the region list was learned, not typed.
    regions = [c["args"]["region"] for c in calls if c["tool"] == "ensembl_region_query"]
    # 5 Mb chromosome 1 in 4 Mb windows = two windows + the past-the-end
    # probe; chromosome 2 times out at 4 Mb (logged as a failed call), is
    # re-asked at 2 Mb, then the probe; "3" is unknown.
    assert regions == ["1", "1", "1", "2", "2", "2", "3"]
    windows = [
        (c["args"]["region"], c["args"]["end"] - c["args"]["start"] + 1, c["ok"])
        for c in calls
        if c["tool"] == "ensembl_region_query" and c["args"]["region"] == "2"
    ]
    assert windows == [("2", 4_000_000, False), ("2", 2_000_000, True), ("2", 2_000_000, False)]


ARF_DIR = Path(__file__).parent.parent / "examples" / "arf_family"


def _recount_ortholog_sources() -> dict[str, int]:
    with open(ARF_DIR / "ortholog_sources.tsv") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    out = {}
    for tool in ("gramene_homologs", "orthodb_orthologs"):
        mine = [r for r in rows if r["tool"] == tool]
        out[tool] = len({r["query_locus"] for r in mine if r["hits"] != "none"})
        out[f"{tool}_queries"] = len({r["query_locus"] for r in mine})
    return out


def test_the_ortholog_disagreement_row_matches_a_recount_of_its_own_evidence():
    """The `ortholog-tools-disagree` gap row says "N of M queries" for each
    tool; the numbers are re-derived here from the file the row cites.
    Round 1 of review found 19 written where the file says 16 — a false
    number in text drafted to become a public issue — because nothing
    read the row against its evidence."""
    rows = [json.loads(line) for line in (ARF_DIR / "gaps.jsonl").read_text().splitlines()]
    row = next(r for r in rows if r["kind"] == "ortholog-tools-disagree")
    assert "ortholog_sources.tsv" in row["raw"]
    counts = _recount_ortholog_sources()
    assert counts["gramene_homologs_queries"] == counts["orthodb_orthologs_queries"]
    m = counts["gramene_homologs_queries"]
    # The row was closed by a later run: the observation to recount is the
    # one that run produced, kept beside the original.
    observed = row["closed"]["returned"] if row.get("closed") else row["returned"]
    claims = dict(
        re.findall(
            r"(gramene_homologs|orthodb_orthologs) names (?:a rice or wheat locus|one) for (\d+) of",
            observed,
        )
    )
    assert set(claims) == {"gramene_homologs", "orthodb_orthologs"}, observed
    assert f"of {m} queries" in observed
    assert int(claims["gramene_homologs"]) == counts["gramene_homologs"]
    assert int(claims["orthodb_orthologs"]) == counts["orthodb_orthologs"]
    # Positive control for the recount: it is not trivially zero or total.
    # (On the first run orthodb was 0 of 26 — the gap; the closing run has
    # both tools answering, so the recount must see both strictly inside.)
    assert 0 < counts["gramene_homologs"] < m
    assert 0 < counts["orthodb_orthologs"] < m


def test_the_undecidable_row_matches_a_recount_of_family_candidates():
    """The `candidate-undecidable` gap row says how many of the candidates
    could not be decided and how they split by organism; the numbers are
    re-derived from the TSV the row cites, so the row cannot carry a
    count typed from an earlier state of the file."""
    with open(ARF_DIR / "family_candidates.tsv") as f:
        cands = list(csv.DictReader(f, delimiter="\t"))
    undecided = [r for r in cands if r["kept"] == "undecided"]
    by_org = {
        org: sum(1 for r in undecided if r["organism"] == org)
        for org in ("triticum_aestivum", "arabidopsis_thaliana")
    }
    rows = [json.loads(line) for line in (ARF_DIR / "gaps.jsonl").read_text().splitlines()]
    row = next(r for r in rows if r["kind"] == "candidate-undecidable")
    assert "family_candidates.tsv" in row["raw"]
    m = re.search(
        r"(\d+) of the (\d+) could not be decided, (\d+) wheat and (\d+) Arabidopsis",
        row["returned"],
    )
    assert m, row["returned"]
    total, pool, wheat, ath = (int(x) for x in m.groups())
    assert pool == len(cands)
    assert total == len(undecided)
    assert wheat == by_org["triticum_aestivum"]
    assert ath == by_org["arabidopsis_thaliana"]
    assert wheat + ath == total
    # Positive control for the recount: the undecided are a strict minority
    # and both organisms contribute.
    assert 0 < total < len(cands) and wheat > 0 and ath > 0


def test_a_seed_without_the_family_entry_stops_the_run():
    bad_seed = {**SEED, "locus": "AT1G00030"}
    with pytest.raises(EnumerationError, match="AT1G00030 does not carry IPR010525"):
        _run([bad_seed])
    # Positive control: the same call with the real seed completes.
    rows, *_ = _run([SEED], scan=False, orthologs=False)
    assert [r["locus"] for r in rows] == ["AT1G00010", "AT1G00050", "AT1G00060"]


def test_helpers_classify_locus_shapes_and_read_interpro_entries():
    assert organism_of_locus("AT1G19850") == "arabidopsis_thaliana"
    assert organism_of_locus("ATMG00940") == "arabidopsis_thaliana"
    assert organism_of_locus("Os01g0236300") == "oryza_sativa"
    assert organism_of_locus("TraesCS3B02G123400") == "triticum_aestivum"
    assert organism_of_locus("Tritim_EIv0.3_1529040") is None
    assert organism_of_locus("Zm00001d000001") is None
    payload = {
        "domains": [
            {"accession": "cd10017", "interpro": "IPR003340"},
            {"accession": "IPR010525", "interpro": "IPR010525"},
            {"accession": "PF02309", "interpro": None},
        ]
    }
    assert interpro_entries(payload) == ["IPR003340", "IPR010525"]
    assert interpro_entries({"domains": []}) == []


def test_written_tsv_round_trips_through_csv():
    rows, candidates, _, _ = _run([SEED], orthologs=False)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(candidates[0]), delimiter="\t", lineterminator="\n")
    w.writeheader()
    w.writerows(candidates)
    back = list(csv.DictReader(io.StringIO(buf.getvalue()), delimiter="\t"))
    assert back == candidates
    assert len(rows) == 4
