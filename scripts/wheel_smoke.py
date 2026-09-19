#!/usr/bin/env python3
"""Build the wheel, install it into a clean venv OUTSIDE the source tree, and use it.

The test job installs the project editable, so it imports the source tree: a
module or data file the build leaves out is present for every test and absent
for every user. This asks the question the tests cannot: does the artifact that
gets published work?

  1. `uv build --wheel`; install it (with its dependencies) into a fresh venv.
  2. BASE install (what `pip install pkg` gives a user), from a directory that
     is not the repo: import every top-level package and load every
     console-script entry point (resolve `pkg.mod:func`; it is not called: an
     MCP server would sit on stdin). Refuse anything that resolves into the repo.
  3. Then install every declared extra (minus `scripts/guardrails-no-extras.txt`,
     see uv_extras.py) and import EVERY submodule. A module
     behind an optional extra may fail in pass 2 only if nothing imports it
     eagerly; it may not fail here.
  4. Every git-tracked file under a shipped package directory must be in the
     wheel.

Deliberate exceptions go in `scripts/wheel-smoke-ignore.txt` (repo-owned,
optional; `#` comments, say why): an fnmatch pattern for a tracked file left out
of the wheel on purpose, or `module:pkg.mod` for a module that cannot be
imported outside its host (a Blender script importing `bpy`).

Exit 0 = passed, or skipped with a printed reason (no [build-system]: nothing is
published). Exit 1 = a finding. Exit 2 = the check could not run.
"""

# Template-owned and byte-identical in every repo, so it cannot follow each host
# repo's line length: formatted once, in repo-template.
# fmt: off

from __future__ import annotations

import fnmatch
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from uv_extras import excluded  # sibling script: scripts/ is sys.path[0]

PROBE = r"""
import importlib, importlib.metadata as md, pkgutil, sys
repo, dist, tops = sys.argv[1], sys.argv[2], sys.argv[3].split(",")
walk, skip = sys.argv[4] == "walk", set(filter(None, sys.argv[5].split(",")))
bad = 0
def fail(msg):
    global bad
    bad += 1
    print("FINDING " + msg)
def check(name):
    try:
        m = importlib.import_module(name)
    except BaseException as e:  # a SystemExit at import is a finding too
        fail(f"import {name}: {type(e).__name__}: {e}")
        return None
    f = getattr(m, "__file__", None) or ""
    if f.startswith(repo + "/"):
        fail(f"import {name} resolved into the source tree ({f}), not the installed wheel")
    return m
n = 0
for top in tops:
    m = check(top)
    n += 1
    if not walk or m is None or not hasattr(m, "__path__"):
        continue
    for info in pkgutil.walk_packages(m.__path__, top + ".", onerror=lambda _n: None):
        if info.name.rsplit(".", 1)[-1] == "__main__":
            continue  # importing it runs the program
        if info.name in skip:
            print("ignored " + info.name)
            continue
        check(info.name)
        n += 1
eps = [] if walk else [e for e in md.distribution(dist).entry_points if e.group == "console_scripts"]
for e in eps:
    try:
        e.load()
    except BaseException as x:
        fail(f"console script {e.name} = {e.value}: {type(x).__name__}: {x}")
print(f"{'all extras' if walk else 'base install'}: imported {n} module(s), loaded {len(eps)} console script(s)")
sys.exit(1 if bad else 0)
"""


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    # check=False: every caller reads the return code and reports it as a finding
    return subprocess.run(cmd, text=True, capture_output=True, check=False, **kw)  # nosec B603 - fixed argv built here, no shell


def die(msg: str) -> None:
    print(f"::error::wheel smoke could not run: {msg}")
    sys.exit(2)


