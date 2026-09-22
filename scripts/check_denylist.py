"""Block a push if added text contains a term from a private denylist.

The denylist itself lives outside this repo (``$PGMCP_DENYLIST`` points at a
file the author maintains elsewhere); this script never reads, writes, or
prints its contents beyond the individual terms that actually match.

Scope. This is a per-line, byte-substring text gate. It does NOT: see a term
split across two lines; understand UTF-16 or any other non-UTF-8-compatible
encoding stored raw (content is decoded as UTF-8 with ``errors="replace"``);
look inside base64, compressed, or encrypted content; or look inside a
submodule's own history (a gitlink record is skipped entirely -- its
"content" is a commit pointer, not text). In the denylist file, a line
starting with ``#`` is a comment; there is no escape syntax, so a term that
must literally start with ``#`` cannot be expressed. "Added" uses a SET
DIFFERENCE (see ``added_lines``): a line already present verbatim in the
base version of the SAME PATH is never reported, even if re-added elsewhere
in the same file -- so after adding a new term to the denylist, run the gate
once over the whole tree with ``--files`` to catch text already in the base.
The comparison is TIP TREE against BASE TREE, not commit by commit: a term
added in one commit and removed by a later commit in the same pushed range
is NOT reported, yet its blob still reaches the remote -- squash such a
range before pushing. Commit messages and annotated-tag messages are never
scanned; only tree content is compared.

``added_lines`` does not parse diff TEXT at all -- not even by counting hunk
bodies (that was round 2's fix and it still broke, see LESSONS.md
2026-09-21). A hardened text parser is still a parser of a *presentation*
format: ``str.splitlines()`` breaks on bytes git itself never treats as line
terminators (form feeds, vertical tabs, Unicode line/paragraph separators),
so a planted term placed after one of those bytes was silently dropped even
though the counts-based hunk parser was otherwise correct. Round 3 removed
the presentation layer entirely; round 4 found the listing step was
still reading UI config it shouldn't, and round 4's own fix then pinned the
WRONG THING (LESSONS.md 2026-09-21): it pinned every git call's
working directory to ``git rev-parse --show-toplevel``. A work-tree path is
a PROXY for the repository, not the repository, and ``core.worktree`` (git
writes this key into every submodule's config) or ``GIT_WORK_TREE`` can
point it at a DIFFERENT repository -- the gate then scanned that one and
reported clean. The mechanism now:

1. The repository is resolved ONCE, from the invoking directory, exactly as
   git itself discovers it: ``git rev-parse --absolute-git-dir``. That path
   -- the git directory -- IS the repository's identity; a work tree is not.
   Every subsequent call is ``git --git-dir=<that path> ...``, so the
   current directory, ``core.worktree`` and ``GIT_WORK_TREE`` cannot change
   which repository is read. An explicit ``GIT_DIR`` in the environment IS
   honoured, because that is the caller deliberately naming a repository and
   is what git itself would do.
2. Only OBJECT-DATABASE commands are used -- ``merge-base``, ``diff-tree``,
   ``cat-file blob`` -- none of which needs a work tree. Verified against
   git 2.43.0 that all three return the same answer under an explicit
   ``--git-dir`` with ``core.worktree`` and ``GIT_WORK_TREE`` pointed at an
   unrelated repository (round-4 report).
3. Listing uses git's machine-format PLUMBING (``git diff-tree -r -z
   --raw``, never ``git diff``, which is porcelain and reads ``diff.*``/
   ``color.*``/``core.*`` config -- ``diff.relative`` silently narrowed the
   listing to the invoking subdirectory) giving NUL-delimited,
   never-quoted ``(old_mode, new_mode, old_sha, new_sha, status, path)``
   records: no text to misparse, and no UI config to react to.
4. Blob content is fetched by SHA (``git cat-file blob <sha>``), never by
   ``<rev>:<path>`` revision syntax (which chokes on gitlinks and paths
   containing ``:``). Object ids are read with ``--no-abbrev`` and accepted
   at 40 hex (SHA-1) or 64 hex (SHA-256 repositories).
5. "Added text" is computed by SET DIFFERENCE on whole lines (every line of
   the new blob whose exact bytes don't already occur as a line in the old
   blob), splitting on ``b"\\n"`` only -- the one byte git itself uses as a
   line terminator.
6. Object REPLACEMENT is off. Round 4 pinned WHICH repository is read; it
   did not pin WHAT that repository says an object contains. ``git replace
   <planted blob> <clean blob>`` leaves ``diff-tree --raw`` listing the real
   planted SHA while ``cat-file blob`` returns the clean text, and ``git
   replace HEAD <clean commit>`` empties the listing outright -- both exit
   0, and ``refs/replace/*`` is not pushed by default, so the remote still
   receives the planted content. The ref namespace is itself movable
   (``GIT_REPLACE_REF_BASE``), so excluding ``refs/replace/`` would be
   guarding an instance of the class. ``--no-replace-objects`` guards
   replacement only: GRAFTS (``GIT_GRAFT_FILE``, ``info/grafts``) are a
   second read-only, never-pushed rewrite with no argv form, closed by the
   environment policy in ``_git_env``. The flag is applied in ``_run_git``
   -- the ONE place every git call goes through, including the discovery
   call -- so no call site can omit it (LESSONS.md 2026-09-21).
7. A caller-controlled value never enters a git argv as an option.
   ``check_denylist.py -- --octopus`` put ``--octopus`` in ``base``, which
   reached ``git merge-base --octopus HEAD``; that prints HEAD, the diff
   against HEAD is empty, and the gate exited 0 on a planted branch.
   ``--end-of-options`` precedes every such value (``base`` and the pushed
   sha in ``merge-base``, the resolved merge base and the pushed sha in
   ``diff-tree``, the blob SHA in ``cat-file``), so a leading-dash value is
   a "Not a valid object name" failure -- exit 2 -- rather than a flag.

WHAT IS SCANNED. By default the gate diffs ``merge-base(base, HEAD)..HEAD``.
That is wrong for the job it was written for. As a pre-push hook, ``git
push origin feat`` run while ``main`` is checked out scanned ``main``,
found nothing new, exited 0, and the planted ``feat`` landed on the remote
(reproduced; LESSONS.md 2026-09-21). HEAD is the branch you are STANDING
on, not the branch that is LEAVING. So: when stdin is not a terminal and
carries pre-push lines (``<local ref> <local sha> <remote ref> <remote
sha>``, githooks(5)), each non-deletion local sha is scanned in turn as
``merge-base(base, sha)..sha``; an all-zeros local sha is a deletion and
sends no content, so it is skipped; any other malformed line exits 2
rather than being skipped, because a skipped line is a ref pushed
unscanned. The local sha is validated as a 40- or 64-hex object id BEFORE
it reaches a git argv, and still travels behind ``--end-of-options``. The
remote sha is not read at all: the gate's own base ref, not the remote's
current tip, defines "added". With no stdin lines the behaviour is
unchanged -- HEAD.

Every run of the ``added_lines`` path prints one line to stderr naming the
absolute git directory it resolved AND the ref/sha it is about to scan
(``check_denylist: scanning <path> <ref-or-sha>``); one such line per
pushed ref. Exit codes and stdout are unaffected: an exit 0 from inside a
submodule or under a stale ``GIT_DIR`` states on its face WHICH repository
and WHICH ref it is about.

One consequence worth stating plainly: run from inside a SUBMODULE's
directory, ``--absolute-git-dir`` names the submodule's own repository, so
the gate scans the SUBMODULE and says nothing about the superproject. That
is git-consistent, but a human reading that exit 0 as "the superproject is
clean" would be wrong -- run the gate from the superproject to check the
superproject.

This also means a hunk-level notion of "line N was added" no longer exists;
line numbers are simply the new blob's own 1-based line index. A line that
already existed verbatim in the old blob is treated as already-published,
even if it was also re-added elsewhere in the new blob (documented above and
in ``added_lines``).
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import traceback
import unicodedata
from pathlib import Path

# `:<oldmode> <newmode> <oldsha> <newsha> <status>[<similarity>]` -- git's
# documented `--raw` record format (git-diff-tree(1), "raw output format").
# Modes are octal; object ids are full hex under `--no-abbrev`, 40 chars in
# a SHA-1 repository and 64 in a SHA-256 one (`git init
# --object-format=sha256`), so BOTH lengths are accepted -- a 40-only
# pattern makes every record in a SHA-256 repo a parse surprise. Status is
# one letter optionally followed by a similarity percentage (only for R/C,
# which --no-renames should make impossible -- checked explicitly below,
# never assumed).
_RAW_RECORD_RE = re.compile(
    rb"^:([0-7]{6}) ([0-7]{6}) ([0-9a-f]{40}|[0-9a-f]{64}) "
    rb"([0-9a-f]{40}|[0-9a-f]{64}) ([A-Z])(\d*)$"
)


def _is_null_sha(sha: str) -> bool:
    """True for git's "this side does not exist" all-zeros object id.

    Its LENGTH is the repository's hash length (40 hex for SHA-1, 64 for
    SHA-256), so this compares against zeros of the id's own length rather
    than a hardcoded 40 -- against a 40-char constant, a SHA-256 repo's
    null id would look like a real object and `cat-file` would fail the
    push with exit 2 on every added file.
    """
    return sha == "0" * len(sha)


class DiffParseSurprise(Exception):
    """A raw diff record didn't match git's documented machine format.

    Raised instead of guessing so a format this parser doesn't recognise
    fails the push (exit 2 via the ``main`` wrapper) rather than silently
    under- or over-reporting added lines.
    """


def _normalize(s: str) -> str:
    """NFC-normalize then casefold, so a denylist term and scanned text
    that represent the same characters with different Unicode
    decompositions (NFC vs NFD) or different case still match, and so a
    German ``ß``-style casefold expansion (``STRASSE`` vs ``straße``) is
    honoured on both sides."""
    return unicodedata.normalize("NFC", s).casefold()


def load_terms() -> list[str]:
    path = os.environ.get("PGMCP_DENYLIST")
    if not path:
        print(
            "check_denylist: PGMCP_DENYLIST is unset — refusing to report clean",
            file=sys.stderr,
        )
        raise SystemExit(2)
    p = Path(path)
    if not p.is_file():
        print(f"check_denylist: {p} not found", file=sys.stderr)
        raise SystemExit(2)
    # utf-8-sig: strips a leading UTF-8 BOM if present, behaves as plain utf-8
    # otherwise. Without it a BOM-prefixed file's first term reads as
    # "﻿TERM", which never matches anything -- a silent pass.
    terms = []
    for raw_line in p.read_text(encoding="utf-8-sig").splitlines():
        stripped = raw_line.strip()
        if stripped and not stripped.startswith("#"):
            terms.append(_normalize(stripped))
    if not terms:
        print(f"check_denylist: {p} has no terms", file=sys.stderr)
        raise SystemExit(2)
    return terms


# Global flags applied to EVERY git invocation, in `_run_git` -- the single
# place every call goes through -- rather than at each call site, so a call
# added later cannot omit them.
#
# `--no-replace-objects`: git's object-replacement mechanism rewrites what
# `cat-file` returns and what `diff-tree` lists, on a per-object basis, from
# refs that are NOT pushed by default (`refs/replace/*`, and that prefix is
# itself relocatable via `GIT_REPLACE_REF_BASE`). A gate that honours
# replacement reports on a fiction: the planted blob is the one that leaves
# for the remote, the clean replacement is the one the gate read.
# Discovery (`rev-parse --absolute-git-dir`) reads no objects and so does not
# need it, but it carries it anyway: the invariant is "every git call this
# gate makes", not "every git call a reader judged object-reading".
_GIT_GLOBAL_FLAGS = ["--no-replace-objects"]


def _git_env() -> dict[str, str]:
    """The environment every git call runs in. Some levers that change what
    git says a repository contains have NO argv form -- ``GIT_GRAFT_FILE``
    rewrites commit parentage for reads only and is never pushed -- so argv
    hygiene alone cannot close the class. Policy: inherit nothing
    ``GIT_*`` except ``GIT_DIR`` (the caller's stated repository; git sets
    it for a pre-push hook in a linked worktree) and ``GIT_CONFIG_*``
    (config cannot reach these plumbing commands' answers; see the sweep
    test), and point ``GIT_GRAFT_FILE`` at the null device, which also
    overrides ``$GIT_DIR/info/grafts``."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("GIT_") or k == "GIT_DIR" or k.startswith("GIT_CONFIG_")
    }
    env["GIT_GRAFT_FILE"] = os.devnull
    return env


