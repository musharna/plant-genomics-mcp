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

# Arabidopsis AGI locus: AT<chr>G<5 digits>, chr ∈ 1-5 / C (chloroplast) / M
# (mitochondrion), optional ``.N`` transcript suffix. Used by the
# Arabidopsis-only backends (AraGWAS, 1001 Genomes) where a malformed AGI
# otherwise reaches an upstream that answers HTTP 500 (misread as an outage)
# instead of a clean not-found — see the 2026-07-21 audit (M6).
AGI_RE: re.Pattern[str] = re.compile(r"^AT[1-5CM]G\d{5}(\.\d+)?\Z", re.IGNORECASE)

# JASPAR matrix (profile) identifier: a 2-4 letter collection prefix, digits,
# and an optional ``.N`` release version — e.g. ``MA0570.1`` (CORE), ``PB0001.1``
# (PBM), ``UN0123.1`` (unvalidated). Templated straight into the API path, so it
# gets a dedicated shape check rather than the permissive ``LOCUS_RE``.
JASPAR_MATRIX_RE: re.Pattern[str] = re.compile(r"^[A-Za-z]{2,4}\d{3,7}(\.\d+)?\Z")

# Characters that would break out of the URL path/query segment a value is
# templated into (path traversal, extra query params, fragment). Applied to
# the variant ``region``/``allele`` strings that VEP splices into its path —
# those aren't loci, so they don't fit ``LOCUS_RE``, but they must still be
# metachar-free (audit L2).
_PATH_METACHARS = re.compile(r"[/\s?#&%]")


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
    if not any(c.isalnum() for c in locus):
        # ``LOCUS_RE`` alone accepts all-punctuation values such as ``"."`` /
        # ``".."`` which, templated into a URL path, collapse via RFC-3986
        # dot-segment removal to a *different* endpoint than intended. A real
        # locus always carries an identifier character, so require one.
        raise NotFoundError(f"{backend}: invalid locus {locus!r} (needs an alphanumeric character)")


def assert_valid_agi(locus: str, *, backend: str) -> None:
    """Raise ``NotFoundError`` if ``locus`` is not a well-formed Arabidopsis AGI.

    Stricter than :func:`assert_valid_locus` — for the Arabidopsis-only backends
    where a typo'd AGI (wrong length/prefix) hits an upstream 500 that the retry
    layer surfaces as ``UpstreamUnavailableError`` ("service down"). Rejecting
    malformed AGIs up front turns that common case into a clear not-found.
    """
    if not AGI_RE.match(locus):
        raise NotFoundError(
            f"{backend}: {locus!r} is not a valid Arabidopsis AGI locus (e.g. AT1G01060)"
        )


def assert_valid_jaspar_matrix_id(matrix_id: str, *, backend: str) -> None:
    """Raise ``NotFoundError`` if ``matrix_id`` is not a well-formed JASPAR profile id.

    JASPAR splices the id directly into its REST path, and an unknown-but-clean
    id already yields a clean 404 — this rejects the *malformed* shapes (path
    separators, empty strings) before they reach the wire.
    """
    if not JASPAR_MATRIX_RE.match(matrix_id):
        raise NotFoundError(f"{backend}: invalid matrix id {matrix_id!r} (expected e.g. MA0570.1)")


def assert_no_path_metachars(value: str, *, field: str, backend: str) -> None:
    """Raise ``NotFoundError`` if ``value`` contains URL path/query metacharacters.

    For non-locus caller inputs (e.g. VEP ``region``/``allele``) that are
    templated into a URL path segment and so must not smuggle a ``/``, space,
    ``?``, ``#``, ``&`` or ``%``.
    """
    if not value or _PATH_METACHARS.search(value):
        raise NotFoundError(
            f"{backend}: invalid {field} {value!r} (contains a path/query metacharacter)"
        )
