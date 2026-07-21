"""JASPAR client — locus → UniProt → transcription-factor binding motifs (PFM).

JASPAR (jaspar.elixir.no) is the reference open database of curated,
non-redundant TF binding profiles. It carries 1745 plant matrices, derived from
SELEX / ChIP-seq / PBM / DAP-seq experiments. This is the *cis-regulatory* view:
which DNA motif a transcription factor binds, complementing the protein-side
tools (structure, domains, family).

Why the join is two-step
------------------------
JASPAR indexes matrices by **TF name**, not by locus, and its ``?search=``
filter is *fuzzy*: ``?search=CCA1&tax_id=3702`` returns seven matrices, one of
which (``MA1187.1``) is **RVE4** — a different gene. Reporting that as CCA1's
motif would be a false scientific attribution. So we:

1. resolve the locus to a UniProt accession + gene name(s) via
   ``plant_genomics_mcp.uniprot.lookup_locus`` (the seam quickgo / alphafold /
   interpro / pdbe already use),
2. retrieve *candidates* with ``?search=<geneName>&tax_id=<taxid>``,
3. fetch each candidate's detail and **confirm identity** by testing the
   resolved accession against the matrix's ``uniprot_ids``.

Confirmed matrices land in ``motifs``; name-similarity hits that belong to
another gene are kept separately in ``name_only_matches`` so the distinction
can't be missed by a consumer that ignores a boolean flag.

Do NOT use ``?uniprot_id=``
--------------------------
It looks like a filter and is silently ignored — it returns the full 5935-matrix
set, identical to a nonsense parameter. Only ``search`` and ``tax_id`` actually
filter. (``search`` is empirically case-insensitive despite the API docs saying
otherwise, so one query per gene name suffices.)

Coverage is Arabidopsis-heavy and thin elsewhere (probed 2026-07-21): Arabidopsis
1236 matrices, maize 131, soybean 91, wheat 58, tomato 51, rice 10, Medicago 4,
barley / grape / poplar 2, Brachypodium and sorghum 0. No organism is gated — a
species with no matrices simply yields ``found=False``, a legitimate answer.

Endpoints (free, no key):
``/api/v1/matrix/?search={name}&tax_id={taxid}`` and ``/api/v1/matrix/{id}/``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from plant_genomics_mcp import _http, cache, organisms, uniprot, validators
from plant_genomics_mcp.errors import NotFoundError, PlantGenomicsError

BASE_URL = "https://jaspar.elixir.no"
API_PREFIX = "/api/v1"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3

# Confirmed motifs returned per locus. A TF usually has 1-7 matrices (one per
# assay / JASPAR release), so this is generous; ``motif_count`` reports the true
# total when it bites.
MAX_MOTIFS = 25

# Candidate matrices whose detail we fetch to run the identity check. Bounds the
# per-call HTTP fan-out — an unusually generic gene name could otherwise match
# dozens of profiles.
MAX_CANDIDATES = 25

# Concurrent detail fetches. Small enough to stay polite to a public academic
# endpoint, large enough that the common 1-7 candidate case is ~one round-trip.
_DETAIL_CONCURRENCY = 5

_CACHE = cache.TTLCache()

# IUPAC nucleotide ambiguity codes, keyed by the sorted set of bases they cover.
# Used to render a PFM column that no single base dominates.
_IUPAC: dict[str, str] = {
    "A": "A",
    "C": "C",
    "G": "G",
    "T": "T",
    "AC": "M",
    "AG": "R",
    "AT": "W",
    "CG": "S",
    "CT": "Y",
    "GT": "K",
    "ACG": "V",
    "ACT": "H",
    "AGT": "D",
    "CGT": "B",
    "ACGT": "N",
}

# A base must reach this share of a column's counts to appear in the consensus.
# 0.25 is the uniform-background expectation, so anything at or above it is
# enriched; a column where all four clear it collapses to ``N``.
_CONSENSUS_THRESHOLD = 0.25


def _consensus(pfm: Any) -> str | None:
    """Render a position-frequency matrix as an IUPAC consensus string.

    ``pfm`` is JASPAR's ``{"A": [...], "C": [...], "G": [...], "T": [...]}`` of
    per-position counts. Each column contributes every base holding at least
    ``_CONSENSUS_THRESHOLD`` of that column's total (always at least the top
    base), collapsed through :data:`_IUPAC`. Returns ``None`` if the matrix is
    absent or malformed — a missing consensus is better than a wrong one.
    """
    if not isinstance(pfm, dict):
        return None
    rows: dict[str, list[float]] = {}
    for base in "ACGT":
        row = pfm.get(base)
        if not isinstance(row, list):
            return None
        try:
            rows[base] = [float(v) for v in row]
        except (TypeError, ValueError):
            return None
    width = len(rows["A"])
    if width == 0 or any(len(rows[b]) != width for b in "ACGT"):
        return None
    out: list[str] = []
    for i in range(width):
        counts = {b: rows[b][i] for b in "ACGT"}
        total = sum(counts.values())
        if total <= 0:
            out.append("N")
            continue
        top = max(counts.values())
        selected = [
            b for b in "ACGT" if counts[b] / total >= _CONSENSUS_THRESHOLD or counts[b] == top
        ]
        out.append(_IUPAC.get("".join(selected), "N"))
    return "".join(out)


def _project(detail: dict[str, Any]) -> dict[str, Any]:
    """Project a JASPAR matrix detail record to the surfaced field set.

    Carries the derived ``consensus``/``length`` rather than the raw PFM — the
    matrix itself is available per-id from :func:`lookup_matrix`, and 25 raw
    4×N count matrices would dominate a per-locus payload.
    """
    matrix_id = detail.get("matrix_id")
    consensus = _consensus(detail.get("pfm"))
    return {
        "matrix_id": matrix_id,
        "name": detail.get("name"),
        "collection": detail.get("collection"),
        "base_id": detail.get("base_id"),
        "version": detail.get("version"),
        "tf_class": detail.get("class"),
        "tf_family": detail.get("family"),
        "data_type": detail.get("type"),
        "consensus": consensus,
        "length": len(consensus) if consensus else None,
        "uniprot_ids": detail.get("uniprot_ids") or [],
        "pubmed_ids": detail.get("pubmed_ids") or [],
        "sequence_logo": detail.get("sequence_logo"),
        "web_url": f"{BASE_URL}/matrix/{matrix_id}/" if matrix_id else None,
    }


async def _get_json(client: httpx.AsyncClient, path: str, params: dict[str, str] | None) -> Any:
    """GET a JASPAR API path with retry + caching, returning decoded JSON.

    Returns ``None`` on 404 so callers can distinguish "no such matrix" from a
    transport failure.
    """
    key = cache.make_key("GET", BASE_URL, path, params)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    resp = await _http.request_with_retry(
        client,
        "GET",
        f"{BASE_URL}{path}",
        service=f"JASPAR {path}",
        params=params,
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
        not_found_returns=None,
    )
    if resp is None:
        return None
    data = resp.json()
    _CACHE.set(key, data)
    return data


async def fetch_matrix(client: httpx.AsyncClient, matrix_id: str) -> dict[str, Any] | None:
    """Fetch one JASPAR matrix detail record. ``None`` when JASPAR answers 404."""
    data = await _get_json(client, f"{API_PREFIX}/matrix/{matrix_id}/", None)
    if data is None:
        return None
    if not isinstance(data, dict):
        raise PlantGenomicsError(
            f"JASPAR matrix/{matrix_id} returned unexpected payload: {type(data).__name__}"
        )
    return data


async def _resolve_latest_version(client: httpx.AsyncClient, base_id: str) -> str:
    """Resolve an unversioned base id (``MA0570``) to its newest ``MA0570.3``.

    Exists to route *around* an upstream bug, not merely to add convenience:
    ``/matrix/{base_id}/`` answers **HTTP 500** — not 404 — when the base id is
    unknown (probed 2026-07-21: ``MA1234`` → 200, ``MA9999`` → 500, while the
    versioned ``MA9999.9`` → 404). A 500 would burn the retry budget and surface
    as ``UpstreamUnavailableError`` ("JASPAR is down") for what is really a typo.
    The ``versions/`` endpoint has no such defect — it answers 200 with
    ``count: 0`` for an unknown base id — so we settle existence there and only
    ever fetch a *versioned* detail path.
    """
    data = await _get_json(client, f"{API_PREFIX}/matrix/{base_id}/versions/", None)
    results = data.get("results") if isinstance(data, dict) else None
    versions = [r for r in results if isinstance(r, dict)] if isinstance(results, list) else []
    if not versions:
        raise NotFoundError(f"JASPAR has no matrix with id={base_id!r}")
    newest = max(versions, key=lambda r: r.get("version") or 0)
    resolved = newest.get("matrix_id")
    if not isinstance(resolved, str):
        raise PlantGenomicsError(f"JASPAR versions/{base_id} returned a row with no matrix_id")
    return resolved


async def lookup_matrix(client: httpx.AsyncClient, matrix_id: str) -> dict[str, Any]:
    """Fetch a single matrix by JASPAR id, including the raw PFM counts.

    The drill-down companion to :func:`lookup_locus`, which returns the derived
    consensus but not the matrix. Accepts either a versioned id (``MA0570.1``)
    or a bare base id (``MA0570``), which resolves to the newest version.
    Raises ``NotFoundError`` for an unknown id.
    """
    validators.assert_valid_jaspar_matrix_id(matrix_id, backend="JASPAR")
    if "." not in matrix_id:
        matrix_id = await _resolve_latest_version(client, matrix_id)
    detail = await fetch_matrix(client, matrix_id)
    if detail is None:
        raise NotFoundError(f"JASPAR has no matrix with id={matrix_id!r}")
    result = _project(detail)
    result["species"] = detail.get("species") or []
    result["pfm"] = detail.get("pfm")
    return result


async def _search_candidates(
    client: httpx.AsyncClient, name: str, tax_id: int
) -> list[dict[str, Any]]:
    """Return the lean matrix rows JASPAR's fuzzy name search yields for ``name``.

    Scoped by ``tax_id`` so a plant query can't collect same-named vertebrate
    profiles. The rows carry no ``uniprot_ids`` — identity is settled by the
    per-candidate detail fetch in :func:`lookup_locus`.
    """
    data = await _get_json(
        client,
        f"{API_PREFIX}/matrix/",
        {"search": name, "tax_id": str(tax_id), "page_size": str(MAX_CANDIDATES)},
    )
    if not isinstance(data, dict):
        return []
    results = data.get("results")
    return [r for r in results if isinstance(r, dict)] if isinstance(results, list) else []


def _empty(locus: str, accession: str, tax_id: int, names: list[str]) -> dict[str, Any]:
    """Result for a locus with no confirmed JASPAR profile (the common case)."""
    return {
        "locus": locus,
        "accession": accession,
        "tax_id": tax_id,
        "gene_names_searched": names,
        "found": False,
        "motif_count": 0,
        "truncated": False,
        "motifs": [],
        "name_only_matches": [],
    }


async def lookup_locus(
    client: httpx.AsyncClient,
    locus: str,
    organism: str = organisms.DEFAULT_ORGANISM,
) -> dict[str, Any]:
    """Resolve a locus to UniProt, then find its confirmed JASPAR binding motifs.

    Only matrices whose ``uniprot_ids`` contain the resolved accession are
    reported as ``motifs``. Fuzzy name hits belonging to a different gene are
    returned separately as ``name_only_matches`` — never mixed in.

    ``found=False`` means the gene has no curated profile: either it isn't a
    transcription factor, or its TF family is unprofiled for this species. That
    is a normal outcome, not an error. A locus with no UniProt entry propagates
    ``NotFoundError``.
    """
    validators.assert_valid_locus(locus, backend="JASPAR")
    up = await uniprot.lookup_locus(client, locus, organism=organism)
    accession = up["primaryAccession"]
    # Prefer the taxid on the resolved record: it is authoritative for the actual
    # protein and stays correct when the caller passed a bare UniProt accession
    # (that path ignores ``organism``).
    tax_id = up.get("taxonId") or organisms.ncbi_taxid_for(organism)
    names = [n for n in up.get("geneNames") or [] if n]
    if not names:
        # No gene symbol to search with — JASPAR is name-indexed, so the join
        # has no key. Common for sparsely-annotated TrEMBL entries.
        return _empty(locus, accession, tax_id, names)

    candidates: dict[str, dict[str, Any]] = {}
    for name in names:
        for row in await _search_candidates(client, name, tax_id):
            mid = row.get("matrix_id")
            if isinstance(mid, str) and mid not in candidates:
                candidates[mid] = row
    if not candidates:
        return _empty(locus, accession, tax_id, names)

    # Bounded fan-out: identity can only be settled from the detail record.
    sem = asyncio.Semaphore(_DETAIL_CONCURRENCY)

    async def _detail(mid: str) -> dict[str, Any] | None:
        async with sem:
            return await fetch_matrix(client, mid)

    ids = list(candidates)[:MAX_CANDIDATES]
    details = await asyncio.gather(*(_detail(mid) for mid in ids))

    confirmed: list[dict[str, Any]] = []
    name_only: list[dict[str, Any]] = []
    for mid, detail in zip(ids, details, strict=True):
        if detail is None:
            continue
        accessions = detail.get("uniprot_ids") or []
        if accession in accessions:
            confirmed.append(_project(detail))
        else:
            name_only.append(
                {
                    "matrix_id": mid,
                    "name": detail.get("name"),
                    "uniprot_ids": accessions,
                }
            )
    total = len(confirmed)
    return {
        "locus": locus,
        "accession": accession,
        "tax_id": tax_id,
        "gene_names_searched": names,
        "found": total > 0,
        "motif_count": total,
        "truncated": total > MAX_MOTIFS,
        "motifs": confirmed[:MAX_MOTIFS],
        "name_only_matches": name_only,
    }