def _run_git(args: list[str]) -> bytes:
    """Run one git command as an argv list (never a shell) and return its
    stdout. A non-zero exit prints git's own stderr and fails the push with
    exit 2 -- never 0 (a silent pass) and never 1 (a real hit).

    There is deliberately no ``cwd`` parameter. The current directory only
    drives repository DISCOVERY, and discovery is done exactly once, in
    ``_resolve_git_dir``; every caller here passes ``--git-dir=<that>``, so
    the directory this process happens to run in cannot change which
    repository is read.

    ``_GIT_GLOBAL_FLAGS`` is prepended here, before the subcommand, where
    git requires its global options -- verified accepted by ``rev-parse``,
    ``merge-base``, ``diff-tree`` and ``cat-file`` on git 2.43.0.
    """
    argv = [*_GIT_GLOBAL_FLAGS, *args]
    try:
        result = subprocess.run(["git", *argv], capture_output=True, check=True, env=_git_env())
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
        print(stderr, file=sys.stderr, end="")
        print(f"check_denylist: git {' '.join(argv)} failed", file=sys.stderr)
        raise SystemExit(2) from exc
    return result.stdout


def _resolve_git_dir() -> str:
    """Resolve the repository ONCE, from the invoking directory, the way
    git itself does: the absolute path of its git directory.

    The git directory IS the repository's identity. The work-tree top level
    (``git rev-parse --show-toplevel``) is only a PROXY for it, and a proxy
    that ambient state redirects: ``core.worktree`` in the repo's own config
    (git writes that key into every submodule's config) or ``GIT_WORK_TREE``
    in the environment makes the top level resolve to a path inside an
    entirely different repository. Round 4 used that path as ``cwd`` for
    every git call, i.e. as the repository's identity, and so scanned the
    other repository and reported clean -- LESSONS.md 2026-09-21.

    An explicit ``GIT_DIR`` in the environment is honoured here (git
    resolves ``--absolute-git-dir`` from it), and deliberately so: that is
    the caller naming the repository to scan, which is git's own meaning
    for the variable, not ambient redirection of a path we chose.

    Failure -- no repository at all, or anything else git objects to --
    prints git's stderr and exits 2 via ``_run_git``; there is no fallback
    to guess with. A bare repository needs none: it has a git directory
    like any other, and every command this gate runs reads only objects.
    """
    raw = _run_git(["rev-parse", "--absolute-git-dir"])
    # removesuffix, not .strip(): git terminates the path with exactly one
    # "\n" and the path's own bytes are data. A repository whose directory
    # name ends in a space is legal, and .strip() ate that space, turning
    # the gate into an exit-2 crash on a path git handles fine.
    return raw.decode("utf-8", errors="replace").removesuffix("\n")


