"""Unit tests for `examples.arf_family.coverage`.

`build_rows` and `release_under_another_key_tools` take plain Python data
(a fixed tool-name list, calls grouped by tool, a `gaps.jsonl` path and a
raw-file root) — no MCP server, no network, no real repo files beyond what
each test writes to `tmp_path`.

`build_rows`'s fixture has five tools: one all-`ok` (positive control), one
with a failed call (`status == "error"` — the negative case, run in the
same test so a checker that only ever sees good input can't pass by
accident), one never called at all (`status == "unused"`), and two that
exercise `release_status`'s two "called but no `upstream_version`" branches
— one in `release_under_another_key` (must read `RELEASE_UNDER_ANOTHER_KEY`)
and one not (must read `RELEASE_ABSENT`), the exact distinction round 1 of
the visual review caught missing: a null `upstream_version` was being read
as "no release reported" for tools that do report one, under another key.
"""

from __future__ import annotations

import json

import pytest

from examples.arf_family.coverage import (
    RELEASE_ABSENT,
    RELEASE_UNDER_ANOTHER_KEY,
    RELEASE_UNUSED,
    RELEASE_UPSTREAM_FIELD,
    build_rows,
    load_calls,
    release_under_another_key_tools,
)

TOOL_NAMES = ["tool_a", "tool_b", "tool_c", "tool_d", "tool_e"]

SYNTHETIC_ROWS = [
    # tool_a: two ok calls, one with an upstream_version and one without ->
    # RELEASE_UPSTREAM_FIELD (at least one non-null call is enough).
    {"tool": "tool_a", "ok": True, "n_bytes": 100, "elapsed_s": 1.0, "upstream_version": "1.0"},
    {"tool": "tool_a", "ok": True, "n_bytes": 200, "elapsed_s": 3.0, "upstream_version": None},
    # tool_b: one ok, one failed -> status == "error". Neither carries a
    # version and tool_b is not in release_under_another_key -> RELEASE_ABSENT.
    {"tool": "tool_b", "ok": True, "n_bytes": 50, "elapsed_s": 0.5, "upstream_version": None},
    {"tool": "tool_b", "ok": False, "n_bytes": 150, "elapsed_s": 1.5, "upstream_version": None},
    # tool_c: never called -> "unused" / RELEASE_UNUSED. No row here.
    # tool_d: null upstream_version on every call, but IS in
    # release_under_another_key -> RELEASE_UNDER_ANOTHER_KEY.
    {"tool": "tool_d", "ok": True, "n_bytes": 300, "elapsed_s": 0.2, "upstream_version": None},
    {"tool": "tool_d", "ok": True, "n_bytes": 300, "elapsed_s": 0.2, "upstream_version": None},
    # tool_e: null upstream_version on every call, NOT in
    # release_under_another_key -> RELEASE_ABSENT (the same as tool_b, but
    # all-ok, so this isolates the release_status branch from the error path).
    {"tool": "tool_e", "ok": True, "n_bytes": 400, "elapsed_s": 0.3, "upstream_version": None},
]

RELEASE_UNDER_ANOTHER_KEY_SET = {"tool_d"}


def _write_calls_jsonl(tmp_path):
    p = tmp_path / "calls.jsonl"
    with open(p, "w") as f:
        for row in SYNTHETIC_ROWS:
            f.write(json.dumps(row) + "\n")
    return p


def test_build_rows_counts_median_null_count_release_status_and_status(tmp_path):
    calls_path = _write_calls_jsonl(tmp_path)
    by_tool = load_calls(calls_path)

    rows = build_rows(TOOL_NAMES, by_tool, RELEASE_UNDER_ANOTHER_KEY_SET)
    by_name = {r[0]: r for r in rows}

    assert set(by_name) == set(TOOL_NAMES)

    # tool_a: all-ok positive control, reports under upstream_version.
    (tool, calls, ok, errors, median_bytes, uv_null, median_elapsed, release_status, status, *_) = (
        by_name["tool_a"]
    )
    assert calls == 2
    assert ok == 2
    assert errors == 0
    assert median_bytes == 150  # median(100, 200)
    assert uv_null == 1  # one of the two calls carried no upstream_version
    assert median_elapsed == 2.0  # median(1.0, 3.0)
    assert release_status == RELEASE_UPSTREAM_FIELD
    assert status == "ok"

    # tool_b: one failed call -> the negative case, "error" status; no
    # release under any key.
    (tool, calls, ok, errors, median_bytes, uv_null, median_elapsed, release_status, status, *_) = (
        by_name["tool_b"]
    )
    assert calls == 2
    assert ok == 1
    assert errors == 1
    assert median_bytes == 100  # median(50, 150)
    assert uv_null == 2
    assert median_elapsed == 1.0  # median(0.5, 1.5)
    assert release_status == RELEASE_ABSENT
    assert status == "error"

    # tool_c: never appears in calls.jsonl -> "unused", zeroed numerics.
    (tool, calls, ok, errors, median_bytes, uv_null, median_elapsed, release_status, status, *_) = (
        by_name["tool_c"]
    )
    assert calls == 0
    assert ok == 0
    assert errors == 0
    assert median_bytes == 0
    assert uv_null == 0
    assert median_elapsed == 0.0
    assert release_status == RELEASE_UNUSED
    assert status == "unused"

    # tool_d: null upstream_version every call, but in
    # release_under_another_key -> RELEASE_UNDER_ANOTHER_KEY, not RELEASE_ABSENT.
    (tool, calls, ok, errors, median_bytes, uv_null, median_elapsed, release_status, status, *_) = (
        by_name["tool_d"]
    )
    assert uv_null == 2
    assert release_status == RELEASE_UNDER_ANOTHER_KEY
    assert status == "ok"

    # tool_e: null upstream_version, all-ok, NOT in release_under_another_key
    # -> RELEASE_ABSENT. The positive control for RELEASE_ABSENT isolated
    # from tool_b's error status.
    (tool, calls, ok, errors, median_bytes, uv_null, median_elapsed, release_status, status, *_) = (
        by_name["tool_e"]
    )
    assert uv_null == 1
    assert release_status == RELEASE_ABSENT
    assert status == "ok"


