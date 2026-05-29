"""Tiny in-memory TTL + LRU cache for upstream HTTP responses.

Each backend module (``ensembl_plants``, ``europe_pmc``, ``quickgo``,
``phytozome``) instantiates its own :class:`TTLCache` and consults it
inside ``_get`` / ``_post`` before issuing an HTTP request. A hit returns
the cached payload without touching the network; a miss falls through to
the retry-wrapped HTTP call and stores the successful 200 response.

Why per-module caches and not one global: each backend has its own URL
namespace, so collisions across backends are impossible by construction.
Per-module instances also make tests easier — clearing one module's
cache between cases doesn't perturb others.

What is **not** cached:
  - Non-200 responses (a 4xx/5xx is raised, not stored).
  - The retry loop's intermediate failures.
  - Anything when the cache is disabled via env (see Knobs).

Knobs (read once at import):
  ``PLANT_GENOMICS_MCP_CACHE_TTL``      seconds, default 600 (10 min)
  ``PLANT_GENOMICS_MCP_CACHE_SIZE``     max entries per cache, default 256
  ``PLANT_GENOMICS_MCP_CACHE_DISABLED`` any non-empty value → all caches no-op

The cache is process-local — there is no cross-process or persistent
backing store. Restart the MCP server to drop all entries.
"""

from __future__ import annotations

import copy
import json
import os
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


DEFAULT_TTL_SECONDS: float = float(_env_int("PLANT_GENOMICS_MCP_CACHE_TTL", 600))
DEFAULT_MAX_ENTRIES: int = _env_int("PLANT_GENOMICS_MCP_CACHE_SIZE", 256)


def _disabled() -> bool:
    """Read the disabled flag each call so tests can flip it at runtime."""
    return bool(os.environ.get("PLANT_GENOMICS_MCP_CACHE_DISABLED"))


@dataclass
class _Entry:
    value: Any
    expires_at: float  # monotonic seconds


class TTLCache:
    """Fixed-capacity TTL cache with LRU eviction on overflow.

    Single-event-loop safe: built on a standard ``OrderedDict``, no locks.
    Two concurrent ``get(key)`` calls cannot corrupt the structure, but
    two concurrent misses on the same key will each fetch upstream — we
    do NOT coalesce in-flight requests. That's an acceptable tradeoff
    for the batch use case (duplicate loci in a batch are rare and the
    fetch cost is bounded by the retry budget).
    """

    def __init__(
        self,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        default_ttl: float = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._store: OrderedDict[str, _Entry] = OrderedDict()
        self._max = max_entries
        self._ttl = default_ttl
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Any | None:
        """Return cached value, or ``None`` on miss or expiry."""
        if _disabled():
            return None
        entry = self._store.get(key)
        if entry is None:
            self.misses += 1
            return None
        if entry.expires_at <= time.monotonic():
            # Lazy eviction — expired entries are dropped on read, not by sweep.
            del self._store[key]
            self.misses += 1
            return None
        self._store.move_to_end(key)
        self.hits += 1
        # Return an isolated copy so a consumer that mutates the result (or
        # aliases it into tool output) can never corrupt the shared cache entry
        # for the next concurrent reader. Paired with the copy-on-store in
        # ``set`` below, this makes the read-only-cached-value contract uniform
        # across all backends (audit P5) instead of relying on each ``_get``
        # helper to copy defensively.
        return copy.deepcopy(entry.value)

    def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        """Store ``value`` under ``key`` with the given TTL (default = ctor TTL)."""
        if _disabled():
            return
        ttl_s = self._ttl if ttl is None else ttl
        # Store an isolated copy so the caller mutating the object it just
        # cached (e.g. the miss-path ``result`` it also returns) can't reach
        # back into the cache. See the copy-on-read note in ``get``.
        self._store[key] = _Entry(value=copy.deepcopy(value), expires_at=time.monotonic() + ttl_s)
        self._store.move_to_end(key)
        while len(self._store) > self._max:
            # LRU eviction — drop oldest.
            self._store.popitem(last=False)

    def clear(self) -> None:
        self._store.clear()
        self.hits = 0
        self.misses = 0

    def stats(self) -> dict[str, int]:
        return {"hits": self.hits, "misses": self.misses, "size": len(self._store)}


def make_key(
    method: str,
    base_url: str,
    path: str,
    params: dict[str, Any] | None = None,
    body: Any = None,
) -> str:
    """Canonical cache key for an HTTP call.

    Sorts params and JSON-serializes the body with ``sort_keys=True`` so a
    different dict-iteration order produces the same key. We do NOT
    include the client (cookies, headers, base auth) — the assumption is
    that the backend modules are the sole consumers of their cache, and
    they don't vary headers per request.
    """
    parts: list[str] = [method, base_url, path]
    if params:
        # JSON-serialize the sorted (k, v) pairs rather than hand-joining with
        # literal '&'/'=' — a param value containing those separators (e.g. an
        # unvalidated STRING identifier) would otherwise be able to collide with
        # a different param set (audit P6). json.dumps quotes/escapes each token,
        # so the encoding is unambiguous. Stays order-invariant via the sort.
        items = sorted((str(k), str(v)) for k, v in params.items())
        parts.append(json.dumps(items))
    if body is not None:
        parts.append(json.dumps(body, sort_keys=True))
    return "|".join(parts)
