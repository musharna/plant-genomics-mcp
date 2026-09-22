"""Tests for scripts/check_denylist.py.

Covers both interfaces the script exposes: the ``--files`` scan (used for
ad-hoc checks against arbitrary files) and the real ``added_lines(base)``
path a pre-push hook actually drives. Real-execution coverage against
actual git repositories -- no mocking of ``git`` -- is extensive here
because this script has now been rewritten twice after review found real
bypasses in a *text*-parsing mechanism (round 1: prefix-matching a diff's
own file-header bytes; round 2: a hardened counts-based hunk parser that
still called ``str.splitlines()``, which breaks on bytes git itself never
treats as line terminators). Round 3 (LESSONS.md 2026-09-21) removes
diff-text parsing entirely: filenames and blob SHAs come from git's raw
plumbing (``git diff-tree -r -z --raw``, NUL-delimited, machine format --
``diff-tree``, the plumbing command, NOT the porcelain ``git diff``, which
reads UI config that changes what it lists), and "added text" is a SET
DIFFERENCE between the old and new blob's lines (split on ``b"\\n"`` only),
fetched by SHA via ``git cat-file blob``. Round 4 pinned the
repository's IDENTITY -- ``git rev-parse --absolute-git-dir`` once, then
``--git-dir=`` on every call -- after the previous round's work-tree-path
``cwd`` pin turned out to be redirectable by ``core.worktree`` and
``GIT_WORK_TREE``. Round 5 pinned what that repository SAYS an object
contains -- ``--no-replace-objects`` on every call, because git's
replacement refs rewrite ``cat-file``/``diff-tree`` answers from refs that
are never pushed -- and stopped the caller-supplied ``base`` from entering a
git argv as an option (``--end-of-options``).

All git-invoking test helpers are hermetic: the subprocess environment
pins ``GIT_CONFIG_GLOBAL``/``GIT_CONFIG_SYSTEM`` to ``os.devnull``, sets
``GIT_CONFIG_NOSYSTEM=1`` so the suite never reads the real user's
``~/.gitconfig``, and removes any inherited repository-pointing variable
(``GIT_DIR``, ``GIT_WORK_TREE``, ...) so a test can only see one it set
itself. The script under test is always invoked by an absolute path
resolved relative to this test file, never a cwd-relative one.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_denylist import (  # noqa: E402
    DiffParseSurprise,
    _blob_lines,
    _merge_base,
    _parse_raw_records,
    _raw_diff,
    _resolve_git_dir,
)

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check_denylist.py"


def _hermetic_env(**extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    # A repository-pointing variable inherited from whoever ran the suite
    # would silently redirect every git call in these tests to another
    # repository -- the exact class of bug this file exists to catch, so it
    # must not be able to arrive by accident. Tests that WANT one set it
    # explicitly through ``extra``.
    # ``GIT_NO_REPLACE_OBJECTS``/``GIT_REPLACE_REF_BASE`` are here for the
    # same reason one step further in: an inherited ``GIT_NO_REPLACE_OBJECTS``
    # would disable object replacement for the whole suite, so the
    # replace-ref tests would pass on code that never asked for
    # ``--no-replace-objects`` at all -- the environment, not the fix, doing
    # the work. Tests that WANT one set it explicitly through ``extra``.
    for repo_var in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_CEILING_DIRECTORIES",
        "GIT_NO_REPLACE_OBJECTS",
        "GIT_REPLACE_REF_BASE",
        "GIT_GRAFT_FILE",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    ):
        env.pop(repo_var, None)
    env.update(extra)
    return env


def _run_script(
    args: list[str], env: dict[str, str], cwd: Path | None = None, stdin: str | None = None
):
    """Run the gate. ``stdin`` is load-bearing: the script reads pre-push
    ref lines from it when it is not a TTY, so the default here is
    DEVNULL -- an fd inherited from whoever ran the suite must never be
    able to feed the gate ref lines it did not mean to send."""
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        input=stdin,
        stdin=None if stdin is not None else subprocess.DEVNULL,
    )


def scan(text: str, denylist: str, tmp_path, env_set=True):
    dl = tmp_path / "deny.txt"
    dl.write_text(denylist)
    target = tmp_path / "added.txt"
    target.write_text(text)
    env = _hermetic_env()
    env.pop("PGMCP_DENYLIST", None)
    if env_set:
        env["PGMCP_DENYLIST"] = str(dl)
    return _run_script(["--files", str(target)], env)


def test_planted_term_blocks_and_clean_text_passes(tmp_path):
    hit = scan("notes on ZZPLANTED9 here\n", "ZZPLANTED9\n", tmp_path)
    # The printed term is load_terms()'s NFC-normalized/casefolded form
    # of the term, not the denylist file's original casing.
    assert hit.returncode == 1 and "zzplanted9" in hit.stdout
    clean = scan("AT1G19850 public locus\n", "ZZPLANTED9\n", tmp_path)
    assert clean.returncode == 0


def test_case_insensitive(tmp_path):
    assert scan("zzplanted9\n", "ZZPLANTED9\n", tmp_path).returncode == 1


def test_unset_env_is_exit_2_not_a_pass(tmp_path):
    assert scan("anything\n", "X\n", tmp_path, env_set=False).returncode == 2


def test_empty_denylist_is_exit_2(tmp_path):
    assert scan("anything\n", "# only a comment\n", tmp_path).returncode == 2


# --- I2: the --files path fails loud, never silently as exit 1 ---


def test_files_path_missing_directory_invalid_utf8_and_zero_paths(tmp_path):
    dl = tmp_path / "deny.txt"
    dl.write_text("ZZPLANTED9\n")
    env = _hermetic_env(PGMCP_DENYLIST=str(dl))

    missing = _run_script(["--files", str(tmp_path / "nope.txt")], env)
    assert missing.returncode == 2, missing.stdout + missing.stderr

    a_dir = tmp_path / "a_directory"
    a_dir.mkdir()
    dir_result = _run_script(["--files", str(a_dir)], env)
    assert dir_result.returncode == 2, dir_result.stdout + dir_result.stderr

    bad_utf8 = tmp_path / "bad_utf8.txt"
    bad_utf8.write_bytes(b"\xff\xfe garbage bytes ZZPLANTED9 tail\n")
    bad_result = _run_script(["--files", str(bad_utf8)], env)
    assert bad_result.returncode == 1, bad_result.stdout + bad_result.stderr
    assert "zzplanted9" in bad_result.stdout

    zero_paths = _run_script(["--files"], env)
    assert zero_paths.returncode == 2, zero_paths.stdout + zero_paths.stderr


# --- I1: BOM / CRLF denylists, and the indented-comment minor fix ---


def test_bom_and_crlf_denylist_first_term_matches(tmp_path):
    target = tmp_path / "added.txt"
    target.write_text("notes on ZZPLANTED9 here\n")

    bom_deny = tmp_path / "deny_bom.txt"
    bom_deny.write_bytes("ZZPLANTED9\nOTHER\n".encode("utf-8-sig"))
    env = _hermetic_env(PGMCP_DENYLIST=str(bom_deny))
    bom_result = _run_script(["--files", str(target)], env)
    assert bom_result.returncode == 1, bom_result.stdout + bom_result.stderr
    assert "ZZPLANTED9" in bom_result.stdout.upper()

    crlf_deny = tmp_path / "deny_crlf.txt"
    crlf_deny.write_bytes(b"ZZPLANTED9\r\nOTHER\r\n")
    env2 = _hermetic_env(PGMCP_DENYLIST=str(crlf_deny))
    crlf_result = _run_script(["--files", str(target)], env2)
    assert crlf_result.returncode == 1, crlf_result.stdout + crlf_result.stderr


def test_indented_comment_line_ignored_real_term_still_hits(tmp_path):
    deny = tmp_path / "deny.txt"
    deny.write_text("  # comment\nZZPLANTED9\n")
    env = _hermetic_env(PGMCP_DENYLIST=str(deny))

    # Positive control first: the literal comment text is NOT a term, so
    # scanning it must pass clean -- proves "  # comment" was excluded, not
    # silently promoted to a denylist term of its own.
    comment_target = tmp_path / "has_comment.txt"
    comment_target.write_text("this line has # comment in it\n")
    clean = _run_script(["--files", str(comment_target)], env)
    assert clean.returncode == 0, clean.stdout + clean.stderr

    real_target = tmp_path / "has_term.txt"
    real_target.write_text("ZZPLANTED9 present\n")
    hit = _run_script(["--files", str(real_target)], env)
    assert hit.returncode == 1, hit.stdout + hit.stderr


# --- N4: casefold expansion (STRASSE/straße) and NFC/NFD normalisation ---


def test_n4_casefold_expansion_and_nfc_nfd_equivalence_both_directions(tmp_path):
    deny = tmp_path / "deny.txt"
    deny.write_text("STRASSE\n")
    env = _hermetic_env(PGMCP_DENYLIST=str(deny))

    # Positive control: an exact-case match still works.
    exact = tmp_path / "exact.txt"
    exact.write_text("a STRASSE mention\n")
    exact_result = _run_script(["--files", str(exact)], env)
    assert exact_result.returncode == 1, exact_result.stdout + exact_result.stderr

    # casefold("STRASSE") == casefold("straße") in Python/Unicode.
    strasse = tmp_path / "strasse.txt"
    strasse.write_text("eine Straße hier: straße\n", encoding="utf-8")
    strasse_result = _run_script(["--files", str(strasse)], env)
    assert strasse_result.returncode == 1, strasse_result.stdout + strasse_result.stderr

    # NFD-encoded term (base char + combining accent) must match NFC content.
    nfd_deny = tmp_path / "deny_nfd.txt"
    nfd_deny.write_text(unicodedata.normalize("NFD", "café") + "\n", encoding="utf-8")
    env_nfd = _hermetic_env(PGMCP_DENYLIST=str(nfd_deny))
    nfc_target = tmp_path / "nfc.txt"
    nfc_target.write_text(unicodedata.normalize("NFC", "a café mention") + "\n", encoding="utf-8")
    nfd_term_result = _run_script(["--files", str(nfc_target)], env_nfd)
    assert nfd_term_result.returncode == 1, nfd_term_result.stdout + nfd_term_result.stderr

    # And the reverse: NFC term must match NFD-encoded content.
    nfc_deny = tmp_path / "deny_nfc.txt"
    nfc_deny.write_text(unicodedata.normalize("NFC", "café") + "\n", encoding="utf-8")
    env_nfc = _hermetic_env(PGMCP_DENYLIST=str(nfc_deny))
    nfd_target = tmp_path / "nfd.txt"
    nfd_target.write_text(unicodedata.normalize("NFD", "a café mention") + "\n", encoding="utf-8")
    nfc_term_result = _run_script(["--files", str(nfd_target)], env_nfc)
    assert nfc_term_result.returncode == 1, nfc_term_result.stdout + nfc_term_result.stderr


# --- Real-execution coverage for the added_lines(base) / git-plumbing path ---


def _run_git(args: list[str], repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, env=_hermetic_env()
    )


def _init_repo(repo: Path, base_files: dict[str, str]) -> Path:
    repo.mkdir(parents=True)
    _run_git(["init", "-q", "-b", "main"], repo)
    _run_git(["config", "user.email", "test@example.invalid"], repo)
    _run_git(["config", "user.name", "Test"], repo)
    for name, content in base_files.items():
        (repo / name).write_text(content)
    _run_git(["add", *base_files.keys()], repo)
    _run_git(["commit", "-q", "-m", "base"], repo)
    return repo


def _run_denylist(repo: Path, deny_file: Path, base: str) -> subprocess.CompletedProcess:
    env = _hermetic_env(PGMCP_DENYLIST=str(deny_file))
    return _run_script([base], env, cwd=repo)


def test_c1_quoted_and_spaced_filenames_are_scanned_correctly(tmp_path):
    """A file git renders as a QUOTED octal-escaped header in TEXT diff
    output (non-ASCII path) and a file with a literal space both get
    scanned under their real path with the correct line number. The
    plumbing-based parser never sees quoted text at all -- paths come from
    a NUL-delimited raw record -- so this is no longer special, but the
    scenario stays as a regression guard for round 1's original bug."""
    repo = _init_repo(tmp_path / "repo", {"base.txt": "base\n"})
    _run_git(["checkout", "-q", "-b", "feature"], repo)
    (repo / "café.txt").write_text("notes on ZZPLANTED9 here\n", encoding="utf-8")
    (repo / "with space.txt").write_text("notes on ZZPLANTED9 here\n")
    _run_git(["add", "café.txt", "with space.txt"], repo)
    _run_git(["commit", "-q", "-m", "add unicode and spaced files"], repo)

    deny_file = tmp_path / "deny.txt"
    deny_file.write_text("ZZPLANTED9\n")
    result = _run_denylist(repo, deny_file, "main")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "café.txt:1: " in result.stdout
    assert "with space.txt:1: " in result.stdout