def _split_lines(raw: bytes) -> list[bytes]:
    """Split on ``b"\\n"`` ONLY -- never ``bytes.splitlines()`` /
    ``str.splitlines()``, which also breaks on ``\\x0b \\x0c \\x1c \\x1d
    \\x1e``, U+0085, U+2028, U+2029. git only ever breaks lines on ``\\n``;
    splitting on anything else silently drops or misattributes the text
    after one of those bytes (form feeds occur in real source, U+2028 /
    U+0085 arrive via pasted web text) -- confirmed against a real repo,
    LESSONS.md 2026-09-21.

    A trailing empty segment caused by a final ``\\n`` is dropped (it isn't
    an extra line); an unterminated final segment is kept as a real line.
    """
    if raw == b"":
        return []
    lines = raw.split(b"\n")
    if lines[-1] == b"":
        lines = lines[:-1]
    return lines


def _parse_raw_records(raw: bytes) -> list[tuple[str, str, str, str, str]]:
    """Parse ``git diff-tree --raw -z`` output into ``(old_mode, new_mode,
    old_sha, new_sha, path)`` tuples, skipping records this gate doesn't
    scan.

    Skipped: status ``D`` (deleted -- nothing new to leak) and any record
    whose NEW mode is ``160000`` (a gitlink; a submodule pointer carries a
    commit SHA, not text). Everything else -- ``A``, ``M``, ``T``, and any
    status not anticipated here -- is scanned; this is a skip-LIST, never
    an allow-list, so a status this parser has never seen is scanned by
    default rather than silently dropped. An ``R``/``C`` status (rename or
    copy) should be impossible under ``--no-renames`` and is treated as a
    surprise if seen rather than assumed away. A record that doesn't match
    git's documented format at all is the same: a surprise, not a guess.
    """
    parts = raw.split(b"\0")
    if parts and parts[-1] == b"":
        parts = parts[:-1]
    if len(parts) % 2 != 0:
        raise DiffParseSurprise(
            f"raw diff record stream has an odd number of NUL-delimited "
            f"parts ({len(parts)}); expected pairs of (metadata, path)"
        )
    out: list[tuple[str, str, str, str, str]] = []
    for i in range(0, len(parts), 2):
        meta, path_bytes = parts[i], parts[i + 1]
        m = _RAW_RECORD_RE.match(meta)
        if not m:
            raise DiffParseSurprise(f"unrecognised raw diff record: {meta!r}")
        old_mode_b, new_mode_b, old_sha_b, new_sha_b, status_b, _similarity_b = m.groups()
        status = status_b.decode("ascii")
        if status in ("R", "C"):
            raise DiffParseSurprise(
                f"unexpected {status!r} (rename/copy) status despite --no-renames: {meta!r}"
            )
        new_mode = new_mode_b.decode("ascii")
        if status == "D" or new_mode == "160000":
            continue
        out.append(
            (
                old_mode_b.decode("ascii"),
                new_mode,
                old_sha_b.decode("ascii"),
                new_sha_b.decode("ascii"),
                path_bytes.decode("utf-8", errors="replace"),
            )
        )
    return out


