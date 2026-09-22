import asyncio
import csv
import os
import sys
from pathlib import Path

import pytest

from examples.arf_family.mcp_client import SERVER_CMD
from examples.arf_family.verify_genes import GENES_TSV, ManifestError, verify

FAKE_SERVER = Path(__file__).parent / "_fake_mcp_server.py"
FAKE_ARF = [sys.executable, str(FAKE_SERVER), "arf"]


def run(coro):
    return asyncio.run(coro)


def _write_genes_tsv(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "genes.tsv"
    fieldnames = ["locus", "symbol", "organism", "panther_subfamily", "has_pb1_domain"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, delimiter="\t")
        w.writeheader()
        for row in rows:
            w.writerow(row)
    return path


def _row(locus: str, symbol: str, panther_subfamily: str, has_pb1_domain: str) -> dict:
    return {
        "locus": locus,
        "symbol": symbol,
        "organism": "arabidopsis_thaliana",
        "panther_subfamily": panther_subfamily,
        "has_pb1_domain": has_pb1_domain,
    }


def test_verify_genes_passes_when_every_row_matches_live_data(tmp_path):
    # Positive baseline: two rows, both correct against the fake server's
    # canned fixtures — no reasons should be returned for either.
    genes = _write_genes_tsv(
        tmp_path,
        [
            _row("GOOD_ARF_PB1", "ARF5", "PTHR31384:SF10", "true"),
            _row("GOOD_ARF_NO_PB1", "ARF10", "PTHR31384:SF193", "false"),
        ],
    )
    bad = run(verify(genes, FAKE_ARF))
    assert bad == []


def test_verify_genes_fails_on_locus_missing_arf_family_membership(tmp_path):
    # Positive control (GOOD_ARF_PB1, correct) and the negative case
    # (NOT_ARF, missing InterPro IPR010525) in the same test, per CLAUDE.md
    # 2.2 — the failure must name NOT_ARF specifically, not the whole run.
    genes = _write_genes_tsv(
        tmp_path,
        [
            _row("GOOD_ARF_PB1", "ARF5", "PTHR31384:SF10", "true"),
            _row("NOT_ARF", "ARF1", "PTHR31384:SF96", "true"),
        ],
    )
    bad = run(verify(genes, FAKE_ARF))
    assert [locus for locus, _, _ in bad] == ["NOT_ARF"]
    assert "IPR010525" in bad[0][2]


def test_verify_genes_fails_on_has_pb1_domain_mismatch(tmp_path):
    # GOOD_ARF_NO_PB1 genuinely lacks the PB1 domain (fake fixture has no
    # IPR033389/PF02309); declaring has_pb1_domain=true for it must be
    # caught, while the correctly declared row passes in the same run.
    genes = _write_genes_tsv(
        tmp_path,
        [
            _row("GOOD_ARF_PB1", "ARF5", "PTHR31384:SF10", "true"),
            _row("GOOD_ARF_NO_PB1", "ARF10", "PTHR31384:SF193", "true"),
        ],
    )
    bad = run(verify(genes, FAKE_ARF))
    assert [locus for locus, _, _ in bad] == ["GOOD_ARF_NO_PB1"]
    assert "has_pb1_domain mismatch" in bad[0][2]


def test_verify_genes_fails_on_panther_subfamily_mismatch(tmp_path):
    genes = _write_genes_tsv(
        tmp_path,
        [
            _row("GOOD_ARF_PB1", "ARF5", "PTHR31384:SF10", "true"),
            _row("GOOD_ARF_NO_PB1", "ARF10", "PTHR31384:SF10", "false"),
        ],
    )
    bad = run(verify(genes, FAKE_ARF))
    assert [locus for locus, _, _ in bad] == ["GOOD_ARF_NO_PB1"]
    assert "panther_subfamily mismatch" in bad[0][2]


def test_verify_genes_passes_each_rows_organism_to_the_tools(tmp_path):
    # Task 7: rows in rice and wheat. The fake's rice fixture answers only
    # when the call carries organism=oryza_sativa, so a rice row verifies
    # clean (positive) and the same locus filed under Arabidopsis fails on
    # the call itself (negative, same run) — the failure is a failed call,
    # never read as "not an ARF".
    rice = {**_row("Os01g0000100", "OsX", "PTHR31384:SF50", "false"), "organism": "oryza_sativa"}
    misfiled = _row("Os01g0000100", "OsX", "PTHR31384:SF50", "false")
    genes = _write_genes_tsv(
        tmp_path, [rice, _row("GOOD_ARF_PB1", "ARF5", "PTHR31384:SF10", "true")]
    )
    assert run(verify(genes, FAKE_ARF)) == []
    genes = _write_genes_tsv(tmp_path, [misfiled])
    bad = run(verify(genes, FAKE_ARF))
    assert [locus for locus, _, _ in bad] == ["Os01g0000100"]
    assert "interpro_domains call failed" in bad[0][2]
    assert "not ARF family" not in bad[0][2]


def test_verify_genes_matches_an_empty_subfamily_only_to_a_live_null(tmp_path):
    # An empty panther_subfamily cell is the declared form of "PANTHER
    # returned null" and must verify clean (positive); the same cell on a
    # locus PANTHER does classify must fail (negative), same run.
    genes = _write_genes_tsv(
        tmp_path,
        [
            _row("UNCLASSIFIED_ARF", "X", "", "false"),
            _row("GOOD_ARF_PB1", "ARF5", "", "true"),
        ],
    )
    bad = run(verify(genes, FAKE_ARF))
    assert [locus for locus, _, _ in bad] == ["GOOD_ARF_PB1"]
    assert "declared=None live='PTHR31384:SF10'" in bad[0][2]


def _write_raw_tsv(tmp_path: Path, lines: list[str]) -> Path:
    # For malformed manifests _write_genes_tsv's DictWriter can't produce:
    # a bad/missing header, or a data row with fewer tab-separated fields
    # than the header (csv.DictReader fills the missing trailing keys with
    # None rather than raising).
    path = tmp_path / "genes.tsv"
    path.write_text("\n".join(lines) + "\n")
    return path


def test_verify_genes_rejects_has_pb1_domain_values_outside_true_false(tmp_path):
    # Finding 1: `.strip().lower() == "true"` coerced ANY non-"true" string
    # (a typo, "yes", "1", an empty cell) to False. GOOD_ARF_NO_PB1's live
    # PB1 genuinely IS False, so under the old code every one of these
    # malformed declared values silently PASSED (coerced-False == live-
    # False) instead of being flagged — the exact silent-pass case the
    # finding names, reproduced here with the real fixture locus rather
    # than a locus that would merely fail the network call for an unrelated
    # reason. locus is deliberately repeated (a manifest may do this; TSV
    # rows aren't locus-unique) so each bad declared value is checked
    # against the SAME live PB1=False fact, distinguished by symbol.
    # Positive control (GOOD_ARF_PB1, correctly declared "true") is in the
    # same run.
    genes = _write_genes_tsv(
        tmp_path,
        [
            _row("GOOD_ARF_PB1", "GOOD", "PTHR31384:SF10", "true"),
            _row("GOOD_ARF_NO_PB1", "BAD_YES", "PTHR31384:SF193", "yes"),
            _row("GOOD_ARF_NO_PB1", "BAD_ONE", "PTHR31384:SF193", "1"),
            _row("GOOD_ARF_NO_PB1", "BAD_EMPTY", "PTHR31384:SF193", ""),
            _row("GOOD_ARF_NO_PB1", "BAD_TYPO", "PTHR31384:SF193", "true1"),
        ],
    )
    bad = run(verify(genes, FAKE_ARF))
    bad_symbols = {symbol for _, symbol, _ in bad}
    assert bad_symbols == {"BAD_YES", "BAD_ONE", "BAD_EMPTY", "BAD_TYPO"}
    reasons = {symbol: reason for _, symbol, reason in bad}
    assert "'true' or 'false'" in reasons["BAD_YES"] and "'yes'" in reasons["BAD_YES"]
    assert "'1'" in reasons["BAD_ONE"]
    assert "''" in reasons["BAD_EMPTY"]
    assert "'true1'" in reasons["BAD_TYPO"]
    # Caught before any network call — never conflated with a call failure
    # or a live has_pb1_domain mismatch.
    for reason in reasons.values():
        assert "call failed" not in reason
        assert "mismatch" not in reason


def test_verify_genes_reports_a_structurally_short_row_instead_of_crashing(tmp_path):
    # Finding 2a: a TSV data row with fewer tab-separated fields than the
    # header gives csv.DictReader's missing trailing keys a value of None,
    # and the old code crashed with a raw AttributeError the first time
    # `row["has_pb1_domain"].strip()` ran on None. Positive control (a
    # normal, complete row) is in the same manifest and the same run.
    genes = _write_raw_tsv(
        tmp_path,
        [
            "locus\tsymbol\torganism\tpanther_subfamily\thas_pb1_domain",
            "GOOD_ARF_NO_PB1\tARF10\tarabidopsis_thaliana\tPTHR31384:SF193\tfalse",
            "SHORT_ROW\tOnly",
        ],
    )
    bad = run(verify(genes, FAKE_ARF))
    assert [locus for locus, _, _ in bad] == ["SHORT_ROW"]
    locus, symbol, reason = bad[0]
    assert symbol == "Only"
    assert "structurally short" in reason
    assert "has_pb1_domain" in reason and "organism" in reason and "panther_subfamily" in reason


def test_verify_genes_raises_manifest_error_on_a_bad_header(tmp_path):
    # Finding 2b: a manifest whose header is missing or has extra columns
    # must fail loud up front, before any MCP call, rather than reading a
    # None into a real column downstream. Positive control: the same helper
    # producing a genuinely correct header does NOT raise, in the same test.
    good = _write_genes_tsv(
        tmp_path, [_row("GOOD_ARF_NO_PB1", "ARF10", "PTHR31384:SF193", "false")]
    )
    assert run(verify(good, FAKE_ARF)) == []

    missing_column = _write_raw_tsv(
        tmp_path,
        [
            "locus\tsymbol\torganism\tpanther_subfamily",
            "GOOD_ARF_NO_PB1\tARF10\tarabidopsis_thaliana\tPTHR31384:SF193",
        ],
    )
    with pytest.raises(ManifestError, match="header must be exactly"):
        run(verify(missing_column, FAKE_ARF))

    extra_column = _write_raw_tsv(
        tmp_path,
        [
            "locus\tsymbol\torganism\tpanther_subfamily\thas_pb1_domain\textra",
            "GOOD_ARF_NO_PB1\tARF10\tarabidopsis_thaliana\tPTHR31384:SF193\tfalse\tx",
        ],
    )
    with pytest.raises(ManifestError, match="header must be exactly"):
        run(verify(extra_column, FAKE_ARF))


def test_verify_genes_reports_non_dict_payload_as_its_own_failure(tmp_path):
    # Finding 3: CallResult.payload is typed dict | None, but every helper
    # assumed dict. Unreachable against the real server today, but the fake
    # server can legitimately decode a non-dict JSON value (ok=True) for
    # either tool — that must be its own failure reason, never conflated
    # with "not ARF family" (interpro) or silently passed (panther).
    # Positive control (GOOD_ARF_PB1) is in the same run as both cases.
    genes = _write_genes_tsv(
        tmp_path,
        [
            _row("GOOD_ARF_PB1", "ARF5", "PTHR31384:SF10", "true"),
            _row("INTERPRO_NON_DICT_PAYLOAD", "X5", "PTHR31384:SF10", "true"),
            _row("PANTHER_NON_DICT_PAYLOAD", "X6", "PTHR31384:SF10", "true"),
        ],
    )
    bad = run(verify(genes, FAKE_ARF))
    reasons = {locus: reason for locus, _, reason in bad}
    assert set(reasons) == {"INTERPRO_NON_DICT_PAYLOAD", "PANTHER_NON_DICT_PAYLOAD"}
    assert "interpro_domains returned a non-dict payload" in reasons["INTERPRO_NON_DICT_PAYLOAD"]
    assert "not ARF family" not in reasons["INTERPRO_NON_DICT_PAYLOAD"]
    assert "panther_family returned a non-dict payload" in reasons["PANTHER_NON_DICT_PAYLOAD"]


def test_verify_genes_reports_a_failed_call_as_an_error_not_an_absence(tmp_path):
    # Applied to verify_genes itself: CALL_FAILS makes both tool calls
    # return isError over the fake transport. The reason must say the call
    # failed, never "not ARF family" (which would misread a network/lookup
    # failure as a structured absence). Positive control (GOOD_ARF_PB1) is
    # in the same run and must stay clean.
    genes = _write_genes_tsv(
        tmp_path,
        [
            _row("GOOD_ARF_PB1", "ARF5", "PTHR31384:SF10", "true"),
            _row("CALL_FAILS", "ARF1", "PTHR31384:SF96", "true"),
        ],
    )
    bad = run(verify(genes, FAKE_ARF))
    assert [locus for locus, _, _ in bad] == ["CALL_FAILS"]
    reason = bad[0][2]
    assert "call failed" in reason
    assert "not ARF family" not in reason


def test_fake_server_rejects_an_unknown_mode_string():
    # The fake server's mode dispatch must fail loud on an unrecognised
    # mode rather than silently falling back to "ok" behaviour (task
    # instructions: "unknown mode strings should fail loud, not fall
    # back"). No stdin is needed — the mode is validated before the input
    # loop starts.
    import subprocess

    proc = subprocess.run(
        [sys.executable, str(FAKE_SERVER), "not-a-real-mode"],
        input="",
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 2
    assert "not-a-real-mode" in proc.stderr


@pytest.mark.skipif(
    not os.environ.get("PLANT_GENOMICS_MCP_STDIO_SMOKE"),
    reason="set PLANT_GENOMICS_MCP_STDIO_SMOKE=1 to run the stdio smoke test",
)
def test_verify_genes_over_real_stdio_passes_the_manifest_and_catches_a_planted_bad_row(tmp_path):
    # Real-execution control for verify_genes.py itself (not just the
    # client): the checked-in genes.tsv must verify clean against the live
    # server, and a planted bad row (AT1G23490, the ADP-ribosylation factor
    # from the interpro negative control, mislabeled as an ARF1 row) must be
    # caught in the same run — the brief's Step 4 "prove it can fail",
    # automated and gated exactly like the client's live test.
    with open(GENES_TSV) as f:
        real_rows = list(csv.DictReader(f, delimiter="\t"))
    planted = _row("AT1G23490", "ARF1", "PTHR31384:SF96", "true")
    genes = _write_genes_tsv(tmp_path, [*real_rows, planted])

    bad = run(verify(genes, SERVER_CMD))

    assert [locus for locus, _, _ in bad] == ["AT1G23490"]
