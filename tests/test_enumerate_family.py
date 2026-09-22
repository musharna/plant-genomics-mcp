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
    ] * 4  # interpro, orthodb, panther, ensembl — never the seed's organism

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
