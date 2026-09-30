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
import base64
import contextlib
import json
import os
import re
import weakref
from collections.abc import Callable, Mapping, Sized
from typing import Any, TypeVar, overload

import httpx

from plant_genomics_mcp import cache, progress
from plant_genomics_mcp.errors import (
    InvalidArguments,
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


def stated_count(body: Mapping[str, Any], key: str, *, service: str) -> int:
    """The total an upstream body states under ``key`` — never a default.

    ``int(body.get(key, 0))`` turns a body that carries no count into a count
    of zero: Europe PMC's intermittent ``{"version":"6.9"}`` became "no papers"
    for genes with 22-91 (issue #141). A missing or non-integer count is an
    upstream fault, raised as one.
    """
    value = body.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise UpstreamUnavailableError(
            f"{service} answered without an integer {key!r} "
            f"(got {value!r}); this is not a count of zero"
        )
    return value


def counted(total: int | None, rows: Sized, *, offset: int = 0) -> dict[str, Any]:
    """The count fields every list-returning tool carries (issue #123).

    ``total`` is how many exist upstream for the query as asked, ``returned``
    how many rows this payload holds, ``truncated`` whether more exist AFTER
    this page (``offset`` rows came before it). A backend whose upstream states
    no total (a ranked top-N such as STRING or ATTED) passes ``None``: then
    ``total`` and ``truncated`` are null, meaning unknown, never guessed from
    the page size.
    """
    return {
        "total": total,
        "returned": len(rows),
        "truncated": None if total is None else total > offset + len(rows),
    }


def encode_cursor(tool: str, query: Mapping[str, Any], position: Mapping[str, Any]) -> str:
    """An opaque cursor for the page after this one (issue #123).

    It carries the tool and the query it continues, so a cursor passed back
    to another tool, or with another locus or page size, is refused by
    ``decode_cursor`` instead of silently paging a different list.
    """
    raw = json.dumps({"t": tool, "q": dict(query), "p": dict(position)}, sort_keys=True)
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(tool: str, query: Mapping[str, Any], cursor: str | None) -> dict[str, Any]:
    """The position ``cursor`` encodes, or ``{}`` for the first page."""
    if cursor is None:
        return {}
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        state = json.loads(base64.urlsafe_b64decode(padded.encode()))
        ok = isinstance(state, dict) and isinstance(state.get("p"), dict)
    except (ValueError, TypeError):
        ok = False
    if not ok:
        raise InvalidArguments(f"{tool}: cursor is not one this server issued")
    if state.get("t") != tool or state.get("q") != dict(query):
        raise InvalidArguments(
            f"{tool}: cursor continues {state.get('t')} {state.get('q')}, not {tool} {dict(query)}"
        )
    position: dict[str, Any] = state["p"]
    return position


class UpstreamLimit:
    """At most ``n`` requests in flight to one upstream, across every tool (#153).

    ``batch_locus_call`` fans out at one width for every backend; OrthoDB
    refuses the excess as a rate limit, and so would any upstream that only
    tolerates a few concurrent requests, however the calls reach it. A backend
    module that needs a lower ceiling owns one of these and passes it as
    ``limit=``. The slot is held for one HTTP exchange, not across a retry's
    backoff, so a request that is waiting to retry does not block the rest.

    One semaphore per running event loop: an ``asyncio.Semaphore`` binds to
    the first loop it makes a caller wait on and raises ``RuntimeError`` on
    any other, and a module-level limit outlives loops (pytest runs one per
    test; a host may restart its loop).
    """

    def __init__(self, n: int) -> None:
        if n < 1:
            raise ValueError(f"UpstreamLimit needs n >= 1, got {n}")
        self.n = n
        self._per_loop: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
            weakref.WeakKeyDictionary()
        )

    def _semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        sem = self._per_loop.get(loop)
        if sem is None:
            sem = self._per_loop[loop] = asyncio.Semaphore(self.n)
        return sem

    async def __aenter__(self) -> None:
        await self._semaphore().acquire()

    async def __aexit__(self, *_exc: object) -> None:
        self._semaphore().release()


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
    no_content_ok: bool = False,
    retry_403_pattern: re.Pattern[str] | None = None,
    not_found_404_pattern: re.Pattern[str] | None = None,
    limit: UpstreamLimit | None = None,
) -> httpx.Response | Any:
    """Issue ``method url`` with the shared retry + classification policy.

    Returns the raw ``httpx.Response`` on 2xx so callers retain control of
    JSON vs text parsing and per-backend caching. Raises a typed subclass
    of ``PlantGenomicsError`` on terminal failure.

    A 404 is the service's failure unless the caller shows otherwise: it is
    retried on the 5xx backoff and, once the budget is spent, raised as
    ``UpstreamUnavailableError`` quoting the body. It used to be
    ``NotFoundError`` by default, and 23 calls whose identifier travels in the
    query or body (UniProt search, QuickGO, BLAST, the Ensembl batch POST ...)
    told a caller their gene does not exist whenever the route itself was
    missing: a wrong or retired URL, or Phytozome's BioMart answering Apache's
    404 page for every path (2026-09-29).

    ``not_found_404_pattern=<compiled regex>`` is for an upstream that answers
    an unknown identifier in the path with 404 and a miss body of its own.
    Only a 404 whose body matches is ``NotFoundError`` (or the
    ``not_found_returns`` sentinel, KEGG's "no record" idiom). The pattern is
    the upstream's live miss body, not the status: every one probed also
    answers 404 for a missing route, with a different body (UniProt's miss
    names "Resource not found", its missing route is an nginx page; live
    2026-09-29). ``not_found_returns`` without the pattern is refused, since
    no 404 could reach it.

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

    A 204 No Content is returned (not raised) only with ``no_content_ok=True``,
    for an upstream whose 204 is an answer: InterPro serves 204 with an empty
    body for a protein with no entries (live, 2026-09-22). Elsewhere it stays
    an error, since a caller that parses the body has nothing to parse.

    ``retry_403_pattern=<compiled regex>`` covers upstreams that refuse an
    excess request rate with 403 plus a body marker rather than 429: OrthoDB
    serves an HTML page naming "too high request rate" (#153). A matching 403
    is retried on the 429 backoff and, once the budget is spent, raises
    ``RateLimitError`` quoting the page. Opt-in and body-matched, like
    ``not_found_400_pattern``: any other 403 stays terminal.

    ``limit=<UpstreamLimit>`` caps this upstream's requests in flight; see
    :class:`UpstreamLimit`.
    """
    if not_found_returns is not _RAISE and not_found_404_pattern is None:
        raise ValueError(f"{service}: not_found_returns needs not_found_404_pattern")
    delay = 1.0
    last_refusal: str | None = None
    last_fault: str | None = None
    last_status: int | None = None
    last_exc: httpx.TransportError | None = None
    last_html_media: str | None = None
    for attempt in range(max_retries):
        try:
            # Stream so an oversized body is refused BEFORE it is fully buffered:
            # a declared Content-Length over the cap is rejected without reading
            # the body at all; a chunked / no-Content-Length body is capped
            # mid-read. Bounds peak memory against a hostile/buggy upstream (L4).
            async with (
                limit or contextlib.nullcontext(),
                client.stream(
                    method,
                    url,
                    params=params,
                    data=data,
                    json=json,
                    headers=headers,
                    timeout=timeout,
                ) as streamed,
            ):
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
            last_refusal = None
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
        last_refusal = None
        last_fault = None

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

        if resp.status_code == 204 and no_content_ok:
            return resp

        faulted = resp.status_code == 404
        if faulted:
            if not_found_404_pattern is not None and not_found_404_pattern.search(resp.text):
                if not_found_returns is not _RAISE:
                    return not_found_returns
                raise NotFoundError(f"{service} → HTTP 404 (not found): {resp.text[:200]}")
            last_fault = " ".join(resp.text.split())[:200]

        refused = (
            resp.status_code == 403
            and retry_403_pattern is not None
            and retry_403_pattern.search(resp.text) is not None
        )
        if refused:
            last_refusal = " ".join(resp.text.split())[:200]
        if resp.status_code in _RETRYABLE_STATUSES or refused or faulted:
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
        # 429 and 5xx never get here: they are _RETRYABLE_STATUSES, so they
        # retry or break to the "exhausted" raises below (#96: the raises for
        # them that stood here were unreachable, and their mutants survived).
        raise PlantGenomicsError(f"{service} → HTTP {resp.status_code}: {resp.text[:200]}")

    if last_exc is not None:
        raise UpstreamUnavailableError(
            f"{service} exhausted {max_retries} retries ({type(last_exc).__name__}: {last_exc})"
        ) from last_exc
    if last_status == 429:
        raise RateLimitError(f"{service} exhausted {max_retries} retries (HTTP 429)")
    if last_refusal is not None:
        raise RateLimitError(
            f"{service} exhausted {max_retries} retries (HTTP 403: {last_refusal})"
        )
    if last_fault is not None:
        raise UpstreamUnavailableError(
            f"{service} exhausted {max_retries} retries (HTTP 404: {last_fault})"
        )
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
        f"{service} exhausted {max_retries} retries (last HTTP {last_status})",
        status=last_status,
    )


