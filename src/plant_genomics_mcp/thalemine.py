"""ThaleMine client — experimental interaction evidence and curated GeneRIFs.

ThaleMine (bar.utoronto.ca/thalemine) is the Arabidopsis InterMine instance at
BAR, and the one the InterMine registry lists as canonical for *A. thaliana*.
This module talks to its full **query web service**, which is distinct from the
narrow BAR proxy (``/api/thalemine/gene_information/{locus}``) that
``plant_genomics_mcp.bar`` already uses for the gene summary.

What it adds over our existing backends
---------------------------------------
1. **Interactions with experimental provenance.** ``Gene.interactions`` joins
   through ``InteractionDetail`` (physical vs genetic, relationship type,
   confidence) to ``InteractionExperiment`` (detection method, publication).
   Sources are BioGRID, IntAct and PSI-MI. This is *curated experimental*
   evidence: ``string_interactions`` returns predicted / text-mined partners
   with channel sub-scores but no per-pair method or publication, and
   ``bar_aiv_interactions`` returns GRN *paper references* for Arabidopsis
   rather than partner pairs.
2. **GeneRIFs** — curated one-line functional statements, each tied to a
   PubMed ID. No other backend here carries them.

What it does NOT have
---------------------
Its ``Allele``, ``Strain`` and ``RegulatoryRegion`` classes are **present in the
data model but hold zero rows**, and there is no ``Phenotype`` class at all
(probed 2026-07-21 against release ``5.1.0-20250704``, web-service API v35).
Do not build allele / germplasm / phenotype features on this backend without
re-counting first: in the InterMine family, a class existing in ``/service/model``
says nothing about whether it is populated.

Talking to the service
----------------------
``GET /service/query/results?query=<XML>&format=json|count|tab[&size=N]``.

Two behaviours shape the code below:

* **There is no 404.** An unknown locus and a real gene with no data both answer
  ``HTTP 200`` with zero rows. We disambiguate with an ``OUTER`` join, which
  makes the service emit one row of nulls for a gene that exists but has no
  members in the joined collection (``AT1G01010`` → ``['AT1G01010','NAC001',None]``)
  versus no rows at all for a gene that does not exist. So one request settles
  both existence and emptiness.
* **Bad paths fail loudly.** A view path not in the model returns ``HTTP 400``
  with a readable message, so typos surface in tests rather than silently
  yielding empty columns.

The locus is interpolated into query XML, so ``validators.assert_valid_locus``
is load-bearing rather than decorative: its character class excludes ``< > & " '``
and therefore closes the XML-injection route into the query.

Arabidopsis only — ThaleMine carries taxon 3702 for genes (human and yeast
appear solely as interaction partners).
"""

from __future__ import annotations

from typing import Any

import httpx

from plant_genomics_mcp import _http, cache, organisms, validators
from plant_genomics_mcp.errors import NotFoundError, OrganismNotSupported, PlantGenomicsError

BASE_URL = "https://bar.utoronto.ca/thalemine"
QUERY_PATH = "/service/query/results"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3

# Genes live under taxon 3702 only; the other organisms in the mine exist purely
# as interaction partners and have no gene records to query.
SUPPORTED_ORGANISMS = ("arabidopsis_thaliana",)

# Rows requested per query. Interaction rows are per evidence *detail*, not per
# partner, so a well-studied gene fans out: HY5 (AT5G11260) yields 133 rows for
# ~50 partners. This bounds a pathological response without truncating real genes.
MAX_ROWS = 1000

# Aggregated partners / GeneRIFs surfaced. Counts report the true total when the
# cap bites, and ``truncated`` flags it.
MAX_PARTNERS = 50
MAX_RIFS = 50

_CACHE = cache.TTLCache()