def smoke() -> int:
    repo = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    pyproject = repo / "pyproject.toml"
    if not pyproject.is_file():
        print("skipped: no pyproject.toml, nothing is built or published")
        return 0
    # By line, not tomllib: this also ships to repos that support Python 3.10.
    if not re.search(r"(?m)^\s*\[\s*build-system\s*\]", pyproject.read_text()):
        print(
            "skipped: pyproject.toml has no [build-system], nothing is built or published"
        )
        return 0
    with tempfile.TemporaryDirectory() as tmp_s:
        tmp = Path(tmp_s)
        b = run(["uv", "build", "--wheel", "-o", str(tmp / "dist"), str(repo)])
        if b.returncode != 0:
            print(f"FINDING the wheel does not build:\n{b.stderr[-2000:]}")
            return 1
        wheels = list((tmp / "dist").glob("*.whl"))
        if len(wheels) != 1:
            die(f"expected one wheel, found {[w.name for w in wheels]}")
        whl = wheels[0]
        names = zipfile.ZipFile(whl).namelist()
        dist = whl.name.split("-")[0]
        tops = sorted(
            {
                n.split("/")[0]
                for n in names
                if n.endswith("/__init__.py") and n.count("/") == 1
            }
            | {n[:-3] for n in names if "/" not in n and n.endswith(".py")}
        )
        if not tops:
            print(f"FINDING {whl.name} contains no importable package or module")
            return 1
        print(f"built {whl.name}: top-level {', '.join(tops)}")

        findings = 0
        ignore_file = repo / "scripts" / "wheel-smoke-ignore.txt"
        entries = [
            ln.split("#")[0].strip()
            for ln in (
                ignore_file.read_text().splitlines() if ignore_file.is_file() else []
            )
            if ln.split("#")[0].strip()
        ]
        mod_ignore = [e[7:].strip() for e in entries if e.startswith("module:")]
        file_ignore = [e for e in entries if not e.startswith("module:")]
        in_wheel = set(names)
        for top in tops:
            src = next(
                (d for d in (repo / "src" / top, repo / top) if d.is_dir()), None
            )
            if src is None:
                continue
            ls = run(
                ["git", "-C", str(repo), "ls-files", "--", str(src.relative_to(repo))]
            )
            if ls.returncode != 0:
                die(f"git ls-files failed: {ls.stderr.strip()}")
            for tracked in ls.stdout.splitlines():
                shipped = str(Path(tracked).relative_to(src.relative_to(repo).parent))
                if shipped in in_wheel or any(
                    fnmatch.fnmatch(tracked, p) for p in file_ignore
                ):
                    continue
                print(f"FINDING tracked file is not in the wheel: {tracked}")
                findings += 1

        v = run(["uv", "venv", "-q", str(tmp / "venv")])
        if v.returncode != 0:
            die(f"uv venv: {v.stderr.strip()}")
        py = str(tmp / "venv" / "bin" / "python")
        i = run(["uv", "pip", "install", "-q", "--python", py, str(whl)])
        if i.returncode != 0:
            print(
                f"FINDING the wheel does not install into a clean venv:\n{i.stderr[-2000:]}"
            )
            return 1
        (tmp / "cwd").mkdir()

        def probe(mode: str) -> int:
            p = run(
                [
                    py,
                    "-B",
                    "-c",
                    PROBE,
                    str(repo),
                    dist,
                    ",".join(tops),
                    mode,
                    ",".join(mod_ignore),
                ],
                cwd=tmp / "cwd",
            )
            print(p.stdout, end="")
            if p.returncode not in (0, 1):
                die(f"probe exited {p.returncode}: {p.stderr[-2000:]}")
            got = sum(ln.startswith("FINDING ") for ln in p.stdout.splitlines())
            if (p.returncode == 1) != (got > 0):
                die(
                    f"probe exit {p.returncode} disagrees with {got} finding(s): {p.stderr[-2000:]}"
                )
            return got

        findings += probe("base")
        metadata = next(n for n in names if n.endswith(".dist-info/METADATA"))
        extras = [
            ln.split(":", 1)[1].strip()
            for ln in zipfile.ZipFile(whl).read(metadata).decode().splitlines()
            if ln.startswith("Provides-Extra:")
        ]
        try:
            no_extras = excluded(repo)
        except SystemExit as e:  # a typo there is "could not run", not a finding
            die(str(e.code))
        skip = {re.sub(r"[-_.]+", "-", e).lower() for e in no_extras}
        for e in [e for e in extras if e in skip]:
            print(f"excluded extra {e} (scripts/guardrails-no-extras.txt)")
        extras = [e for e in extras if e not in skip]
        if extras:
            spec = f"{dist}[{','.join(extras)}] @ {whl.as_uri()}"
            i = run(["uv", "pip", "install", "-q", "--python", py, spec])
            if i.returncode != 0:
                print(
                    f"FINDING the declared extras do not install ({', '.join(extras)}):\n{i.stderr[-2000:]}"
                )
                return 1
        findings += probe("walk")
    print(f"{findings} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(smoke())