def test_c2_removed_and_added_lines_that_look_like_a_file_header(tmp_path):
    """A base line reading ``-- fake header line`` replaced in place by an
    added line reading ``++ ZZPLANTED9 marker`` renders, in TEXT diff
    output, as ``--- fake header line`` / ``+++ ZZPLANTED9 marker`` --
    indistinguishable from a real diff header pair. The gate never looks at
    diff text at all, so this is no longer special; kept as a regression guard."""
    repo = _init_repo(tmp_path / "repo", {"sql.txt": "-- fake header line\nkeep1\nkeep2\n"})
    _run_git(["checkout", "-q", "-b", "feature"], repo)
    (repo / "sql.txt").write_text("++ ZZPLANTED9 marker\nkeep1\nkeep2\nZZPLANTED9 second hit\n")
    _run_git(["add", "sql.txt"], repo)
    _run_git(["commit", "-q", "-m", "feature"], repo)

    deny_file = tmp_path / "deny.txt"
    deny_file.write_text("ZZPLANTED9\n")
    result = _run_denylist(repo, deny_file, "main")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "sql.txt:1: " in result.stdout
    assert "sql.txt:4: " in result.stdout


def test_added_line_starting_with_plusplus_is_not_dropped(tmp_path):
    """An added line whose own content starts with ``++`` would collide
    with a naive ``+++`` file-header prefix check in TEXT diff output.
    The plumbing parser has no such check at all."""
    repo = _init_repo(tmp_path / "repo", {"a.txt": "clean\n"})
    _run_git(["checkout", "-q", "-b", "feature"], repo)
    (repo / "a.txt").write_text("clean\n++ZZPLANTED9 marker\n")
    _run_git(["add", "a.txt"], repo)
    _run_git(["commit", "-q", "-m", "feature"], repo)

    deny_file = tmp_path / "deny.txt"
    deny_file.write_text("ZZPLANTED9\n")
    result = _run_denylist(repo, deny_file, "main")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "a.txt:2: " in result.stdout


def test_multi_region_additions_hit_first_and_last_exact_line_numbers(tmp_path):
    base_lines = "".join(f"base line {i}\n" for i in range(1, 11))
    repo = _init_repo(tmp_path / "repo", {"multi.txt": base_lines})
    _run_git(["checkout", "-q", "-b", "feature"], repo)
    lines = [f"base line {i}\n" for i in range(1, 11)]
    lines[0] = "ZZPLANTED9 region one\n"
    lines[4] = "changed middle unrelated\n"
    lines[9] = "ZZPLANTED9 region three\n"
    (repo / "multi.txt").write_text("".join(lines))
    _run_git(["add", "multi.txt"], repo)
    _run_git(["commit", "-q", "-m", "feature"], repo)

    deny_file = tmp_path / "deny.txt"
    deny_file.write_text("ZZPLANTED9\n")
    result = _run_denylist(repo, deny_file, "main")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "multi.txt:1: " in result.stdout
    assert "multi.txt:10: " in result.stdout
    # the unrelated middle change must not spuriously appear as a hit
    assert "multi.txt:5:" not in result.stdout


def test_no_trailing_newline_last_line_is_reported(tmp_path):
    repo = _init_repo(tmp_path / "repo", {"base.txt": "base\n"})
    _run_git(["checkout", "-q", "-b", "feature"], repo)
    (repo / "notrail.txt").write_text("line1\nZZPLANTED9 no newline")
    _run_git(["add", "notrail.txt"], repo)
    _run_git(["commit", "-q", "-m", "feature"], repo)

    deny_file = tmp_path / "deny.txt"
    deny_file.write_text("ZZPLANTED9\n")
    result = _run_denylist(repo, deny_file, "main")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "notrail.txt:2: " in result.stdout


def test_binary_file_scanned_by_blob_content(tmp_path):
    repo = _init_repo(tmp_path / "repo", {"base.txt": "base\n"})
    _run_git(["checkout", "-q", "-b", "feature"], repo)
    (repo / "bin_hit.dat").write_bytes(b"\x00ZZPLANTED9binarydata")
    (repo / "bin_clean.dat").write_bytes(b"\x00CLEANbinarydata")
    _run_git(["add", "bin_hit.dat", "bin_clean.dat"], repo)
    _run_git(["commit", "-q", "-m", "feature"], repo)

    deny_file = tmp_path / "deny.txt"
    deny_file.write_text("ZZPLANTED9\n")
    result = _run_denylist(repo, deny_file, "main")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "bin_hit.dat:1: " in result.stdout
    assert "bin_clean.dat" not in result.stdout


def test_added_lines_ignores_a_term_only_on_a_removed_line_with_positive_control(tmp_path):
    repo = _init_repo(tmp_path / "repo", {"notes.txt": "ZZREMOVEDTERM was here\nkeep this line\n"})
    _run_git(["checkout", "-q", "-b", "feature/removal"], repo)
    (repo / "notes.txt").write_text("keep this line\n")
    _run_git(["add", "notes.txt"], repo)
    _run_git(["commit", "-q", "-m", "remove the term, add nothing sensitive"], repo)

    deny_file = tmp_path / "deny.txt"
    deny_file.write_text("ZZREMOVEDTERM\n")
    result = _run_denylist(repo, deny_file, "main")
    assert result.returncode == 0, result.stdout + result.stderr

    # Positive control, same repo: the gate CAN fire when the term is
    # actually added, proving the exit-0 above is a real negative and not
    # a broken gate that never fires.
    (repo / "notes.txt").write_text("keep this line\nZZREMOVEDTERM added back on purpose\n")
    _run_git(["add", "notes.txt"], repo)
    _run_git(["commit", "-q", "-m", "add the term back"], repo)
    positive = _run_denylist(repo, deny_file, "main")
    assert positive.returncode == 1, positive.stdout + positive.stderr


def test_clean_branch_passes_with_positive_control(tmp_path):
    repo = _init_repo(tmp_path / "repo", {"base.txt": "base line\n"})
    _run_git(["checkout", "-q", "-b", "feature/clean"], repo)
    (repo / "other.txt").write_text("nothing sensitive here\nsecond clean line\n")
    _run_git(["add", "other.txt"], repo)
    _run_git(["commit", "-q", "-m", "clean additions only"], repo)

    deny_file = tmp_path / "deny.txt"
    deny_file.write_text("ZZPLANTED9\n")
    result = _run_denylist(repo, deny_file, "main")
    assert result.returncode == 0, result.stdout + result.stderr

    # Positive control, same repo: prove the gate can fire here at all.
    (repo / "other.txt").write_text(
        "nothing sensitive here\nsecond clean line\nZZPLANTED9 now present\n"
    )
    _run_git(["add", "other.txt"], repo)
    _run_git(["commit", "-q", "-m", "add the term"], repo)
    positive = _run_denylist(repo, deny_file, "main")
    assert positive.returncode == 1, positive.stdout + positive.stderr


def test_duplicate_line_already_published_is_not_a_hit_new_line_is(tmp_path):
    """Documented behaviour: a line that already exists verbatim in the old
    blob is treated as already-published even if it's added a second time
    elsewhere in the file. A genuinely new line in the same commit is still
    reported."""
    repo = _init_repo(tmp_path / "repo", {"f.txt": "ZZPLANTED9 already published\nkeep\n"})
    _run_git(["checkout", "-q", "-b", "feature"], repo)
    (repo / "f.txt").write_text(
        "ZZPLANTED9 already published\nkeep\n"
        "ZZPLANTED9 already published\nZZNEWPLANT genuinely new\n"
    )
    _run_git(["add", "f.txt"], repo)
    _run_git(["commit", "-q", "-m", "duplicate + new"], repo)

    deny_file = tmp_path / "deny.txt"
    deny_file.write_text("ZZPLANTED9\nZZNEWPLANT\n")
    result = _run_denylist(repo, deny_file, "main")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "f.txt:4: " in result.stdout  # the genuinely new line
    assert "f.txt:3: " not in result.stdout  # the re-added-but-published duplicate


def test_base_ref_that_does_not_exist_is_exit_2_with_git_stderr_visible(tmp_path):
    repo = _init_repo(tmp_path / "repo", {"base.txt": "base\n"})
    deny_file = tmp_path / "deny.txt"
    deny_file.write_text("ZZPLANTED9\n")
    result = _run_denylist(repo, deny_file, "nonexistent-ref-xyz")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "nonexistent-ref-xyz" in result.stderr


# --- N1: line-terminator lookalikes that are NOT git line breaks ---

_LINE_TERMINATOR_LOOKALIKES = [
    "\x0b",  # vertical tab
    "\x0c",  # form feed
    "\x1c",  # file separator
    "\x1d",  # group separator
    "\x1e",  # record separator
    "\u0085",  # NEL (next line)
    " ",  # line separator
    " ",  # paragraph separator
]