def _blob_lines(mode: str, sha: str, git_dir: str) -> list[bytes]:
    """Bytes-exact lines of a blob at ``sha``, read from ``git_dir``'s
    object database. A side with an all-zeros object id (the file didn't
    exist on that side) or mode ``160000`` (gitlink -- its "content" is a
    commit pointer, not text) has no real content and is treated as empty
    rather than fetched.
    """
    if _is_null_sha(sha) or mode == "160000":
        return []
    return _split_lines(
        _run_git([f"--git-dir={git_dir}", "cat-file", "blob", "--end-of-options", sha])
    )


def _merge_base(base: str, rev: str, git_dir: str) -> str:
    """Merge base of ``base`` and ``rev``, read from ``git_dir``.

    BOTH are caller-controlled: ``base`` comes from the argv
    (``check_denylist.py <base>``) and ``rev`` is either the literal
    ``HEAD`` or an object id read from a pre-push hook's stdin. Neither may
    become a git OPTION. It could: ``check_denylist.py -- --octopus``
    reached ``git merge-base --octopus HEAD``, which prints HEAD, whose
    diff against HEAD is empty -- exit 0 on a planted branch
    (``--independent`` likewise). ``--end-of-options`` makes any
    leading-dash value a "Not a valid object name" failure, i.e. exit 2 via
    ``_run_git``, never a silent pass. ``rev`` is additionally checked
    against ``_SHA_RE`` before it ever reaches here (see ``pre_push_revs``).
    """
    return (
        _run_git([f"--git-dir={git_dir}", "merge-base", "--end-of-options", base, rev])
        .decode("ascii")
        .strip()
    )