def json_body(resp: httpx.Response, service: str) -> object:
    """``resp`` parsed as JSON; a body that is not JSON is an upstream fault.

    Every caller asked a JSON API, so a 200 it cannot parse is the service
    failing to deliver, not the caller's mistake: :class:`UpstreamUnavailableError`
    (a :class:`PlantGenomicsError`). PANTHER sent non-JSON 200s in two live
    runs (2026-09-27, and -28 with an empty body on every call); as a plain
    ``PlantGenomicsError`` that read as a regression, not an outage.

    Typed ``object``, not ``Any``: a 200 can carry any JSON value, and a
    caller that reads it as a dict without checking leaks ``AttributeError``
    on an array or a scalar (#96). ``object`` makes mypy refuse that read
    until the caller has narrowed the shape itself.
    """
    try:
        return resp.json()
    except ValueError as e:
        raise UpstreamUnavailableError(f"{service} returned non-JSON: {resp.text[:200]}") from e


class UnreadableBody(PlantGenomicsError):
    """A parsed body that is not an answer: raised by a ``shape`` function.

    :func:`cached_get` catches it, asks once more and stores nothing; it is
    not meant to reach a caller.
    """


def expect_object(value: object) -> dict[str, Any]:
    """``value`` when it is a JSON object, else :class:`UnreadableBody`."""
    if not isinstance(value, dict):
        raise UnreadableBody(f"{type(value).__name__}, not an object")
    return value


