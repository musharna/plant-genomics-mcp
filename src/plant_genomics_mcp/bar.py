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


# BAR uses a body-level success envelope, not HTTP status. Both endpoints
# return HTTP 200 even for missing loci; the `wasSuccessful` flag carries
# the real outcome. Failure modes seen live 2026-05-23:
#   /thalemine/gene_information/AT1G99999  → 200 {"wasSuccessful":true,"results":[]}
#   /thalemine/gene_information/LOC_Os01g01080 → 400 {"wasSuccessful":false,"error":"Invalid gene id"}
#   /gaia/aliases/AT1G99999                → 200 {"wasSuccessful":false,"error":"Nothing found"}

# Positional indices into thalemine's results[0] row. Order is fixed by the
# InterMine `views` list (column ordering survives schema versions). Live
# shape (probed 2026-05-23 against AT1G01010):
#   [0] "AT1G01010"                                          → agi (Gene.primaryIdentifier)
#   [1] "NAC domain containing protein 1"                    → full_name (Gene.name)
#   [2] "locus:2200935"                                      → tair_locus_id (Gene.secondaryIdentifier)
#   [3] "NAC domain containing protein 1"                    → brief_description (Gene.briefDescription)
#   [4] "NAC001"                                              → symbol (Gene.symbol)
#   [5] "ANAC001, NAC001, NTL10"                             → synonyms (Gene.tairAliases, CSV)
#   [6] "NAC domain containing protein 1;(source:Araport11)" → computational_description (Gene.tairComputationalDescription)
#   [7] "Member of the NAC domain containing family..."      → curator_summary (Gene.tairCuratorSummary)
#   [8] "NAC domain containing protein 1"                    → tair_short_description (Gene.tairShortDescription)
_GI_AGI = 0
_GI_FULL_NAME = 1
_GI_TAIR_LOCUS_ID = 2
_GI_BRIEF = 3
_GI_SYMBOL = 4
_GI_SYNONYMS = 5
_GI_COMPUTATIONAL = 6
_GI_CURATOR = 7
_GI_TAIR_SHORT = 8


async def gene_summary(
    client: httpx.AsyncClient,
    locus: str,
) -> dict[str, Any]:
    """Fetch Arabidopsis gene summary from BAR/ThaleMine + /gaia/aliases/.

    Two BAR endpoints fetched in parallel:
      - ``/thalemine/gene_information/{locus}`` → InterMine envelope with
        ``results: [[positional]]`` carrying TAIR curator summary + Araport11
        computational description.
      - ``/gaia/aliases/{locus}`` → ``{data: [{species, locus, geneid,
        aliases:[...]}]}`` carrying NCBI Gene ID + cross-DB synonyms
        (RefSeq, UniProt, locus-model IDs).

    Arabidopsis only — BAR's ThaleMine instance carries taxon 3702 plus
    yeast/human for ortholog cross-reference. Non-Arabidopsis loci raise
    NotFoundError because thalemine 400s and gaia returns empty.

    Aliases are best-effort: a /gaia failure does not fail the call, since
    the canonical TAIR fields come from thalemine. Returns ``ncbi_gene_id:
    None`` and ``aliases: []`` in that case.
    """
    if not _LOCUS_RE.match(locus):
        raise NotFoundError(f"BAR: invalid locus {locus!r} (must match {_LOCUS_RE.pattern})")
    gi_env, aliases_env = await asyncio.gather(
        _get(client, f"/thalemine/gene_information/{locus}"),
        _get(client, f"/gaia/aliases/{locus}"),
        return_exceptions=True,
    )
    if isinstance(gi_env, BaseException):
        raise gi_env
    if not isinstance(gi_env, dict) or not gi_env.get("wasSuccessful"):
        err = gi_env.get("error") if isinstance(gi_env, dict) else "non-dict response"
        raise NotFoundError(f"BAR /thalemine/gene_information/{locus}: {err}")
    results = gi_env.get("results") or []
    if not results:
        raise NotFoundError(
            f"BAR /thalemine/gene_information/{locus}: no record (wasSuccessful but empty results)"
        )
    row = results[0]
    if not isinstance(row, list) or len(row) <= _GI_TAIR_SHORT:
        raise NotFoundError(
            f"BAR /thalemine/gene_information/{locus}: malformed row "
            f"({type(row).__name__} of len {len(row) if hasattr(row, '__len__') else '?'})"
        )

    ncbi_gene_id: str | None = None
    aliases_list: list[str] = []
    if (
        not isinstance(aliases_env, BaseException)
        and isinstance(aliases_env, dict)
        and aliases_env.get("wasSuccessful")
    ):
        entries = aliases_env.get("data") or []
        # /gaia/aliases/ can return multiple entries per locus (case-variant
        # rows, e.g. At1g01010 + AT1G01010 — only the uppercase one carries
        # the geneid). Prefer the exact-uppercase match, then any entry with
        # a non-null geneid, then the first entry.
        chosen = next(
            (
                e
                for e in entries
                if e.get("geneid") and (e.get("locus") or "").upper() == locus.upper()
            ),
            None,
        )
        if chosen is None:
            chosen = next((e for e in entries if e.get("geneid")), None)
        if chosen is None and entries:
            chosen = entries[0]
        if chosen is not None:
            ncbi_gene_id = chosen.get("geneid")
            aliases_list = list(chosen.get("aliases") or [])

    return {
        "locus": locus,
        "agi": row[_GI_AGI],
        "symbol": row[_GI_SYMBOL],
        "full_name": row[_GI_FULL_NAME],
        "tair_locus_id": row[_GI_TAIR_LOCUS_ID],
        "synonyms": [s.strip() for s in row[_GI_SYNONYMS].split(",")] if row[_GI_SYNONYMS] else [],
        "computational_description": row[_GI_COMPUTATIONAL],
        "curator_summary": row[_GI_CURATOR],
        "brief_description": row[_GI_BRIEF],
        "tair_short_description": row[_GI_TAIR_SHORT],
        "ncbi_gene_id": ncbi_gene_id,
        "aliases": aliases_list,
        "species": "arabidopsis_thaliana",
        "source_url": f"https://bar.utoronto.ca/api/thalemine/gene_information/{locus}",
    }