def _raw_diff(merge_base: str, rev: str, git_dir: str) -> bytes:
    """``git diff-tree`` raw records for ``merge_base``..``rev``, from ``git_dir``.

    PLUMBING, never ``git diff``: the porcelain command reads
    ``diff.*``/``color.*``/``core.*`` UI config that changes what it reports
    for the exact same tree comparison -- ``diff.relative`` narrowed the
    listing to the invoking subdirectory and hid a planted term outside it
    (LESSONS.md 2026-09-21). ``--end-of-options`` guards the revision argument
    for the same reason it guards ``base`` in ``_merge_base``: the value is
    data, and a value that can turn into a flag is not data.
    """
    return _run_git(
        [
            f"--git-dir={git_dir}",
            "diff-tree",
            "-r",
            "-z",
            "--raw",
            "--no-renames",
            "--no-relative",
            "--no-abbrev",
            "--end-of-options",
            merge_base,
            rev,
        ]
    )


# A pre-push hook's stdin carries one line per ref being pushed:
# `<local ref> <local sha> <remote ref> <remote sha>` (githooks(5)). Only
# the local sha is used -- it is the commit whose content is about to leave
# for the remote -- and it is an OBJECT ID, so it is validated as one
# (40 hex for SHA-1, 64 for SHA-256) BEFORE it reaches a git argv. The
# remote sha is not needed: the gate's base ref, not the remote's current
# tip, decides what counts as newly added text.
_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def pre_push_revs(stdin_text: str) -> list[tuple[str, str]]:
    """Parse a pre-push hook's stdin into ``(local_ref, local_sha)`` pairs.

    A line whose local sha is all zeros is a DELETION -- nothing is being
    sent, so there is nothing to scan and the line is skipped. Any other
    line that is not exactly four whitespace-separated fields, or whose
    local sha is not a full hex object id, is a format this gate does not
    understand: it exits 2 (via ``SystemExit``) rather than skipping the
    line, because a skipped line is a ref pushed unscanned.

    Empty stdin yields an empty list, and the caller then scans ``HEAD`` --
    the behaviour every non-hook invocation has always had.
    """
    out: list[tuple[str, str]] = []
    for raw_line in stdin_text.splitlines():
        if not raw_line.strip():
            continue
        fields = raw_line.split()
        if len(fields) != 4:
            print(
                f"check_denylist: malformed pre-push line ({len(fields)} fields, "
                f"expected 4): {raw_line!r}",
                file=sys.stderr,
            )
            raise SystemExit(2)
        local_ref, local_sha = fields[0], fields[1]
        if local_sha in ("0" * 40, "0" * 64):
            # Deleting a remote ref sends no content. Compared against the
            # two real hash lengths, not `_is_null_sha`, which would also
            # call an EMPTY string null.
            continue
        if not _SHA_RE.match(local_sha):
            print(
                f"check_denylist: pre-push line has a local sha that is not a "
                f"40- or 64-hex object id: {local_sha!r}",
                file=sys.stderr,
            )
            raise SystemExit(2)
        out.append((local_ref, local_sha))
    return out


