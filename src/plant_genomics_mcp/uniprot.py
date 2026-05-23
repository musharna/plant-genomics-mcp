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
import re
from typing import Any

import httpx

from plant_genomics_mcp import cache, organisms, progress
from plant_genomics_mcp.errors import (
    NotFoundError,
    PlantGenomicsError,
    RateLimitError,
    UpstreamUnavailableError,
)

BASE_URL = "https://rest.uniprot.org"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3

# UniProtKB accession syntax — https://www.uniprot.org/help/accession_numbers
# Either the 6-char legacy form (e.g. P12345, Q9LIV2) or the 10-char form
# (e.g. A0A1B2C3D4). We allow an optional trailing `.N` version suffix
# because BLAST text reports emit `Q9FLJ2.1` rather than the bare accession.
_UNIPROT_ACCESSION_RE = re.compile(
    r"^(?:[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2})$"
)


def _looks_like_uniprot_accession(value: str) -> bool:
    """True if ``value`` matches the UniProtKB accession syntax.

    Strips an optional ``.N`` version suffix (BLAST text reports emit
    e.g. ``Q9FLJ2.1``) before matching. Used to dispatch ``lookup_locus``
    between gene-name search and direct-by-accession fetch.
    """
    if not value:
        return False
    base = value.split(".", 1)[0]
    return bool(_UNIPROT_ACCESSION_RE.match(base))


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
            retry_after = min(float(resp.headers.get("Retry-After", delay)), 60.0)
            await progress.notify(
                f"UniProt /uniprotkb/search: HTTP {resp.status_code}, retrying in "
                f"{retry_after:.1f}s (attempt {attempt + 2}/{MAX_RETRIES})"
            )
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


async def _fetch_by_accession(
    client: httpx.AsyncClient,
    accession: str,
) -> dict[str, Any]:
    """Fetch a UniProtKB entry directly by accession.

    Strips any trailing ``.N`` version suffix (UniProt's per-accession
    endpoint expects the bare accession, but BLAST text reports emit
    ``Q9FLJ2.1`` etc.). Retries on 429/5xx mirror the search path.
    Raises ``NotFoundError`` on 404, ``RateLimitError`` on persistent 429.
    """
    bare = accession.split(".", 1)[0]
    url = f"{BASE_URL}/uniprotkb/{bare}.json"
    key = cache.make_key("GET", BASE_URL, f"/uniprotkb/{bare}.json", {})
    cached = _CACHE.get(key)
    if cached is not None:
        return dict(cached)
    headers = {"Accept": "application/json"}
    delay = 1.0
    last_status: int | None = None
    for attempt in range(MAX_RETRIES):
        resp = await client.get(url, headers=headers, timeout=DEFAULT_TIMEOUT)
        last_status = resp.status_code
        if resp.status_code == 200:
            data = resp.json()
            _CACHE.set(key, data)
            return data
        if resp.status_code == 404:
            raise NotFoundError(f"UniProt has no entry for accession={bare!r}")
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES - 1:
            retry_after = min(float(resp.headers.get("Retry-After", delay)), 60.0)
            await progress.notify(
                f"UniProt /uniprotkb/{bare}.json: HTTP {resp.status_code}, retrying in "
                f"{retry_after:.1f}s (attempt {attempt + 2}/{MAX_RETRIES})"
            )
            await asyncio.sleep(retry_after)
            delay *= 2
            continue
        if resp.status_code == 429:
            raise RateLimitError(
                f"UniProt accession fetch rate-limited (HTTP 429): {resp.text[:200]}"
            )
        if resp.status_code in (500, 502, 503, 504):
            raise UpstreamUnavailableError(
                f"UniProt accession fetch → HTTP {resp.status_code}: {resp.text[:200]}"
            )
        raise PlantGenomicsError(
            f"UniProt accession fetch → HTTP {resp.status_code}: {resp.text[:200]}"
        )
    if last_status == 429:
        raise RateLimitError(f"UniProt accession fetch exhausted {MAX_RETRIES} retries (429)")
    raise UpstreamUnavailableError(
        f"UniProt accession fetch exhausted {MAX_RETRIES} retries (last HTTP {last_status})"
    )


