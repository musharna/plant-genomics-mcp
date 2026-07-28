"""OrthoDB orthology client — locus → ortholog group + cross-species members.

OrthoDB (data.orthodb.org) clusters genes into hierarchical ortholog groups.
For a plant locus we search at the Viridiplantae level (NCBI taxid 33090) to
find the gene's ortholog group, fetch the group's metadata (name, evolutionary
rate), then list its member genes grouped by organism.

The API is free, needs no key, and takes native gene identifiers, so no UniProt
hop is required. A locus with no ortholog group returns ``found=False``.

Three-hop flow (each response cached independently):
    /current/search?query={locus}&level=33090   → group id
    /current/group?id={gid}                       → group metadata
    /current/orthologs?id={gid}                   → per-organism member clusters
"""

from __future__ import annotations

from typing import Any

import httpx

from plant_genomics_mcp import _http, cache, organisms, validators
from plant_genomics_mcp.errors import PlantGenomicsError

BASE_URL = "https://data.orthodb.org"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3

# Viridiplantae — scope the ortholog search to green plants.
LEVEL = "33090"

# Cap member genes returned; a Viridiplantae group can span hundreds of species.
# ``organism_count`` reports the true cluster total even when members are capped.
MAX_MEMBERS = 100

_CACHE = cache.TTLCache()


async def _get(client: httpx.AsyncClient, path: str, params: dict[str, Any]) -> dict[str, Any]:
    """GET an OrthoDB endpoint (own cache), returning the parsed dict."""
    key = cache.make_key("GET", BASE_URL, path, params)
    cached = _CACHE.get(key)
    if cached is None:
        resp = await _http.request_with_retry(
            client,
            "GET",
            f"{BASE_URL}{path}",
            service=f"OrthoDB {path}",
            params=params,
            headers={"Accept": "application/json"},
            timeout=DEFAULT_TIMEOUT,
            max_retries=MAX_RETRIES,
        )
        cached = resp.json()
        _CACHE.set(key, cached)
    if not isinstance(cached, dict):
        raise PlantGenomicsError(
            f"OrthoDB {path} returned unexpected payload: {type(cached).__name__}"
        )
    return cached


def _project_group(data: dict[str, Any]) -> dict[str, Any]:
    """Project the ``/group`` metadata to the surfaced field set."""
    return {
        "id": data.get("id"),
        "public_id": data.get("public_id"),
        "name": data.get("name"),
        "evolutionary_rate": data.get("evolutionary_rate"),
        "level_name": data.get("level_name"),
        "tax_id": data.get("tax_id"),
    }


def _resolve_limit(limit: int | None) -> int:
    """Clamp a caller's ``limit`` into ``1..MAX_MEMBERS``.

    A non-positive limit is nonsense rather than a request for everything;
    returning zero rows beside a non-zero count reads as "no orthologs".
    """
    if limit is None:
        return MAX_MEMBERS
    return max(1, min(int(limit), MAX_MEMBERS))


def _members(clusters: list[Any], cap: int) -> tuple[list[dict[str, Any]], int]:
    """Flatten ortholog clusters to ``[{organism, gene_id, xref, description}]``.

    Returns the capped rows AND the TRUE total member count. The loop no longer
    returns early on reaching the cap: it kept counting nothing after that
    point, so ``member_count`` was the number of rows RETURNED rather than the
    number that exist. A caller seeing ``member_count: 100, truncated: true``
    could not tell whether 101 or 10,000 orthologs existed — the count agreed
    with the list instead of describing the data, which is the same class of
    lying count the 2026-07-25 semantic audit fixed three of.
    """
    out: list[dict[str, Any]] = []
    total = 0
    for cluster in clusters:
        if not isinstance(cluster, dict):
            continue
        org = (cluster.get("organism") or {}).get("name")
        for gene in cluster.get("genes") or []:
            if not isinstance(gene, dict):
                continue
            total += 1
            if len(out) >= cap:
                continue
            gid = gene.get("gene_id") or {}
            out.append(
                {
                    "organism": org,
                    "gene_id": gid.get("id"),
                    "xref": gid.get("param"),
                    "description": gene.get("description"),
                }
            )
    return out, total


def _empty(locus: str, organism: str) -> dict[str, Any]:
    return {
        "locus": locus,
        "organism": organism,
        "found": False,
        "group": None,
        "organism_count": 0,
        "member_count": 0,
        "truncated": False,
        "members": [],
    }


async def lookup_locus(
    client: httpx.AsyncClient,
    locus: str,
    organism: str | int = organisms.DEFAULT_ORGANISM,
    limit: int | None = None,
) -> dict[str, Any]:
    """Resolve a locus to its OrthoDB ortholog group and cross-species members.

    ``organism`` is validated/echoed (the OrthoDB search keys on the gene id at
    the Viridiplantae level, not a species id). Returns ``found=False`` when the
    locus maps to no ortholog group.

    ``limit`` caps the returned members (default ``MAX_MEMBERS``);
    ``member_count`` reports the TRUE pre-cap total and ``truncated`` says
    whether the cap bit.
    """
    canonical = organisms.resolve(organism).canonical
    validators.assert_valid_locus(locus, backend="OrthoDB")
    search = await _get(client, "/current/search", {"query": locus, "level": LEVEL, "limit": 1})
    ids = search.get("data")
    if not isinstance(ids, list) or not ids:
        return _empty(locus, canonical)
    gid = ids[0]

    group = await _get(client, "/current/group", {"id": gid})
    ortho = await _get(client, "/current/orthologs", {"id": gid})
    clusters = ortho.get("data")
    clusters = clusters if isinstance(clusters, list) else []
    members, member_total = _members(clusters, _resolve_limit(limit))

    return {
        "locus": locus,
        "organism": canonical,
        "found": True,
        "group": _project_group(group.get("data") or {}),
        "organism_count": len(clusters),
        # TRUE pre-cap total, so a truncated answer still says how much exists.
        "member_count": member_total,
        "truncated": member_total > len(members),
        "members": members,
    }
