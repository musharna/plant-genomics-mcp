"""Shared pytest fixtures for the plant-genomics-mcp test suite.

The per-module HTTP response caches (``cache.TTLCache`` instances inside
each backend) live at module scope and persist across pytest cases by
default. That bleeds state between tests — a URL cached by test A would
satisfy a mocked request in test B without consuming the registered
``httpx_mock`` response, leaving pytest-httpx with unconsumed mocks at
teardown.

The autouse fixture below clears every module cache before each test so
each case starts with a cold cache. Live integration tests are unaffected
(the cache only matters when two requests share a key, which is what we
explicitly probe in test_cache.py).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from plant_genomics_mcp import (
    alphafold,
    aragwas,
    atted,
    bar,
    ensembl_plants,
    ensembl_variation,
    europe_pmc,
    gprofiler,
    gramene,
    interpro,
    jaspar,
    kegg,
    onekg,
    orthodb,
    panther,
    pdbe,
    phytozome,
    plantcyc,
    planteome,
    quickgo,
    string_db,
    thalemine,
    uniprot,
)
from tests import _output_contract as output_contract


@pytest.fixture(autouse=True)
def _clear_module_caches() -> None:
    for mod in (
        alphafold,
        aragwas,
        atted,
        bar,
        ensembl_plants,
        ensembl_variation,
        europe_pmc,
        gramene,
        gprofiler,
        interpro,
        jaspar,
        kegg,
        onekg,
        orthodb,
        panther,
        pdbe,
        phytozome,
        plantcyc,
        planteome,
        quickgo,
        string_db,
        thalemine,
        uniprot,
    ):
        mod._CACHE.clear()


@pytest.fixture(autouse=True)
def _answers_match_their_tool_schema(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fail a test in which a tool function returned an answer its schema rejects.

    See tests/_output_contract.py: every function `server._dispatch` routes a
    tool to is wrapped for the test, and each answer it returns is checked
    against the tool's published outputSchema and for every declared key.
    """
    output_contract.install(monkeypatch.setattr)
    start = len(output_contract.violations)
    yield
    new = output_contract.violations[start:]
    del output_contract.violations[start:]
    assert not new, "tool answers break their published schema:\n" + "\n".join(new)


@pytest.fixture(scope="session", autouse=True)
def _record_the_live_gate(record_testsuite_property: Callable[[str, object], None]) -> None:
    """Write the live gate as this pytest process saw it into the JUnit report.

    The nightly live run (scripts/classify_live_failures.py) fails a report
    whose gate is not "1". It read the gate from skip reasons before, which
    only worked while every live test's reason named the variable; two
    Gramene tests' reason is just "live". Without --junitxml this records
    nothing.
    """
    record_testsuite_property(
        "PLANT_GENOMICS_MCP_LIVE", os.environ.get("PLANT_GENOMICS_MCP_LIVE", "")
    )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """On a run of the whole suite, fail if a tool's answers were never checked.

    A check that never engaged reads the same as a check that passed. A
    subset run (one file, a node id, mutmut's per-mutant runs) is not
    expected to reach every tool, so this only applies to a run of tests/.
    """
    tests_dir = Path(__file__).parent.resolve()
    args = [Path(a).resolve() for a in session.config.args]
    if args != [tests_dir] or exitstatus != 0:
        return
    targets, _ = output_contract.dispatch_targets()
    unseen = sorted(set(targets) - set(output_contract.validated_calls))
    if unseen:
        print(f"\nno answer from these tools was checked against its schema: {unseen}")
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
