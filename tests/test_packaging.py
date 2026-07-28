"""Version identity across every place plant-genomics-mcp records it.

Ported from data-aggregator-mcp, the only repo in this account that had such a
test. It earned its keep immediately, catching two incomplete version bumps during
the v0.45.1 release; the repos without it stayed green while carrying a stale
``__version__``. This repo was one of them — during the v1.19.5 release its
``__version__`` sat at 1.19.4 with CI fully green, because nothing compared the two.

A version recorded in four places with nothing enforcing agreement will drift.
"""

from __future__ import annotations

import json
import tomllib  # stdlib on 3.11+, and this package requires >=3.11
from pathlib import Path

import plant_genomics_mcp

_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = tomllib.loads((_ROOT / "pyproject.toml").read_text())


def _cff_version() -> str:
    cff = (_ROOT / "CITATION.cff").read_text()
    line = next(ln for ln in cff.splitlines() if ln.startswith("version:"))
    return line.split(":", 1)[1].strip().strip("\"'")


def test_version_is_synced_across_all_sources() -> None:
    pyproject_version = _PYPROJECT["project"]["version"]
    module_version = plant_genomics_mcp.__version__
    sj = json.loads((_ROOT / "server.json").read_text())

    assert module_version == pyproject_version, (
        f"__version__ {module_version!r} != pyproject version {pyproject_version!r}"
    )
    assert sj["version"] == pyproject_version, (
        f"server.json top-level version {sj['version']!r} != pyproject version {pyproject_version!r}"
    )
    assert sj["packages"][0]["version"] == pyproject_version, (
        f"server.json packages[0].version {sj['packages'][0]['version']!r} "
        f"!= pyproject version {pyproject_version!r}"
    )


def test_citation_cff_version_matches_pyproject() -> None:
    """CITATION.cff feeds GitHub's cite panel and the Zenodo DOI record.

    Stale citation metadata is worse than none: it is machine-readable, and a wrong
    version propagates into other people's bibliographies where nobody re-checks it
    against the tag. This file drifted to 1.19.3 within two hours of being added.
    """
    assert _cff_version() == _PYPROJECT["project"]["version"], (
        f"CITATION.cff version {_cff_version()!r} != pyproject version "
        f"{_PYPROJECT['project']['version']!r}"
    )


def test_server_json_matches_package_identity() -> None:
    sj = json.loads((_ROOT / "server.json").read_text())
    assert sj["name"] == "io.github.musharna/plant-genomics-mcp"
    pkg = sj["packages"][0]
    assert pkg["registryType"] == "pypi"
    assert pkg["identifier"] == _PYPROJECT["project"]["name"]
    assert pkg["version"] == plant_genomics_mcp.__version__