# Column order is fixed by the ``view`` string — InterMine returns rows as
# positional lists, so these indices and the view must change together.
_INTERACTION_VIEW = (
    "Gene.primaryIdentifier "
    "Gene.symbol "
    "Gene.interactions.participant2.primaryIdentifier "
    "Gene.interactions.participant2.symbol "
    "Gene.interactions.details.type "
    "Gene.interactions.details.relationshipType "
    "Gene.interactions.details.experiment.interactionDetectionMethods.name "
    "Gene.interactions.details.experiment.publication.pubMedId "
    "Gene.interactions.details.dataSets.name"
)

_RIF_VIEW = (
    "Gene.primaryIdentifier "
    "Gene.symbol "
    "Gene.geneRifs.annotation "
    "Gene.geneRifs.publication.pubMedId "
    "Gene.geneRifs.timeStamp"
)


def _assert_arabidopsis(organism: str | int) -> str:
    """Resolve ``organism`` and reject anything ThaleMine has no genes for."""
    record = organisms.resolve(organism)
    if record.canonical not in SUPPORTED_ORGANISMS:
        raise OrganismNotSupported(
            backend="thalemine",
            organism=record.canonical,
            supported=list(SUPPORTED_ORGANISMS),
        )
    return record.canonical


def _query_xml(view: str, outer_path: str, locus: str) -> str:
    """Build the InterMine query XML for one locus.

    ``outer_path`` is joined OUTER so an existing gene with an empty collection
    still yields a row — see the module docstring on why that distinction has to
    survive to the caller. ``locus`` is pre-validated, so no escaping is needed.
    """
    return (
        f'<query model="genomic" view="{view}">'
        f'<join path="{outer_path}" style="OUTER"/>'
        f'<constraint path="Gene.primaryIdentifier" op="=" value="{locus}"/>'
        f"</query>"
    )


def _report_url(locus: str) -> str:
    """Human-facing ThaleMine report page for a gene."""
    return f"{BASE_URL}/portal.do?externalids={locus}&class=Gene"


async def _rows(client: httpx.AsyncClient, xml: str) -> list[list[Any]]:
    """Run a query and return its positional result rows, with retry + caching."""
    params = {"query": xml, "format": "json", "size": str(MAX_ROWS)}
    key = cache.make_key("GET", BASE_URL, QUERY_PATH, params)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    resp = await _http.request_with_retry(
        client,
        "GET",
        f"{BASE_URL}{QUERY_PATH}",
        service="ThaleMine query",
        params=params,
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
    )
    data = resp.json()
    if not isinstance(data, dict):
        raise PlantGenomicsError(
            f"ThaleMine query returned unexpected payload: {type(data).__name__}"
        )
    # InterMine reports query-level failures in-band on some paths.
    if data.get("error"):
        raise PlantGenomicsError(f"ThaleMine query error: {data['error']}")
    results = data.get("results")
    if not isinstance(results, list):
        raise PlantGenomicsError("ThaleMine query returned no 'results' list")
    rows = [r for r in results if isinstance(r, list)]
    _CACHE.set(key, rows)
    return rows


def _split(rows: list[list[Any]], locus: str, width: int) -> tuple[str | None, list[list[Any]]]:
    """Assert the gene exists and separate real data rows from the OUTER null row.

    Zero rows means the locus is not in the mine at all. A row whose first
    collection column (index 2) is ``None`` is the OUTER-join placeholder for a
    gene that exists but has nothing in that collection.
    """
    if not rows:
        raise NotFoundError(f"ThaleMine has no gene with primaryIdentifier={locus!r}")
    first = rows[0]
    symbol = first[1] if len(first) > 1 and isinstance(first[1], str) else None
    data = [r for r in rows if len(r) >= width and r[2] is not None]
    return symbol, data


def _uniq(values: list[Any]) -> list[str]:
    """Sorted unique non-empty strings, for the per-partner evidence roll-ups."""
    return sorted({v for v in values if isinstance(v, str) and v})