def test_n1_all_line_terminator_lookalikes_are_not_line_breaks(tmp_path):
    """str.splitlines() breaks on all eight of these bytes/code points; git
    breaks a line only on b"\\n". A planted term placed right after one of
    these in the source text must still be found, on the correct line,
    for every one of them -- not just the byte the reviewer happened to
    pick as a repro."""
    for idx, sep in enumerate(_LINE_TERMINATOR_LOOKALIKES):
        repo = _init_repo(tmp_path / f"n1_{idx}", {"base.txt": "base\n"})
        _run_git(["checkout", "-q", "-b", "feature"], repo)
        (repo / "last.txt").write_text(f"prefix{sep}ZZPLANTED9 secret\n", encoding="utf-8")
        (repo / "middle.txt").write_text(
            f"line1\nprefix2{sep}ZZPLANTED9 middle\nline3\n", encoding="utf-8"
        )
        _run_git(["add", "last.txt", "middle.txt"], repo)
        _run_git(["commit", "-q", "-m", "plant"], repo)

        deny_file = tmp_path / f"deny_{idx}.txt"
        deny_file.write_text("ZZPLANTED9\n")
        result = _run_denylist(repo, deny_file, "main")
        assert result.returncode == 1, f"sep={sep!r}: {result.stdout}{result.stderr}"
        assert "last.txt:1: " in result.stdout, f"sep={sep!r}: {result.stdout}"
        assert "middle.txt:2: " in result.stdout, f"sep={sep!r}: {result.stdout}"

    # P2: bare \r (not one of the eight lookalikes above) IS a boundary
    # bytes.splitlines() would split on, unlike the eight above -- content
    # with no b"\n" at all must still come back as ONE line. This is the
    # case that distinguishes _split_lines from a bytes.splitlines()
    # regression that the eight lookalikes alone cannot expose.
    cr_repo = _init_repo(tmp_path / "n1_cr", {"base.txt": "base\n"})
    _run_git(["checkout", "-q", "-b", "feature"], cr_repo)
    (cr_repo / "cr.txt").write_bytes(b"line1\rZZPLANTED9\r")
    _run_git(["add", "cr.txt"], cr_repo)
    _run_git(["commit", "-q", "-m", "bare cr"], cr_repo)
    deny_cr = tmp_path / "deny_cr.txt"
    deny_cr.write_text("ZZPLANTED9\n")
    cr_result = _run_denylist(cr_repo, deny_cr, "main")
    assert cr_result.returncode == 1, cr_result.stdout + cr_result.stderr
    assert "cr.txt:1: " in cr_result.stdout, cr_result.stdout


# --- N2: type changes (--diff-filter=AM would have excluded these) ---


