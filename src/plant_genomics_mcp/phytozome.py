"""Phytozome BioMart client — async httpx wrapper around phytozome-next.jgi.doe.gov.

Phytozome exposes its gene catalog via a BioMart endpoint at
``/biomart/martservice``. The protocol is form-encoded POST whose body
carries a BioMart XML query, and the response is TSV (tab-separated values,
header row present when ``header="1"``). No auth required; public.

Quirks worth knowing:
  * BioMart returns HTTP 200 for **both** success and query errors. Errors are
    response bodies beginning with ``Query ERROR:``. We detect that and raise
    ``PlantGenomicsError``.
  * Zero-row filter matches return only the header line (or empty body).
    Treated as a 404-equivalent here. A real HTTP 404 is therefore never a
    missing locus: it is the endpoint itself gone (Apache's own 404 page at
    every BioMart path on 2026-09-29, site root 200), raised as
    ``UpstreamUnavailableError``.
  * The ``organism_id`` filter is a Phytozome proteome integer ID, NOT a
    species slug. Per-organism IDs live in ``organisms.ORGANISMS`` (the
    ``phytozome_int`` slot); ``organisms.phytozome_int_for()`` raises
    ``OrganismNotSupported`` for records with no Phytozome coverage.

We reuse ``ensembl_plants.PlantGenomicsError`` as the shared error type so
the server dispatch can handle one exception class for all backends.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from plant_genomics_mcp import _http, cache, organisms, progress, validators
from plant_genomics_mcp.errors import (
    NotFoundError,
    PlantGenomicsError,
    UpstreamUnavailableError,
)

BASE_URL = "https://phytozome-next.jgi.doe.gov/biomart/martservice"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3

# Every 404 is the service, never the locus: BioMart answers a miss with 200 and
# its header line alone. Matches any body, so each 404 is retried and then
# raised as UpstreamUnavailableError quoting the page.
ANY_404 = re.compile("")

# Per-module response cache. See plant_genomics_mcp.cache for env knobs.
_CACHE = cache.TTLCache()

_QUERY_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE Query>
<Query virtualSchemaName="zome_mart" header="1" uniqueRows="0" count="" datasetConfigVersion="0.7">
  <Dataset name="phytozome" interface="default">
    <Filter name="organism_id" value="{organism_id}"/>
    <Filter name="gene_name_filter" value="{locus}"/>
    <Attribute name="organism_name"/>
    <Attribute name="gene_name1"/>
    <Attribute name="chr_name1"/>
    <Attribute name="gene_chrom_start"/>
    <Attribute name="gene_chrom_end"/>
    <Attribute name="gene_chrom_strand"/>
    <Attribute name="gene_description"/>
  </Dataset>
</Query>"""

# BioMart's header for the template's attributes, in order (live, 2026-09-28).
_HEADER = "\t".join(
    (
        "Organism Name",
        "Gene Name",
        "Chromosome Name",
        "Gene Start (bp)",
        "Gene End (bp)",
        "Strand",
        "Description",
    )
)

# Output field order MUST match the <Attribute> order in the template.
_FIELDS = (
    "organism_name",
    "gene_name",
    "chromosome",
    "gene_start",
    "gene_end",
    "strand",
    "description",
)


async def _post(client: httpx.AsyncClient, xml_payload: str) -> str:
    """POST the BioMart query, returning raw response text.

    BioMart is the slowest backend in this server — the XML query is
    parsed server-side and can take multiple seconds even for simple
    single-locus lookups. We emit a progress notification before and
    after the POST so clients that opted in see "BioMart still working"
    instead of a silent stall.

    Retries on 429 / 5xx / 404 (see ``ANY_404``) with exponential backoff,
    honoring ``Retry-After``.
    BioMart application-level errors (``Query ERROR:``) are returned as 200
    and surfaced upstream — they are NOT retried here.
    """
    key = cache.make_key("POST", BASE_URL, "", body=xml_payload)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    await progress.notify("Phytozome BioMart: submitting query")
    resp = await _http.request_with_retry(
        client,
        "POST",
        BASE_URL,
        service="Phytozome BioMart",
        data={"query": xml_payload},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
        retry_404_pattern=ANY_404,
    )
    text = resp.text
    # BioMart returns 200 with a "Query ERROR:" body on filter / dataset
    # mis-configuration. Checked before it is stored: a body stored first was
    # served back as the same failure for the whole TTL without asking again
    # (#96).
    if text.startswith("Query ERROR"):
        raise PlantGenomicsError(f"Phytozome: {text.strip()[:300]}")
    # A zero-row match is the header line alone (live, 2026-09-28). A body that
    # does not open with it is not an answer: ``{}`` was read as "not found".
    if text.strip() and text.splitlines()[0].rstrip("\r") != _HEADER:
        raise UpstreamUnavailableError(
            f"Phytozome BioMart answered without its header line ({text[:120]!r}); "
            "this is not a missing locus"
        )
    _CACHE.set(key, text)
    await progress.notify("Phytozome BioMart: query complete")
    return text


async def lookup_locus(
    client: httpx.AsyncClient,
    locus: str,
    organism: str | int = organisms.DEFAULT_ORGANISM,
) -> dict[str, Any]:
    """Fetch Phytozome BioMart gene record for a locus.

    ``locus`` is the Phytozome / source-genome gene name (e.g. ``AT1G01010``
    for Arabidopsis thaliana TAIR10, ``Glyma.01G000100`` for soybean).
    ``organism`` accepts any form the resolver supports (canonical slug,
    scientific or common name, NCBI taxid). Defaults to Arabidopsis.
    Raises ``OrganismNotSupported`` if the resolved record has no
    ``phytozome_int`` (Phytozome doesn't index every plant we support).

    Returns a dict with string-valued keys: organism_name, gene_name,
    chromosome, gene_start, gene_end, strand, description. Numeric fields
    (gene_start, gene_end, strand) are returned as strings — BioMart's TSV
    is untyped and we preserve the wire representation rather than guess
    casts.
    """
    # Pre-flight reject before any HTTP — prevents XML injection via the
    # string-formatted template AND fails loud on accidental whitespace
    # / shell quoting damage.
    locus = validators.assert_valid_locus(locus, backend="Phytozome")

    phyto_id = organisms.phytozome_int_for(organism)

    xml_payload = _QUERY_TEMPLATE.format(organism_id=phyto_id, locus=locus)
    body = await _post(client, xml_payload)

    # Split on \n, drop trailing blanks. With header="1" we always get the
    # header line first (when the response is non-empty).
    lines = [ln for ln in body.splitlines() if ln.strip()]
    if len(lines) < 2:
        # Header present (no rows) OR empty body → nothing matched the
        # gene_name_filter for that organism_id.
        raise NotFoundError(f"Phytozome: locus {locus} not found for organism_id {phyto_id}")

    # First non-empty line is the header (we don't use it — column order is
    # pinned by our Attribute order). Second line is the first data row.
    values = lines[1].split("\t")
    if len(values) != len(_FIELDS):
        raise PlantGenomicsError(
            f"Phytozome: unexpected column count {len(values)} (expected {len(_FIELDS)}): {lines[1]!r}"
        )
    return dict(zip(_FIELDS, values, strict=True))
