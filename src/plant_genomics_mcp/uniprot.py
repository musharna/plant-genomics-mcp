"""UniProt REST client — async httpx wrapper around ``rest.uniprot.org``.

Resolves a TAIR-style locus (e.g. ``AT1G01010``) to its canonical UniProtKB
record. This is the entry node for any downstream protein-side workflow:
structure prediction (AlphaFold), domain assignment (InterPro), pathway
mapping (Reactome / PlantCyc subscriber path), variant analysis.

Strategy:

1. Search the ``/uniprotkb/search`` endpoint with
   ``gene:{locus} AND organism_id:{taxon} AND reviewed:true``. Reviewed
   = Swiss-Prot, the curated subset.
2. If zero reviewed hits, drop the ``reviewed:true`` filter and retry —
   many plant species (rice, maize, etc.) have only TrEMBL coverage.
3. If still zero hits, raise ``NotFoundError``.

Endpoint is documented at https://www.uniprot.org/help/api_queries. No auth.
Public users get the same rate-limit budget as everyone else; we retry on
429/5xx the same as the Ensembl client.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from plant_genomics_mcp import _http, cache, organisms, validators
from plant_genomics_mcp.errors import (
    InvalidArguments,
    NotFoundError,
    PlantGenomicsError,
)

BASE_URL = "https://rest.uniprot.org"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3

# UniProtKB accession syntax — https://www.uniprot.org/help/accession_numbers
# Either the 6-char legacy form (e.g. P12345, Q9LIV2) or the 10-char form
# (e.g. A0A1B2C3D4). We allow an optional trailing `.N` version suffix
# because BLAST text reports emit `Q9FLJ2.1` rather than the bare accession.
_UNIPROT_ACCESSION_RE = re.compile(
    r"^(?:[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2})$"
)


def _looks_like_uniprot_accession(value: str) -> bool:
    """True if ``value`` matches the UniProtKB accession syntax.

    Strips an optional ``.N`` version suffix (BLAST text reports emit
    e.g. ``Q9FLJ2.1``) before matching. Used to dispatch ``lookup_locus``
    between gene-name search and direct-by-accession fetch.
    """
    if not value:
        return False
    base = value.split(".", 1)[0]
    return bool(_UNIPROT_ACCESSION_RE.match(base))


# Per-module response cache. See plant_genomics_mcp.cache for env knobs.
_CACHE = cache.TTLCache()

# NCBI taxonomy ID for the default species. Mirrors ensembl_plants'
# ``arabidopsis_thaliana`` default — both refer to TAIR's reference genome.
DEFAULT_TAXON_ID = 3702  # Arabidopsis thaliana

# Hints for the most common plant taxa. Unlike Phytozome's KNOWN_ORGANISMS
# these are NCBI taxonomy IDs which are stable identifiers backed by
# https://www.ncbi.nlm.nih.gov/taxonomy. Each one has been verified to
# match the corresponding Ensembl Plants species slug.
KNOWN_TAXA: dict[str, int] = {
    "arabidopsis_thaliana": 3702,
    "oryza_sativa": 39947,  # Oryza sativa subsp. japonica
    "zea_mays": 4577,
    "solanum_lycopersicum": 4081,
    "glycine_max": 3847,
    "sorghum_bicolor": 4558,
    "triticum_aestivum": 4565,
    "hordeum_vulgare": 4513,
    "brachypodium_distachyon": 15368,
}


async def _search(
    client: httpx.AsyncClient,
    query: str,
    *,
    size: int = 1,
) -> list[dict[str, Any]]:
    """Run a single UniProtKB search and return the ``results`` list.

    Retries on 429/5xx with exponential backoff, honors ``Retry-After``.
    Returns an empty list if the search succeeds but matches nothing —
    distinguishing "no hits" from "bad request" is the caller's job.
    """
    params = {"query": query, "format": "json", "size": str(size)}
    key = cache.make_key("GET", BASE_URL, "/uniprotkb/search", params)
    cached = _CACHE.get(key)
    if cached is not None:
        return list(cached)
    resp = await _http.request_with_retry(
        client,
        "GET",
        f"{BASE_URL}/uniprotkb/search",
        service="UniProt search",
        params=params,
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
    )
    data = resp.json()
    results = list(data.get("results", []))
    # Carry the release UniProt reported on the response that produced these
    # rows INSIDE the cached value, so a warm hit reports the release it was
    # actually fetched under instead of silently reporting none. Stored on the
    # hit rather than beside it because the cache holds only this list; a
    # sibling entry could expire independently and mismatch its payload.
    if results:
        results[0]["_upstream_version"] = _http.upstream_version(resp)
    _CACHE.set(key, results)
    return results


def _normalize(hit: dict[str, Any], locus_query: str) -> dict[str, Any]:
    """Flatten a UniProtKB record into the tool's stable output shape.

    UniProt's JSON is deeply nested. We surface the fields a downstream
    chain step most often needs (accession, ID, recommended name, organism,
    sequence length, gene names) without forcing the caller to walk three
    levels of nested dicts. The full record is NOT included — clients can
    re-fetch from ``https://rest.uniprot.org/uniprotkb/{accession}.json``
    if they need everything.
    """
    accession = hit.get("primaryAccession", "")
    entry_type = hit.get("entryType", "")
    recommended = (
        hit.get("proteinDescription", {})
        .get("recommendedName", {})
        .get("fullName", {})
        .get("value")
    )
    # This loop owns the ``geneNames: list[str]`` invariant that
    # synthesis._reconcile_analyze consumes (#95): a non-string geneName.value
    # is an upstream contract violation and raises here (→ status="error"
    # step) rather than a TypeError in the reconciler.
    gene_names: list[str] = []
    for g in hit.get("genes", []):
        gn = g.get("geneName", {}).get("value")
        if gn is None:
            continue
        if not isinstance(gn, str):
            raise PlantGenomicsError(
                f"UniProt record {accession!r}: geneName.value must be a string, "
                f"got {type(gn).__name__} {gn!r}"
            )
        if gn:
            gene_names.append(gn)
    organism = hit.get("organism", {})
    sequence = hit.get("sequence", {})
    return {
        "locus_query": locus_query,
        "primaryAccession": accession,
        "uniProtkbId": hit.get("uniProtkbId", ""),
        "entryType": entry_type,
        # Swiss-Prot is the canonical curated marker. Checking for "reviewed"
        # alone false-positives on "unreviewed (TrEMBL)" (substring collision).
        "reviewed": "swiss-prot" in entry_type.lower(),
        "recommendedName": recommended,
        "geneNames": gene_names,
        "organism": organism.get("scientificName"),
        "taxonId": organism.get("taxonId"),
        "sequenceLength": sequence.get("length"),
        "web_url": f"https://www.uniprot.org/uniprotkb/{accession}" if accession else None,
        # UniProt states its release on every response, so this is the release
        # that produced THIS record. None means the header was absent, never
        # that no release exists.
        "upstream_version": hit.get("_upstream_version"),
    }


async def _fetch_by_accession(
    client: httpx.AsyncClient,
    accession: str,
) -> dict[str, Any]:
    """Fetch a UniProtKB entry directly by accession.

    Strips any trailing ``.N`` version suffix (UniProt's per-accession
    endpoint expects the bare accession, but BLAST text reports emit
    ``Q9FLJ2.1`` etc.). Retries on 429/5xx mirror the search path.
    Raises ``NotFoundError`` on 404, ``RateLimitError`` on persistent 429.
    """
    bare = accession.split(".", 1)[0]
    url = f"{BASE_URL}/uniprotkb/{bare}.json"
    key = cache.make_key("GET", BASE_URL, f"/uniprotkb/{bare}.json", {})
    cached = _CACHE.get(key)
    if cached is not None:
        return dict(cached)
    try:
        resp = await _http.request_with_retry(
            client,
            "GET",
            url,
            service="UniProt accession fetch",
            headers={"Accept": "application/json"},
            timeout=DEFAULT_TIMEOUT,
            max_retries=MAX_RETRIES,
        )
    except NotFoundError:
        raise NotFoundError(f"UniProt has no entry for accession={bare!r}") from None
    data = resp.json()
    if isinstance(data, dict):
        data["_upstream_version"] = _http.upstream_version(resp)
    _CACHE.set(key, data)
    return data


async def fetch_sequence(
    client: httpx.AsyncClient,
    accession: str,
) -> str:
    """Fetch the raw amino-acid sequence for a UniProt accession.

    Returns the sequence string (newlines stripped, header line dropped).
    Strips trailing ``.N`` version suffix on the same rationale as
    ``_fetch_by_accession``. Retries on 429/5xx mirror the search path;
    NotFoundError on 404.
    """
    bare = accession.split(".", 1)[0]
    url = f"{BASE_URL}/uniprotkb/{bare}.fasta"
    key = cache.make_key("GET", BASE_URL, f"/uniprotkb/{bare}.fasta", {})
    cached = _CACHE.get(key)
    if cached is not None:
        return str(cached)
    try:
        resp = await _http.request_with_retry(
            client,
            "GET",
            url,
            service="UniProt FASTA",
            timeout=DEFAULT_TIMEOUT,
            max_retries=MAX_RETRIES,
        )
    except NotFoundError:
        raise NotFoundError(f"UniProt has no FASTA for accession={bare!r}") from None
    lines = resp.text.splitlines()
    seq = "".join(line.strip() for line in lines if not line.startswith(">"))
    _CACHE.set(key, seq)
    return seq


async def lookup_locus(
    client: httpx.AsyncClient,
    locus: str,
    organism: str | int = organisms.DEFAULT_ORGANISM,
) -> dict[str, Any]:
    """Resolve a locus OR UniProt accession to its UniProtKB entry.

    Two input shapes are accepted:

    * **Gene/locus name** (TAIR ``AT1G01010``, rice ``Os01g0100100``, …) —
      searches ``/uniprotkb/search`` with ``(gene:{locus} OR
      xref:ensemblplants-{locus}) AND organism_id``.
      Prefers reviewed (Swiss-Prot) hits, falls back to unreviewed (TrEMBL).
    * **UniProt accession** (``Q9LIV2``, ``A0A1B2C3D4``, optionally with a
      trailing ``.N`` version suffix from a BLAST text report) — bypasses
      search and fetches ``/uniprotkb/{accession}.json`` directly. The
      ``organism`` argument is ignored on this path because the
      accession is already organism-scoped.

    ``organism`` accepts a slug (``"arabidopsis_thaliana"``), scientific
    name (``"Arabidopsis thaliana"``), common name, alias, or an explicit
    NCBI taxonomy ID. Resolved via ``organisms.ncbi_taxid_for``. The
    wire-format query field stays ``organism_id:<taxid>`` — UniProt's
    REST API has not renamed it.

    A gene *symbol* (``ARF1``) matches through ``gene:`` too. When the hits
    of the pass that answers name more than one locus and none of them is
    the input, the symbol is shared and ``InvalidArguments`` lists the loci
    instead of answering with whichever hit UniProt ranked first (#128).

    Raises ``NotFoundError`` if the search/fetch returns zero hits.
    """
    if _looks_like_uniprot_accession(locus):
        record = await _fetch_by_accession(client, locus)
        return _normalize(record, locus_query=locus)
    taxid = organisms.ncbi_taxid_for(organism)
    # Issue #138: UniProt carries wheat IWGSC ids (TraesCS3A02G159200) only as
    # EnsemblPlants cross-references, never as gene names, so `gene:` alone
    # missed every wheat locus (live, 2026-09-22: gene: 0 hits, xref: 1).
    # Arabidopsis and rice still answer through `gene:` (ordered-locus names).
    base = f"(gene:{locus} OR xref:ensemblplants-{locus}) AND organism_id:{taxid}"
    # Pass 1: reviewed only (Swiss-Prot).
    results = await _search(client, f"{base} AND reviewed:true", size=SYMBOL_PROBE_SIZE)
    if not results:
        # Pass 2: drop the reviewed filter; TrEMBL is acceptable.
        results = await _search(client, base, size=SYMBOL_PROBE_SIZE)
    if not results:
        raise NotFoundError(f"UniProt has no entry for gene={locus} organism_id={taxid}")
    _refuse_shared_symbol(locus, taxid, results)
    return _normalize(results[0], locus_query=locus)


# Issue #128: `size=1` returned UniProt's first hit for a symbol and hid the
# rest; ARF1 names two reviewed Arabidopsis genes (AT1G59750 auxin response
# factor 1, AT2G47170 ADP-ribosylation factor 1; live, 2026-09-23).
SYMBOL_PROBE_SIZE = 25


def _refuse_shared_symbol(query: str, taxid: int, hits: list[dict[str, Any]]) -> None:
    """Raise when ``hits`` span several loci and ``query`` is none of them.

    A locus query is recognised by appearing among its hits' loci, so an
    entry that lists the queried locus beside another (a tandem duplicate)
    still answers. A symbol whose hits are all one locus (ARF5: two entries,
    both AT1G19850) is unambiguous and answers too.
    """
    loci: list[str] = []
    for hit in hits:
        for value in _member_loci(hit)[0]:
            if value.upper() == query.upper():
                return
            if value not in loci:
                loci.append(value)
    if len(loci) > 1:
        more = " (first page of hits only)" if len(hits) >= SYMBOL_PROBE_SIZE else ""
        raise InvalidArguments(
            f"{query!r} is not a locus id and UniProt matches it to {len(loci)} loci in "
            f"organism_id={taxid}{more}: {', '.join(loci)}. Pass one of them as `locus`."
        )


# ---- issue #124: entry -> member loci ---------------------------------------

# The entry accessions UniProt can filter on as a cross-reference, and the
# xref database each one lives in. PANTHER subfamilies (PTHR31384:SF10) are
# left out: the colon is Lucene syntax in a query term.
_ENTRY_XREF_DB: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^IPR\d{6}\Z"), "interpro"),
    (re.compile(r"^PF\d{5}\Z"), "pfam"),
    (re.compile(r"^PTHR\d{5}\Z"), "panther"),
)
ENTRY_MEMBERS_DEFAULT_PAGE = 100
ENTRY_MEMBERS_MAX_PAGE = 500  # UniProt's documented /search page ceiling
_MEMBER_FIELDS = (
    "accession,reviewed,gene_primary,gene_oln,protein_name,"
    "xref_ensemblplants,xref_araport,xref_tair"
)
_NEXT_LINK = re.compile(r'<([^<>]*)>;\s*rel="next"')


def _entry_xref_db(entry: str) -> str:
    for pattern, db in _ENTRY_XREF_DB:
        if pattern.match(entry):
            return db
    raise InvalidArguments(
        f"entry_members: {entry!r} is not an InterPro (IPR000000), Pfam (PF00000) or "
        "PANTHER family (PTHR00000) accession"
    )


def _member_loci(hit: dict[str, Any]) -> tuple[list[str], str | None]:
    """The member's locus ids, and which cross-reference they came from.

    Preference is the id the server's own locus tools accept: an EnsemblPlants
    xref's GeneId (rice Os04g0664400, wheat TraesCS1A02G156600), else the
    Araport / TAIR AGI Arabidopsis carries instead, else UniProt's
    ordered-locus name (AGIs recased, as every other tool spells them).
    """
    xrefs = [x for x in hit.get("uniProtKBCrossReferences") or [] if isinstance(x, dict)]

    def _ids(database: str, *, gene_id: bool = False) -> list[str]:
        found: list[str] = []
        for x in xrefs:
            if x.get("database") != database:
                continue
            value = x.get("id")
            if gene_id:
                props = {p.get("key"): p.get("value") for p in x.get("properties") or []}
                value = props.get("GeneId")
            if isinstance(value, str) and value and value not in found:
                found.append(value)
        return found

    for source, loci in (
        ("EnsemblPlants", _ids("EnsemblPlants", gene_id=True)),
        ("Araport", _ids("Araport")),
        ("TAIR", _ids("TAIR")),
    ):
        if loci:
            return loci, source
    ordered: list[str] = []
    for gene in hit.get("genes") or []:
        for name in gene.get("orderedLocusNames") or []:
            value = name.get("value")
            if isinstance(value, str) and value:
                value = value.upper() if validators.AGI_RE.match(value) else value
                if value not in ordered:
                    ordered.append(value)
    return (ordered, "ordered_locus_name") if ordered else ([], None)


def _member(hit: dict[str, Any]) -> dict[str, Any]:
    genes = hit.get("genes") or [{}]
    description = hit.get("proteinDescription") or {}
    name = (description.get("recommendedName") or {}).get("fullName") or {}
    if not name:
        submitted = description.get("submissionNames") or [{}]
        name = submitted[0].get("fullName") or {}
    loci, source = _member_loci(hit)
    return {
        "accession": hit.get("primaryAccession"),
        "reviewed": "reviewed" in str(hit.get("entryType", "")).lower()
        and "unreviewed" not in str(hit.get("entryType", "")).lower(),
        "symbol": (genes[0].get("geneName") or {}).get("value"),
        "protein_name": name.get("value"),
        "locus": loci[0] if loci else None,
        "loci": loci,
        "locus_source": source,
    }


def _next_cursor(link_header: str | None) -> str | None:
    """The cursor UniProt's ``Link: <...>; rel="next"`` carries, or None at the end.

    Only the cursor parameter is kept: the next request is rebuilt against
    BASE_URL, so an upstream link can never redirect this server elsewhere.
    """
    if not link_header:
        return None
    match = _NEXT_LINK.search(link_header)
    if not match:
        return None
    values = httpx.URL(match.group(1)).params.get_list("cursor")
    return values[0] if values else None


async def entry_members(
    client: httpx.AsyncClient,
    entry: str,
    organism: str | int = organisms.DEFAULT_ORGANISM,
    *,
    reviewed_only: bool = True,
    page_size: int = ENTRY_MEMBERS_DEFAULT_PAGE,
    cursor: str | None = None,
) -> dict[str, Any]:
    """UniProt proteins cross-referenced to ``entry`` in ``organism``, with loci.

    Issue #124: nothing on this server went from a family or domain accession
    back to genes. One UniProt query, ``xref:<db>-<entry> AND organism_id``,
    answers it with each member's locus inline. ``total`` is UniProt's own
    count for the query; a page that cannot hold it returns ``next_cursor``,
    passed back unchanged to continue.
    """
    db = _entry_xref_db(entry)
    taxid = organisms.ncbi_taxid_for(organism)
    page_size = max(1, min(page_size, ENTRY_MEMBERS_MAX_PAGE))
    query = f"xref:{db}-{entry} AND organism_id:{taxid}"
    if reviewed_only:
        query += " AND reviewed:true"
    params: dict[str, Any] = {
        "query": query,
        "format": "json",
        "fields": _MEMBER_FIELDS,
        "size": str(page_size),
    }
    if cursor is not None:
        params["cursor"] = cursor
    key = cache.make_key("GET", BASE_URL, "/uniprotkb/search#members", params)
    page = _CACHE.get(key)
    if page is None:
        resp = await _http.request_with_retry(
            client,
            "GET",
            f"{BASE_URL}/uniprotkb/search",
            service="UniProt search (entry members)",
            params=params,
            headers={"Accept": "application/json"},
            timeout=DEFAULT_TIMEOUT,
            max_retries=MAX_RETRIES,
        )
        header = resp.headers.get("x-total-results")
        stated = int(header) if header is not None and header.isdigit() else header
        total = _http.stated_count(
            {"x-total-results": stated}, "x-total-results", service="UniProt search"
        )
        results = resp.json().get("results")
        if not isinstance(results, list):
            raise PlantGenomicsError(
                f"UniProt search results is not a list: {type(results).__name__}"
            )
        page = {
            "total": total,
            "results": results,
            "next_cursor": _next_cursor(resp.headers.get("link")),
            "upstream_version": _http.upstream_version(resp),
        }
        _CACHE.set(key, page)
    members = [_member(hit) for hit in page["results"] if isinstance(hit, dict)]
    return {
        "entry": entry,
        "entry_database": db,
        "organism": organisms.resolve(organism).canonical,
        "taxid": taxid,
        "reviewed_only": reviewed_only,
        "query": query,
        "total": page["total"],
        "returned": len(members),
        "truncated": page["next_cursor"] is not None,
        "next_cursor": page["next_cursor"],
        "members": members,
        "upstream_version": page["upstream_version"],
    }