def added_lines(
    base: str, rev: str = "HEAD", label: str | None = None
) -> list[tuple[str, int, str]]:
    """Return (file, lineno, content) for every line "added" to ``rev`` since ``base``.

    ``rev`` defaults to ``HEAD``. As a pre-push hook the gate is handed the
    local sha of EACH ref being pushed instead: ``git push origin feat``
    with ``main`` checked out pushes ``feat`` while ``HEAD`` is ``main``,
    so a HEAD-only scan reported on a branch that was not leaving and
    exited 0 while the planted one landed on the remote (LESSONS.md
    2026-09-21). ``label`` is what the stderr "scanning" line calls ``rev``
    (the pushed ref name); it defaults to ``rev`` itself.

    "Added" = present in the new blob but not, byte-for-byte, anywhere in
    the old blob (the old blob's lines are compared as a SET, not
    positionally) -- a line already published in the old version is not a
    new leak, regardless of where in the file it now sits. Line numbers are
    the new blob's own 1-based line index; there is no hunk concept here at
    all. One consequence, by design: if a planted line already exists
    verbatim in the old blob and is added a SECOND time (e.g. duplicated
    elsewhere in the file), neither occurrence is reported -- it was
    already published under the old blob, so re-adding it publishes
    nothing new.

    The repository is resolved once (``_resolve_git_dir``) and named
    explicitly on every git call, so neither the invoking directory nor
    ``core.worktree``/``GIT_WORK_TREE`` can move which repository is read.
    Listing uses ``git diff-tree`` (PLUMBING), never ``git diff``
    (porcelain): the porcelain command reads ``diff.*``/``color.*``/
    ``core.*`` UI config that changes what it reports for the exact same
    tree comparison -- ``diff.relative`` narrows the listing to the
    invoking subdirectory, which silently hid a planted term outside it
    (LESSONS.md 2026-09-21). ``diff-tree`` does not read that config
    (confirmed empirically; see the round-4 report).

    The resolved git directory is announced on stderr, once per run: an
    exit 0 from inside a submodule, or under a stale ``GIT_DIR``, is
    truthful but says nothing about the repository the reader had in mind,
    and a reader cannot check an identity the gate never states.
    """
    git_dir = _resolve_git_dir()
    print(f"check_denylist: scanning {git_dir} {label or rev}", file=sys.stderr)
    mb = _merge_base(base, rev, git_dir)
    raw = _raw_diff(mb, rev, git_dir)
    out: list[tuple[str, int, str]] = []
    for old_mode, new_mode, old_sha, new_sha, path in _parse_raw_records(raw):
        old_lines = set(_blob_lines(old_mode, old_sha, git_dir))
        for i, line_bytes in enumerate(_blob_lines(new_mode, new_sha, git_dir), 1):
            if line_bytes not in old_lines:
                out.append((path, i, line_bytes.decode("utf-8", errors="replace")))
    return out


