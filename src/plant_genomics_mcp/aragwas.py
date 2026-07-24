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

from typing import Any

import httpx

from plant_genomics_mcp import _http, cache, organisms, validators
from plant_genomics_mcp.errors import OrganismNotSupported, PlantGenomicsError

BASE_URL = "https://aragwas.1001genomes.org"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3

# Follow at most this many 25-row pages (≈100 top associations). ``association_count``
# always reports the true total (from the API ``count``) even when page-capped.
MAX_PAGES = 4

_CACHE = cache.TTLCache()


async def _get(client: httpx.AsyncClient, url: str) -> dict[str, Any]:
    """GET one AraGWAS page (cached by full URL), returning the parsed dict."""
    key = cache.make_key("GET", url, "", None)
    cached = _CACHE.get(key)
    if cached is None:
        resp = await _http.request_with_retry(
            client,
            "GET",
            url,
            service="AraGWAS associations",
            headers={"Accept": "application/json"},
            timeout=DEFAULT_TIMEOUT,
            max_retries=MAX_RETRIES,
        )
        try:
            cached = resp.json()
        except ValueError as e:
            raise PlantGenomicsError(f"AraGWAS returned non-JSON: {resp.text[:200]}") from e
        _CACHE.set(key, cached)
    if not isinstance(cached, dict):
        raise PlantGenomicsError(
            f"AraGWAS associations returned unexpected payload: {type(cached).__name__}"
        )
    return cached


def _annotation_for(snp: dict[str, Any], locus: str) -> dict[str, Any]:
    """Pick the SNP annotation for this gene (else the first), else empty."""
    anns = [a for a in snp.get("annotations") or [] if isinstance(a, dict)]
    for a in anns:
        if a.get("geneName") == locus:
            return a
    return anns[0] if anns else {}


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
            "name": study.get("name"),
            "method": study.get("method"),
            "phenotype": pheno.get("name"),
            "phenotype_description": pheno.get("description"),
        },
    }


async def lookup_locus(
    client: httpx.AsyncClient,
    locus: str,
    organism: str | int = organisms.DEFAULT_ORGANISM,
) -> dict[str, Any]:
    """Fetch AraGWAS GWAS associations for an Arabidopsis locus.

    Raises ``OrganismNotSupported`` for any non-Arabidopsis organism (the panel
    is *A. thaliana* only). Follows pagination up to ``MAX_PAGES``;
    ``association_count`` is the true total even when the row list is capped.
    """
    canonical = organisms.resolve(organism).canonical
    if canonical != "arabidopsis_thaliana":
        raise OrganismNotSupported(
            backend="aragwas", organism=canonical, supported=["arabidopsis_thaliana"]
        )
    validators.assert_valid_agi(locus, backend="AraGWAS")
    url: str | None = f"{BASE_URL}/api/genes/{locus}/associations/"
    associations: list[dict[str, Any]] = []
    total = 0
    pages = 0
    while url and pages < MAX_PAGES:
        page = await _get(client, url)
        total = int(page.get("count") or 0)
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
    return {
        "locus": locus,
        "organism": canonical,
        "found": True,
        "association_count": total,
        "returned": len(associations),
        # total>returned covers the count-known case; a still-set next link
        # covers a null/absent upstream count where more pages remain (audit L3).
        "truncated": total > len(associations) or url is not None,
        "associations": associations,
    }
