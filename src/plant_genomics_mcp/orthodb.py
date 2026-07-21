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

from plant_genomics_mcp import _http, cache, organisms
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


def _members(clusters: list[Any]) -> tuple[list[dict[str, Any]], bool]:
    """Flatten ortholog clusters to capped ``[{organism, gene_id, xref, description}]``."""
    out: list[dict[str, Any]] = []
    truncated = False
    for cluster in clusters:
        if not isinstance(cluster, dict):
            continue
        org = (cluster.get("organism") or {}).get("name")
        for gene in cluster.get("genes") or []:
            if len(out) >= MAX_MEMBERS:
                return out, True
            if not isinstance(gene, dict):
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
    return out, truncated


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
) -> dict[str, Any]:
    """Resolve a locus to its OrthoDB ortholog group and cross-species members.

    ``organism`` is validated/echoed (the OrthoDB search keys on the gene id at
    the Viridiplantae level, not a species id). Returns ``found=False`` when the
    locus maps to no ortholog group.
    """
    canonical = organisms.resolve(organism).canonical
    search = await _get(client, "/current/search", {"query": locus, "level": LEVEL, "limit": 1})
    ids = search.get("data")
    if not isinstance(ids, list) or not ids:
        return _empty(locus, canonical)
    gid = ids[0]

    group = await _get(client, "/current/group", {"id": gid})
    ortho = await _get(client, "/current/orthologs", {"id": gid})
    clusters = ortho.get("data")
    clusters = clusters if isinstance(clusters, list) else []
    members, truncated = _members(clusters)

    return {
        "locus": locus,
        "organism": canonical,
        "found": True,
        "group": _project_group(group.get("data") or {}),
        "organism_count": len(clusters),
        "member_count": len(members),
        "truncated": truncated,
        "members": members,
    }
