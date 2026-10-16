"""The perf ratchet's comparison and update rules (tests/perf/ratchet.py).

The counts themselves are gated by the CI step that runs
``python -m tests.perf.ratchet``; these tests pin what the gate does with a
difference: equal passes, any rise or drop fails and names the function,
``--update`` refuses a rise without ``--allow-increase``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.perf import ratchet


def _row(calls: dict[str, int], http: dict[str, int], sha: str = "a" * 64) -> dict[str, Any]:
    return {
        "tool": "t",
        "output_sha256": sha,
        "calls_total": sum(calls.values()),
        "http": http,
        "calls": calls,
    }


BASE = _row({"src/x.py:f": 3, "src/x.py:g": 1}, {"rest.ensembl.org": 2})


def test_equal_passes_and_one_extra_call_fails_naming_the_function() -> None:
    assert ratchet.compare(BASE, _row({"src/x.py:f": 3, "src/x.py:g": 1}, {"rest.ensembl.org": 2})) == ([], False)  # fmt: skip
    lines, rose = ratchet.compare(
        BASE, _row({"src/x.py:f": 4, "src/x.py:g": 1}, {"rest.ensembl.org": 2})
    )
    assert rose
    assert lines == ["  calls_total 4 -> 5 (+1)", "    +1  src/x.py:f"]


def test_a_drop_fails_but_is_not_a_rise() -> None:
    lines, rose = ratchet.compare(BASE, _row({"src/x.py:f": 3}, {"rest.ensembl.org": 2}))
    assert not rose
    assert lines == ["  calls_total 4 -> 3 (-1)", "    -1  src/x.py:g"]


def test_a_moved_call_with_the_same_total_still_fails() -> None:
    lines, rose = ratchet.compare(
        BASE, _row({"src/x.py:f": 2, "src/x.py:g": 2}, {"rest.ensembl.org": 2})
    )
    assert not rose and lines[0] == "  same calls_total, different functions:"


def test_an_extra_request_or_another_answer_fails() -> None:
    calls = {"src/x.py:f": 3, "src/x.py:g": 1}
    lines, rose = ratchet.compare(BASE, _row(calls, {"rest.ensembl.org": 2, "rest.uniprot.org": 1}))
    assert rose and lines == ["  http rest.uniprot.org 0 -> 1 (+1)"]
    lines, rose = ratchet.compare(BASE, _row(calls, {"rest.ensembl.org": 2}, sha="b" * 64))
    assert not rose and lines[0].startswith("  output_sha256 aaaaaaaaaaaa -> bbbbbbbbbbbb")


def test_update_refuses_a_rise_unless_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"workloads": {"lookup_locus": BASE}}))
    risen = _row({"src/x.py:f": 4, "src/x.py:g": 1}, {"rest.ensembl.org": 2})
    monkeypatch.setattr(ratchet, "BASELINE", baseline)
    monkeypatch.setattr(ratchet, "REPO", tmp_path)
    monkeypatch.setattr(ratchet, "measure", lambda name, record=False: risen)
    assert ratchet.main(["--only", "lookup_locus"]) == 1
    assert ratchet.main(["--only", "lookup_locus", "--update"]) == 1
    assert json.loads(baseline.read_text())["workloads"]["lookup_locus"] == BASE
    assert ratchet.main(["--only", "lookup_locus", "--update", "--allow-increase"]) == 0
    assert json.loads(baseline.read_text())["workloads"]["lookup_locus"] == risen
    assert ratchet.main(["--only", "lookup_locus"]) == 0  # positive control: now equal


def test_update_lowers_without_a_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"workloads": {"lookup_locus": BASE}}))
    lower = _row({"src/x.py:f": 3}, {"rest.ensembl.org": 2})
    monkeypatch.setattr(ratchet, "BASELINE", baseline)
    monkeypatch.setattr(ratchet, "REPO", tmp_path)
    monkeypatch.setattr(ratchet, "measure", lambda name, record=False: lower)
    assert ratchet.main(["--only", "lookup_locus"]) == 1
    assert ratchet.main(["--only", "lookup_locus", "--update"]) == 0
    assert json.loads(baseline.read_text())["workloads"]["lookup_locus"] == lower
