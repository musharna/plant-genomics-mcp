"""#96: one cache contract, asserted for every backend's cached ``_get``.

The nightly mutation run's largest survivor class sits in 14 near-copies of
the same function: build a cache key, return a hit, else fetch through
``_http.request_with_retry``, parse, store. No test observed the key or the
store, so dropping ``params`` from the key, caching an error, or skipping the
store all survived. This file states the contract once and runs it through
every copy:

1. the same request twice reaches upstream once and returns the same value;
2. a request differing only in params, or only in path (or URL), is a
   different key and reaches upstream again;
3. a failed request is not cached: the next identical call goes upstream.

Upstream is ``_http.request_with_retry`` replaced by a recorder; every
backend calls it through the ``_http`` module, so this is the one seam all 14
share. Real HTTP for each backend is covered by its own live tests.
"""

from __future__ import annotations

import importlib
from typing import Any

import httpx
import pytest

from plant_genomics_mcp import _http
from plant_genomics_mcp.errors import UpstreamUnavailableError

BACKENDS_PATH_PARAMS = [
    "atted",
    "bar",
    "ensembl_plants",
    "ensembl_variation",
    "europe_pmc",
    "gramene",
    "orthodb",
    "planteome",
    "quickgo",
    "string_db",
]
BACKENDS_URL = ["aragwas", "interpro", "onekg"]
BACKENDS_PATH = ["kegg"]
ALL = BACKENDS_PATH_PARAMS + BACKENDS_URL + BACKENDS_PATH


class Upstream:
    """Stands in for ``_http.request_with_retry``; counts what reaches it."""

    def __init__(self, text: bool) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.fail_next = False
        self.text = text

    async def __call__(
        self, client: Any, method: str, url: str, *, params: Any = None, **kw: Any
    ) -> httpx.Response:
        self.calls.append((url, dict(params) if params else None))
        if self.fail_next:
            self.fail_next = False
            raise UpstreamUnavailableError(f"fake outage for {url}")
        n = len(self.calls)
        request = httpx.Request(method, url, params=params)
        if self.text:
            return httpx.Response(200, text=f"body {n}", request=request)
        return httpx.Response(200, json={"n": n, "data": [n]}, request=request)


def _requests(name: str) -> tuple[tuple[Any, ...], tuple[Any, ...], tuple[Any, ...]]:
    """(base request, params-only change, path-only change) as ``_get`` args."""
    if name in BACKENDS_URL:
        mod = importlib.import_module(f"plant_genomics_mcp.{name}")
        base = str(getattr(mod, "BASE_URL", "https://example.org")).rstrip("/")
        return (f"{base}/x/?q=1",), (f"{base}/x/?q=2",), (f"{base}/y/?q=1",)
    if name in BACKENDS_PATH:
        return ("/x/1",), ("/x/2",), ("/y/1",)
    return ("/x", {"q": "1"}), ("/x", {"q": "2"}), ("/y", {"q": "1"})


@pytest.fixture(params=ALL)
def backend(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Any:
    mod = importlib.import_module(f"plant_genomics_mcp.{request.param}")
    mod._CACHE.clear()
    upstream = Upstream(text=request.param in BACKENDS_PATH)
    monkeypatch.setattr(_http, "request_with_retry", upstream)
    yield mod, upstream, _requests(request.param)
    mod._CACHE.clear()


@pytest.mark.asyncio
async def test_a_repeat_is_served_from_cache_and_any_change_is_not(backend: Any) -> None:
    mod, upstream, (base, other_params, other_path) = backend
    async with httpx.AsyncClient() as c:
        first = await mod._get(c, *base)
        again = await mod._get(c, *base)
        assert len(upstream.calls) == 1, upstream.calls
        assert again == first
        for changed in (other_params, other_path):
            before = len(upstream.calls)
            fresh = await mod._get(c, *changed)
            assert len(upstream.calls) == before + 1, (changed, upstream.calls)
            assert fresh != first, changed
        # The first answer is still the one cached for the first request.
        assert await mod._get(c, *base) == first
    assert len(upstream.calls) == 3


@pytest.mark.asyncio
async def test_a_failure_is_not_cached(backend: Any) -> None:
    mod, upstream, (base, _, _) = backend
    upstream.fail_next = True
    async with httpx.AsyncClient() as c:
        with pytest.raises(UpstreamUnavailableError, match="fake outage"):
            await mod._get(c, *base)
        # Positive control, same request: it goes upstream again and answers.
        answer = await mod._get(c, *base)
        assert await mod._get(c, *base) == answer
    assert len(upstream.calls) == 2
