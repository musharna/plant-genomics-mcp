"""InterPro domain client — locus → UniProt → domain / family architecture.

InterPro (www.ebi.ac.uk/interpro) integrates protein signatures from member
databases (Pfam, CDD, PANTHER, CATH-Gene3D, …) into a per-protein domain
architecture. Its REST API is free and needs no key. Plant loci aren't indexed
directly, so we resolve the locus to a UniProt accession via
``plant_genomics_mcp.uniprot.lookup_locus`` (the same seam quickgo/alphafold
use), then fetch the protein's entries.

Pfam is not a separate backend — it appears as ``source_database == "pfam"``
among the returned rows. A protein that exists but has no annotated domains
returns ``found=True`` with an empty ``domains`` list (distinct from a locus
that resolves to no UniProt entry, which propagates ``NotFoundError``).

Endpoint: https://www.ebi.ac.uk/interpro/api/entry/all/protein/uniprot/{acc}/
(paginated: ``{count, next, previous, results[]}``).
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import httpx

from plant_genomics_mcp import _http, cache, organisms, uniprot, validators
from plant_genomics_mcp.errors import PlantGenomicsError

BASE_URL = "https://www.ebi.ac.uk"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3

# Follow at most this many pages of results — a guard against a pathological
# protein with hundreds of signatures fanning out into unbounded requests. The
# true total is always reported via ``domain_count`` even when paging is capped.
MAX_PAGES = 5

_CACHE = cache.TTLCache()


async def _get(client: httpx.AsyncClient, url: str) -> dict[str, Any]:
    """GET one InterPro page (cached by full URL), returning the parsed dict."""
    key = cache.make_key("GET", url, "", None)
    cached = _CACHE.get(key)
    if cached is None:
        resp = await _http.request_with_retry(
            client,
            "GET",
            url,
            service="InterPro entry/protein",
            headers={"Accept": "application/json"},
            timeout=DEFAULT_TIMEOUT,
            max_retries=MAX_RETRIES,
        )
        cached = resp.json()
        _CACHE.set(key, cached)
    if not isinstance(cached, dict):
        raise PlantGenomicsError(
            f"InterPro entry/protein returned unexpected payload: {type(cached).__name__}"
        )
    return cached


def _locations(result: dict[str, Any]) -> list[dict[str, int]]:
    """Flatten a result's protein-location fragments to ``[{start, end}]``."""
    out: list[dict[str, int]] = []
    for prot in result.get("proteins") or []:
        for loc in prot.get("entry_protein_locations") or []:
            for frag in loc.get("fragments") or []:
                start, end = frag.get("start"), frag.get("end")
                if start is not None and end is not None:
                    out.append({"start": start, "end": end})
    return out


def _project(result: dict[str, Any]) -> dict[str, Any]:
    """Project one InterPro result row to the surfaced field set."""
    md = result.get("metadata") or {}
    return {
        "accession": md.get("accession"),
        "name": md.get("name"),
        "type": md.get("type"),
        "source_database": md.get("source_database"),
        "interpro": md.get("integrated"),
        "go_terms": md.get("go_terms"),
        "locations": _locations(result),
    }


async def lookup_by_uniprot(client: httpx.AsyncClient, accession: str) -> dict[str, Any]:
    """Fetch InterPro domain / family entries for a UniProt accession.

    Follows pagination up to ``MAX_PAGES``; ``domain_count`` reports the true
    total (from the API's ``count``) even if the row list is page-capped.
    Returns ``found=True`` with an empty list for a protein that has no
    annotated domains. Reusable by synthesis tools with a resolved accession.
    """
    url: str | None = f"{BASE_URL}/interpro/api/entry/all/protein/uniprot/{accession}/"
    domains: list[dict[str, Any]] = []
    total = 0
    pages = 0
    while url and pages < MAX_PAGES:
        page = await _get(client, url)
        total = int(page.get("count") or 0)
        for result in page.get("results") or []:
            if isinstance(result, dict):
                domains.append(_project(result))
        url = page.get("next")
        pages += 1
    by_type = Counter(d["type"] for d in domains if d["type"])
    return {
        "accession": accession,
        "found": True,
        "domain_count": total,
        "truncated": total > len(domains),
        "domains": domains,
        "count_by_type": dict(by_type),
    }


async def lookup_locus(
    client: httpx.AsyncClient,
    locus: str,
    organism: str = organisms.DEFAULT_ORGANISM,
) -> dict[str, Any]:
    """Resolve a locus to UniProt, then fetch its InterPro domain architecture.

    Propagates ``NotFoundError`` when the locus has no UniProt entry, mirroring
    the locus→UniProt→QuickGO path.
    """
    validators.assert_valid_locus(locus, backend="InterPro")
    up = await uniprot.lookup_locus(client, locus, organism=organism)
    accession = up["primaryAccession"]
    result = await lookup_by_uniprot(client, accession)
    return {"locus": locus, **result}
