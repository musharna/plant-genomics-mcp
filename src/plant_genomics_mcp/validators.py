"""Shared input validators for backend boundaries.

Every backend that splices a caller-supplied locus identifier into a
URL path, query parameter, or string-formatted body must reject
malformed input before the HTTP call. ``httpx`` percent-encodes paths
defensively, but the audit (2026-05-23 I-4) called out the
inconsistency: Phytozome validates, Ensembl / KEGG / Gramene don't.
This module centralises the regex so the boundary check is identical
across backends.

The character class — ``[A-Za-z0-9._-]`` — matches the real shape of
plant locus IDs (``AT1G01010``, ``Os01g0100100``, ``Glyma.01G000100``,
``Zm00001d027231``, ``AC149818.2``). Anything outside that set means
the caller smuggled in a slash, space, NUL, or markup byte we don't
want to template into upstream traffic.
"""

from __future__ import annotations

import re

from plant_genomics_mcp.errors import NotFoundError

# Anchor with ``\Z`` rather than ``$`` — Python's ``$`` matches before a
# trailing newline at end-of-string, so ``$`` would silently accept
# ``"AT1G01010\n"``. ``\Z`` is true end-of-string and rejects the newline.
LOCUS_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9._-]+\Z")


def assert_valid_locus(locus: str, *, backend: str) -> None:
    """Raise ``NotFoundError`` if ``locus`` doesn't match ``LOCUS_RE``.

    The error message names ``backend`` so the caller (and log greps)
    know which boundary rejected the input, and includes the pattern so
    the fix is obvious without source-diving. ``NotFoundError`` matches
    Phytozome's existing pre-flight rejection — clients already handle
    that shape and a uniform contract is what 1.0 promises.
    """
    if not LOCUS_RE.match(locus):
        raise NotFoundError(f"{backend}: invalid locus {locus!r} (must match {LOCUS_RE.pattern})")
