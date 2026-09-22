"""The ARF example's authored text is published; this pins what it may say.

The miss this exists for: a docstring in `examples/arf_family/verify_genes.py`
explained, for the benefit of a reviewer, WHY the three loci were picked and
why the manifest has the columns it has. The explanation was true and
defensible, and it named the axis the sample was selected on — which is the
one thing the published page must not do, because the author's related work
on that axis is unpublished. Nothing in the suite read prose, so nothing
could fail on it.

Scope: every authored text file under `examples/arf_family/`. `raw/` is
excluded deliberately — those files are verbatim JSON responses captured
from live third-party APIs (Europe PMC abstracts arrive inside them), so
their wording is not the author's and rewriting them would falsify the
capture. `coverage.png` is binary. Everything else — the page, the issue
drafts, the gap log, the scripts — is text this repository wrote.

A term here is not forbidden forever; it is forbidden by default. Adding
one back is a deliberate act that has to edit `FORBIDDEN`.
"""

from __future__ import annotations

import re
from pathlib import Path

EXAMPLE_DIR = Path(__file__).resolve().parents[1] / "examples" / "arf_family"
RAW_DIR = EXAMPLE_DIR / "raw"

TEXT_SUFFIXES = {".md", ".py", ".R", ".tsv", ".jsonl", ".txt"}

# The vocabulary that names a grouping axis rather than an observation.
# `subfamily` is NOT here: `panther_subfamily` / `subfamily_id` are PANTHER's
# own field names, returned by the tool and checked against it, so banning
# the word would fire on the data instead of on the claim.
FORBIDDEN = re.compile(r"A/B/C|class\s+[ABC]\b|Finet|clade|phylogenetic", re.IGNORECASE)


def _authored_files() -> list[Path]:
    return sorted(
        p
        for p in EXAMPLE_DIR.rglob("*")
        if p.is_file() and RAW_DIR not in p.parents and p.suffix in TEXT_SUFFIXES
    )


def _hits(paths: list[Path]) -> list[str]:
    out: list[str] = []
    for p in paths:
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if FORBIDDEN.search(line):
                out.append(f"{p}:{i}: {line.strip()[:120]}")
    return out


def test_authored_example_text_names_no_selection_axis(tmp_path) -> None:
    """The assertion, and both controls it needs, in one test.

    A grep that matches nothing passes on an empty file list and on a
    regex that can never match, so: the file list is asserted to contain
    the page itself and to be non-trivially long, and the same scanner is
    run over planted text that it must flag.
    """
    files = _authored_files()
    assert EXAMPLE_DIR / "PAGE.md" in files, "the page itself is not being scanned"
    assert EXAMPLE_DIR / "verify_genes.py" in files, "the file that leaked is not being scanned"
    assert len(files) >= 10, f"only {len(files)} files scanned — the walk is not finding them"

    assert _hits(files) == [], "authored text names a selection axis"

    # Positive control, same scanner: planted text of each shape must fire.
    for planted_text in (
        "the three genes are one per phylogenetic subfamily\n",
        "genes.tsv has no A/B/C column\n",
        "AT1G19850 is a class A member\n",
        "(Finet et al. 2013)\n",
        "one locus per clade\n",
    ):
        planted = tmp_path / "planted.md"
        planted.write_text(planted_text)
        assert _hits([planted]), f"scanner did not fire on {planted_text!r}"

    # Negative control for the exclusion that makes the rule usable: the
    # PANTHER field names the manifest really carries must NOT fire.
    allowed = tmp_path / "allowed.md"
    allowed.write_text("panther_subfamily matches the live subfamily_id\n")
    assert _hits([allowed]) == [], "the PANTHER field names are being flagged"


def test_raw_captures_are_excluded_and_would_otherwise_fire() -> None:
    """The exclusion is load-bearing, so it is proved rather than assumed.

    If `raw/` were scanned this rule would fail on text the repository did
    not write — third-party abstracts returned by `locus_literature`. That
    is exactly why it is excluded, and a silent exclusion of a directory
    that happened to be clean would be indistinguishable from no
    exclusion at all.
    """
    captures = sorted(RAW_DIR.glob("*.json"))
    assert captures, f"{RAW_DIR} has no captures"
    assert _hits(captures), "raw captures no longer contain the vocabulary — re-check the premise"
    assert all(RAW_DIR not in p.parents for p in _authored_files())
