"""KEGG pathway-membership backend — async httpx wrapper around rest.kegg.jp.

KEGG REST returns plain TSV-like text, not JSON. Two-call sequence:

  1. GET /link/pathway/ath:{locus_lowercased}
     → per-line ``ath:{locus}\\tpath:ath{NNNNN}\\n`` (empty body = not found)
  2. For each pathway ID, GET /get/path:ath{NNNNN}
     → multi-line record with NAME and CLASS rows

KEGG is free for academic use; no API key. ``caller_identity`` parameter
is not supported by KEGG REST (unlike STRING / NCBI BLAST). The 24h cache
TTL is conservative; KEGG mirrors update weekly at most.

If a per-pathway GET fails (404, timeout, …), we skip that pathway and
append the failure to ``errors[]`` rather than aborting the whole call —
partial pathway metadata is more useful than nothing.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from plant_genomics_mcp import cache, progress, validators
from plant_genomics_mcp.errors import (
    NotFoundError,
    PlantGenomicsError,
    RateLimitError,
    UpstreamUnavailableError,
)

BASE_URL = "https://rest.kegg.jp"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3
CACHE_TTL_SECONDS = 86400.0  # 24h

_CACHE = cache.TTLCache(default_ttl=CACHE_TTL_SECONDS)


async def _get(client: httpx.AsyncClient, path: str) -> str:
    """GET a KEGG endpoint with retry. Returns response body as text.

    KEGG returns text/plain (TSV-like for /link, multi-record for /get).
    Empty 200 body is valid and signals 'no record' upstream.
    """
    key = cache.make_key("GET", BASE_URL, path)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    delay = 1.0
    last_status: int | None = None
    for attempt in range(MAX_RETRIES):
        resp = await client.get(f"{BASE_URL}{path}", timeout=DEFAULT_TIMEOUT)
        last_status = resp.status_code
        if resp.status_code == 200:
            _CACHE.set(key, resp.text)
            return resp.text
        if resp.status_code == 404:
            # KEGG returns 404 with an empty body for unknown IDs.
            _CACHE.set(key, "")
            return ""
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES - 1:
            retry_after = min(float(resp.headers.get("Retry-After", delay)), 60.0)
            await progress.notify(
                f"KEGG {path}: HTTP {resp.status_code}, retrying in "
                f"{retry_after:.1f}s (attempt {attempt + 2}/{MAX_RETRIES})"
            )
            await asyncio.sleep(retry_after)
            delay *= 2
            continue
        if resp.status_code == 429:
            raise RateLimitError(f"KEGG {path} rate-limited (HTTP 429): {resp.text[:200]}")
        if resp.status_code in (500, 502, 503, 504):
            raise UpstreamUnavailableError(
                f"KEGG {path} → HTTP {resp.status_code}: {resp.text[:200]}"
            )
        raise PlantGenomicsError(f"KEGG {path} → HTTP {resp.status_code}: {resp.text[:200]}")
    if last_status == 429:
        raise RateLimitError(f"KEGG {path} exhausted {MAX_RETRIES} retries (429)")
    raise UpstreamUnavailableError(
        f"KEGG {path} exhausted {MAX_RETRIES} retries (last HTTP {last_status})"
    )


def _parse_link_pathway(body: str, gene_id: str) -> list[str]:
    """Extract pathway IDs from the /link/pathway response.

    Each line is ``ath:atNgNNNNN\\tpath:athNNNNN``. We pull the second
    column and strip the ``path:`` prefix.
    """
    pathway_ids: list[str] = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        if parts[0] != gene_id:
            continue
        pid = parts[1].removeprefix("path:")
        if pid:
            pathway_ids.append(pid)
    return pathway_ids


def _parse_pathway_record(body: str) -> dict[str, str]:
    """Extract NAME and CLASS from a /get/path record.

    KEGG flat-file records start each row with a 12-char left-justified
    keyword followed by the value. Continuation lines start with spaces.
    """
    fields: dict[str, list[str]] = {}
    current: str | None = None
    for line in body.splitlines():
        if not line:
            continue
        head = line[:12].strip()
        rest = line[12:].rstrip()
        if head:
            current = head
            fields.setdefault(current, []).append(rest)
        elif current:
            fields[current].append(rest.strip())
    name = " ".join(fields.get("NAME", [])).strip()
    pathway_class = "; ".join(fields.get("CLASS", [])).strip()
    return {"name": name, "pathway_class": pathway_class}


async def lookup_pathways(client: httpx.AsyncClient, locus: str) -> dict[str, Any]:
    """Fetch KEGG pathway memberships for an Arabidopsis locus.

    Two-call sequence; per-pathway metadata fetches run via gather. If
    KEGG step-2 fails for a pathway, we surface the ID in ``pathways[]``
    with empty name/class and append the message to ``errors[]``.
    """
    validators.assert_valid_locus(locus, backend="KEGG")
    gene_id = f"ath:{locus.lower()}"
    body = await _get(client, f"/link/pathway/{gene_id}")
    if not body.strip():
        raise NotFoundError(f"KEGG: no pathway memberships for {locus} (ath gene db)")
    pathway_ids = _parse_link_pathway(body, gene_id)
    if not pathway_ids:
        raise NotFoundError(f"KEGG: response had no pathway IDs for {locus}")

    pathways: list[dict[str, Any]] = []
    errors: list[str] = []

    async def _one(pid: str) -> tuple[str, dict[str, str] | str]:
        try:
            record = await _get(client, f"/get/path:{pid}")
        except PlantGenomicsError as e:
            return pid, str(e)
        if not record.strip():
            return pid, "[NotFoundError] empty record from /get/path"
        return pid, _parse_pathway_record(record)

    raw = await asyncio.gather(*[_one(pid) for pid in pathway_ids])
    for pid, outcome in raw:
        if isinstance(outcome, str):
            pathways.append({"id": pid, "name": "", "pathway_class": ""})
            errors.append(f"{pid}: {outcome}")
        else:
            pathways.append({"id": pid, **outcome})

    return {
        "locus": locus,
        "kegg_gene_id": gene_id,
        "pathways": pathways,
        "errors": errors,
    }
