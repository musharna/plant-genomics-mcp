"""BAR (Bio-Analytic Resource for Plant Biology) backend — async httpx wrapper around bar.utoronto.ca/api.

BAR is the U Toronto plant bioinformatics hub: ThaleMine InterMine front-end
for Arabidopsis annotation, eFP browser microarray data, AIV curated PPIs,
and a rice PPI lane. Global Core Biodata Resource (2023); NSERC + Genome
Canada (OGI-162) funded. Keyless — no auth, no API key, no rate-limit
headers observed in probing.

Probe writeup: ``docs/superpowers/audits/2026-05-23-bar-api-probe.md``.

Endpoints this module wraps:

  GET /thalemine/gene_information/{locus} → positional JSON array:
    [agi, full_name, tair_locus_id, display_name, symbol, synonyms,
     computational_description, curator_summary, brief_description]
  GET /gaia/aliases/{locus} → {species, locus, geneid, aliases:[...]}
  GET /microarray_gene_expression/world_efp/arabidopsis/{locus} → eFP world map
  GET /interactions/get_paper_by_agi/{locus} → curated Arabidopsis GRN papers
  GET /interactions/rice/{locus} → rice PPI rows

ThaleMine carries only Arabidopsis (taxon 3702); yeast/human present for
ortholog cross-reference but not plant data. Rice support is limited to
the /interactions/rice/ lane.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx

from plant_genomics_mcp import __version__, cache, progress
from plant_genomics_mcp.errors import (
    NotFoundError,
    PlantGenomicsError,
    RateLimitError,
    UpstreamUnavailableError,
)

BASE_URL = "https://bar.utoronto.ca/api"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3
CACHE_TTL_SECONDS = 3600.0  # 1h — BAR doesn't version-stamp releases

# Loose locus regex — BAR accepts AGI ("AT1G01010"), MSU rice loci
# ("LOC_Os01g01080"), and various synonym forms. Reject obvious garbage
# (whitespace, HTML, shell metacharacters) but stay permissive.
_LOCUS_RE = re.compile(r"^[A-Za-z0-9._-]+$")

_CACHE = cache.TTLCache(default_ttl=CACHE_TTL_SECONDS)


def _user_agent() -> str:
    return f"plant-genomics-mcp/{__version__}"


async def _get(
    client: httpx.AsyncClient,
    path: str,
    params: dict[str, Any] | None = None,
) -> Any:
    """GET JSON from BAR with retry + cache. Raises typed errors on failure."""
    key = cache.make_key("GET", BASE_URL, path, params)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    headers = {"Accept": "application/json", "User-Agent": _user_agent()}
    delay = 1.0
    last_status: int | None = None
    for attempt in range(MAX_RETRIES):
        resp = await client.get(
            f"{BASE_URL}{path}",
            params=params,
            headers=headers,
            timeout=DEFAULT_TIMEOUT,
        )
        last_status = resp.status_code
        if resp.status_code == 200:
            try:
                result = resp.json()
            except ValueError as e:
                raise PlantGenomicsError(f"BAR {path} returned non-JSON: {resp.text[:200]}") from e
            _CACHE.set(key, result)
            return result
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES - 1:
            retry_after = float(resp.headers.get("Retry-After", delay))
            await progress.notify(
                f"BAR {path}: HTTP {resp.status_code}, retrying in "
                f"{retry_after:.1f}s (attempt {attempt + 2}/{MAX_RETRIES})"
            )
            await asyncio.sleep(retry_after)
            delay *= 2
            continue
        if resp.status_code == 429:
            raise RateLimitError(f"BAR {path} rate-limited (HTTP 429): {resp.text[:200]}")
        if resp.status_code in (500, 502, 503, 504):
            raise UpstreamUnavailableError(
                f"BAR {path} → HTTP {resp.status_code}: {resp.text[:200]}"
            )
        if resp.status_code == 404:
            raise NotFoundError(f"BAR {path}: not found")
        raise PlantGenomicsError(f"BAR {path} → HTTP {resp.status_code}: {resp.text[:200]}")
    if last_status == 429:
        raise RateLimitError(f"BAR {path} exhausted {MAX_RETRIES} retries (429)")
    raise UpstreamUnavailableError(
        f"BAR {path} exhausted {MAX_RETRIES} retries (last HTTP {last_status})"
    )


# Positional indices of /thalemine/gene_information/{locus} array response.
# Live shape (probed 2026-05-23 against AT1G01010):
#   [0] "AT1G01010"                                          → input echo
#   [1] "NAC domain containing protein 1"                    → full_name
#   [2] "locus:2200935"                                      → tair_locus_id
#   [3] "NAC domain containing protein 1"                    → display_name (dup)
#   [4] "NAC001"                                              → symbol
#   [5] "ANAC001, NAC001, NTL10"                             → synonyms (CSV)
#   [6] "NAC domain containing protein 1;(source:Araport11)" → computational_description
#   [7] "Member of the NAC domain containing family..."      → curator_summary
#   [8] "NAC domain containing protein 1"                    → brief_description
_GI_AGI = 0
_GI_FULL_NAME = 1
_GI_TAIR_LOCUS_ID = 2
_GI_SYMBOL = 4
_GI_SYNONYMS = 5
_GI_COMPUTATIONAL = 6
_GI_CURATOR = 7
_GI_BRIEF = 8


async def gene_summary(
    client: httpx.AsyncClient,
    locus: str,
) -> dict[str, Any]:
    """Fetch Arabidopsis gene summary from BAR/ThaleMine.

    Hits ``/thalemine/gene_information/{locus}`` and projects the positional
    response array into a named-field dict. Arabidopsis only — BAR's
    ThaleMine instance carries only taxon 3702.

    The returned ``curator_summary`` is the TAIR-curated functional summary
    that the v0.9 ``tair_locus_info`` stub could not provide for free.
    """
    if not _LOCUS_RE.match(locus):
        raise NotFoundError(f"BAR: invalid locus {locus!r} (must match {_LOCUS_RE.pattern})")
    raw = await _get(client, f"/thalemine/gene_information/{locus}")
    if not isinstance(raw, list) or len(raw) <= _GI_BRIEF:
        raise NotFoundError(
            f"BAR /thalemine/gene_information/{locus}: empty or malformed "
            f"response ({type(raw).__name__} of len {len(raw) if hasattr(raw, '__len__') else '?'})"
        )
    return {
        "locus": locus,
        "agi": raw[_GI_AGI],
        "symbol": raw[_GI_SYMBOL],
        "full_name": raw[_GI_FULL_NAME],
        "tair_locus_id": raw[_GI_TAIR_LOCUS_ID],
        "synonyms": [s.strip() for s in raw[_GI_SYNONYMS].split(",")] if raw[_GI_SYNONYMS] else [],
        "computational_description": raw[_GI_COMPUTATIONAL],
        "curator_summary": raw[_GI_CURATOR],
        "brief_description": raw[_GI_BRIEF],
        "species": "arabidopsis_thaliana",
        "source_url": f"https://bar.utoronto.ca/api/thalemine/gene_information/{locus}",
    }
