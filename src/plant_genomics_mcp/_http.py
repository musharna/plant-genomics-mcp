"""Shared HTTP retry helper for backend clients.

Before Wave D, 9 backends each carried a copy of the same
429/5xx-retry + Retry-After-cap + progress-notify + status-to-typed-
exception loop. ``request_with_retry`` is the single canonical version;
backend modules now wrap it with their own URL/JSON/cache concerns.

Per Wave B2, ``Retry-After`` is capped at 60s so a hostile upstream
returning ``Retry-After: 3600`` cannot pin the agent for an hour.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import httpx

from plant_genomics_mcp import progress
from plant_genomics_mcp.errors import (
    NotFoundError,
    PlantGenomicsError,
    RateLimitError,
    UpstreamUnavailableError,
)

_RAISE = object()
_RETRY_AFTER_CAP = 60.0
_RETRYABLE_STATUSES = (429, 500, 502, 503, 504)


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    service: str,
    params: Mapping[str, Any] | None = None,
    data: Any = None,
    json: Any = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 30.0,
    max_retries: int = 3,
    not_found_returns: Any = _RAISE,
) -> httpx.Response | Any:
    """Issue ``method url`` with the shared retry + classification policy.

    Returns the raw ``httpx.Response`` on 2xx so callers retain control of
    JSON vs text parsing and per-backend caching. Raises a typed subclass
    of ``PlantGenomicsError`` on terminal failure. Pass
    ``not_found_returns=<sentinel>`` to suppress ``NotFoundError`` on 404
    and return the sentinel instead (KEGG's "no record" idiom).
    """
    delay = 1.0
    last_status: int | None = None
    last_exc: httpx.TransportError | None = None
    for attempt in range(max_retries):
        try:
            resp = await client.request(
                method,
                url,
                params=params,
                data=data,
                json=json,
                headers=headers,
                timeout=timeout,
            )
        except httpx.TransportError as exc:
            # Connection-level failures (ConnectTimeout / ConnectError /
            # ReadTimeout / …) are raised before any HTTP status exists, so
            # the status-code branches below never see them. Without this
            # they propagate on the first attempt with zero retries — a
            # single transient blip reaching any backend then hard-fails.
            # Retry them on the same backoff schedule as 429/5xx.
            last_exc = exc
            last_status = None
            if attempt < max_retries - 1:
                retry_after = min(delay, _RETRY_AFTER_CAP)
                await progress.notify(
                    f"{service}: {type(exc).__name__}, retrying in "
                    f"{retry_after:.1f}s (attempt {attempt + 2}/{max_retries})"
                )
                await asyncio.sleep(retry_after)
                delay *= 2
                continue
            break
        last_exc = None
        last_status = resp.status_code

        if resp.status_code == 200:
            return resp

        if resp.status_code == 404 and not_found_returns is not _RAISE:
            return not_found_returns

        if resp.status_code in _RETRYABLE_STATUSES:
            if attempt < max_retries - 1:
                retry_after_hdr = resp.headers.get("Retry-After")
                try:
                    retry_after = float(retry_after_hdr) if retry_after_hdr else delay
                except ValueError:
                    retry_after = delay
                retry_after = min(retry_after, _RETRY_AFTER_CAP)
                await progress.notify(
                    f"{service}: HTTP {resp.status_code}, retrying in "
                    f"{retry_after:.1f}s (attempt {attempt + 2}/{max_retries})"
                )
                await asyncio.sleep(retry_after)
                delay *= 2
                continue
            # Retry budget exhausted on a retryable status — fall through
            # to the post-loop "exhausted" raise so the message reflects
            # that we tried, not that this single response failed.
            break

        if resp.status_code == 404:
            raise NotFoundError(f"{service} → HTTP 404: {resp.text[:200]}")
        if resp.status_code == 429:
            raise RateLimitError(f"{service} rate-limited (HTTP 429): {resp.text[:200]}")
        if resp.status_code in (500, 502, 503, 504):
            raise UpstreamUnavailableError(
                f"{service} → HTTP {resp.status_code}: {resp.text[:200]}"
            )
        raise PlantGenomicsError(f"{service} → HTTP {resp.status_code}: {resp.text[:200]}")

    if last_exc is not None:
        raise UpstreamUnavailableError(
            f"{service} exhausted {max_retries} retries ({type(last_exc).__name__}: {last_exc})"
        ) from last_exc
    if last_status == 429:
        raise RateLimitError(f"{service} exhausted {max_retries} retries (HTTP 429)")
    raise UpstreamUnavailableError(
        f"{service} exhausted {max_retries} retries (last HTTP {last_status})"
    )