def _run() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("base", nargs="?", default="origin/main")
    ap.add_argument("--files", nargs="+")
    a = ap.parse_args()
    terms = load_terms()
    if a.files:
        lines: list[tuple[str, int, str]] = []
        for f in a.files:
            p = Path(f)
            if p.is_dir():
                print(f"check_denylist: {p} is a directory", file=sys.stderr)
                raise SystemExit(2)
            if not p.is_file():
                print(f"check_denylist: {p} not found", file=sys.stderr)
                raise SystemExit(2)
            for i, line_bytes in enumerate(_split_lines(p.read_bytes()), 1):
                lines.append((f, i, line_bytes.decode("utf-8", errors="replace")))
    else:
        # Not a TTY => this may be a pre-push hook, whose stdin names the
        # refs actually being pushed. No stdin at all (empty, /dev/null, a
        # terminal) => HEAD, unchanged. Stdin that HAS lines is authoritative
        # even when every line is a deletion: then nothing is leaving, so
        # nothing is scanned -- falling back to HEAD there would block a
        # branch deletion over content that is not being pushed.
        stdin_text = "" if sys.stdin is None or sys.stdin.isatty() else sys.stdin.read()
        if stdin_text.strip():
            pushed = pre_push_revs(stdin_text)
            if not pushed:
                print(
                    "check_denylist: nothing to scan — every pushed ref is a deletion",
                    file=sys.stderr,
                )
            lines = []
            for local_ref, local_sha in pushed:
                lines += added_lines(a.base, local_sha, f"{local_ref} {local_sha}")
        else:
            lines = added_lines(a.base)
    # Normalize each line ONCE, not once per (line, term) pair: with 501
    # terms over 50k lines that inner-loop normalize call measured 0.08s
    # -> 2.05s (reviewer, round-4 review).
    hits: list[tuple[str, int, str]] = []
    for f, i, t in lines:
        normalized_t = _normalize(t)
        for term in terms:
            if term in normalized_t:
                hits.append((f, i, term))
    for f, i, term in hits:
        print(f"{f}:{i}: {term}")
    return 1 if hits else 0


def main() -> int:
    """Total exit-code contract: an intentional ``SystemExit`` (2, from the
    checks above, or from argparse itself) passes through unchanged; ANY
    other uncaught throwable -- not just ``Exception``, all of
    ``BaseException`` -- prints a full traceback and exits 2. Never 1
    (indistinguishable from a real hit) and never 0 (a silent pass on a bug
    in the gate itself).
    """
    try:
        return _run()
    except SystemExit:
        raise
    except BaseException:
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
