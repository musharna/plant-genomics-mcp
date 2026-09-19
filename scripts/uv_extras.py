#!/usr/bin/env python3
"""Print the uv flags that select this repo's extras: `--all-extras`, minus any
extra the repo has excluded from CI on purpose.

    uv sync $(python3 scripts/uv_extras.py) --dev

Exclusions live in `scripts/guardrails-no-extras.txt` (repo-owned, optional; one
extra per line, `#` comments, say why): an extra too heavy for every CI run
(a 2 GB torch stack) that the repo tests in a job of its own. `wheel_smoke.py`
reads the same file. With no file this prints `--all-extras`.

A name that is not a declared extra is an error (exit 2), not a no-op: a typo
here would silently put the heavy extra back into every job.
"""

# Template-owned and byte-identical in every repo, so it cannot follow each host
# repo's line length: formatted once, in repo-template.
# fmt: off

from __future__ import annotations

import re
import sys
from pathlib import Path


def declared_extras(repo: Path) -> list[str]:
    """Keys of the [project.optional-dependencies] table.

    Read by line, not with tomllib: these scripts also ship to repos that
    support Python 3.10, which has no tomllib. Only the table form is
    recognised; the inline form (`optional-dependencies = {...}`) yields no
    names, so an exclusion there is reported as undeclared: loud, not silent.
    """
    names, inside = [], False
    for ln in (repo / "pyproject.toml").read_text().splitlines():
        head = re.match(r"\s*\[\s*([^\]]+?)\s*\]\s*(#.*)?$", ln)
        if head:
            inside = head.group(1) == "project.optional-dependencies"
        elif inside:
            key = re.match(r"""\s*["']?([A-Za-z0-9][A-Za-z0-9._-]*)["']?\s*=""", ln)
            if key:
                names.append(key.group(1))
    return names


def excluded(repo: Path) -> list[str]:
    """Extras excluded on purpose, validated against pyproject.toml."""
    f = repo / "scripts" / "guardrails-no-extras.txt"
    if not f.is_file():
        return []
    names = [ln.split("#")[0].strip() for ln in f.read_text().splitlines()]
    names = [n for n in names if n]
    declared = {re.sub(r"[-_.]+", "-", e).lower() for e in declared_extras(repo)}
    unknown = [n for n in names if re.sub(r"[-_.]+", "-", n).lower() not in declared]
    if unknown:
        raise SystemExit(
            f"::error::{f.relative_to(repo)} names {unknown}, not declared in "
            f"[project.optional-dependencies] ({sorted(declared)})"
        )
    return names


def flags() -> int:
    repo = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    try:
        names = excluded(repo)
    except SystemExit as e:
        print(e.code, file=sys.stderr)
        return 2
    print(" ".join(["--all-extras", *(f"--no-extra {n}" for n in names)]))
    return 0


if __name__ == "__main__":
    sys.exit(flags())
