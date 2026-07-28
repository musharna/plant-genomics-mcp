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
import re
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


def _media_type(resp: httpx.Response) -> str:
    """The response's media type, lowercased, without parameters."""
    return resp.headers.get("content-type", "").split(";")[0].strip().lower()


# Headers by which an upstream states the release that produced THIS response.
# Deliberately not a list of /info endpoints: a separate metadata call is a
# DIFFERENT request and may describe a different release than the one that
# answered you, so it can be confidently wrong — the failure mode this codebase
# is most prone to. Only a header on the answering response is true by
# construction. Probed live 2026-07-28; ensembl, alphafold, quickgo and jaspar
# send nothing, and are honestly null rather than filled in from elsewhere.
_VERSION_HEADERS = ("x-uniprot-release", "interpro-version")


def upstream_version(resp: httpx.Response) -> str | None:
    """The upstream's own release identifier for this response, if it states one.

    ``None`` means "this backend did not tell us", never "no version exists".
    Callers must keep that distinction: a fabricated or inferred version in a
    scientific result is worse than an absent one.
    """
    for h in _VERSION_HEADERS:
        v = resp.headers.get(h)
        if v:
            return v.strip()
    return None


def _is_interposed_html(resp: httpx.Response) -> bool:
    """True when a 200 carries an HTML body that cannot be the requested payload.

    Every backend here serves JSON, XML, plain text or FASTA. HTML on a 200 is
    the signature of something *interposed* between us and the API: a WAF
    bot-challenge, a captive portal, a login wall, a maintenance page. The
    interposer answers 200 because, from its point of view, serving the
    challenge IS success — so status alone cannot distinguish it from data.

    PlantCyc/PMN sits behind Imperva and does exactly this: ``GET
    /{orgid}/xmlquery`` intermittently returns ``200 text/html`` carrying an
    ``_Incapsula_Resource`` challenge instead of ptools-XML. Handed to
    ``ET.fromstring`` that surfaced as ``mismatched tag: line 1, column 356``
    (column 356 is the challenge page's ``</head>``) — an error that blames the
    upstream's data for what is really a blocked request. Every backend that
    calls ``resp.json()`` or parses ``resp.text`` inherits the same confusion,
    which is why this lives here and not in one client.

    NCBI's QBlast is the one legitimate HTML producer in this codebase (the RID
    arrives inside an HTML comment), so it opts out via ``allow_html=True``.

    The decision is made on the BODY, not on ``Content-Type``. v1.19.3 checked
    the header alone and broke ``arabidopsis_natural_variation``: 1001 Genomes
    serves perfectly good JSON under ``Content-Type: text/html; charset=UTF-8``,
    so a valid payload was rejected as a challenge page. That was the same
    mistake as the bug this function exists to fix — trusting a label (there,
    the status code; here, the media type) over the content it describes. Only
    the bytes know what they are, so only the bytes are consulted.
    """
    head = resp.content[:512].lstrip().lstrip(b"\xef\xbb\xbf").lstrip().lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html")


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
    not_found_400_pattern: re.Pattern[str] | None = None,
    allow_html: bool = False,
) -> httpx.Response | Any:
    """Issue ``method url`` with the shared retry + classification policy.

    Returns the raw ``httpx.Response`` on 2xx so callers retain control of
    JSON vs text parsing and per-backend caching. Raises a typed subclass
    of ``PlantGenomicsError`` on terminal failure. Pass
    ``not_found_returns=<sentinel>`` to suppress ``NotFoundError`` on 404
    and return the sentinel instead (KEGG's "no record" idiom).

    ``not_found_400_pattern=<compiled regex>`` covers upstreams that signal an
    unknown identifier with 400 plus a body marker rather than 404 — Ensembl
    answers an unknown gene id with ``400 {"error":"ID '...' not found"}``.
    It is opt-in and body-matched rather than a blanket 400 mapping because
    Ensembl overloads 400 for genuinely malformed requests too (an oversized
    region span), and calling those "not found" would just be a different
    wrong answer.

    A 200 carrying an HTML body is not treated as success — see
    ``_is_interposed_html``. It is retried on the same backoff as 429/5xx
    (the interposed page is typically transient) and, once the budget is
    spent, raises ``UpstreamUnavailableError``. Pass ``allow_html=True`` for
    the rare endpoint that genuinely serves HTML (NCBI QBlast).
    """
    delay = 1.0
    last_status: int | None = None
    last_exc: httpx.TransportError | None = None
    last_html_media: str | None = None
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
                #
                # The content-coding headers MUST be dropped. `aiter_bytes()`
                # yields DECODED bytes, so `body` is already decompressed —
                # carrying `Content-Encoding: gzip` over to the new Response
                # makes httpx decode a second time, and every gzipped upstream
                # (UniProt, Phytozome, InterPro, and everything keyed on the
                # locus->UniProt resolution) dies with
                # "DecodingError: incorrect header check". `Content-Length`
                # likewise describes the compressed wire size, not this body.
                # Headers describe the wire representation; this body is
                # post-decode, so the two must be reconciled here.
                reassembled = httpx.Headers(
                    [
                        (k, v)
                        for k, v in streamed.headers.multi_items()
                        if k.lower() not in ("content-encoding", "content-length")
                    ]
                )
                resp = httpx.Response(
                    status_code=streamed.status_code,
                    headers=reassembled,
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
            last_html_media = None
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
        last_html_media = None

        if resp.status_code == 200:
            if allow_html or not _is_interposed_html(resp):
                return resp
            # An interposed page, not our payload. Retrying is worthwhile
            # rather than merely cosmetic: the challenge is issued per
            # request, so the same URL alternates between a challenge and
            # real data seconds apart. Because this arrives as 200 it never
            # reached _RETRYABLE_STATUSES before, making a transient block a
            # hard first-attempt failure.
            last_html_media = _media_type(resp) or "no content-type"
            if attempt < max_retries - 1:
                retry_after = min(delay, _RETRY_AFTER_CAP)
                await progress.notify(
                    f"{service}: HTTP 200 but {last_html_media} (interposed page, "
                    f"not payload), retrying in {retry_after:.1f}s "
                    f"(attempt {attempt + 2}/{max_retries})"
                )
                await asyncio.sleep(retry_after)
                delay *= 2
                continue
            break

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
        # Some upstreams signal an unknown identifier with 400 + a body marker
        # instead of 404. Ensembl is one: an unknown gene id returns
        # `400 {"error":"ID 'AT1G01010' not found"}`. Without this, callers
        # catching NotFoundError to separate "no such gene" from "the backend
        # is broken" cannot — both arrive as PlantGenomicsError.
        #
        # It is opt-in and pattern-matched rather than a blanket 400 mapping
        # because Ensembl OVERLOADS 400: an oversized region range also returns
        # 400 (ensembl_plants.region_query documents this). Mapping every 400 to
        # NotFoundError would tell a caller "no such gene" when their span was
        # simply too wide — trading one wrong type for another.
        if (
            resp.status_code == 400
            and not_found_400_pattern is not None
            and not_found_400_pattern.search(resp.text)
        ):
            raise NotFoundError(f"{service} → HTTP 400 (not found): {resp.text[:200]}")
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
    if last_html_media is not None:
        # Name the real problem. The pre-fix path let this body reach the
        # caller's parser, so the user saw an XML/JSON syntax error and would
        # reasonably conclude the upstream's *data* was corrupt.
        raise UpstreamUnavailableError(
            f"{service} exhausted {max_retries} retries (HTTP 200 with "
            f"{last_html_media}, not the requested data — the upstream or a "
            "bot-mitigation layer in front of it served a challenge, login or "
            "maintenance page)"
        )
    raise UpstreamUnavailableError(
        f"{service} exhausted {max_retries} retries (last HTTP {last_status})"
    )
