"""AraGWAS association client — Arabidopsis locus → GWAS hits.

AraGWAS (aragwas.1001genomes.org) is a curated catalog of genome-wide
association study hits across the Arabidopsis 1001 Genomes panel. Its
per-gene endpoint returns every significant SNP association overlapping a
locus, each carrying effect size (score), minor-allele frequency, the SNP's
predicted molecular effect, and the phenotype/study it came from.

Arabidopsis-only by construction — the panel is *A. thaliana* accessions — so
any other organism raises ``OrganismNotSupported``. A valid AGI locus with no
associations returns ``found=True`` with an empty list; an unknown locus makes
the upstream 500, surfaced as ``UpstreamUnavailableError`` (fail loud).

Endpoint (paginated via ``links.next``):
    https://aragwas.1001genomes.org/api/genes/{AGI}/associations/
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from plant_genomics_mcp import _http, cache, organisms, validators
from plant_genomics_mcp.errors import OrganismNotSupported

BASE_URL = "https://aragwas.1001genomes.org"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3

# Rows per answer. The default is one upstream page: at the old 100-row default
# every Arabidopsis answer in the ARF dossier was 36-40k tokens, over Claude
# Code's 25k default cap on its own (22 of 22 loci, 2026-09-27). Rows come
# strongest first; ``next_cursor`` resumes at the first row not returned.
DEFAULT_LIMIT = 25
MAX_LIMIT = 100

# Follow at most this many upstream pages per answer: AraGWAS honours
# ``?limit=`` (checked live, 100 rows on one page), so this only bounds an
# upstream that pages shorter than asked. ``association_count`` always reports
# the true total (from the API ``count``) even when page-capped.
MAX_PAGES = 4

_CACHE = cache.TTLCache()


def _resolve_limit(limit: int | None) -> int:
    """Clamp a caller's ``limit`` into ``1..MAX_LIMIT`` (gramene's rule): a
    non-positive limit is nonsense, not a request for everything."""
    if limit is None:
        return DEFAULT_LIMIT
    return max(1, min(int(limit), MAX_LIMIT))


async def _get(client: httpx.AsyncClient, url: str) -> dict[str, Any]:
    """GET one AraGWAS page (cached by full URL), returning the parsed dict."""
    return await _http.cached_get(
        client,
        _CACHE,
        url,
        service="AraGWAS associations",
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
        # The count is checked before the store: read afterwards, a page
        # without one was stored first and failed the call for the whole TTL.
        shape=_http.expect_count("count"),
    )


def _annotation_for(snp: dict[str, Any], locus: str) -> dict[str, Any]:
    """Pick the SNP annotation for this gene (else the first), else empty."""
    anns = [a for a in snp.get("annotations") or [] if isinstance(a, dict)]
    for a in anns:
        if a.get("geneName") == locus:
            return a
    return anns[0] if anns else {}


# Issue #137: AraGWAS stores study.name as a one-element Python tuple repr,
# "('As75_raw_Full imputed genotype_amm',)" (live, 2026-09-22). Anchored,
# no nested quantifiers: one quote, anything but that quote, the same quote.
_TUPLE_REPR = re.compile(r"^\((['\"])([^'\"]*)\1,\)$")


def _study_name(name: Any) -> Any:
    """Unwrap AraGWAS's tuple-repr study name; anything else comes back as sent."""
    if isinstance(name, str):
        match = _TUPLE_REPR.match(name)
        if match:
            return match.group(2)
    return name


def _thresholds(study: dict[str, Any]) -> dict[str, Any]:
    """The study's own significance thresholds, keyed by AraGWAS's names.

    Same -log10(p) scale as ``score``: bonferroni_threshold05 is
    -log10(0.05 / total_associations) (checked live against the study's own
    total). over_bonferroni / over_fdr / over_permutation are ``score`` against
    bonferroni_threshold05 / bh_threshold / permutation_threshold.
    """
    out: dict[str, Any] = {}
    for item in study.get("thresholds") or []:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            out[item["name"]] = item.get("value")
    return out