def test_build_rows_counts_loci_locus_errors_and_expected_refusals():
    # Task 7 rows: a batch call covering three loci with one per-locus
    # error inside an ok envelope; a single call refused for an organism
    # (`expected`, in neither ok nor errors); and a thin-slice row with no
    # `kind`, which must still derive its kind from `ok`.
    by_tool = {
        "batch_x": [
            {
                "tool": "batch_x",
                "ok": True,
                "kind": "error",
                "loci": ["A", "B", "C"],
                "n_ok": 2,
                "n_error": 1,
                "n_expected": 0,
                "n_bytes": 900,
                "elapsed_s": 1.0,
                "upstream_version": None,
            }
        ],
        "y": [
            {
                "tool": "y",
                "ok": False,
                "kind": "expected",
                "locus": "A",
                "n_ok": 0,
                "n_error": 0,
                "n_expected": 1,
                "n_bytes": 200,
                "elapsed_s": 0.1,
                "upstream_version": None,
            },
            {
                "tool": "y",
                "ok": True,
                "kind": "ok",
                "locus": "B",
                "n_ok": 1,
                "n_error": 0,
                "n_expected": 0,
                "n_bytes": 300,
                "elapsed_s": 0.1,
                "upstream_version": None,
            },
        ],
        "z": [{"tool": "z", "ok": False, "n_bytes": 1, "elapsed_s": 0.1, "upstream_version": None}],
    }
    rows = {r[0]: r for r in build_rows(["batch_x", "y", "z"], by_tool, {"x"})}
    # The batch form inherits its single form's release-under-another-key
    # status (the envelope carries the same per-locus payloads); `y` has no
    # such entry and stays RELEASE_ABSENT.
    assert rows["batch_x"][7] == RELEASE_UNDER_ANOTHER_KEY
    assert rows["y"][7] == RELEASE_ABSENT
    # columns: tool, calls, ok, errors, ..., status, loci, locus_errors, expected
    assert rows["batch_x"][1:4] == [1, 0, 1]
    assert rows["batch_x"][8:] == ["error", 3, 1, 0]
    assert rows["y"][1:4] == [2, 1, 0]
    assert rows["y"][8:] == ["ok", 2, 0, 1]  # the refusal is neither ok nor error
    assert rows["z"][1:4] == [1, 0, 1]
    assert rows["z"][8:] == ["error", 1, 1, 0]


def _write_gaps_jsonl(tmp_path, raw_field, extra_rows=()):
    p = tmp_path / "gaps.jsonl"
    with open(p, "w") as f:
        for row in extra_rows:
            f.write(json.dumps(row) + "\n")
        f.write(
            json.dumps(
                {
                    "kind": "version-under-another-key",
                    "attempted": "collect upstream release ids across tools with one field name",
                    "returned": "synthetic test row",
                    "expected": "one field name for the upstream release across tools",
                    "raw": raw_field,
                    "origin": "tool",
                    "auto": False,
                }
            )
            + "\n"
        )
    return p


def _write_raw_files(tmp_path, names):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir(exist_ok=True)
    for name in names:
        (raw_dir / name).write_text("{}\n")


def test_release_under_another_key_tools_reads_names_from_the_gap_row(tmp_path):
    # Positive control: the row exists and every raw file it cites exists.
    _write_raw_files(
        tmp_path,
        ["AT1G19850__atted_coexpression.json", "AT1G19850__gramene_homologs.json"],
    )
    raw_field = "raw/AT1G19850__atted_coexpression.json, raw/AT1G19850__gramene_homologs.json"
    gaps_path = _write_gaps_jsonl(tmp_path, raw_field)

    tools = release_under_another_key_tools(gaps_path, root=tmp_path)
    assert tools == {"atted_coexpression", "gramene_homologs"}


def test_release_under_another_key_tools_raises_if_the_gap_row_is_missing(tmp_path):
    # Negative case: no "version-under-another-key" row at all -> fail loud,
    # never silently return an empty set (which would read as "fixed").
    gaps_path = tmp_path / "gaps.jsonl"
    gaps_path.write_text(
        json.dumps({"kind": "argument-name", "raw": "irrelevant", "auto": False}) + "\n"
    )

    with pytest.raises(RuntimeError, match="version-under-another-key"):
        release_under_another_key_tools(gaps_path, root=tmp_path)


def test_release_under_another_key_tools_raises_if_a_cited_raw_file_is_missing(tmp_path):
    # Negative case: the row exists but one of its raw/ files does not ->
    # the evidence can't be verified, so this must not trust the row.
    _write_raw_files(tmp_path, ["AT1G19850__atted_coexpression.json"])
    raw_field = "raw/AT1G19850__atted_coexpression.json, raw/AT1G19850__gramene_homologs.json"
    gaps_path = _write_gaps_jsonl(tmp_path, raw_field)

    with pytest.raises(RuntimeError, match="does not exist"):
        release_under_another_key_tools(gaps_path, root=tmp_path)