def expect_count(key: str) -> Callable[[object], dict[str, Any]]:
    """A shape: a JSON object stating an integer ``key``, else :class:`UnreadableBody`.

    For an upstream whose page states its total (InterPro, AraGWAS). A count
    read after :func:`cached_get` returns was read from a body already stored:
    InterPro's ``{"Error":1040}`` on a 200 then failed the same call for the
    whole TTL. Keys this package stamps on a body (``_upstream_version``) are
    left out of the message.
    """

    def shape(value: object) -> dict[str, Any]:
        body = expect_object(value)
        count = body.get(key)
        if isinstance(count, bool) or not isinstance(count, int):
            shown = {k: v for k, v in body.items() if not str(k).startswith("_")}
            raise UnreadableBody(f"no integer {key!r} in {str(shown)[:200]}")
        return body

    return shape


def expect_fields(**kinds: type) -> Callable[[object], dict[str, Any]]:
    """A shape: a JSON object whose named fields each have the given type.

    ``{}`` passes :func:`expect_object`, and a reader that defaulted a missing
    field (``body.get("results") or []``) took it for the upstream's own "no
    record": "UniProt has no entry", no variants, no motif (#96, 2026-09-28).
    Naming the fields a reader needs makes such a body :class:`UnreadableBody`.
    """

    def shape(value: object) -> dict[str, Any]:
        body = expect_object(value)
        for key, kind in kinds.items():
            if not isinstance(body.get(key), kind):
                raise UnreadableBody(f"no {kind.__name__} {key!r} in {str(body)[:120]}")
        return body

    return shape


