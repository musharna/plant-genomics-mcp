"""UniProt ID mapping — a batch of gene/transcript/protein IDs to every UniProtKB entry.

``resolve_locus_to_uniprot`` answers one locus with one entry: UniProt's
first search hit, reviewed preferred. A gene with several entries (maize
Zm00001eb181120 has four TrEMBL entries, live 2026-10-26) loses the rest, and
a question asking for "every UniProtKB accession UniProt's ID mapping returns"
cannot be answered from it. This module drives UniProt's own ID mapping
service, the batch job its website runs:

1. ``POST /idmapping/run`` with ``from``, ``to=UniProtKB`` and the IDs; the
   answer is a ``jobId``.
2. ``GET /idmapping/status/{jobId}``: ``{"jobStatus": "RUNNING"}`` (or
   ``NEW``) while it runs, then a 303 to the results once it has finished
   (live 2026-10-26; a 200 ``FINISHED`` is read the same way).
3. ``GET /idmapping/uniprotkb/results/{jobId}``, paged by ``Link: rel="next"``.
   Its ``failedIds`` lists every input that mapped to nothing. The
   ``results/stream`` endpoint does NOT carry ``failedIds`` (live
   2026-10-26), so an ID that failed would be indistinguishable from one
   never sent; it is not used.

Each input gets ``mapped`` (one or more accessions) or ``unmapped`` (in
``failedIds``). An input UniProt reports neither way is an error, not a
silent gap.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

import httpx

from plant_genomics_mcp import _http, cache, uniprot
from plant_genomics_mcp.errors import InvalidArguments, PlantGenomicsError, UpstreamUnavailableError

BASE_URL = "https://rest.uniprot.org/idmapping"
SERVICE = "UniProt ID mapping"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3
MAX_IDS = 500
PAGE_SIZE = 500
# 20 pages of 500 rows: a 500-ID batch averaging 20 entries per ID. A job that
# needs more is refused rather than cut short.
MAX_PAGES = 20
POLL_BUDGET_S = 120.0
_POLL_INTERVALS = (0.5, 1.0, 1.0, 2.0, 2.0, 3.0)

# Source databases a plant gene, transcript or protein ID can come from, as
# UniProt names them (/configure/idmapping/fields, live 2026-10-26).
FROM_DBS = ("Ensembl_Genomes", "Ensembl_Genomes_Transcript", "Ensembl_Genomes_Protein", "Araport")
TO_DB = "UniProtKB"

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")
_CACHE = cache.TTLCache()
_sleep = asyncio.sleep


def _check(ids: list[str], from_db: str) -> list[str]:
    if from_db not in FROM_DBS:
        raise InvalidArguments(f"from_db must be one of {list(FROM_DBS)}, got {from_db!r}")
    if not isinstance(ids, list) or not ids:
        raise InvalidArguments("ids must be a non-empty list of identifiers")
    if len(ids) > MAX_IDS:
        raise InvalidArguments(f"at most {MAX_IDS} ids per call, got {len(ids)}")
    out: list[str] = []
    bad = [repr(i) for i in ids if not isinstance(i, str) or not _ID_RE.match(i.strip())]
    if bad:
        raise InvalidArguments(
            "each id is one identifier (letters, digits, . _ : -; no spaces or commas); "
            f"not one: {', '.join(bad[:5])}"
        )
    for raw in ids:
        value = raw.strip()
        if value not in out:
            out.append(value)
    return out


def _object(resp: httpx.Response, what: str) -> dict[str, Any]:
    body = _http.json_body(resp, SERVICE)
    if not isinstance(body, dict):
        raise PlantGenomicsError(f"{SERVICE} {what} returned {type(body).__name__}, not an object")
    return body


async def _submit(client: httpx.AsyncClient, ids: list[str], from_db: str) -> str:
    resp = await _http.request_with_retry(
        client,
        "POST",
        f"{BASE_URL}/run",
        service=SERVICE,
        data={"from": from_db, "to": TO_DB, "ids": ",".join(ids)},
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
    )
    job = _object(resp, "run").get("jobId")
    if not isinstance(job, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", job):
        raise PlantGenomicsError(f"{SERVICE} run returned no usable jobId: {job!r}")
    return job


async def _wait(client: httpx.AsyncClient, job: str) -> None:
    deadline = time.monotonic() + POLL_BUDGET_S
    polls = 0
    while True:
        resp = await _http.request_with_retry(
            client,
            "GET",
            f"{BASE_URL}/status/{job}",
            service=SERVICE,
            headers={"Accept": "application/json"},
            timeout=DEFAULT_TIMEOUT,
            max_retries=MAX_RETRIES,
            see_other_ok=True,
        )
        if resp.status_code == 303:
            return
        body = _object(resp, "status")
        status = body.get("jobStatus")
        if status == "FINISHED" or "results" in body:
            return
        if status not in ("NEW", "RUNNING"):
            raise UpstreamUnavailableError(
                f"{SERVICE} job {job} ended {status!r}: {str(body.get('errors') or body)[:300]}"
            )
        if time.monotonic() >= deadline:
            raise UpstreamUnavailableError(
                f"{SERVICE} job {job} still {status} after {POLL_BUDGET_S:.0f} s; "
                "this is not an empty mapping"
            )
        await _sleep(_POLL_INTERVALS[min(polls, len(_POLL_INTERVALS) - 1)])
        polls += 1


async def _results(client: httpx.AsyncClient, job: str) -> tuple[list[Any], list[Any], Any, Any]:
    rows: list[Any] = []
    failed: list[Any] = []
    obsolete: Any = None
    release: Any = None
    cursor: str | None = None
    for _page in range(MAX_PAGES):
        params = {"format": "json", "fields": "accession", "size": str(PAGE_SIZE)}
        if cursor is not None:
            params["cursor"] = cursor
        resp = await _http.request_with_retry(
            client,
            "GET",
            f"{BASE_URL}/uniprotkb/results/{job}",
            service=SERVICE,
            params=params,
            headers={"Accept": "application/json"},
            timeout=DEFAULT_TIMEOUT,
            max_retries=MAX_RETRIES,
        )
        body = _object(resp, "results")
        page = body.get("results")
        if not isinstance(page, list):
            raise PlantGenomicsError(f"{SERVICE} results is {type(page).__name__}, not a list")
        rows.extend(page)
        # failedIds and obsoleteCount ride on the first page only.
        if cursor is None:
            failed = body.get("failedIds") or []
            obsolete = body.get("obsoleteCount")
            release = _http.upstream_version(resp)
        cursor = uniprot._next_cursor(resp.headers.get("link"))
        if cursor is None:
            return rows, failed, obsolete, release
    raise PlantGenomicsError(
        f"{SERVICE} job {job} has more than {MAX_PAGES * PAGE_SIZE} rows; send fewer ids per call"
    )


def _accession(row: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(row, dict) or not isinstance(row.get("to"), dict):
        raise PlantGenomicsError(f"{SERVICE} row is not {{from, to: {{...}}}}: {str(row)[:120]}")
    source, entry = row.get("from"), row["to"]
    accession = entry.get("primaryAccession")
    if not isinstance(source, str) or not isinstance(accession, str):
        raise PlantGenomicsError(f"{SERVICE} row lacks from or primaryAccession: {str(row)[:120]}")
    entry_type = entry.get("entryType")
    return source, {
        "accession": accession,
        "entry_type": entry_type,
        "reviewed": isinstance(entry_type, str) and "reviewed (Swiss-Prot)" in entry_type,
    }


async def map_ids(
    client: httpx.AsyncClient,
    ids: list[str],
    from_db: str = "Ensembl_Genomes",
) -> dict[str, Any]:
    """Map ``ids`` from ``from_db`` to every UniProtKB entry UniProt's ID mapping returns."""
    queries = _check(ids, from_db)
    key = cache.make_key("POST", BASE_URL, "/run", {"from": from_db, "ids": ",".join(queries)})
    page = _CACHE.get(key)
    if page is None:
        job = await _submit(client, queries, from_db)
        await _wait(client, job)
        rows, failed, obsolete, release = await _results(client, job)
        page = {"rows": rows, "failed": failed, "obsolete": obsolete, "release": release}
        _CACHE.set(key, page)
    by_id: dict[str, list[dict[str, Any]]] = {}
    for row in page["rows"]:
        source, accession = _accession(row)
        by_id.setdefault(source, [])
        if accession["accession"] not in {a["accession"] for a in by_id[source]}:
            by_id[source].append(accession)
    failed_ids = {f for f in page["failed"] if isinstance(f, str)}
    results: list[dict[str, Any]] = []
    neither: list[str] = []
    for query in queries:
        if query in by_id:
            results.append({"query": query, "status": "mapped", "accessions": by_id[query]})
        elif query in failed_ids:
            results.append({"query": query, "status": "unmapped", "accessions": []})
        else:
            neither.append(query)
    unasked = sorted(set(by_id) - set(queries))
    if neither or unasked:
        raise PlantGenomicsError(
            f"{SERVICE} answered neither mapped nor failed for {neither[:5]}"
            + (f" and mapped ids never sent {unasked[:5]}" if unasked else "")
        )
    return {
        "from_db": from_db,
        "to_db": TO_DB,
        "count": len(results),
        "mapped": sum(r["status"] == "mapped" for r in results),
        "unmapped": sum(r["status"] == "unmapped" for r in results),
        "obsolete_count": page["obsolete"] if isinstance(page["obsolete"], int) else None,
        "results": results,
        "upstream_version": page["release"],
    }