def _project(assoc: dict[str, Any], locus: str) -> dict[str, Any]:
    """Project one AraGWAS association to the surfaced field set."""
    snp = assoc.get("snp") or {}
    ann = _annotation_for(snp, locus)
    study = assoc.get("study") or {}
    pheno = study.get("phenotype") or {}
    return {
        "score": assoc.get("score"),
        "maf": assoc.get("maf"),
        "mac": assoc.get("mac"),
        "over_bonferroni": assoc.get("overBonferroni"),
        "over_fdr": assoc.get("overFDR"),
        "over_permutation": assoc.get("overPermutation"),
        "snp": {
            "chr": snp.get("chr"),
            "position": snp.get("position"),
            "ref": snp.get("ref"),
            "alt": snp.get("alt"),
            "coding": snp.get("coding"),
            "gene": snp.get("geneName"),
            "effect": ann.get("effect"),
            "impact": ann.get("impact"),
            "amino_acid_change": ann.get("aminoAcidChange"),
            "transcript": ann.get("transcriptId"),
        },
        "study": {
            "name": _study_name(study.get("name")),
            "method": study.get("method"),
            "phenotype": pheno.get("name"),
            "phenotype_description": pheno.get("description"),
            "thresholds": _thresholds(study),
        },
    }


async def lookup_locus(
    client: httpx.AsyncClient,
    locus: str,
    organism: str | int = organisms.DEFAULT_ORGANISM,
    cursor: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Fetch AraGWAS GWAS associations for an Arabidopsis locus.

    ``limit`` caps the rows (default ``DEFAULT_LIMIT``, clamped to
    ``1..MAX_LIMIT``). ``cursor`` is a ``next_cursor`` from the previous call
    (#123); it resumes at AraGWAS's own ``offset``, whatever ``limit`` either
    call used.

    Raises ``OrganismNotSupported`` for any non-Arabidopsis organism (the panel
    is *A. thaliana* only). Follows pagination up to ``MAX_PAGES``;
    ``association_count`` is the true total even when the row list is capped.
    """
    canonical = organisms.resolve(organism).canonical
    if canonical != "arabidopsis_thaliana":
        raise OrganismNotSupported(
            backend="aragwas", organism=canonical, supported=["arabidopsis_thaliana"]
        )
    locus = validators.assert_valid_agi(locus, backend="AraGWAS")
    query = {"locus": locus}
    offset = int(_http.decode_cursor("aragwas_associations", query, cursor).get("offset", 0))
    cap = _resolve_limit(limit)
    first = f"{BASE_URL}/api/genes/{locus}/associations/?limit={cap}"
    url: str | None = f"{first}&offset={offset}" if offset else first
    associations: list[dict[str, Any]] = []
    total = 0
    pages = 0
    while url and pages < MAX_PAGES and len(associations) < cap:
        page = await _get(client, url)
        total = _http.stated_count(page, "count", service="AraGWAS associations")
        for assoc in page.get("results") or []:
            if isinstance(assoc, dict):
                associations.append(_project(assoc, locus))
        links = page.get("links") or {}
        # Only follow a same-host next link — the URL comes from the upstream
        # body, so an off-host value would be an SSRF vector (audit L5).
        next_url = links.get("next")
        # ``startswith(BASE_URL + "/")`` — a host match, not a bare prefix:
        # ``BASE_URL`` has no trailing slash, so a plain ``startswith(BASE_URL)``
        # would also accept ``https://aragwas.1001genomes.org.evil.example/…``
        # and defeat this same-host SSRF guard.
        url = (
            next_url if isinstance(next_url, str) and next_url.startswith(BASE_URL + "/") else None
        )
        pages += 1
    # An upstream page longer than asked is cut here; the cursor below resumes
    # at the first row not returned, so the cut rows come next, not never.
    associations = associations[:cap]
    return {
        "locus": locus,
        "organism": canonical,
        "found": True,
        "association_count": total,
        **_http.counted(total, associations, offset=offset),
        # total>offset+returned covers the count-known case; a still-set next
        # link covers a null/absent upstream count where more pages remain (audit L3).
        "truncated": total > offset + len(associations) or url is not None,
        "next_cursor": _http.encode_cursor(
            "aragwas_associations", query, {"offset": offset + len(associations)}
        )
        if total > offset + len(associations) or url is not None
        else None,
        "associations": associations,
        # Issue #121: uniform key; null because this backend states no release on
        # the answering response (headers probed live 2026-09-22).
        "upstream_version": None,
    }
