"""UniProt REST client — async httpx wrapper around ``rest.uniprot.org``.

Resolves a TAIR-style locus (e.g. ``AT1G01010``) to its canonical UniProtKB
record. This is the entry node for any downstream protein-side workflow:
structure prediction (AlphaFold), domain assignment (InterPro), pathway
mapping (Reactome / PlantCyc subscriber path), variant analysis.

Strategy:

1. Search the ``/uniprotkb/search`` endpoint with
   ``gene:{locus} AND organism_id:{taxon} AND reviewed:true``. Reviewed
   = Swiss-Prot, the curated subset.
2. If zero reviewed hits, drop the ``reviewed:true`` filter and retry —
   many plant species (rice, maize, etc.) have only TrEMBL coverage.
3. If still zero hits, raise ``NotFoundError``.

Endpoint is documented at https://www.uniprot.org/help/api_queries. No auth.
Public users get the same rate-limit budget as everyone else; we retry on
429/5xx the same as the Ensembl client.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from plant_genomics_mcp import cache
from plant_genomics_mcp.errors import (
    NotFoundError,
    PlantGenomicsError,
    RateLimitError,
    UpstreamUnavailableError,
)

BASE_URL = "https://rest.uniprot.org"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3

# Per-module response cache. See plant_genomics_mcp.cache for env knobs.
_CACHE = cache.TTLCache()

# NCBI taxonomy ID for the default species. Mirrors ensembl_plants'
# ``arabidopsis_thaliana`` default — both refer to TAIR's reference genome.
DEFAULT_TAXON_ID = 3702  # Arabidopsis thaliana

# Hints for the most common plant taxa. Unlike Phytozome's KNOWN_ORGANISMS
# these are NCBI taxonomy IDs which are stable identifiers backed by
# https://www.ncbi.nlm.nih.gov/taxonomy. Each one has been verified to
# match the corresponding Ensembl Plants species slug.
KNOWN_TAXA: dict[str, int] = {
    "arabidopsis_thaliana": 3702,
    "oryza_sativa": 39947,  # Oryza sativa subsp. japonica
    "zea_mays": 4577,
    "solanum_lycopersicum": 4081,
    "glycine_max": 3847,
    "sorghum_bicolor": 4558,
    "triticum_aestivum": 4565,
    "hordeum_vulgare": 4513,
    "brachypodium_distachyon": 15368,
}


async def _search(
    client: httpx.AsyncClient,
    query: str,
    *,
    size: int = 1,
) -> list[dict[str, Any]]:
    """Run a single UniProtKB search and return the ``results`` list.

    Retries on 429/5xx with exponential backoff, honors ``Retry-After``.
    Returns an empty list if the search succeeds but matches nothing —
    distinguishing "no hits" from "bad request" is the caller's job.
    """
    params = {"query": query, "format": "json", "size": str(size)}
    key = cache.make_key("GET", BASE_URL, "/uniprotkb/search", params)
    cached = _CACHE.get(key)
    if cached is not None:
        return list(cached)
    headers = {"Accept": "application/json"}
    delay = 1.0
    last_status: int | None = None
    for attempt in range(MAX_RETRIES):
        resp = await client.get(
            f"{BASE_URL}/uniprotkb/search",
            params=params,
            headers=headers,
            timeout=DEFAULT_TIMEOUT,
        )
        last_status = resp.status_code
        if resp.status_code == 200:
            data = resp.json()
            results = list(data.get("results", []))
            _CACHE.set(key, results)
            return results
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES - 1:
            retry_after = float(resp.headers.get("Retry-After", delay))
            await asyncio.sleep(retry_after)
            delay *= 2
            continue
        if resp.status_code == 404:
            raise NotFoundError(f"UniProt search 404: {resp.text[:200]}")
        if resp.status_code == 429:
            raise RateLimitError(f"UniProt search rate-limited (HTTP 429): {resp.text[:200]}")
        if resp.status_code in (500, 502, 503, 504):
            raise UpstreamUnavailableError(
                f"UniProt search → HTTP {resp.status_code}: {resp.text[:200]}"
            )
        raise PlantGenomicsError(f"UniProt search → HTTP {resp.status_code}: {resp.text[:200]}")
    if last_status == 429:
        raise RateLimitError(f"UniProt search exhausted {MAX_RETRIES} retries (429)")
    raise UpstreamUnavailableError(
        f"UniProt search exhausted {MAX_RETRIES} retries (last HTTP {last_status})"
    )


def _normalize(hit: dict[str, Any], locus_query: str) -> dict[str, Any]:
    """Flatten a UniProtKB record into the tool's stable output shape.

    UniProt's JSON is deeply nested. We surface the fields a downstream
    chain step most often needs (accession, ID, recommended name, organism,
    sequence length, gene names) without forcing the caller to walk three
    levels of nested dicts. The full record is NOT included — clients can
    re-fetch from ``https://rest.uniprot.org/uniprotkb/{accession}.json``
    if they need everything.
    """
    accession = hit.get("primaryAccession", "")
    entry_type = hit.get("entryType", "")
    recommended = (
        hit.get("proteinDescription", {})
        .get("recommendedName", {})
        .get("fullName", {})
        .get("value")
    )
    gene_names: list[str] = []
    for g in hit.get("genes", []):
        gn = g.get("geneName", {}).get("value")
        if gn:
            gene_names.append(gn)
    organism = hit.get("organism", {})
    sequence = hit.get("sequence", {})
    return {
        "locus_query": locus_query,
        "primaryAccession": accession,
        "uniProtkbId": hit.get("uniProtkbId", ""),
        "entryType": entry_type,
        # Swiss-Prot is the canonical curated marker. Checking for "reviewed"
        # alone false-positives on "unreviewed (TrEMBL)" (substring collision).
        "reviewed": "swiss-prot" in entry_type.lower(),
        "recommendedName": recommended,
        "geneNames": gene_names,
        "organism": organism.get("scientificName"),
        "taxonId": organism.get("taxonId"),
        "sequenceLength": sequence.get("length"),
        "web_url": f"https://www.uniprot.org/uniprotkb/{accession}" if accession else None,
    }


async def lookup_locus(
    client: httpx.AsyncClient,
    locus: str,
    organism_id: int = DEFAULT_TAXON_ID,
) -> dict[str, Any]:
    """Resolve a locus to its canonical UniProtKB entry.

    ``locus`` is the gene identifier (TAIR ``AT1G01010``, rice
    ``Os01g0100100``, ...). ``organism_id`` is the NCBI taxonomy ID;
    defaults to 3702 (Arabidopsis thaliana). See ``KNOWN_TAXA`` for hints.

    Prefers reviewed (Swiss-Prot) hits; falls back to unreviewed (TrEMBL)
    if no reviewed hit exists. Returns the normalized shape from
    ``_normalize`` — the top hit only.

    Raises ``NotFoundError`` if both searches return zero hits.
    """
    base = f"gene:{locus} AND organism_id:{organism_id}"
    # Pass 1: reviewed only (Swiss-Prot).
    results = await _search(client, f"{base} AND reviewed:true", size=1)
    if not results:
        # Pass 2: drop the reviewed filter; TrEMBL is acceptable.
        results = await _search(client, base, size=1)
    if not results:
        raise NotFoundError(f"UniProt has no entry for gene={locus} organism_id={organism_id}")
    return _normalize(results[0], locus_query=locus)
