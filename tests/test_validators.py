"""Tests for the shared locus-identifier validator (Wave B6).

The regex lives in ``validators.py`` so every backend boundary that
splices a locus into a URL path, query parameter, or string-formatted
body (Ensembl, KEGG, Gramene, Phytozome) can reject malformed input
identically. ``assert_valid_locus`` raises ``NotFoundError`` so the
existing MCP error contract is preserved.
"""

from __future__ import annotations

import pytest

from plant_genomics_mcp import validators
from plant_genomics_mcp.errors import NotFoundError


@pytest.mark.parametrize(
    "locus",
    [
        "AT1G01010",  # Arabidopsis
        "Os01g0100100",  # rice
        "Glyma.01G000100",  # soybean
        "Sobic.001G000100",  # sorghum
        "Zm00001d027231",  # maize
        "GRMZM2G083841",  # maize legacy
        "AC149818.2",  # accession with version dot
        "id_with_underscore",
        "id-with-hyphen",
    ],
)
def test_locus_re_accepts_real_world_identifiers(locus: str) -> None:
    assert validators.LOCUS_RE.match(locus) is not None, locus


@pytest.mark.parametrize(
    "locus",
    [
        "",  # empty string — must not silently pass
        "AT1G01010<x>",  # XML / HTML injection
        "AT1G01010;DROP",  # statement-injection shape
        "AT1G01010 OR 1=1",  # whitespace inside
        "AT1G01010\n",  # trailing newline
        "AT1G01010/extra",  # path-traversal flavor
        "AT1G01010%2F",  # url-encoded slash
        "AT1G 01010",  # internal space
        "../etc/passwd",  # classic path traversal
    ],
)
def test_locus_re_rejects_malformed_identifiers(locus: str) -> None:
    assert validators.LOCUS_RE.match(locus) is None, locus


def test_assert_valid_locus_passes_through_for_clean_input() -> None:
    # No exception. Helper returns None — its side effect is the raise.
    assert validators.assert_valid_locus("AT1G01010", backend="ensembl") is None


def test_assert_valid_locus_raises_notfound_with_backend_label() -> None:
    """Error message must name the backend so the caller learns which
    boundary rejected the input — same shape as phytozome's existing
    ``invalid locus`` raise so log greps stay consistent.
    """
    with pytest.raises(NotFoundError, match="invalid locus"):
        validators.assert_valid_locus("AT1G01010<x>", backend="ensembl")


def test_assert_valid_locus_includes_pattern_in_message() -> None:
    """Surfacing the regex helps the caller fix the input without
    digging into the source."""
    with pytest.raises(NotFoundError) as exc_info:
        validators.assert_valid_locus("bad value", backend="kegg")
    assert "kegg" in str(exc_info.value).lower()
    assert validators.LOCUS_RE.pattern in str(exc_info.value)
