"""mutmut runs the suite inside its own copy of the repository, ``mutants/``.

That copy holds only ``source_paths``, ``also_copy`` and the few files mutmut
copies by itself. A test that reads or imports from a top-level entry the copy
lacks fails to collect there, mutmut's baseline fails, and the nightly checks
0 mutants. That happened every night from 2026-10-17 to 11-10: the
``test_agent_qa_*`` tests put ``benchmarks/agent_qa`` on ``sys.path``, and
``benchmarks`` was not in ``also_copy``. Each patch fixed one missing module
(``build_questions``, ``analyze_rerun``, ``run``) and the next one broke it.

So the rule is checked for every tracked top-level entry, not only the ones a
test is known to read today: anything git tracks at the top level reaches the
copy.
"""

from __future__ import annotations

import os
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# mutmut 3.x copies these by itself (mutmut/configuration.py, ``also_copy``
# defaults), on top of ``source_paths``.
MUTMUT_DEFAULT_COPIES = {
    "tests",
    "test",
    "setup.cfg",
    "pyproject.toml",
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
    "pdm.lock",
}


def _tracked_top_level() -> set[str]:
    if not (ROOT / ".git").exists():
        # Inside mutmut's copy there is no .git, and git would answer for the
        # enclosing checkout instead. The rule is about that checkout's config.
        pytest.skip("not a git checkout (mutmut's mutants/ copy has no .git)")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    listed = subprocess.run(  # nosec B603, B607 - fixed argv, git from PATH, no shell
        ["git", "-C", str(ROOT), "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    ).stdout
    return {path.split("/", 1)[0] for path in listed.split("\0") if path}


def _carried() -> set[str]:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    mutmut = config["tool"]["mutmut"]
    named = [*mutmut["also_copy"], *mutmut["source_paths"]]
    return {Path(p).parts[0] for p in named} | MUTMUT_DEFAULT_COPIES


def test_every_tracked_top_level_entry_reaches_mutmuts_copy() -> None:
    tracked = _tracked_top_level()
    # Positive control: the listing holds the entries the suite is known to
    # read, so an empty or wrong listing cannot pass as "nothing missing".
    assert {"src", "tests", "benchmarks", "scripts", "examples", "README.md"} <= tracked, tracked
    missing = sorted(tracked - _carried())
    assert not missing, (
        f"{missing} are tracked but not copied into mutmut's mutants/ tree: add them to "
        "[tool.mutmut] also_copy in pyproject.toml, or a test that reads them fails to "
        "collect there and the nightly checks 0 mutants"
    )