async def fetch_sequence(
    client: httpx.AsyncClient,
    accession: str,
) -> str:
    """Fetch the raw amino-acid sequence for a UniProt accession.

    Returns the sequence string (newlines stripped, header line dropped).
    Strips trailing ``.N`` version suffix on the same rationale as
    ``_fetch_by_accession``. Retries on 429/5xx mirror the search path;
    NotFoundError on 404.
    """
    bare = accession.split(".", 1)[0]
    url = f"{BASE_URL}/uniprotkb/{bare}.fasta"
    key = cache.make_key("GET", BASE_URL, f"/uniprotkb/{bare}.fasta", {})
    cached = _CACHE.get(key)
    if cached is not None:
        return str(cached)
    delay = 1.0
    last_status: int | None = None
    for attempt in range(MAX_RETRIES):
        resp = await client.get(url, timeout=DEFAULT_TIMEOUT)
        last_status = resp.status_code
        if resp.status_code == 200:
            lines = resp.text.splitlines()
            seq = "".join(line.strip() for line in lines if not line.startswith(">"))
            _CACHE.set(key, seq)
            return seq
        if resp.status_code == 404:
            raise NotFoundError(f"UniProt has no FASTA for accession={bare!r}")
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES - 1:
            retry_after = min(float(resp.headers.get("Retry-After", delay)), 60.0)
            await progress.notify(
                f"UniProt /uniprotkb/{bare}.fasta: HTTP {resp.status_code}, retrying in "
                f"{retry_after:.1f}s (attempt {attempt + 2}/{MAX_RETRIES})"
            )
            await asyncio.sleep(retry_after)
            delay *= 2
            continue
        if resp.status_code == 429:
            raise RateLimitError(f"UniProt FASTA rate-limited (HTTP 429): {resp.text[:200]}")
        if resp.status_code in (500, 502, 503, 504):
            raise UpstreamUnavailableError(
                f"UniProt FASTA → HTTP {resp.status_code}: {resp.text[:200]}"
            )
        raise PlantGenomicsError(f"UniProt FASTA → HTTP {resp.status_code}: {resp.text[:200]}")
    if last_status == 429:
        raise RateLimitError(f"UniProt FASTA exhausted {MAX_RETRIES} retries (429)")
    raise UpstreamUnavailableError(
        f"UniProt FASTA exhausted {MAX_RETRIES} retries (last HTTP {last_status})"
    )


async def lookup_locus(
    client: httpx.AsyncClient,
    locus: str,
    organism: str | int = organisms.DEFAULT_ORGANISM,
) -> dict[str, Any]:
    """Resolve a locus OR UniProt accession to its UniProtKB entry.

    Two input shapes are accepted:

    * **Gene/locus name** (TAIR ``AT1G01010``, rice ``Os01g0100100``, …) —
      searches ``/uniprotkb/search`` with ``gene:{locus} AND organism_id``.
      Prefers reviewed (Swiss-Prot) hits, falls back to unreviewed (TrEMBL).
    * **UniProt accession** (``Q9LIV2``, ``A0A1B2C3D4``, optionally with a
      trailing ``.N`` version suffix from a BLAST text report) — bypasses
      search and fetches ``/uniprotkb/{accession}.json`` directly. The
      ``organism`` argument is ignored on this path because the
      accession is already organism-scoped.

    ``organism`` accepts a slug (``"arabidopsis_thaliana"``), scientific
    name (``"Arabidopsis thaliana"``), common name, alias, or an explicit
    NCBI taxonomy ID. Resolved via ``organisms.ncbi_taxid_for``. The
    wire-format query field stays ``organism_id:<taxid>`` — UniProt's
    REST API has not renamed it.

    Raises ``NotFoundError`` if the search/fetch returns zero hits.
    """
    if _looks_like_uniprot_accession(locus):
        record = await _fetch_by_accession(client, locus)
        return _normalize(record, locus_query=locus)
    taxid = organisms.ncbi_taxid_for(organism)
    base = f"gene:{locus} AND organism_id:{taxid}"
    # Pass 1: reviewed only (Swiss-Prot).
    results = await _search(client, f"{base} AND reviewed:true", size=1)
    if not results:
        # Pass 2: drop the reviewed filter; TrEMBL is acceptable.
        results = await _search(client, base, size=1)
    if not results:
        raise NotFoundError(f"UniProt has no entry for gene={locus} organism_id={taxid}")
    return _normalize(results[0], locus_query=locus)