def test_n2_type_changes_and_new_symlinks_are_scanned(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _run_git(["init", "-q", "-b", "main"], repo)
    _run_git(["config", "user.email", "test@example.invalid"], repo)
    _run_git(["config", "user.name", "Test"], repo)

    link1 = repo / "link1"
    reg1 = repo / "reg1"
    try:
        link1.symlink_to("cleantarget")
    except OSError as exc:
        pytest.skip(f"platform cannot create symlinks: {exc}")
    reg1.write_text("regular clean\n")
    _run_git(["add", "link1", "reg1"], repo)
    _run_git(["commit", "-q", "-m", "base"], repo)

    _run_git(["checkout", "-q", "-b", "feature"], repo)
    link1.unlink()
    link1.write_text("ZZPLANTED9 now regular\n")  # symlink -> regular file
    reg1.unlink()
    reg1.symlink_to("ZZPLANTED9-target")  # regular file -> symlink
    (repo / "newlink").symlink_to("brandnew-ZZPLANTED9")  # plain added symlink
    _run_git(["add", "link1", "reg1", "newlink"], repo)
    _run_git(["commit", "-q", "-m", "type changes + new symlink"], repo)

    deny_file = tmp_path / "deny.txt"
    deny_file.write_text("ZZPLANTED9\n")
    result = _run_denylist(repo, deny_file, "main")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "link1:1: " in result.stdout
    assert "reg1:1: " in result.stdout
    assert "newlink:1: " in result.stdout


# --- N3: gitlinks (submodules) are skipped without exploding on their SHA ---


def test_n3_submodule_is_skipped_clean_and_under_diff_submodule_log(tmp_path):
    sub = tmp_path / "sub_origin"
    sub.mkdir()
    _run_git(["init", "-q", "-b", "main"], sub)
    _run_git(["config", "user.email", "test@example.invalid"], sub)
    _run_git(["config", "user.name", "Test"], sub)
    (sub / "f.txt").write_text("sub content\n")
    _run_git(["add", "f.txt"], sub)
    _run_git(["commit", "-q", "-m", "sub base"], sub)

    repo = _init_repo(tmp_path / "repo", {"base.txt": "base\n"})
    _run_git(["checkout", "-q", "-b", "feature"], repo)
    env = _hermetic_env()
    subprocess.run(
        ["git", "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "sub"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    _run_git(["commit", "-q", "-m", "add submodule only"], repo)

    deny_file = tmp_path / "deny.txt"
    deny_file.write_text("ZZPLANTED9\n")

    clean_default = _run_denylist(repo, deny_file, "main")
    assert clean_default.returncode == 0, clean_default.stdout + clean_default.stderr

    _run_git(["config", "diff.submodule", "log"], repo)
    clean_under_log = _run_denylist(repo, deny_file, "main")
    assert clean_under_log.returncode == 0, clean_under_log.stdout + clean_under_log.stderr

    # Positive control, same repo/commit set: a planted file alongside the
    # submodule must still be caught -- proves the gitlink skip doesn't
    # also swallow ordinary files in the same commit.
    (repo / "planted.txt").write_text("ZZPLANTED9 alongside submodule\n")
    _run_git(["add", "planted.txt"], repo)
    _run_git(["commit", "-q", "-m", "add planted file"], repo)
    hit = _run_denylist(repo, deny_file, "main")
    assert hit.returncode == 1, hit.stdout + hit.stderr
    assert "planted.txt:1: " in hit.stdout


# --- P1: a "current directory + git config" input the listing command was
# reading -- guarded at the CLASS level (plumbing + pinned cwd), not by
# excluding the one confirmed key ---


def _build_subdir_repo(repo: Path, *, planted: bool, term: str = "ZZPLANTED9") -> Path:
    """A repo laid out to stress config that changes what a listing command
    reports based on the invoking subdirectory: ``cwd_dir`` is where the
    gate is invoked FROM; one file lands at the repo ROOT (outside
    ``cwd_dir``'s subtree) and a second in ``sibling_dir`` (a SIBLING of
    ``cwd_dir``, also outside it) -- both are paths a cwd-relative listing
    could plausibly narrow away."""
    repo.mkdir(parents=True)
    _run_git(["init", "-q", "-b", "main"], repo)
    _run_git(["config", "user.email", "test@example.invalid"], repo)
    _run_git(["config", "user.name", "Test"], repo)
    (repo / "cwd_dir").mkdir()
    (repo / "sibling_dir").mkdir()
    (repo / "base.txt").write_text("base\n")
    (repo / "cwd_dir" / ".gitkeep").write_text("")
    (repo / "sibling_dir" / ".gitkeep").write_text("")
    _run_git(["add", "base.txt", "cwd_dir/.gitkeep", "sibling_dir/.gitkeep"], repo)
    _run_git(["commit", "-q", "-m", "base"], repo)

    _run_git(["checkout", "-q", "-b", "feature"], repo)
    if planted:
        (repo / "planted_root.txt").write_text(f"{term} at root\n")
        (repo / "sibling_dir" / "planted_sibling.txt").write_text(f"{term} in sibling\n")
        _run_git(["add", "planted_root.txt", "sibling_dir/planted_sibling.txt"], repo)
    else:
        (repo / "clean_root.txt").write_text("nothing sensitive at root\n")
        (repo / "sibling_dir" / "clean_sibling.txt").write_text("nothing sensitive in sibling\n")
        _run_git(["add", "clean_root.txt", "sibling_dir/clean_sibling.txt"], repo)
    _run_git(["commit", "-q", "-m", "feature"], repo)
    return repo


def _run_denylist_from(
    cwd: Path, deny_file: Path, base: str, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    env = _hermetic_env(PGMCP_DENYLIST=str(deny_file))
    if extra_env:
        env.update(extra_env)
    return _run_script([base], env, cwd=cwd)


def test_p1_diff_relative_from_subdirectory_still_finds_planted_terms(tmp_path):
    repo = _build_subdir_repo(tmp_path / "repo", planted=True)
    _run_git(["config", "diff.relative", "true"], repo)

    deny_file = tmp_path / "deny.txt"
    deny_file.write_text("ZZPLANTED9\n")
    result = _run_denylist_from(repo / "cwd_dir", deny_file, "main")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "planted_root.txt:1: " in result.stdout
    assert "sibling_dir/planted_sibling.txt:1: " in result.stdout


def test_bare_repo_is_scanned_and_a_bad_base_ref_is_exit_2(tmp_path):
    """A bare repository has no work tree at all -- and is exactly what a
    server-side pre-receive hook runs inside. The gate never asks
    for a work tree (identity is ``--absolute-git-dir``; every command reads
    only objects), so there is no bare-repo fallback code left to test: a
    bare repo is simply an ordinary repository here. Both exit codes are
    asserted in the same test so "exit 1" can't be a gate that fires on
    everything: a base ref that doesn't exist must still be exit 2, with
    git's own stderr visible, not a hit and not a pass."""
    src = _init_repo(tmp_path / "src", {"base.txt": "base\n"})
    _run_git(["checkout", "-q", "-b", "feature"], src)
    (src / "secret.txt").write_text("ZZPLANTED9\n")
    _run_git(["add", "secret.txt"], src)
    _run_git(["commit", "-q", "-m", "feature"], src)

    bare = tmp_path / "bare.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(src), str(bare)],
        check=True,
        capture_output=True,
        text=True,
        env=_hermetic_env(),
    )

    deny_file = tmp_path / "deny.txt"
    deny_file.write_text("ZZPLANTED9\n")
    result = _run_denylist_from(bare, deny_file, "main")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "secret.txt:1: " in result.stdout

    bad_base = _run_denylist_from(bare, deny_file, "nonexistent-ref-xyz")
    assert bad_base.returncode == 2, bad_base.stdout + bad_base.stderr
    assert "nonexistent-ref-xyz" in bad_base.stderr, bad_base.stderr


def test_outside_any_repository_is_exit_2_with_git_stderr_visible(tmp_path):
    """No repository to discover from the invoking directory: git's own
    "not a git repository" message must reach stderr and the gate must exit
    2. There is no fallback that could turn this into a silent pass."""
    nowhere = tmp_path / "nowhere"
    nowhere.mkdir()
    deny_file = tmp_path / "deny.txt"
    deny_file.write_text("ZZPLANTED9\n")
    # GIT_CEILING_DIRECTORIES stops discovery from walking out of tmp_path
    # into whatever repository happens to contain it.
    result = _run_denylist_from(
        nowhere, deny_file, "main", {"GIT_CEILING_DIRECTORIES": str(tmp_path)}
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "not a git repository" in result.stderr, result.stderr

    # Positive control in the same test: from a real repository under the
    # same ceiling, the gate runs normally and reports the planted term --
    # so exit 2 above is "no repository", not "the gate cannot run here".
    repo = _build_subdir_repo(tmp_path / "repo", planted=True)
    ok = _run_denylist_from(
        repo / "cwd_dir", deny_file, "main", {"GIT_CEILING_DIRECTORIES": str(tmp_path)}
    )
    assert ok.returncode == 1, ok.stdout + ok.stderr
    assert "planted_root.txt:1: " in ok.stdout, ok.stdout


# --- Repository IDENTITY, not the current directory. A work-tree
# path is a PROXY for the repository and ambient config/env can point it
# somewhere else; the git directory is the repository. ---


def test_f1a_core_worktree_pointing_at_another_repo_still_scans_this_repo(tmp_path):
    """``core.worktree`` set in THIS repo, pointing at a second, clean
    repository, must not move what the gate scans. git writes this key into
    every submodule's config, so it is ordinary state, not an exotic one.
    """
    planted = _build_subdir_repo(tmp_path / "planted", planted=True)
    other = _build_subdir_repo(tmp_path / "other", planted=False)
    deny_file = tmp_path / "deny.txt"
    deny_file.write_text("ZZPLANTED9\n")

    _run_git(["config", "core.worktree", str(other)], planted)
    crossed = _run_denylist_from(planted / "cwd_dir", deny_file, "main")
    assert crossed.returncode == 1, crossed.stdout + crossed.stderr
    assert "planted_root.txt:1: " in crossed.stdout, crossed.stdout
    assert "sibling_dir/planted_sibling.txt:1: " in crossed.stdout, crossed.stdout

    # Control A (same repo, key removed): the exit 1 above is not an
    # artefact of the crossed config -- the gate fires here either way.
    _run_git(["config", "--unset", "core.worktree"], planted)
    uncrossed = _run_denylist_from(planted / "cwd_dir", deny_file, "main")
    assert uncrossed.returncode == 1, uncrossed.stdout + uncrossed.stderr
    assert "planted_root.txt:1: " in uncrossed.stdout, uncrossed.stdout

    # Control B (the other repo, scanned on its own): it really is clean,
    # so "exit 1 while crossed" cannot be explained by the second repo
    # carrying the term too.
    clean = _run_denylist_from(other / "cwd_dir", deny_file, "main")
    assert clean.returncode == 0, f"exit {clean.returncode}: {clean.stdout}{clean.stderr}"


def test_f1b_git_work_tree_env_pointing_at_another_repo_still_scans_this_repo(tmp_path):
    """``GIT_WORK_TREE`` in the environment, with no ``GIT_DIR``, must not
    move what the gate scans either: no repository config is involved at
    all, so this is the same defect reachable through the environment."""
    planted = _build_subdir_repo(tmp_path / "planted", planted=True)
    other = _build_subdir_repo(tmp_path / "other", planted=False)
    deny_file = tmp_path / "deny.txt"
    deny_file.write_text("ZZPLANTED9\n")
    crossed_env = {"GIT_WORK_TREE": str(other)}

    crossed = _run_denylist_from(planted / "cwd_dir", deny_file, "main", crossed_env)
    assert crossed.returncode == 1, crossed.stdout + crossed.stderr
    assert "planted_root.txt:1: " in crossed.stdout, crossed.stdout
    assert "sibling_dir/planted_sibling.txt:1: " in crossed.stdout, crossed.stdout

    # Control A (same repo, no env override).
    plain = _run_denylist_from(planted / "cwd_dir", deny_file, "main")
    assert plain.returncode == 1, plain.stdout + plain.stderr
    assert "planted_root.txt:1: " in plain.stdout, plain.stdout

    # Control B (the clean repo, under the very same crossed env): proves
    # the gate can still report clean, so exit 1 above is discriminating.
    clean = _run_denylist_from(other / "cwd_dir", deny_file, "main", crossed_env)
    assert clean.returncode == 0, f"exit {clean.returncode}: {clean.stdout}{clean.stderr}"


def test_identity_invariant_is_the_git_dir_git_itself_discovers(tmp_path):
    """The invariant, stated behaviourally: the repository the gate scans is
    the one ``git rev-parse --absolute-git-dir`` names from the invoking
    directory -- not the one a work-tree path points at.

    Two repositories, each carrying a DIFFERENT planted term on its branch,
    with both terms on the denylist: which term comes back identifies which
    repository was actually read, so every case here is its own positive
    control -- a broken gate reports the wrong term, an inert gate reports
    neither. ``GIT_DIR`` is the deliberate exception: it is git's own way of
    naming a repository, so pointing it at the other repo MUST report the
    other repo's term.
    """
    repo_a = _build_subdir_repo(tmp_path / "repo_a", planted=True, term="ZZIDENTAAA")
    repo_b = _build_subdir_repo(tmp_path / "repo_b", planted=True, term="ZZIDENTBBB")
    deny_file = tmp_path / "deny.txt"
    deny_file.write_text("ZZIDENTAAA\nZZIDENTBBB\n")

    def terms_reported(cwd: Path, extra_env: dict[str, str] | None = None) -> set[str]:
        result = _run_denylist_from(cwd, deny_file, "main", extra_env)
        assert result.returncode == 1, (
            f"cwd={cwd} env={extra_env}: exit {result.returncode}: {result.stdout}{result.stderr}"
        )
        return {t for t in ("zzidentaaa", "zzidentbbb") if t in result.stdout}

    for here, there, own_term, other_term in (
        (repo_a, repo_b, "zzidentaaa", "zzidentbbb"),
        (repo_b, repo_a, "zzidentbbb", "zzidentaaa"),
    ):
        from_dir = here / "cwd_dir"

        assert terms_reported(from_dir) == {own_term}, "plain"

        _run_git(["config", "core.worktree", str(there)], here)
        assert terms_reported(from_dir) == {own_term}, "core.worktree crossed"
        _run_git(["config", "--unset", "core.worktree"], here)

        assert terms_reported(from_dir, {"GIT_WORK_TREE": str(there)}) == {own_term}, (
            "GIT_WORK_TREE crossed"
        )

        # By design, and the one case that must NOT report ``own_term``.
        assert terms_reported(from_dir, {"GIT_DIR": str(there / ".git")}) == {other_term}, (
            "explicit GIT_DIR"
        )


def test_linked_worktree_checkout_is_scanned_including_from_a_subdirectory(tmp_path):
    """``git worktree add`` gives a checkout whose ``.git`` is a FILE
    pointing into ``<main>/.git/worktrees/<name>``. ``--absolute-git-dir``
    resolves to that per-worktree directory, whose HEAD is the linked
    checkout's own -- so the linked branch, not the main checkout's, is what
    gets scanned. Also run from a subdirectory under ``diff.relative=true``,
    the config that started this whole class."""
    main_repo = _init_repo(tmp_path / "main_repo", {"base.txt": "base\n"})
    _run_git(["worktree", "add", "-q", "-b", "wtbranch", str(tmp_path / "wt"), "main"], main_repo)
    wt = tmp_path / "wt"
    (wt / "deep").mkdir()
    (wt / "planted_root.txt").write_text("ZZPLANTED9 in the linked worktree\n")
    (wt / "deep" / ".gitkeep").write_text("")
    _run_git(["add", "planted_root.txt", "deep/.gitkeep"], wt)
    _run_git(["commit", "-q", "-m", "plant in linked worktree"], wt)

    deny_file = tmp_path / "deny.txt"
    deny_file.write_text("ZZPLANTED9\n")

    at_top = _run_denylist_from(wt, deny_file, "main")
    assert at_top.returncode == 1, at_top.stdout + at_top.stderr
    assert "planted_root.txt:1: " in at_top.stdout, at_top.stdout

    _run_git(["config", "diff.relative", "true"], wt)
    from_subdir = _run_denylist_from(wt / "deep", deny_file, "main")
    assert from_subdir.returncode == 1, from_subdir.stdout + from_subdir.stderr
    assert "planted_root.txt:1: " in from_subdir.stdout, from_subdir.stdout

    # Positive control for the "wrong repository" direction: the MAIN
    # checkout is still on the clean base commit, so scanning it reports
    # nothing -- proving the hits above came from the linked worktree's own
    # HEAD and not from whatever the main checkout happens to hold.
    main_result = _run_denylist_from(main_repo, deny_file, "main")
    assert main_result.returncode == 0, (
        f"exit {main_result.returncode}: {main_result.stdout}{main_result.stderr}"
    )


def test_run_from_inside_a_submodule_scans_the_submodule_documented_behaviour(tmp_path):
    """Documented scope, asserted so it cannot drift silently: invoked from
    inside a submodule's directory, ``--absolute-git-dir`` names the
    submodule's own repository (``<super>/.git/modules/<name>``), so the
    gate scans the SUBMODULE. Reading that exit 0 as "the superproject is
    clean" would be wrong -- hence the planted-in-the-submodule positive
    control here, which the superproject's own scan does NOT report."""
    sub_origin = _init_repo(tmp_path / "sub_origin", {"f.txt": "sub content\n"})

    super_repo = _init_repo(tmp_path / "super", {"base.txt": "base\n"})
    _run_git(["checkout", "-q", "-b", "feature"], super_repo)
    subprocess.run(
        [
            "git",
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(sub_origin),
            "sub",
        ],
        cwd=super_repo,
        check=True,
        capture_output=True,
        text=True,
        env=_hermetic_env(),
    )
    _run_git(["commit", "-q", "-m", "add submodule"], super_repo)

    sub = super_repo / "sub"
    deny_file = tmp_path / "deny.txt"
    deny_file.write_text("ZZPLANTED9\n")

    # The submodule is its own repository with its own config: the
    # superproject's identity settings do not reach it.
    _run_git(["config", "user.email", "test@example.invalid"], sub)
    _run_git(["config", "user.name", "Test"], sub)

    # Nothing added on the submodule's own branch yet: scanning from inside
    # it is clean, and so is the superproject (gitlinks are skipped).
    _run_git(["checkout", "-q", "-b", "subfeature"], sub)
    clean_inside = _run_denylist_from(sub, deny_file, "main")
    assert clean_inside.returncode == 0, (
        f"exit {clean_inside.returncode}: {clean_inside.stdout}{clean_inside.stderr}"
    )

    # Plant inside the submodule, on the submodule's branch.
    (sub / "leak.txt").write_text("ZZPLANTED9 inside the submodule\n")
    _run_git(["add", "leak.txt"], sub)
    _run_git(["commit", "-q", "-m", "plant in submodule"], sub)

    inside = _run_denylist_from(sub, deny_file, "main")
    assert inside.returncode == 1, inside.stdout + inside.stderr
    assert "leak.txt:1: " in inside.stdout, inside.stdout

    # The superproject, scanned as itself, does not see it: the gitlink is
    # a commit pointer, not text. This is the reading a human must not get
    # wrong, so it is pinned here next to its counterpart.
    from_super = _run_denylist_from(super_repo, deny_file, "main")
    assert from_super.returncode == 0, (
        f"exit {from_super.returncode}: {from_super.stdout}{from_super.stderr}"
    )


def test_sha256_repository_planted_is_a_hit_and_clean_passes(tmp_path):
    """A SHA-256 repository's raw records carry 64-hex object ids, and its
    "side does not exist" id is 64 zeros. A 40-hex-only record pattern makes
    every record a parse surprise (exit 2), and a 40-zero null constant
    makes the null id look like a real object (``cat-file`` failure, exit
    2); both are asserted against here by requiring the ordinary 1/0."""
    init = subprocess.run(
        ["git", "init", "-q", "-b", "main", "--object-format=sha256", str(tmp_path / "s256")],
        capture_output=True,
        text=True,
        env=_hermetic_env(),
    )
    if init.returncode != 0:
        pytest.skip(f"this git cannot create SHA-256 repositories: {init.stderr.strip()}")

    repo = tmp_path / "s256"
    _run_git(["config", "user.email", "test@example.invalid"], repo)
    _run_git(["config", "user.name", "Test"], repo)
    (repo / "base.txt").write_text("base\n")
    _run_git(["add", "base.txt"], repo)
    _run_git(["commit", "-q", "-m", "base"], repo)
    _run_git(["checkout", "-q", "-b", "feature"], repo)
    (repo / "clean.txt").write_text("nothing sensitive\n")
    _run_git(["add", "clean.txt"], repo)
    _run_git(["commit", "-q", "-m", "clean addition"], repo)

    deny_file = tmp_path / "deny.txt"
    deny_file.write_text("ZZPLANTED9\n")

    # Clean first: proves the records PARSE at 64 hex at all (a 40-only
    # pattern would raise DiffParseSurprise -> exit 2, not 0 here).
    clean = _run_denylist_from(repo, deny_file, "main")
    assert clean.returncode == 0, f"exit {clean.returncode}: {clean.stdout}{clean.stderr}"

    (repo / "planted.txt").write_text("ZZPLANTED9 in a sha256 repo\n")
    _run_git(["add", "planted.txt"], repo)
    _run_git(["commit", "-q", "-m", "plant"], repo)
    hit = _run_denylist_from(repo, deny_file, "main")
    assert hit.returncode == 1, hit.stdout + hit.stderr
    assert "planted.txt:1: " in hit.stdout, hit.stdout


@pytest.mark.parametrize(
    "suffix",
    [" ", "\n"],
    ids=["trailing-space", "trailing-newline"],
)
def test_git_dir_path_ending_in_whitespace_still_reports_the_hit(suffix, tmp_path):
    """F3 regression, aimed at the byte that actually matters.

    The resolved git-directory path is DATA, not a token to tidy up, and
    ``.strip()`` on it ate a path's own trailing whitespace -- the gate then
    crashed to exit 2, and a gate that cannot run is a gate someone silences
    with ``|| true``. The case only exists where the path ENDS in that byte:
    for a normal repository the git dir ends in ``/.git``, so a BARE
    repository (which a server-side hook runs inside anyway) named with the
    awkward character is what exercises it. The newline case additionally
    separates ``removesuffix("\\n")`` -- strip exactly the one terminator git
    appended -- from ``.strip()``/``.rstrip("\\n")``, which eat the path's own
    trailing newline as well."""
    src = _init_repo(tmp_path / "src", {"base.txt": "base\n"})
    _run_git(["checkout", "-q", "-b", "feature"], src)
    (src / "secret.txt").write_text("ZZPLANTED9 here\n")
    _run_git(["add", "secret.txt"], src)
    _run_git(["commit", "-q", "-m", "plant"], src)
    clean_src = _init_repo(tmp_path / "clean_src", {"base.txt": "base\n"})
    _run_git(["checkout", "-q", "-b", "feature"], clean_src)
    (clean_src / "ordinary.txt").write_text("nothing sensitive\n")
    _run_git(["add", "ordinary.txt"], clean_src)
    _run_git(["commit", "-q", "-m", "clean"], clean_src)

    def _bare_clone_at(source: Path, name: str) -> Path:
        dest = tmp_path / name
        subprocess.run(
            ["git", "clone", "-q", "--bare", str(source), str(dest)],
            check=True,
            capture_output=True,
            text=True,
            env=_hermetic_env(),
        )
        return dest

    bare_hit = _bare_clone_at(src, "bare hit" + suffix)
    deny_file = tmp_path / "deny.txt"
    deny_file.write_text("ZZPLANTED9\n")

    result = _run_denylist_from(bare_hit, deny_file, "main")
    assert result.returncode == 1, f"exit {result.returncode}: {result.stdout}{result.stderr}"
    assert "secret.txt:1: " in result.stdout, result.stdout

    # Positive control under an equally awkward path: a clean repo there
    # exits 0, so the 1 above is a hit and not a crash-shaped 1.
    bare_clean = _bare_clone_at(clean_src, "bare clean" + suffix)
    clean = _run_denylist_from(bare_clean, deny_file, "main")
    assert clean.returncode == 0, f"exit {clean.returncode}: {clean.stdout}{clean.stderr}"


def test_repository_path_with_an_embedded_newline_still_reports_the_hit(tmp_path):
    """The same class from inside the path rather than at its end: a
    directory component containing a newline must not be mistaken for a
    line boundary anywhere in the pipeline."""
    repo = _build_subdir_repo(tmp_path / "news\nline", planted=True)
    deny_file = tmp_path / "deny.txt"
    deny_file.write_text("ZZPLANTED9\n")

    result = _run_denylist_from(repo / "cwd_dir", deny_file, "main")
    assert result.returncode == 1, f"exit {result.returncode}: {result.stdout}{result.stderr}"
    assert "planted_root.txt:1: " in result.stdout, result.stdout

    clean_repo = _build_subdir_repo(tmp_path / "news\nline_clean", planted=False)
    clean = _run_denylist_from(clean_repo / "cwd_dir", deny_file, "main")
    assert clean.returncode == 0, f"exit {clean.returncode}: {clean.stdout}{clean.stderr}"


# --- Config sweep: the CLASS of "config changes what the listing
# command reports", parametrised over specific keys and both ways a real
# user or CI environment applies git config, run from a subdirectory ---

_CONFIG_SWEEP = [
    ("diff.relative", "true"),
    ("diff.noprefix", "true"),
    ("diff.mnemonicPrefix", "true"),
    ("diff.renames", "copies"),
    ("diff.renames", "true"),
    ("diff.algorithm", "histogram"),
    ("diff.context", "10"),
    ("diff.interHunkContext", "8"),
    ("diff.external", "__NOOP_SCRIPT__"),
    ("diff.submodule", "log"),
    ("diff.ignoreSubmodules", "all"),
    ("diff.orderFile", "__ORDER_FILE__"),
    ("diff.suppressBlankEmpty", "true"),
    ("color.ui", "always"),
    ("color.diff", "always"),
    ("core.quotePath", "false"),
    ("core.pager", "cat"),
    ("core.autocrlf", "true"),
    ("diff.wsErrorHighlight", "all"),
    ("diff.colorMoved", "zebra"),
    # The two keys that do not merely change what git REPORTS about a
    # repository but which repository's work tree git believes it is
    # looking at. ``core.worktree`` is a config key (both application
    # methods apply); ``GIT_WORK_TREE`` is an environment variable, so only
    # the env half runs for it -- see ``_is_env_var`` below.
    ("core.worktree", "__OTHER_REPO__"),
    ("GIT_WORK_TREE", "__OTHER_REPO__"),
]


def _sweep_value(raw_value: str, templates: dict, *, scanning: str) -> str:
    """Resolve a sweep placeholder for the repo about to be scanned.

    ``__OTHER_REPO__`` deliberately points at the OPPOSITE template, so the
    crossing runs in both directions: the planted repo is crossed to a
    clean one (a redirected gate would report clean -- a false negative)
    and the clean repo is crossed to the planted one (a redirected gate
    would report a hit -- proving the case discriminates rather than
    passing because nothing ever fires)."""
    if raw_value == "__NOOP_SCRIPT__":
        return templates["script"]
    if raw_value == "__ORDER_FILE__":
        return templates["orderfile"]
    if raw_value == "__OTHER_REPO__":
        return str(templates["clean"] if scanning == "hit" else templates["hit"])
    return raw_value


@pytest.fixture(scope="module")
def _sweep_templates(tmp_path_factory):
    root = tmp_path_factory.mktemp("sweep_templates")
    hit = _build_subdir_repo(root / "hit", planted=True)
    clean = _build_subdir_repo(root / "clean", planted=False)
    noop_script = root / "noop_diff_external.sh"
    noop_script.write_text("#!/bin/sh\nexit 0\n")
    noop_script.chmod(0o755)
    order_file = root / "orderfile.txt"
    order_file.write_text("*\n")
    return {"hit": hit, "clean": clean, "script": str(noop_script), "orderfile": str(order_file)}


@pytest.mark.parametrize("key,raw_value", _CONFIG_SWEEP, ids=[f"{k}={v}" for k, v in _CONFIG_SWEEP])
def test_config_sweep_from_subdirectory_repo_local_and_env(
    key, raw_value, _sweep_templates, tmp_path
):
    hit_value = _sweep_value(raw_value, _sweep_templates, scanning="hit")
    clean_value = _sweep_value(raw_value, _sweep_templates, scanning="clean")
    # A git CONFIG key has a section (``diff.relative``); ``GIT_WORK_TREE``
    # is an environment variable and cannot be applied with `git config` or
    # GIT_CONFIG_KEY_0 at all, so it runs the env half only.
    is_env_var = "." not in key

    if not is_env_var:
        # -- (a) repo-local, via `git config`, on FRESH copies (never mutate
        # the shared templates) --
        hit_local = tmp_path / "hit_local"
        shutil.copytree(_sweep_templates["hit"], hit_local)
        _run_git(["config", key, hit_value], hit_local)
        deny_hit_local = tmp_path / "deny_hit_local.txt"
        deny_hit_local.write_text("ZZPLANTED9\n")
        hit_local_result = _run_denylist_from(hit_local / "cwd_dir", deny_hit_local, "main")
        assert hit_local_result.returncode == 1, (
            f"{key}={hit_value} repo-local hit: {hit_local_result.stdout}{hit_local_result.stderr}"
        )
        assert "planted_root.txt:1: " in hit_local_result.stdout, hit_local_result.stdout
        assert "sibling_dir/planted_sibling.txt:1: " in hit_local_result.stdout, (
            hit_local_result.stdout
        )

        clean_local = tmp_path / "clean_local"
        shutil.copytree(_sweep_templates["clean"], clean_local)
        _run_git(["config", key, clean_value], clean_local)
        deny_clean_local = tmp_path / "deny_clean_local.txt"
        deny_clean_local.write_text("ZZPLANTED9\n")
        clean_local_result = _run_denylist_from(clean_local / "cwd_dir", deny_clean_local, "main")
        assert clean_local_result.returncode == 0, (
            f"{key}={clean_value} repo-local clean: exit {clean_local_result.returncode}: "
            f"{clean_local_result.stdout}{clean_local_result.stderr}"
        )

    # -- (b) through the process environment, on the shared (unmodified)
    # templates: the variable itself for an env-var case, otherwise
    # GIT_CONFIG_COUNT/KEY_0/VALUE_0. Env vars are process-scoped, no repo
    # mutation, so template reuse is safe --
    def _env_for(value: str) -> dict[str, str]:
        if is_env_var:
            return {key: value}
        return {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": key, "GIT_CONFIG_VALUE_0": value}

    deny_hit_env = tmp_path / "deny_hit_env.txt"
    deny_hit_env.write_text("ZZPLANTED9\n")
    hit_env_result = _run_denylist_from(
        _sweep_templates["hit"] / "cwd_dir", deny_hit_env, "main", _env_for(hit_value)
    )
    assert hit_env_result.returncode == 1, (
        f"{key}={hit_value} env hit: {hit_env_result.stdout}{hit_env_result.stderr}"
    )
    assert "planted_root.txt:1: " in hit_env_result.stdout, hit_env_result.stdout
    assert "sibling_dir/planted_sibling.txt:1: " in hit_env_result.stdout, hit_env_result.stdout

    deny_clean_env = tmp_path / "deny_clean_env.txt"
    deny_clean_env.write_text("ZZPLANTED9\n")
    clean_env_result = _run_denylist_from(
        _sweep_templates["clean"] / "cwd_dir", deny_clean_env, "main", _env_for(clean_value)
    )
    assert clean_env_result.returncode == 0, (
        f"{key}={clean_value} env clean: exit {clean_env_result.returncode}: "
        f"{clean_env_result.stdout}{clean_env_result.stderr}"
    )


# --- Unit-level: every git call names the repository, including the blob
# read. End-to-end tests cannot catch a missing ``--git-dir`` on ONE call,
# because the gate's subprocesses all share its cwd and environment, so
# discovery there agrees with the resolved identity by construction. The
# contract each helper must hold on its own is therefore asserted directly:
# "read from the repository you were HANDED, not the one you are standing
# in". That is what keeps the identity pin from quietly decaying into a
# cwd dependency again. ---


def test_blob_lines_reads_the_repository_it_is_given_not_the_current_directory(
    tmp_path, monkeypatch
):
    named = _build_subdir_repo(tmp_path / "named", planted=True)
    unrelated = _init_repo(tmp_path / "unrelated", {"base.txt": "base\n"})

    env = _hermetic_env()
    sha = subprocess.run(
        ["git", "rev-parse", "feature:planted_root.txt"],
        cwd=named,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()

    # Stand inside a DIFFERENT repository, which does not have that object.
    monkeypatch.chdir(unrelated)
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

    named_git_dir = str(named / ".git")
    assert _blob_lines("100644", sha, named_git_dir) == [b"ZZPLANTED9 at root"]

    # Positive control for the discrimination itself: the repository we are
    # standing in genuinely does NOT have this object, so the success above
    # can only have come from the git dir that was passed in. Without it,
    # the read resolves against the current directory and fails the push.
    with pytest.raises(SystemExit) as unrelated_exc:
        _blob_lines("100644", sha, str(unrelated / ".git"))
    assert unrelated_exc.value.code == 2

    # And the identity resolved from HERE is the unrelated repo, not the
    # named one -- i.e. the two really do differ in this process.
    assert Path(_resolve_git_dir()).resolve() == (unrelated / ".git").resolve()


# --- Unit-level: the raw-record parser's own format-surprise contract ---


def test_parse_raw_records_well_formed_vs_malformed_raises_dedicated_error():
    sha_a = "a" * 40
    sha_b = "b" * 40
    good = f":100644 100644 {sha_a} {sha_b} M\x00path.txt\x00".encode()
    assert _parse_raw_records(good) == [("100644", "100644", sha_a, sha_b, "path.txt")]

    bad = b"not a valid record\x00path.txt\x00"
    with pytest.raises(DiffParseSurprise):
        _parse_raw_records(bad)


def test_parse_raw_records_rename_and_copy_status_both_raise_dedicated_error():
    sha_a = "a" * 40
    sha_b = "b" * 40

    good = f":100644 100644 {sha_a} {sha_b} M\x00normal.txt\x00".encode()
    assert _parse_raw_records(good) == [("100644", "100644", sha_a, sha_b, "normal.txt")]

    # True git rename-record byte shape: meta \0 oldpath \0 newpath \0 --
    # THREE NUL-delimited segments for one logical record. A lone one trips
    # the odd-parts-count check before the loop ever inspects the status
    # letter (both raise DiffParseSurprise, but for different reasons).
    lone_rename = f":100644 100644 {sha_a} {sha_b} R100\x00old.txt\x00new.txt\x00".encode()
    with pytest.raises(DiffParseSurprise):
        _parse_raw_records(lone_rename)

    # A copy-status meta landing correctly in a (meta, path) pairing slot,
    # after a well-formed record -- an EVEN total parts count, so the
    # odd-parts-count check does NOT fire here; the dedicated R/C status
    # check inside the loop must be what catches it.
    copy_among_others = good + f":100644 100644 {sha_a} {sha_b} C100\x00copied.txt\x00".encode()
    with pytest.raises(DiffParseSurprise):
        _parse_raw_records(copy_among_others)


# --- What the repository SAYS an object contains. Round 4 pinned WHICH
# repository is read; git's object-REPLACEMENT mechanism rewrites the answer
# `cat-file`/`diff-tree` give for a given object id, from refs (`refs/replace/*`,
# or wherever `GIT_REPLACE_REF_BASE` points) that `git push` does not send. A
# gate that honours replacement reports on content the remote never receives. ---


def _git_out(args: list[str], repo: Path, extra_env: dict[str, str] | None = None) -> str:
    """Stdout of a real git call, stripped -- used to prove an attack setup
    is LIVE before asserting the gate defeats it."""
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env=_hermetic_env(**(extra_env or {})),
    ).stdout.strip()


def _write_loose_blob(repo: Path, text: str) -> str:
    """Write a blob into ``repo``'s object database and return its id."""
    return subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=repo,
        input=text,
        check=True,
        capture_output=True,
        text=True,
        env=_hermetic_env(),
    ).stdout.strip()


def _repo_with_planted_file(root: Path, name: str, *, planted: bool) -> Path:
    """A two-commit repo: ``main`` clean, ``feature`` adding one file that
    either carries the planted term or does not."""
    repo = _init_repo(root / name, {"base.txt": "base\n"})
    _run_git(["checkout", "-q", "-b", "feature"], repo)
    body = "ZZPLANTED9 secret\n" if planted else "nothing sensitive\n"
    (repo / "added.txt").write_text(body)
    _run_git(["add", "added.txt"], repo)
    _run_git(["commit", "-q", "-m", "feature"], repo)
    return repo


def test_blob_replace_ref_cannot_make_a_planted_blob_read_clean(tmp_path):
    """``git replace <planted blob> <clean blob>``: ``diff-tree --raw`` still
    lists the REAL planted object id, so the listing step sees nothing odd,
    while ``cat-file blob <that id>`` hands back the replacement's clean
    text. The gate scanned the fiction and exited 0; ``refs/replace/*`` is
    not pushed, so the planted blob is what the remote received."""
    repo = _repo_with_planted_file(tmp_path, "repo", planted=True)
    deny_file = tmp_path / "deny.txt"
    deny_file.write_text("ZZPLANTED9\n")

    # Positive control A -- the same repository, before the replace ref
    # exists: the gate fires and names the path.
    before = _run_denylist(repo, deny_file, "main")
    assert before.returncode == 1, before.stdout + before.stderr
    assert "added.txt:1: " in before.stdout, before.stdout

    planted_sha = _git_out(["rev-parse", "feature:added.txt"], repo)
    _run_git(["replace", planted_sha, _write_loose_blob(repo, "totally clean\n")], repo)

    # The attack must be LIVE, or this test passes on vulnerable code: git
    # itself now returns the clean text for the planted id ...
    assert _git_out(["cat-file", "blob", planted_sha], repo) == "totally clean"
    # ... and the listing still carries the planted id, i.e. nothing in the
    # listing step could have noticed.
    raw = _git_out(["diff-tree", "-r", "--raw", "--no-abbrev", "main", "HEAD"], repo)
    assert planted_sha in raw, raw

    after = _run_denylist(repo, deny_file, "main")
    assert after.returncode == 1, (
        f"replace ref hid the planted blob: exit {after.returncode}: {after.stdout}{after.stderr}"
    )
    assert "added.txt:1: " in after.stdout, after.stdout

    # Positive control B -- a clean repository carrying an equally real
    # replace ref still exits 0, so the exit 1 above is a hit and not a gate
    # that fires on the mere presence of ``refs/replace/*``.
    clean_repo = _repo_with_planted_file(tmp_path, "clean_repo", planted=False)
    clean_sha = _git_out(["rev-parse", "feature:added.txt"], clean_repo)
    _run_git(
        ["replace", clean_sha, _write_loose_blob(clean_repo, "also clean\n")],
        clean_repo,
    )
    clean = _run_denylist(clean_repo, deny_file, "main")
    assert clean.returncode == 0, f"exit {clean.returncode}: {clean.stdout}{clean.stderr}"


def test_replace_ref_in_a_relocated_namespace_cannot_hide_a_planted_blob(tmp_path):
    """The same mechanism with the ref namespace moved: ``GIT_REPLACE_REF_BASE``
    points git at ``refs/stealth/`` and ``refs/replace/`` is empty. Excluding
    the default namespace by name would guard an INSTANCE; the flag guards
    the class."""
    repo = _repo_with_planted_file(tmp_path, "repo", planted=True)
    deny_file = tmp_path / "deny.txt"
    deny_file.write_text("ZZPLANTED9\n")
    stealth = {"GIT_REPLACE_REF_BASE": "refs/stealth/"}

    planted_sha = _git_out(["rev-parse", "feature:added.txt"], repo)
    _run_git(
        ["update-ref", f"refs/stealth/{planted_sha}", _write_loose_blob(repo, "totally clean\n")],
        repo,
    )

    # Attack live, and demonstrably NOT reachable through the default
    # namespace: nothing at all sits under ``refs/replace/``.
    assert _git_out(["for-each-ref", "--format=%(refname)", "refs/replace/"], repo) == ""
    assert _git_out(["cat-file", "blob", planted_sha], repo, stealth) == "totally clean"

    result = _run_denylist_from(repo, deny_file, "main", stealth)
    assert result.returncode == 1, (
        f"relocated replace namespace hid the planted blob: exit {result.returncode}: "
        f"{result.stdout}{result.stderr}"
    )
    assert "added.txt:1: " in result.stdout, result.stdout

    # Positive control, under the very same crossed environment: a clean
    # repository still exits 0, so the gate is discriminating and not simply
    # failing whenever ``GIT_REPLACE_REF_BASE`` is set.
    clean_repo = _repo_with_planted_file(tmp_path, "clean_repo", planted=False)
    clean = _run_denylist_from(clean_repo, deny_file, "main", stealth)
    assert clean.returncode == 0, f"exit {clean.returncode}: {clean.stdout}{clean.stderr}"


def test_commit_replace_of_head_cannot_empty_the_listing(tmp_path):
    """``git replace <HEAD commit> <clean commit>``: not one blob rewritten
    but the whole commit, so ``diff-tree`` lists NOTHING and there is no
    object id left for a blob-level check to be suspicious about. Same
    class, one level up the object graph."""
    repo = _repo_with_planted_file(tmp_path, "repo", planted=True)
    deny_file = tmp_path / "deny.txt"
    deny_file.write_text("ZZPLANTED9\n")

    before = _run_denylist(repo, deny_file, "main")
    assert before.returncode == 1, before.stdout + before.stderr
    assert "added.txt:1: " in before.stdout, before.stdout

    head = _git_out(["rev-parse", "HEAD"], repo)
    # The decoy must be a CHILD of main carrying main's own tree, not main
    # itself: replacing HEAD with its own ancestor leaves `merge-base` with
    # no answer at all, and the gate then exits 2 -- loud, and therefore not
    # this bug. An `--allow-empty` commit off main is the shape that keeps
    # `merge-base` answering normally while emptying the listing, i.e. the
    # shape that exits 0.
    _run_git(["checkout", "-q", "-b", "decoy", "main"], repo)
    _run_git(["commit", "-q", "--allow-empty", "-m", "decoy: main's tree, child of main"], repo)
    decoy = _git_out(["rev-parse", "HEAD"], repo)
    _run_git(["checkout", "-q", "feature"], repo)
    _run_git(["replace", head, decoy], repo)

    # Attack live -- and this is the part a blob-level guard could not have
    # seen: under replacement the listing of main..HEAD is EMPTY, so there is
    # no object id left to be suspicious of and nothing at all to scan.
    assert _git_out(["diff-tree", "-r", "--raw", "--no-abbrev", "main", "HEAD"], repo) == ""
    # `merge-base` still answers normally, so the empty listing is the whole
    # of the attack: no error anywhere for the gate to trip over.
    assert _git_out(["merge-base", "main", "HEAD"], repo) == _git_out(["rev-parse", "main"], repo)
    # Only the READ is rewritten: two genuinely different commit objects,
    # and it is the planted one that a push sends.
    assert head != decoy

    after = _run_denylist(repo, deny_file, "main")
    assert after.returncode == 1, (
        f"commit replace emptied the listing: exit {after.returncode}: {after.stdout}{after.stderr}"
    )
    assert "added.txt:1: " in after.stdout, after.stdout

    # Positive control: an untouched clean repository still exits 0.
    clean_repo = _repo_with_planted_file(tmp_path, "clean_repo", planted=False)
    clean = _run_denylist(clean_repo, deny_file, "main")
    assert clean.returncode == 0, f"exit {clean.returncode}: {clean.stdout}{clean.stderr}"


# --- A caller-supplied value is DATA. ``base`` is the one value a
# caller hands this gate, and it went into a git argv unterminated. ---


@pytest.mark.parametrize("injected", ["--octopus", "--independent"])
def test_base_value_cannot_become_a_git_option(injected, tmp_path):
    """``check_denylist.py -- --octopus`` put ``--octopus`` into ``base``,
    which reached ``git merge-base --octopus HEAD``. That prints HEAD, whose
    diff against HEAD is empty -- exit 0 on a planted branch. The control
    uses the IDENTICAL argv shape with an ordinary base value, so what the
    two runs differ in is the value alone."""
    repo = _repo_with_planted_file(tmp_path, "repo", planted=True)
    deny_file = tmp_path / "deny.txt"
    deny_file.write_text("ZZPLANTED9\n")
    env = _hermetic_env(PGMCP_DENYLIST=str(deny_file))

    result = _run_script(["--", injected], env, cwd=repo)
    assert result.returncode == 2, (
        f"{injected} as base: exit {result.returncode}: {result.stdout}{result.stderr}"
    )
    # Exit 2 must come from git refusing the VALUE, not from argparse
    # refusing the flag (argparse also exits 2, and would do so even on the
    # vulnerable code) -- git's own message names it as an object.
    assert f"Not a valid object name {injected}" in result.stderr, result.stderr
    assert result.stdout == "", result.stdout

    control = _run_script(["--", "main"], env, cwd=repo)
    assert control.returncode == 1, f"exit {control.returncode}: {control.stdout}{control.stderr}"
    assert "added.txt:1: " in control.stdout, control.stdout


# --- M1: the same helper contract `_blob_lines` carries, for the other two
# git calls. End-to-end tests cannot catch a missing ``--git-dir`` on ONE
# call, because the gate's subprocesses share its cwd and environment, so
# discovery there agrees with the resolved identity by construction. ---


def _stand_in(repo: Path, monkeypatch) -> None:
    """Make ``repo`` the repository this PROCESS would discover, with no
    inherited git environment of any kind."""
    monkeypatch.chdir(repo)
    for var in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_NO_REPLACE_OBJECTS",
        "GIT_REPLACE_REF_BASE",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def test_merge_base_reads_the_repository_it_is_given_not_the_current_directory(
    tmp_path, monkeypatch
):
    named = _build_subdir_repo(tmp_path / "named", planted=True)
    unrelated = _init_repo(tmp_path / "unrelated", {"base.txt": "base\n"})
    expected = _git_out(["merge-base", "main", "feature"], named)

    _stand_in(unrelated, monkeypatch)
    assert _merge_base("main", "HEAD", str(named / ".git")) == expected

    # Positive control for the discrimination: the repository we are
    # standing in has no ``feature`` at all, so resolving against it fails
    # the push instead of quietly answering with the wrong repo's history.
    with pytest.raises(SystemExit) as unrelated_exc:
        _merge_base("feature", "HEAD", str(unrelated / ".git"))
    assert unrelated_exc.value.code == 2

    # And the identity resolved from HERE is the unrelated repo, so the two
    # really do differ in this process.
    assert Path(_resolve_git_dir()).resolve() == (unrelated / ".git").resolve()


def test_raw_diff_reads_the_repository_it_is_given_not_the_current_directory(tmp_path, monkeypatch):
    named = _build_subdir_repo(tmp_path / "named", planted=True)
    unrelated = _init_repo(tmp_path / "unrelated", {"base.txt": "base\n"})
    merge_base = _git_out(["merge-base", "main", "feature"], named)

    _stand_in(unrelated, monkeypatch)
    paths = {
        rec[4] for rec in _parse_raw_records(_raw_diff(merge_base, "HEAD", str(named / ".git")))
    }
    assert "planted_root.txt" in paths, paths
    assert "sibling_dir/planted_sibling.txt" in paths, paths

    # Positive control: that merge-base commit does not exist in the
    # repository we are standing in, so reading it there is exit 2 -- the
    # success above can only have come from the git dir that was passed in.
    with pytest.raises(SystemExit) as unrelated_exc:
        _raw_diff(merge_base, "HEAD", str(unrelated / ".git"))
    assert unrelated_exc.value.code == 2

    assert Path(_resolve_git_dir()).resolve() == (unrelated / ".git").resolve()


def test_raw_diff_treats_a_leading_dash_revision_as_data_not_an_option(tmp_path, monkeypatch):
    """The listing call's ``--end-of-options``, falsified rather than assumed.

    Most leading-dash values make git error out either way, which proves
    nothing about the terminator. ``--root`` does not: unterminated,
    ``git diff-tree ... --root HEAD`` SUCCEEDS -- exit 0, with a commit id
    prepended to the record stream -- so the helper returns bytes for a
    value that was supposed to be a revision. That returning-vs-raising
    difference is the only thing about this flag a test can fail on, and it
    is the shape the whole finding is about: a value that became an option.
    """
    repo = _build_subdir_repo(tmp_path / "repo", planted=True)
    git_dir = _git_out(["rev-parse", "--absolute-git-dir"], repo)
    merge_base = _git_out(["merge-base", "main", "feature"], repo)
    _stand_in(repo, monkeypatch)

    with pytest.raises(SystemExit) as exc:
        _raw_diff("--root", "HEAD", git_dir)
    assert exc.value.code == 2

    # The SECOND revision argument is caller-controlled too now -- it is
    # the local sha a pre-push hook hands in -- so it gets the same check.
    with pytest.raises(SystemExit) as rev_exc:
        _raw_diff(merge_base, "--root", git_dir)
    assert rev_exc.value.code == 2

    # Positive control in the same test: an ordinary revision on the very
    # same repository still lists the planted paths, so the rejections
    # above are about the leading dash and not a helper that refuses
    # everything.
    paths = {rec[4] for rec in _parse_raw_records(_raw_diff(merge_base, "HEAD", git_dir))}
    assert "planted_root.txt" in paths, paths


# --- M3: an exit code says whether something was found; it does not say what
# was looked at. Inside a submodule, or under a stale ``GIT_DIR``, an exit 0
# is truthful about a repository the reader may not have had in mind. ---


def _scanning_lines(stderr: str) -> list[str]:
    return [ln for ln in stderr.splitlines() if ln.startswith("check_denylist: scanning ")]


def test_m3_diff_mode_names_the_scanned_git_dir_once_per_run(tmp_path):
    planted = _build_subdir_repo(tmp_path / "planted", planted=True)
    clean_repo = _build_subdir_repo(tmp_path / "clean", planted=False)
    deny_file = tmp_path / "deny.txt"
    deny_file.write_text("ZZPLANTED9\n")

    planted_dir = _git_out(["rev-parse", "--absolute-git-dir"], planted / "cwd_dir")
    clean_dir = _git_out(["rev-parse", "--absolute-git-dir"], clean_repo / "cwd_dir")
    assert planted_dir != clean_dir

    hit = _run_denylist_from(planted / "cwd_dir", deny_file, "main")
    assert hit.returncode == 1, hit.stdout + hit.stderr
    assert "planted_root.txt:1: " in hit.stdout, hit.stdout
    assert _scanning_lines(hit.stderr) == [f"check_denylist: scanning {planted_dir} HEAD"], (
        hit.stderr
    )

    # Same one line on a clean run -- it is per RUN, not per hit -- and it
    # names the OTHER repository, so the line tracks what was actually
    # scanned rather than being a constant that happens to look right once.
    clean = _run_denylist_from(clean_repo / "cwd_dir", deny_file, "main")
    assert clean.returncode == 0, f"exit {clean.returncode}: {clean.stdout}{clean.stderr}"
    assert clean.stdout == "", clean.stdout
    assert _scanning_lines(clean.stderr) == [f"check_denylist: scanning {clean_dir} HEAD"], (
        clean.stderr
    )
    assert planted_dir not in clean.stderr, clean.stderr

    # ``--files`` resolves no repository at all, so it announces none --
    # and still reports the hit, i.e. stdout and the exit code are untouched
    # by this whole feature.
    target = tmp_path / "scanned.txt"
    target.write_text("ZZPLANTED9 in a plain file\n")
    files_mode = _run_script(
        ["--files", str(target)],
        _hermetic_env(PGMCP_DENYLIST=str(deny_file)),
        cwd=planted / "cwd_dir",
    )
    assert files_mode.returncode == 1, files_mode.stdout + files_mode.stderr
    assert "zzplanted9" in files_mode.stdout
    assert _scanning_lines(files_mode.stderr) == [], files_mode.stderr


def test_m3_from_inside_a_submodule_the_announced_dir_is_the_submodules_own(tmp_path):
    """The scenario M3 exists for: an exit 0 read as "the superproject is
    clean". The announced path is the submodule's own git directory, which
    is visibly not the superproject's."""
    sub_origin = _init_repo(tmp_path / "sub_origin", {"f.txt": "sub content\n"})
    super_repo = _init_repo(tmp_path / "super", {"base.txt": "base\n"})
    _run_git(["checkout", "-q", "-b", "feature"], super_repo)
    subprocess.run(
        [
            "git",
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(sub_origin),
            "sub",
        ],
        cwd=super_repo,
        check=True,
        capture_output=True,
        text=True,
        env=_hermetic_env(),
    )
    _run_git(["commit", "-q", "-m", "add submodule"], super_repo)

    sub = super_repo / "sub"
    deny_file = tmp_path / "deny.txt"
    deny_file.write_text("ZZPLANTED9\n")

    from_super = _run_denylist_from(super_repo, deny_file, "main")
    assert from_super.returncode == 0, (
        f"exit {from_super.returncode}: {from_super.stdout}{from_super.stderr}"
    )
    super_dir = _git_out(["rev-parse", "--absolute-git-dir"], super_repo)
    assert _scanning_lines(from_super.stderr) == [f"check_denylist: scanning {super_dir} HEAD"], (
        from_super.stderr
    )

    sub_dir = _git_out(["rev-parse", "--absolute-git-dir"], sub)
    assert sub_dir != super_dir
    from_sub = _run_denylist_from(sub, deny_file, "main")
    assert _scanning_lines(from_sub.stderr) == [f"check_denylist: scanning {sub_dir} HEAD"], (
        from_sub.stderr
    )


@pytest.mark.parametrize("via", ["env-graft-file", "info-grafts"])
def test_grafts_cannot_empty_the_listing(tmp_path, via):
    """Grafts rewrite commit parentage for reads only and are never pushed;
    ``--no-replace-objects`` does not disable them, and ``GIT_GRAFT_FILE``
    has no argv form at all. Grafting both HEAD and ``main`` onto a twin
    commit that carries HEAD's tree gives an empty listing. The gate's git
    calls must not inherit the lever."""
    repo = _repo_with_planted_file(tmp_path, "repo", planted=True)
    deny_file = tmp_path / "deny.txt"
    deny_file.write_text("ZZPLANTED9\n")

    before = _run_denylist(repo, deny_file, "main")
    assert before.returncode == 1, before.stdout + before.stderr
    assert "added.txt:1: " in before.stdout, before.stdout

    head = _git_out(["rev-parse", "HEAD"], repo)
    main = _git_out(["rev-parse", "main"], repo)
    twin = _git_out(["commit-tree", "-p", main, "-m", "twin", f"{head}^{{tree}}"], repo)
    grafts = f"{head} {twin}\n{main} {twin}\n"
    extra: dict[str, str] = {}
    if via == "env-graft-file":
        graft_file = tmp_path / "grafts.txt"
        graft_file.write_text(grafts)
        extra["GIT_GRAFT_FILE"] = str(graft_file)
    else:
        info = repo / ".git" / "info"
        info.mkdir(exist_ok=True)
        (info / "grafts").write_text(grafts)

    # the attack is live: under the graft git itself sees an empty listing
    mb = _git_out(["merge-base", "main", "HEAD"], repo, extra_env=extra)
    listing = _git_out(["diff-tree", "-r", "--raw", mb, "HEAD"], repo, extra_env=extra)
    assert listing == "", listing

    env = _hermetic_env(PGMCP_DENYLIST=str(deny_file), **extra)
    after = _run_script(["main"], env, cwd=repo)
    assert after.returncode == 1, after.stdout + after.stderr
    assert "added.txt:1: " in after.stdout, after.stdout


# --- The ref that is LEAVING, not the ref you are standing on. As a
# pre-push hook the gate always diffed `<base>..HEAD`, so `git push origin
# feat` run while `main` was checked out scanned `main`, found nothing new
# and exited 0 while the planted `feat` landed on the remote. Driven here
# through a REAL `git push` to a local bare remote, with the gate installed
# as that repository's own `.git/hooks/pre-push`, because the bug lives
# entirely in what git hands the hook -- no fixture can encode it. ---


def _install_pre_push_hook(repo: Path, base: str) -> None:
    """Install the gate as ``repo``'s own pre-push hook.

    A two-line ``/bin/sh`` wrapper, exactly as a user would write it:
    ``exec`` the suite's interpreter on the real script with a base ref.
    ``exec`` matters -- it hands the hook's own stdin (the ref lines git
    writes) and exit status straight to the gate. ``PGMCP_DENYLIST`` is
    NOT baked in here; it arrives through the environment of the ``git
    push`` that runs the hook, which is how the real hook is configured.
    """
    hook = repo / ".git" / "hooks" / "pre-push"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{SCRIPT_PATH}" "{base}"\n')
    hook.chmod(0o755)


def _push(repo: Path, deny_file: Path, refspec: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "push", "origin", refspec],
        cwd=repo,
        capture_output=True,
        text=True,
        env=_hermetic_env(PGMCP_DENYLIST=str(deny_file)),
    )


def _remote_has(repo: Path, branch: str) -> bool:
    return bool(_git_out(["ls-remote", "--heads", "origin", branch], repo).strip())


def test_pre_push_scans_the_pushed_ref_not_head_over_a_real_push(tmp_path):
    """Real execution, both arms in one test.

    Planted branch, pushed while ``main`` is checked out: the push must
    FAIL and the ref must be ABSENT on the remote -- "exit 1" alone would
    not prove the ref never left. Clean branch, pushed the same way in the
    same repository: the push must SUCCEED and the ref must be PRESENT --
    without that control a gate that rejects every push reads as a pass.

    Seen to fail on the pre-fix code: the planted push exited 0 and
    ``refs/heads/planted`` landed on the remote, the gate's stderr reading
    ``scanning <git dir>`` with no ref because it had scanned ``main``.
    """
    repo = _init_repo(tmp_path / "repo", {"base.txt": "base\n"})
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(remote)],
        check=True,
        capture_output=True,
        env=_hermetic_env(),
    )
    _run_git(["remote", "add", "origin", str(remote)], repo)
    _run_git(["push", "-q", "origin", "main"], repo)

    _run_git(["checkout", "-q", "-b", "planted"], repo)
    (repo / "secret.txt").write_text("notes on ZZPLANTED9 here\n")
    _run_git(["add", "secret.txt"], repo)
    _run_git(["commit", "-q", "-m", "planted"], repo)

    _run_git(["checkout", "-q", "-b", "cleanbr", "main"], repo)
    (repo / "ok.txt").write_text("nothing sensitive here\n")
    _run_git(["add", "ok.txt"], repo)
    _run_git(["commit", "-q", "-m", "clean"], repo)

    # HEAD is `main` for both pushes: neither branch is the one checked out.
    _run_git(["checkout", "-q", "main"], repo)
    assert _git_out(["rev-parse", "--abbrev-ref", "HEAD"], repo) == "main"

    deny_file = tmp_path / "deny.txt"
    deny_file.write_text("ZZPLANTED9\n")
    _install_pre_push_hook(repo, "main")

    blocked = _push(repo, deny_file, "planted")
    assert blocked.returncode != 0, blocked.stdout + blocked.stderr
    assert "secret.txt:1: zzplanted9" in blocked.stdout + blocked.stderr, (
        blocked.stdout + blocked.stderr
    )
    planted_sha = _git_out(["rev-parse", "planted"], repo)
    assert f"refs/heads/planted {planted_sha}" in blocked.stderr, blocked.stderr
    assert not _remote_has(repo, "planted"), "the planted ref reached the remote"

    # Positive control, same repo, same hook, same non-HEAD shape.
    allowed = _push(repo, deny_file, "cleanbr")
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr
    assert _remote_has(repo, "cleanbr"), "the clean ref did not reach the remote"


def test_pre_push_deletion_is_skipped_and_a_malformed_line_is_exit_2(tmp_path):
    """The stdin shapes that are not a ref carrying content.

    A deletion (all-zeros local sha) sends nothing, so it is skipped and
    the run is a pass. Anything else the gate cannot parse -- wrong field
    count, or a local sha that is not a 40/64-hex object id -- is exit 2,
    never a skip: a skipped line is a ref pushed unscanned. The positive
    control is in the same test: the identical stdin shape carrying the
    planted branch's real sha exits 1 and names the file.
    """
    repo = _init_repo(tmp_path / "repo", {"base.txt": "base\n"})
    _run_git(["checkout", "-q", "-b", "planted"], repo)
    (repo / "secret.txt").write_text("notes on ZZPLANTED9 here\n")
    _run_git(["add", "secret.txt"], repo)
    _run_git(["commit", "-q", "-m", "planted"], repo)
    _run_git(["checkout", "-q", "main"], repo)
    planted_sha = _git_out(["rev-parse", "planted"], repo)
    zeros = "0" * 40

    deny_file = tmp_path / "deny.txt"
    deny_file.write_text("ZZPLANTED9\n")
    env = _hermetic_env(PGMCP_DENYLIST=str(deny_file))

    hit = _run_script(
        ["main"],
        env,
        cwd=repo,
        stdin=f"refs/heads/planted {planted_sha} refs/heads/planted {zeros}\n",
    )
    assert hit.returncode == 1, hit.stdout + hit.stderr
    assert "secret.txt:1: zzplanted9" in hit.stdout, hit.stdout

    deletion = _run_script(
        ["main"], env, cwd=repo, stdin=f"(delete) {zeros} refs/heads/planted {planted_sha}\n"
    )
    assert deletion.returncode == 0, deletion.stdout + deletion.stderr
    assert "every pushed ref is a deletion" in deletion.stderr, deletion.stderr
    assert _scanning_lines(deletion.stderr) == [], deletion.stderr

    short = _run_script(["main"], env, cwd=repo, stdin="refs/heads/planted\n")
    assert short.returncode == 2, short.stdout + short.stderr
    assert "malformed pre-push line" in short.stderr, short.stderr

    not_a_sha = _run_script(
        ["main"],
        env,
        cwd=repo,
        stdin=f"refs/heads/planted --output=/tmp/x refs/heads/planted {zeros}\n",
    )
    assert not_a_sha.returncode == 2, not_a_sha.stdout + not_a_sha.stderr
    assert "64-hex object id" in not_a_sha.stderr, not_a_sha.stderr


def test_no_stdin_still_scans_head(tmp_path):
    """The unchanged path. With no pre-push lines on stdin the gate scans
    ``HEAD``, says so on the "scanning" line, and both outcomes are
    asserted in the same test so "exit 0" cannot be a gate that never
    fires."""
    repo = _init_repo(tmp_path / "repo", {"base.txt": "base\n"})
    _run_git(["checkout", "-q", "-b", "feature"], repo)
    (repo / "secret.txt").write_text("notes on ZZPLANTED9 here\n")
    _run_git(["add", "secret.txt"], repo)
    _run_git(["commit", "-q", "-m", "planted on the checked-out branch"], repo)

    deny_file = tmp_path / "deny.txt"
    deny_file.write_text("ZZPLANTED9\n")
    git_dir = _git_out(["rev-parse", "--absolute-git-dir"], repo)

    on_head = _run_denylist(repo, deny_file, "main")
    assert on_head.returncode == 1, on_head.stdout + on_head.stderr
    assert "secret.txt:1: zzplanted9" in on_head.stdout, on_head.stdout
    assert _scanning_lines(on_head.stderr) == [f"check_denylist: scanning {git_dir} HEAD"], (
        on_head.stderr
    )

    _run_git(["checkout", "-q", "main"], repo)
    clean = _run_denylist(repo, deny_file, "main")
    assert clean.returncode == 0, clean.stdout + clean.stderr
    assert _scanning_lines(clean.stderr) == [f"check_denylist: scanning {git_dir} HEAD"], (
        clean.stderr
    )