async def lookup_interactions(
    client: httpx.AsyncClient,
    locus: str,
    organism: str | int = organisms.DEFAULT_ORGANISM,
) -> dict[str, Any]:
    """Curated experimental interaction partners for an Arabidopsis locus.

    One row per *evidence detail* comes back from ThaleMine — the same partner
    recurs once per detection method, relationship type and source database — so
    rows are aggregated into one entry per partner carrying the union of its
    evidence, plus ``evidence_count`` as a crude strength signal. Partners are
    ordered by that count, then by locus for a stable tie-break.

    ``found=False`` with an empty ``partners`` list means the gene is real but
    has no curated interaction on record — a normal answer, not an error. An
    unknown locus raises ``NotFoundError``.
    """
    validators.assert_valid_locus(locus, backend="ThaleMine")
    canonical = _assert_arabidopsis(organism)
    rows = await _rows(client, _query_xml(_INTERACTION_VIEW, "Gene.interactions", locus))
    symbol, data = _split(rows, locus, width=9)

    grouped: dict[str, dict[str, Any]] = {}
    for row in data:
        partner = row[2]
        if not isinstance(partner, str):
            continue
        entry = grouped.setdefault(
            partner,
            {
                "partner_locus": partner,
                "partner_symbol": row[3] if isinstance(row[3], str) else None,
                "_types": [],
                "_rel": [],
                "_methods": [],
                "_pmids": [],
                "_sources": [],
                "evidence_count": 0,
            },
        )
        entry["_types"].append(row[4])
        entry["_rel"].append(row[5])
        entry["_methods"].append(row[6])
        entry["_pmids"].append(row[7])
        entry["_sources"].append(row[8])
        entry["evidence_count"] += 1

    partners = [
        {
            "partner_locus": e["partner_locus"],
            "partner_symbol": e["partner_symbol"],
            "interaction_types": _uniq(e["_types"]),
            "relationship_types": _uniq(e["_rel"]),
            "detection_methods": _uniq(e["_methods"]),
            "pubmed_ids": _uniq(e["_pmids"]),
            "sources": _uniq(e["_sources"]),
            "evidence_count": e["evidence_count"],
        }
        for e in grouped.values()
    ]
    partners.sort(key=lambda p: (-int(p["evidence_count"]), str(p["partner_locus"])))

    total = len(partners)
    return {
        "locus": locus,
        "gene_symbol": symbol,
        "organism": canonical,
        "found": total > 0,
        "partner_count": total,
        "evidence_count": len(data),
        "truncated": total > MAX_PARTNERS,
        "partners": partners[:MAX_PARTNERS],
        "source_url": _report_url(locus),
    }


async def lookup_gene_rifs(
    client: httpx.AsyncClient,
    locus: str,
    organism: str | int = organisms.DEFAULT_ORGANISM,
) -> dict[str, Any]:
    """Curated GeneRIF functional statements for an Arabidopsis locus.

    A GeneRIF is a one-sentence, manually curated statement of what a gene does,
    anchored to the publication that showed it — dense, citable functional
    context that neither GO terms nor abstracts provide directly.

    Upstream order is preserved rather than sorted: ThaleMine returns no
    meaningful ranking, and imposing one would imply a relevance judgement the
    data does not support. ``found=False`` means the gene exists but has no
    GeneRIF; an unknown locus raises ``NotFoundError``.
    """
    validators.assert_valid_locus(locus, backend="ThaleMine")
    canonical = _assert_arabidopsis(organism)
    rows = await _rows(client, _query_xml(_RIF_VIEW, "Gene.geneRifs", locus))
    symbol, data = _split(rows, locus, width=5)

    rifs = [
        {
            "annotation": row[2],
            "pubmed_id": row[3] if isinstance(row[3], str) else None,
            "time_stamp": row[4] if isinstance(row[4], str) else None,
        }
        for row in data
    ]
    total = len(rifs)
    return {
        "locus": locus,
        "gene_symbol": symbol,
        "organism": canonical,
        "found": total > 0,
        "rif_count": total,
        "truncated": total > MAX_RIFS,
        "gene_rifs": rifs[:MAX_RIFS],
        "source_url": _report_url(locus),
    }
