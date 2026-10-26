"""MaizeGDB B73 gene-model translation — RefGen_v4 (Zm00001d) <-> RefGen_v5 (Zm00001eb).

Maize gene IDs changed between B73 assemblies, and the services that answer
for a gene (UniProt, Ensembl Plants release 63) know only the v5 IDs: a v4 ID
looked up there returns no record (benchmark P3, 2026-10-24: the agent tried
Ensembl Plants and UniProt with v4 IDs, got nothing, and declined all 10
questions). MaizeGDB publishes the translation as two plain tables, one per
direction, which no other tool here read.

Each table is a TSV with no header: a source gene model, a tab, and the
target models it translates to, comma-separated, or nothing when the model
has no counterpart in the other assembly (5116 of 38356 v4 rows; live
2025-12-02 file). One source row can name several targets (4027 v4 rows):
a v4 model split or merged in v5. Every line of both files matched the
strict pattern below when checked (2026-10-26), so a line that does not is
an unreadable download, never a row to skip.

The whole table (1.2 MB) is fetched once and cached; every answer carries
the file's URL, ``Last-Modified``, and sha256, so a caller can tell which
release of the translation it got.

Endpoints (free, no key; the directory listing itself answers 403):
``https://download.maizegdb.org/Pan-genes/B73_gene_xref/B73v4_to_B73v5.tsv``
and ``.../B73v5_to_B73v4.tsv``.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

import httpx

from plant_genomics_mcp import _http, cache
from plant_genomics_mcp.errors import InvalidArguments

BASE_URL = "https://download.maizegdb.org/Pan-genes/B73_gene_xref"
SERVICE = "MaizeGDB gene-model translation"
DEFAULT_TIMEOUT = 60.0
MAX_RETRIES = 3
# A lookup is local once the table is cached, so the cap is about the size of
# the answer, not upstream load.
MAX_IDS = 1000

V4_RE = re.compile(r"^Zm00001d\d{6}\Z")
V5_RE = re.compile(r"^Zm00001eb\d{6}\Z")

# direction -> (file, source pattern, target pattern)
DIRECTIONS: dict[str, tuple[str, re.Pattern[str], re.Pattern[str]]] = {
    "v4_to_v5": ("B73v4_to_B73v5.tsv", V4_RE, V5_RE),
    "v5_to_v4": ("B73v5_to_B73v4.tsv", V5_RE, V4_RE),
}
_ASSEMBLY = {"v4_to_v5": ("RefGen_v4", "RefGen_v5"), "v5_to_v4": ("RefGen_v5", "RefGen_v4")}

# A day: MaizeGDB last replaced the v4->v5 file in 2025-12.
_CACHE = cache.TTLCache(max_entries=4, default_ttl=24 * 3600.0)


def _line_re(source: re.Pattern[str], target: re.Pattern[str]) -> re.Pattern[str]:
    s = source.pattern.strip("^").removesuffix(r"\Z")
    t = target.pattern.strip("^").removesuffix(r"\Z")
    return re.compile(rf"^({s})\t((?:{t})(?:,(?:{t}))*)?\Z")


def _parse(resp: httpx.Response) -> dict[str, Any]:
    """Keep the raw table and its provenance; ``_shape`` decides if it is readable."""
    return {
        "body": resp.content,
        "url": str(resp.request.url),
        "last_modified": resp.headers.get("last-modified"),
    }


def _shape(direction: str) -> Any:
    """A table check that runs as ``cached_get``'s ``shape``.

    It must be the shape, not the parse: ``cached_get`` asks once more only
    for a body its shape rejects, and stores nothing until the shape passes.
    A rejection raised from the parse escaped as a raw ``UnreadableBody`` with
    no second request (found by tests/test_maizegdb.py before release).
    """
    _file, source, target = DIRECTIONS[direction]
    line_re = _line_re(source, target)

    def shape(value: object) -> dict[str, Any]:
        if not isinstance(value, dict) or not isinstance(value.get("body"), bytes):
            raise _http.UnreadableBody(f"parsed table is {type(value).__name__}")
        body: bytes = value["body"]
        table: dict[str, list[str]] = {}
        for n, line in enumerate(body.decode("utf-8", errors="replace").splitlines(), 1):
            match = line_re.match(line)
            if match is None:
                raise _http.UnreadableBody(f"line {n} is not '<id>\\t<id>,<id>...': {line[:80]!r}")
            src, targets = match.group(1), match.group(2)
            if src in table:
                raise _http.UnreadableBody(f"line {n} repeats source {src}")
            table[src] = targets.split(",") if targets else []
        if not table:
            raise _http.UnreadableBody("the table has no rows")
        return {
            "table": table,
            "source": {
                "url": value["url"],
                "last_modified": value["last_modified"],
                "sha256": hashlib.sha256(body).hexdigest(),
                "rows": len(table),
                "rows_without_counterpart": sum(1 for v in table.values() if not v),
            },
        }

    return shape


def _check_ids(ids: list[str], source: re.Pattern[str], direction: str) -> list[str]:
    if not isinstance(ids, list) or not ids:
        raise InvalidArguments("ids must be a non-empty list of maize gene IDs")
    if len(ids) > MAX_IDS:
        raise InvalidArguments(f"at most {MAX_IDS} ids per call, got {len(ids)}")
    out: list[str] = []
    bad: list[str] = []
    for raw in ids:
        value = raw.strip() if isinstance(raw, str) else raw
        # Accept any letter case; MaizeGDB writes Zm00001d / Zm00001eb.
        canonical = value[:2].capitalize() + value[2:].lower() if isinstance(value, str) else ""
        if not source.match(canonical):
            bad.append(repr(raw))
        elif canonical not in out:
            out.append(canonical)
    if bad:
        want = "Zm00001d + 6 digits" if direction == "v4_to_v5" else "Zm00001eb + 6 digits"
        raise InvalidArguments(
            f"{direction} takes gene-model IDs ({want}, no transcript suffix); "
            f"not one: {', '.join(bad[:5])}"
        )
    return out


async def translate_gene_models(
    client: httpx.AsyncClient,
    ids: list[str],
    direction: str = "v4_to_v5",
) -> dict[str, Any]:
    """Translate B73 gene-model IDs between RefGen_v4 and RefGen_v5 via MaizeGDB.

    Each result has a ``status``:

    * ``mapped`` — MaizeGDB names one or more ``targets`` (several when the
      model split or merged between assemblies);
    * ``no_counterpart`` — the ID is in MaizeGDB's table with no target: the
      model has no counterpart in the other assembly;
    * ``not_in_table`` — the ID is not in the table at all (a retired or
      mistyped ID, or one from another annotation), so this source says
      nothing about it.
    """
    if direction not in DIRECTIONS:
        raise InvalidArguments(f"direction must be one of {sorted(DIRECTIONS)}, got {direction!r}")
    file, source, _target = DIRECTIONS[direction]
    queries = _check_ids(ids, source, direction)
    data = await _http.cached_get(
        client,
        _CACHE,
        f"{BASE_URL}/{file}",
        service=SERVICE,
        headers={"Accept": "text/tab-separated-values, text/plain, */*"},
        parse=_parse,
        shape=_shape(direction),
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
    )
    table: dict[str, list[str]] = data["table"]
    results: list[dict[str, Any]] = []
    for query in queries:
        targets = table.get(query)
        if targets is None:
            status = "not_in_table"
        elif targets:
            status = "mapped"
        else:
            status = "no_counterpart"
        results.append({"query": query, "status": status, "targets": list(targets or [])})
    from_assembly, to_assembly = _ASSEMBLY[direction]
    return {
        "direction": direction,
        "from_assembly": from_assembly,
        "to_assembly": to_assembly,
        "count": len(results),
        "mapped": sum(r["status"] == "mapped" for r in results),
        "no_counterpart": sum(r["status"] == "no_counterpart" for r in results),
        "not_in_table": sum(r["status"] == "not_in_table" for r in results),
        "results": results,
        "source": dict(data["source"]),
        "upstream_version": data["source"]["last_modified"],
    }
