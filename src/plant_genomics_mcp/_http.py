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
import os
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

# Outbound response-size ceiling (env-tunable). Responses are streamed, so a
# body larger than this is refused BEFORE it is fully buffered (audit L4): a
# declared Content-Length over the cap is rejected without reading the body at
# all, and a chunked / no-Content-Length body is capped mid-read. This bounds
# peak memory against a hostile or buggy upstream. Default 64 MiB comfortably
# fits the largest legitimate payloads (BLAST reports, dense variant /
# coexpression sets).
try:
    _MAX_RESPONSE_BYTES = int(
        os.environ.get("PLANT_GENOMICS_MCP_MAX_RESPONSE_BYTES", str(64 * 1024 * 1024))
    )
except ValueError:
    _MAX_RESPONSE_BYTES = 64 * 1024 * 1024


def _too_large(service: str, detail: str) -> PlantGenomicsError:
    """Build the typed 'response too large' error (shared by both cap checks)."""
    return PlantGenomicsError(
        f"{service} response too large: {detail} exceeds cap {_MAX_RESPONSE_BYTES} "
        "bytes (raise PLANT_GENOMICS_MCP_MAX_RESPONSE_BYTES to allow)"
    )


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
            # Stream so an oversized body is refused BEFORE it is fully buffered:
            # a declared Content-Length over the cap is rejected without reading
            # the body at all; a chunked / no-Content-Length body is capped
            # mid-read. Bounds peak memory against a hostile/buggy upstream (L4).
            async with client.stream(
                method,
                url,
                params=params,
                data=data,
                json=json,
                headers=headers,
                timeout=timeout,
            ) as streamed:
                declared = streamed.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > _MAX_RESPONSE_BYTES:
                    raise _too_large(service, f"{declared} bytes (Content-Length)")
                body = bytearray()
                async for chunk in streamed.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > _MAX_RESPONSE_BYTES:
                        raise _too_large(service, f"{len(body)}+ bytes (streamed)")
                # Reassemble a fully-read Response via the public constructor so
                # callers keep .json()/.text/.status_code/.headers after close.
                resp = httpx.Response(
                    status_code=streamed.status_code,
                    headers=streamed.headers,
                    content=bytes(body),
                    request=streamed.request,
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
