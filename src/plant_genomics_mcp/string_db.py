"""STRING-DB interaction-partners backend — async httpx wrapper around string-db.org.

STRING is the EMBL-hosted protein-protein interaction database. We query
``/api/json/interaction_partners`` to retrieve the first-neighbor network
for a protein, scored by predicted + curated + experimental confidence.

Input shape detection: tools accept either a UniProt accession
(``Q0WV96``, ``P12345``) or a locus identifier (``AT1G01010``). Accession
inputs (matching the UniProt regex) route directly; locus inputs route via
``uniprot.lookup_locus`` first. This mirrors v0.6's
``resolve_locus_to_uniprot`` dispatch added in P2.b.

STRING etiquette: pass ``caller_identity`` to identify the caller. We
hardcode ``plant-genomics-mcp``.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from plant_genomics_mcp import _http, cache, organisms, uniprot
from plant_genomics_mcp.errors import (
    NotFoundError,
    PlantGenomicsError,
)

BASE_URL = "https://string-db.org"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3
CACHE_TTL_SECONDS = 3600.0  # 1h — matches uniprot._CACHE TTL for cache-stats uniformity.

DEFAULT_LIMIT = 20
MAX_LIMIT = 500
CALLER_IDENTITY = "plant-genomics-mcp"

# UniProt accession: 6 or 10 chars. The 10-char form (NEW format) is
# documented at https://www.uniprot.org/help/accession_numbers.
_UNIPROT_RE = re.compile(
    r"^(?:"
    r"[OPQ][0-9][A-Z0-9]{3}[0-9]"
    r"|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2}"
    r")$"
)

_CACHE = cache.TTLCache(default_ttl=CACHE_TTL_SECONDS)


def _looks_like_accession(query: str) -> bool:
    """True if ``query`` matches the UniProt accession pattern.

    Strips an optional version suffix (``.1``, ``.2``) before matching.
    """
    bare = query.split(".", 1)[0]
    return bool(_UNIPROT_RE.match(bare))


async def _get(
    client: httpx.AsyncClient,
    path: str,
    params: dict[str, Any] | None = None,
) -> Any:
    key = cache.make_key("GET", BASE_URL, path, params)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    resp = await _http.request_with_retry(
        client,
        "GET",
        f"{BASE_URL}{path}",
        service=f"STRING {path}",
        params=params,
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
    )
    result = resp.json()
    _CACHE.set(key, result)
    return result


def _normalize(row: dict[str, Any], query_accession: str) -> dict[str, Any]:
    """Project one STRING interaction row to the surfaced field set.

    STRING returns symmetric A/B columns; the query protein is always on
    the A side, so we surface the B-side fields as the partner.
    """
    return {
        "string_id": row.get("stringId_B"),
        "accession": row.get(
            "stringId_B"
        ),  # partner's stringId; UniProt mapping not always trivial
        "preferred_name": row.get("preferredName_B"),
        "score": row.get("score"),
        "escore": row.get("escore"),
        "dscore": row.get("dscore"),
        "tscore": row.get("tscore"),
        "pscore": row.get("pscore"),
    }


async def lookup_partners(
    client: httpx.AsyncClient,
    locus_or_accession: str,
    limit: int = DEFAULT_LIMIT,
    organism: str | int = organisms.DEFAULT_ORGANISM,
) -> dict[str, Any]:
    """Fetch STRING first-neighbor interactors for a protein.

    Accepts either a UniProt accession or a locus identifier; the latter
    is resolved via ``uniprot.lookup_locus`` first. ``organism`` accepts
    any form the resolver supports (slug, scientific/common name, taxid).
    """
    limit = max(1, min(limit, MAX_LIMIT))
    record = organisms.resolve(organism)
    taxid = organisms.string_taxid_for(organism)
    query = locus_or_accession
    if _looks_like_accession(locus_or_accession):
        accession = locus_or_accession.split(".", 1)[0]
    else:
        up = await uniprot.lookup_locus(client, locus_or_accession, organism=organism)
        accession = up["primaryAccession"]

    raw = await _get(
        client,
        "/api/json/interaction_partners",
        params={
            "identifiers": accession,
            "species": taxid,
            "limit": limit,
            "caller_identity": CALLER_IDENTITY,
        },
    )
    if not isinstance(raw, list):
        raise PlantGenomicsError(
            f"STRING /api/json/interaction_partners returned non-list: {type(raw).__name__}"
        )
    if not raw:
        raise NotFoundError(f"STRING: no interaction partners for {accession}")
    partners = [_normalize(r, accession) for r in raw if isinstance(r, dict)]
    return {
        "query": query,
        "accession": accession,
        "organism": record.canonical,
        "partners": partners,
    }
