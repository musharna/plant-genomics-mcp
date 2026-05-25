"""KEGG pathway-membership backend — async httpx wrapper around rest.kegg.jp.

KEGG REST returns plain TSV-like text, not JSON. Two-call sequence:

  1. GET /link/pathway/<org>:<locus>
     → per-line ``<org>:<locus>\\tpath:<org>{NNNNN}\\n`` (empty body = not found)
  2. For each pathway ID, GET /get/path:<org>{NNNNN}
     → multi-line record with NAME and CLASS rows

KEGG v118.0 (May 2026) made ``/link/pathway`` case-sensitive on the locus
side — uppercase AGI loci (``ath:AT1G01010``) return rows, lowercase
returns empty. We preserve the caller's case verbatim and do not down-case.

KEGG is free for academic use; no API key. ``caller_identity`` parameter
is not supported by KEGG REST (unlike STRING / NCBI BLAST). The 24h cache
TTL is conservative; KEGG mirrors update weekly at most.

If a per-pathway GET fails (404, timeout, …), we skip that pathway and
append the failure to ``errors[]`` rather than aborting the whole call —
partial pathway metadata is more useful than nothing.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from plant_genomics_mcp import _http, cache, organisms, validators
from plant_genomics_mcp.errors import (
    NotFoundError,
    PlantGenomicsError,
)

BASE_URL = "https://rest.kegg.jp"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3
CACHE_TTL_SECONDS = 86400.0  # 24h

_CACHE = cache.TTLCache(default_ttl=CACHE_TTL_SECONDS)


def _normalize_locus_for_ensembl(locus: str, organism_canonical: str) -> str:
    """Transform community-locus IDs into the form Ensembl Plants indexes.

    Soybean: SoyBase ``Glyma.04G220900`` → Ensembl ``GLYMA_04G220900``.
    Other organisms (and soybean inputs without the ``Glyma.`` prefix):
    pass-through. Literal prefix swap, no regex.

    Scoped to the KEGG→Entrez bridge — ``ensembl_plants.lookup_xrefs`` is
    exposed as its own MCP tool with other callers; silently rewriting
    locus there would surprise consumers who pass either form intentionally.
    """
    if organism_canonical == "glycine_max" and locus.startswith("Glyma."):
        return "GLYMA_" + locus[len("Glyma.") :]
    return locus


async def _get(client: httpx.AsyncClient, path: str) -> str:
    """GET a KEGG endpoint with retry. Returns response body as text.

    KEGG returns text/plain (TSV-like for /link, multi-record for /get).
    KEGG also returns 404 with an empty body for unknown IDs; we treat
    that as "no record" and return "" rather than raising NotFoundError.
    """
    key = cache.make_key("GET", BASE_URL, path)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    result = await _http.request_with_retry(
        client,
        "GET",
        f"{BASE_URL}{path}",
        service=f"KEGG {path}",
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
        not_found_returns="",
    )
    body = result if isinstance(result, str) else result.text
    _CACHE.set(key, body)
    return body


def _parse_link_pathway(body: str, gene_id: str) -> list[str]:
    """Extract pathway IDs from the /link/pathway response.

    Each line is ``<org>:<locus>\\tpath:<org>NNNNN``. We pull the second
    column and strip the ``path:`` prefix.
    """
    pathway_ids: list[str] = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        if parts[0] != gene_id:
            continue
        pid = parts[1].removeprefix("path:")
        if pid:
            pathway_ids.append(pid)
    return pathway_ids


def _parse_pathway_record(body: str) -> dict[str, str]:
    """Extract NAME and CLASS from a /get/path record.

    KEGG flat-file records start each row with a 12-char left-justified
    keyword followed by the value. Continuation lines start with spaces.
    """
    fields: dict[str, list[str]] = {}
    current: str | None = None
    for line in body.splitlines():
        if not line:
            continue
        head = line[:12].strip()
        rest = line[12:].rstrip()
        if head:
            current = head
            fields.setdefault(current, []).append(rest)
        elif current:
            fields[current].append(rest.strip())
    name = " ".join(fields.get("NAME", [])).strip()
    pathway_class = "; ".join(fields.get("CLASS", [])).strip()
    return {"name": name, "pathway_class": pathway_class}


async def lookup_pathways(
    client: httpx.AsyncClient, locus: str, *, organism: str | int
) -> dict[str, Any]:
    """Fetch KEGG pathway memberships for ``locus`` in ``organism``.

    v1.1.0 BREAKING: ``organism`` is keyword-only and required. The
    organism is resolved through ``organisms.kegg_org_code_for`` to a
    3-letter KEGG org code (``ath``, …) and spliced into ``<code>:<locus>``
    (case preserved — KEGG v118+ is case-sensitive) for the /link/pathway
    and /get/path calls. Organisms with no KEGG code in the matrix raise
    :class:`OrganismNotSupported` before any HTTP fires.

    KEGG identifier-namespace caveat (v1.1.0): KEGG accepts AGI loci
    (``AT1G01010``) for ``ath`` but uses NCBI Entrez Gene IDs (numeric)
    for ``osa``/``zma``/``gmx``/etc. — RAP-DB / MaizeGDB / Phytozome locus
    IDs return empty from KEGG. Until an internal Entrez bridge lands,
    only Arabidopsis has ``kegg_org_code`` populated; all other matrix
    entries return ``OrganismNotSupported(backend='kegg', ...)`` so the
    failure is honest at resolution time, not a silent NotFoundError.

    Two-call sequence; per-pathway metadata fetches run via gather. If
    KEGG step-2 fails for a pathway, we surface the ID in ``pathways[]``
    with empty name/class and append the message to ``errors[]``.
    """
    validators.assert_valid_locus(locus, backend="KEGG")
    org_code = organisms.kegg_org_code_for(organism)
    gene_id = f"{org_code}:{locus}"
    body = await _get(client, f"/link/pathway/{gene_id}")
    if not body.strip():
        raise NotFoundError(f"KEGG: no pathway memberships for {locus} ({org_code} gene db)")
    pathway_ids = _parse_link_pathway(body, gene_id)
    if not pathway_ids:
        raise NotFoundError(f"KEGG: response had no pathway IDs for {locus}")

    pathways: list[dict[str, Any]] = []
    errors: list[str] = []

    async def _one(pid: str) -> tuple[str, dict[str, str] | str]:
        try:
            record = await _get(client, f"/get/path:{pid}")
        except PlantGenomicsError as e:
            return pid, str(e)
        if not record.strip():
            return pid, "[NotFoundError] empty record from /get/path"
        return pid, _parse_pathway_record(record)

    raw = await asyncio.gather(*[_one(pid) for pid in pathway_ids])
    for pid, outcome in raw:
        if isinstance(outcome, str):
            pathways.append({"id": pid, "name": "", "pathway_class": ""})
            errors.append(f"{pid}: {outcome}")
        else:
            pathways.append({"id": pid, **outcome})

    return {
        "locus": locus,
        "kegg_gene_id": gene_id,
        "pathways": pathways,
        "errors": errors,
    }
