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
    uniprot,
)


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
        uniprot,
    ):
        mod._CACHE.clear()
