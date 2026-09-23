"""Issue #123, ``no-pagination``: every tool that reports more rows upstream
than it returns hands back a ``next_cursor`` that reaches the rest.

Three upstreams page natively (Europe PMC ``cursorMark``, QuickGO ``page``,
AraGWAS ``offset``); three answer with the whole set in one response
(OrthoDB, Gramene, 1001 Genomes), where a page is an offset into it. Each
mocked test walks to the end and checks that every row arrives exactly once
and that the request carries the upstream's own paging token. The cursor
codec refuses a cursor minted for another tool or query.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import pytest

from plant_genomics_mcp import _http, aragwas, europe_pmc, gramene, onekg, orthodb, quickgo
from plant_genomics_mcp.errors import InvalidArguments

LIVE = os.environ.get("PLANT_GENOMICS_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set PLANT_GENOMICS_MCP_LIVE=1 to run")


# ---------- the codec ----------


def test_a_cursor_continues_only_the_tool_and_query_it_came_from() -> None:
    q = {"locus": "AT1G19850", "limit": 100}
    cur = _http.encode_cursor("gramene_homologs", q, {"offset": 100})
    # Positive control: the same tool and query read their own cursor back.
    assert _http.decode_cursor("gramene_homologs", q, cur) == {"offset": 100}
    assert _http.decode_cursor("gramene_homologs", q, None) == {}

    with pytest.raises(InvalidArguments, match="continues gramene_homologs"):
        _http.decode_cursor("orthodb_orthologs", q, cur)
    with pytest.raises(InvalidArguments, match="continues gramene_homologs"):
        _http.decode_cursor("gramene_homologs", {**q, "locus": "AT1G01010"}, cur)
    with pytest.raises(InvalidArguments, match="not one this server issued"):
        _http.decode_cursor("gramene_homologs", q, "not-a-cursor!")


def test_later_pages_are_truncated_only_while_rows_remain() -> None:
    assert _http.counted(177, range(77), offset=100)["truncated"] is False
    assert _http.counted(177, range(100), offset=0)["truncated"] is True


# ---------- one walk per backend ----------


async def _walk(fetch: Any) -> list[dict[str, Any]]:
    pages, cursor = [], None
    for _ in range(20):
        page = await fetch(cursor)
        pages.append(page)
        cursor = page["next_cursor"]
        if cursor is None:
            return pages
    raise AssertionError("never reached the last page")


@pytest.mark.asyncio
async def test_orthodb_pages_through_the_whole_group(monkeypatch: pytest.MonkeyPatch) -> None:
    genes = [{"gene_id": {"id": f"g{i}", "param": "x"}, "description": "d"} for i in range(5)]

    async def fake(client: Any, path: str, params: dict[str, Any]) -> dict[str, Any]:
        answers: dict[str, dict[str, Any]] = {
            "/current/search": {"data": ["G1"]},
            "/current/group": {"data": {"id": "G1"}},
            "/current/orthologs": {"data": [{"organism": {"name": "o"}, "genes": genes}]},
        }
        return answers[path]

    monkeypatch.setattr(orthodb, "_get", fake)
    async with httpx.AsyncClient() as c:
        pages = await _walk(lambda cur: orthodb.lookup_locus(c, "AT1G19850", limit=2, cursor=cur))
    ids = [m["gene_id"] for p in pages for m in p["members"]]
    assert ids == [f"g{i}" for i in range(5)]
    assert [(p["total"], p["returned"], p["truncated"]) for p in pages] == [
        (5, 2, True),
        (5, 2, True),
        (5, 1, False),
    ]
    async with httpx.AsyncClient() as c:
        with pytest.raises(InvalidArguments):  # a page size change is a different list
            await orthodb.lookup_locus(c, "AT1G19850", limit=3, cursor=pages[0]["next_cursor"])


@pytest.mark.asyncio
async def test_gramene_pages_through_the_homology_set(monkeypatch: pytest.MonkeyPatch) -> None:
    loci = [f"Os0{i}g0100100" for i in range(1, 6)]

    async def fake(client: Any, path: str, **kw: Any) -> list[dict[str, Any]]:
        return [
            {"homology": {"gene_tree": {"id": "T"}, "homologous_genes": {"ortholog_one2one": loci}}}
        ]

    monkeypatch.setattr(gramene, "_get", fake)
    async with httpx.AsyncClient() as c:
        pages = await _walk(
            lambda cur: gramene.lookup_homologs(c, "AT1G19850", limit=2, cursor=cur)
        )
    assert [h["target_locus"].upper() for p in pages for h in p["homologs"]] == [
        x.upper() for x in loci
    ]
    assert pages[-1]["truncated"] is False and pages[0]["truncated"] is True


@pytest.mark.asyncio
async def test_onekg_pages_through_the_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(onekg, "MAX_EFFECTS", 2)
    rows = [[f"chr1:{i}"] for i in range(5)]

    async def fake(client: Any, url: str) -> dict[str, Any]:
        return {"regions": [{"reg_str": "1:1-9"}]} if "gi2coords" in url else {"data": rows}

    monkeypatch.setattr(onekg, "_get", fake)
    async with httpx.AsyncClient() as c:
        pages = await _walk(lambda cur: onekg.lookup_locus(c, "AT1G19850", cursor=cur))
    assert sum(p["returned"] for p in pages) == 5 and len(pages) == 3
    assert [p["truncated"] for p in pages] == [True, True, False]


@pytest.mark.asyncio
async def test_aragwas_resumes_at_its_own_offset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aragwas, "MAX_PAGES", 1)
    urls: list[str] = []

    async def fake(client: Any, url: str) -> dict[str, Any]:
        urls.append(url)
        offset = int(httpx.URL(url).params.get("offset", 0))
        more = offset + 2 < 5
        return {
            "count": 5,
            "results": [{"snp": {"position": i}} for i in range(offset, min(offset + 2, 5))],
            "links": {"next": f"{aragwas.BASE_URL}/api/x/?offset={offset + 2}" if more else None},
        }

    monkeypatch.setattr(aragwas, "_get", fake)
    async with httpx.AsyncClient() as c:
        pages = await _walk(lambda cur: aragwas.lookup_locus(c, "AT1G19850", cursor=cur))
    assert [p["returned"] for p in pages] == [2, 2, 1]
    assert [httpx.URL(u).params.get("offset") for u in urls] == [None, "2", "4"]


@pytest.mark.asyncio
async def test_quickgo_resumes_at_its_own_page(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, Any]] = []

    async def fake(client: Any, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        sent.append(dict(params or {}))
        page = int((params or {}).get("page", 1))
        n = min(2, 5 - (page - 1) * 2)
        return {"numberOfHits": 5, "results": [{"goId": f"GO:{page}{i}"} for i in range(n)]}

    monkeypatch.setattr(quickgo, "_get", fake)
    async with httpx.AsyncClient() as c:
        pages = await _walk(lambda cur: quickgo.lookup_by_uniprot(c, "P93024", limit=2, cursor=cur))
    assert [p["returned"] for p in pages] == [2, 2, 1]
    assert [s.get("page") for s in sent] == [None, 2, 3]


@pytest.mark.asyncio
async def test_europe_pmc_resumes_at_its_own_cursor_mark(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, Any]] = []

    async def fake(client: Any, path: str, **kw: Any) -> dict[str, Any]:
        params = kw["params"]
        sent.append(dict(params))
        start = {"*": 0, "M2": 2, "M4": 4}[params.get("cursorMark", "*")]
        n = min(2, 5 - start)
        return {
            "hitCount": 5,
            "nextCursorMark": f"M{start + 2}",
            "resultList": {"result": [{"id": str(start + i)} for i in range(n)]},
        }

    monkeypatch.setattr(europe_pmc, "_get", fake)
    async with httpx.AsyncClient() as c:
        pages = await _walk(lambda cur: europe_pmc.lookup_locus(c, "AT1G19850", size=2, cursor=cur))
    assert [p["returned"] for p in pages] == [2, 2, 1]
    # The first request is unchanged (no cursorMark); later ones carry the mark.
    assert [s.get("cursorMark") for s in sent] == [None, "M2", "M4"]
    assert pages[-1]["next_cursor"] is None and pages[-1]["truncated"] is False


# ---------- live: walk the real upstreams ----------


@live_only
@pytest.mark.asyncio
async def test_live_every_page_is_new_rows_until_the_total() -> None:
    """AT1G19850 (ARF5): the issue's own capped answers, walked to the end."""
    from plant_genomics_mcp import server
    from plant_genomics_mcp.errors import UpstreamUnavailableError

    async def walk(tool: str, args: dict[str, Any], rows: str, key: Any) -> tuple[int, list[Any]]:
        seen: list[Any] = []
        cursor, total = None, -1
        for _ in range(10):
            call = {**args, **({"cursor": cursor} if cursor else {})}
            try:
                page = await server._dispatch(tool, call)
            except UpstreamUnavailableError:
                # Europe PMC answers ~1 in 6 requests with a body carrying no
                # hitCount, with or without cursorMark (live 2026-09-23); #141
                # made that loud. One more try tests paging, not that fault.
                page = await server._dispatch(tool, call)
            total = page["total"]
            seen += [key(r) for r in page[rows]]
            cursor = page["next_cursor"]
            if cursor is None:
                break
        return total, seen

    L: dict[str, Any] = {"locus": "AT1G19850"}
    cases: list[tuple[str, dict[str, Any], str, Any]] = [
        ("gramene_homologs", L, "homologs", lambda r: r["target_locus"]),
        ("aragwas_associations", L, "associations", lambda r: str(r)),
        ("locus_go_annotations", L, "annotations", lambda r: str(r)),
        ("locus_literature", {**L, "size": 25}, "hits", lambda r: r.get("id") or r.get("pmid")),
    ]
    for tool, args, rows, key in cases:
        total, seen = await walk(tool, args, rows, key)
        assert len(seen) == total, (tool, len(seen), total)
        assert len(set(map(str, seen))) == len(seen), (tool, "a row arrived twice")