def object_rows(value: object) -> list[dict[str, Any]]:
    """``value`` when it is a list of JSON objects, else :class:`UnreadableBody`.

    Narrowing a body to ``list`` leaves its rows ``Any``, and a row that was
    not an object used to be skipped or passed through: VEP answered
    ``found: false`` for a variant it had annotated, STRING answered zero
    partners, and xrefs and region rows broke the tool's schema (#96). No
    live row of these endpoints is anything but an object, so such a row
    means the body is not an answer.
    """
    if not isinstance(value, list):
        raise UnreadableBody(f"{type(value).__name__}, not a list")
    for i, row in enumerate(value):
        if not isinstance(row, dict):
            raise UnreadableBody(f"row {i} is {type(row).__name__}, not an object")
    return value


def expect_page(count: str, rows: str) -> Callable[[object], dict[str, Any]]:
    """A shape: a page stating an integer ``count`` and a list of objects under ``rows``.

    For a paged upstream (AraGWAS, InterPro). Checking the count alone let a
    page whose rows were missing read as zero rows, and a row that was not an
    object was skipped by the reader, so the answer held fewer rows than the
    upstream sent with nothing to say so (#96). Every live row is an object.
    """
    has_count = expect_count(count)

    def shape(value: object) -> dict[str, Any]:
        body = has_count(value)
        try:
            object_rows(body.get(rows))
        except UnreadableBody as e:
            raise UnreadableBody(f"{rows!r}: {e.args[0]}") from None
        return body

    return shape


_T = TypeVar("_T")
_P = TypeVar("_P")


@overload
async def cached_get(
    client: httpx.AsyncClient,
    store: cache.TTLCache,
    url: str,
    *,
    service: str,
    params: Mapping[str, Any] | None = ...,
    headers: Mapping[str, str] | None = ...,
    parse: Callable[[httpx.Response], object] | None = ...,
    shape: Callable[[object], _T],
    **retry: Any,
) -> _T: ...


@overload
async def cached_get(
    client: httpx.AsyncClient,
    store: cache.TTLCache,
    url: str,
    *,
    service: str,
    params: Mapping[str, Any] | None = ...,
    headers: Mapping[str, str] | None = ...,
    parse: Callable[[httpx.Response], _P],
    shape: None = ...,
    **retry: Any,
) -> _P: ...


@overload
async def cached_get(
    client: httpx.AsyncClient,
    store: cache.TTLCache,
    url: str,
    *,
    service: str,
    params: Mapping[str, Any] | None = ...,
    headers: Mapping[str, str] | None = ...,
    parse: None = ...,
    shape: None = ...,
    **retry: Any,
) -> object: ...


async def cached_get(
    client: httpx.AsyncClient,
    store: cache.TTLCache,
    url: str,
    *,
    service: str,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    parse: Callable[[httpx.Response], object] | None = None,
    shape: Callable[[object], object] | None = None,
    **retry: Any,
) -> object:
    """GET ``url`` through ``store``: the one cache contract every backend uses (#96).

    A hit is returned as stored. A miss goes through :func:`request_with_retry`
    (``retry`` carries its timeout, retry budget and not-found options), is
    parsed (``parse``, default :func:`json_body`) and stored under a key made
    of the URL and params. Nothing is stored on a failure.

    ``shape`` checks the parsed body and returns it narrowed to what the
    caller reads, raising :class:`UnreadableBody` for a body that must not be
    served as an answer (#141): such a body is asked for once more, never
    stored, and a second one is :class:`UpstreamUnavailableError`. A check
    made after this returns would run on a body already stored, so one bad
    200 was served back as the same failure for the whole TTL without asking
    upstream again.

    Fourteen backends carried their own copy of these lines; the copies drifted
    (six leaked a raw ``JSONDecodeError`` on a non-JSON body) and no test
    observed their keys. tests/test_cache_contract.py holds this contract.
    """
    key = cache.make_key("GET", url, "", dict(params) if params else None)
    hit = store.get(key)
    if hit is not None:
        return hit
    problem = ""
    for _attempt in range(2 if shape else 1):
        resp = await request_with_retry(
            client, "GET", url, service=service, params=params, headers=headers, **retry
        )
        value = parse(resp) if parse else json_body(resp, service)
        if shape:
            try:
                value = shape(value)
            except UnreadableBody as e:
                problem = e.args[0]
                continue
        store.set(key, value)
        return value
    raise UpstreamUnavailableError(
        f"{service} answered 200 twice without a readable result ({problem}); "
        "this is not a count of zero"
    )
