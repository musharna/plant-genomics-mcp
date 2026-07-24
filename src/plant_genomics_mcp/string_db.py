"""STRING-DB interaction-partners backend — async httpx wrapper around string-db.org.

STRING is the EMBL-hosted protein-protein interaction database. We query
``/api/json/interaction_partners`` to retrieve the first-neighbor network
for a protein, scored by predicted + curated + experimental confidence.

Input shape: tools accept either a UniProt accession (``Q0WV96``,
``P12345``) or a locus identifier (``AT1G01010``, ``Os01g0100100``). Both
are passed through to STRING unchanged — STRING's own identifier resolver
picks the canonical species-scoped accession. Pre-resolving loci through
UniProt produces accession-choice mismatches when a locus has multiple
valid UniProt accessions and STRING canonicalizes on a different one
(observed v1.1.0 with rice Os01g0100100 → UniProt Q0JRI1 vs STRING
A0A0P0UX28), so v1.1.1 removed the UniProt pre-resolution step.

STRING etiquette: pass ``caller_identity`` to identify the caller. We
hardcode ``plant-genomics-mcp``.
"""

from __future__ import annotations

from typing import Any

import httpx

from plant_genomics_mcp import _http, cache, organisms, validators
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

_CACHE = cache.TTLCache(default_ttl=CACHE_TTL_SECONDS)


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
    try:
        result = resp.json()
    except ValueError as e:
        raise PlantGenomicsError(f"STRING {path} returned non-JSON: {resp.text[:200]}") from e
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

    Accepts either a UniProt accession or a locus identifier; both are
    passed through to STRING's ``/api/json/interaction_partners`` endpoint
    unchanged. STRING's internal resolver picks the species-canonical
    accession and returns it in ``stringId_A`` (taxid-prefixed); we surface
    the bare accession as ``accession`` on the result. ``organism`` accepts
    any form the resolver supports (slug, scientific/common name, taxid).
    """
    # Validate before the value reaches cache.make_key / the wire — STRING is
    # the lone locus-accepting backend that previously skipped this, so a
    # caller identifier containing cache-key separators ('&', '=', '|') could
    # slip through (audit P6). UniProt accessions and loci both match the
    # [A-Za-z0-9._-] class, so this rejects only genuinely malformed input.
    validators.assert_valid_locus(locus_or_accession, backend="STRING")
    limit = max(1, min(limit, MAX_LIMIT))
    record = organisms.resolve(organism)
    taxid = organisms.string_taxid_for(organism)
    query = locus_or_accession.split(".", 1)[0]  # strip optional UniProt version suffix

    raw = await _get(
        client,
        "/api/json/interaction_partners",
        params={
            "identifiers": query,
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
        raise NotFoundError(f"STRING: no interaction partners for {query}")

    # STRING returns stringId_A as "{taxid}.{accession}"; the accession is
    # STRING's species-canonical pick, which may differ from the input
    # (e.g. locus → accession resolution, or one of several UniProt accessions).
    string_id_a = raw[0].get("stringId_A", "") if isinstance(raw[0], dict) else ""
    canonical_accession = string_id_a.split(".", 1)[1] if "." in string_id_a else query

    partners = [_normalize(r, canonical_accession) for r in raw if isinstance(r, dict)]
    return {
        "query": query,
        "accession": canonical_accession,
        "organism": record.canonical,
        "partners": partners,
    }
