"""KEGG REST client — async httpx wrapper around rest.kegg.jp.

⚠ License: KEGG REST is academic-use-only. Commercial users require a paid
EULA from Pathway Solutions (https://www.pathway.jp/en/licensing.html).

⚠ Rate limit: KEGG TOS caps requests at 3 per second per IP. We enforce
this client-side via a token-bucket limiter to avoid getting blocked.

Endpoint reference: https://www.kegg.jp/kegg/rest/keggapi.html
Response format is plain text (TSV / flat-file), NOT JSON.
"""

from __future__ import annotations

import asyncio
import time

import httpx

BASE_URL = "https://rest.kegg.jp"
DEFAULT_TIMEOUT = 30.0
RATE_LIMIT_PER_SEC = 3.0


class KeggError(RuntimeError):
    pass


class _RateLimiter:
    """Token bucket — at most RATE_LIMIT_PER_SEC requests per rolling second.

    Module-level singleton (``_LIMITER`` below) so that concurrent client
    instances within the same process all share one bucket. KEGG enforces
    the cap per IP, not per connection.
    """

    def __init__(self, rate: float = RATE_LIMIT_PER_SEC) -> None:
        self._rate = rate
        self._min_interval = 1.0 / rate
        self._lock = asyncio.Lock()
        self._next_allowed: float = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next_allowed = now + self._min_interval


_LIMITER = _RateLimiter()


async def _get_text(client: httpx.AsyncClient, path: str) -> str:
    """GET a KEGG endpoint, returning plain text. Rate-limited."""
    await _LIMITER.acquire()
    resp = await client.get(f"{BASE_URL}{path}", timeout=DEFAULT_TIMEOUT)
    if resp.status_code == 200:
        return resp.text
    if resp.status_code == 404:
        return ""  # KEGG returns 404 for "no result" — represent as empty
    raise KeggError(f"KEGG {path} → HTTP {resp.status_code}: {resp.text[:200]}")


def _parse_tsv(text: str) -> list[dict[str, str]]:
    """KEGG list/find/link/conv responses are TSV: id<TAB>description."""
    rows: list[dict[str, str]] = []
    for line in text.strip().splitlines():
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) >= 2:
            rows.append({"id": parts[0], "value": "\t".join(parts[1:])})
        else:
            rows.append({"id": parts[0], "value": ""})
    return rows


async def find(client: httpx.AsyncClient, database: str, query: str) -> list[dict[str, str]]:
    """Search a KEGG database. database ∈ {pathway, module, ko, genome,
    compound, glycan, reaction, rclass, enzyme, drug, dgroup, brite,
    <organism-code>}."""
    text = await _get_text(client, f"/find/{database}/{query}")
    return _parse_tsv(text)


async def get(client: httpx.AsyncClient, entry_id: str) -> str:
    """Fetch a KEGG entry by ID. Returns the raw flat-file text — callers
    parse based on the entry type (e.g. pathway, gene, compound)."""
    return await _get_text(client, f"/get/{entry_id}")


async def link(
    client: httpx.AsyncClient, target_db: str, source_db_or_entry: str
) -> list[dict[str, str]]:
    """Find linked entries between two KEGG databases. Either two database
    names (e.g. link(pathway, hsa)) or a database + entry id (e.g.
    link(pathway, hsa:7157))."""
    text = await _get_text(client, f"/link/{target_db}/{source_db_or_entry}")
    return _parse_tsv(text)


async def conv(
    client: httpx.AsyncClient, target_db: str, source_db_or_entry: str
) -> list[dict[str, str]]:
    """Convert KEGG IDs to/from external databases (NCBI gene/proteins,
    UniProt, ChEBI, PubChem). Examples:
        conv(ncbi-geneid, hsa)            # all hsa→NCBI mappings
        conv(hsa, ncbi-geneid:7157)       # one NCBI ID → KEGG
    """
    text = await _get_text(client, f"/conv/{target_db}/{source_db_or_entry}")
    return _parse_tsv(text)
