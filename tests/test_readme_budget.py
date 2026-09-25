"""The README has a line budget, and it is frozen here.

The failure mode this guards is not a bug in the server: it is that every
addition to `examples/` arrives with a paragraph of README to explain it,
and a reader who opens the repo for the first time pays for all of them.
`BUDGET` is the line count of `README.md` on `origin/main` (340) plus the
two lines the ARF dossier is allowed to add: the link line itself and the
blank line that separates it from the paragraph above, without which
Markdown glues the link onto that paragraph. Growing the README past it is
a deliberate act that has to edit this number.

Two controls, because a budget test is easy to write so that it cannot
fail:

- The path is resolved from this file (`parents[1]`), never from the
  process's working directory. Run from `tests/`, a cwd-relative
  `Path("README.md")` raises `FileNotFoundError` — which a `pytest`
  invocation from the repo root would never show — and, worse, a
  `README.md` that happened to exist in some other cwd would be measured
  instead and quietly pass.
- `test_the_budget_test_measures_the_real_readme` is the positive
  control: it asserts the file being counted is this repository's README
  (it carries the project title line), so "0 lines, under budget" cannot
  read as a pass.
"""

from __future__ import annotations

from pathlib import Path

README = Path(__file__).resolve().parents[1] / "README.md"

# `git show origin/main:README.md | wc -l` = 340, + 2 for
# examples/arf_family/PAGE.md: the link line and the blank line that must
# precede it (a link line glued to the previous paragraph renders as part
# of that paragraph). +1 (#124): the tool-matrix row for `entry_members`;
# a new tool's row is the one addition the matrix exists to carry. +1
# (#130): the row for `gene_tree_members`, on the same terms.
BUDGET = 344

TITLE_LINE = "# 🌱 plant-genomics-mcp"


def test_readme_does_not_grow() -> None:
    lines = README.read_text(encoding="utf-8").splitlines()
    assert len(lines) <= BUDGET, (
        f"{README} is {len(lines)} lines, budget {BUDGET}. "
        "Adding to the repo must not cost the front page: link the detail "
        "from examples/ instead, or raise BUDGET deliberately."
    )


def test_the_budget_test_measures_the_real_readme() -> None:
    """Positive control for the assertion above.

    A budget assertion passes on an empty file, a missing file measured
    as zero lines, or some other project's README picked up from the
    working directory. This pins the file identity and a non-trivial
    length, in the same suite as the ceiling.
    """
    assert README.is_file(), f"{README} does not exist"
    lines = README.read_text(encoding="utf-8").splitlines()
    assert lines[0] == TITLE_LINE, f"first line is {lines[0]!r}, not this repo's README"
    assert len(lines) > 300, f"only {len(lines)} lines — README is not the real one"
