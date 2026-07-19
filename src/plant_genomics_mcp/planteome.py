"""Planteome client — Plant Ontology (PO) + Trait Ontology (TO) annotations.

Planteome (planteome.org) is the reference database for the plant-specific
ontologies — PO (plant anatomy + developmental stages), TO (traits), and
PECO (experimental conditions). Its browser is AmiGO2/GOlr-backed, so the
open Solr ``/select`` endpoint returns structured annotation records with
no API key. This complements ``quickgo.py``: QuickGO serves GO (the
species-agnostic ontology); Planteome serves the plant-specific ones.

We query by locus across the searchable bioentity fields and filter by
``taxon`` (NCBI taxid), so a locus that exists in more than one species
resolves to the requested organism. Organisms Planteome doesn't curate
simply return zero annotations — coverage is strong for arabidopsis, rice,
maize, grape, soybean, and tomato (probed 2026-07-19); thinner elsewhere.

Solr endpoint: https://browser.planteome.org/solr/select (AmiGO2 GOlr).
"""

from __future__ import annotations

from typing import Any

import httpx

from plant_genomics_mcp import _http, cache, organisms
from plant_genomics_mcp.errors import PlantGenomicsError

BASE_URL = "https://browser.planteome.org/solr"
SELECT_PATH = "/select"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3
DEFAULT_LIMIT = 100
MAX_LIMIT = 200  # Solr rows cap we impose; a single locus rarely exceeds this

# edismax query fields — the locus can live in any of these depending on the
# curating source (rice/maize use bioentity_label; arabidopsis puts the AGI
# locus in synonym), so we search across all three rather than exact-match one.
_QUERY_FIELDS = "bioentity_label_searchable synonym bioentity_name_searchable"

# Per-module response cache. See plant_genomics_mcp.cache for env knobs.
_CACHE = cache.TTLCache()


async def _get(
    client: httpx.AsyncClient,
    path: str,
    params: dict[str, Any] | None = None,
) -> Any:
    """GET a Planteome Solr endpoint with retry on 429/5xx."""
    key = cache.make_key("GET", BASE_URL, path, params)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    resp = await _http.request_with_retry(
        client,
        "GET",
        f"{BASE_URL}{path}",
        service=f"Planteome {path}",
        params=params,
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
    )
    result = resp.json()
    _CACHE.set(key, result)
    return result


def _ontology_of(term_id: str | None) -> str | None:
    """Namespace prefix of an annotation class id, e.g. 'PO:0009005' → 'PO'."""
    if term_id and ":" in term_id:
        return term_id.split(":", 1)[0]
    return None


def _normalize(doc: dict[str, Any]) -> dict[str, Any]:
    """Project a Planteome GOlr annotation doc to the surfaced field set."""
    term_id = doc.get("annotation_class")
    return {
        "term_id": term_id,
        "term_name": doc.get("annotation_class_label"),
        "ontology": _ontology_of(term_id),
        "aspect": doc.get("aspect"),
        "evidence": doc.get("evidence_type"),
        "taxon": doc.get("taxon"),
        "taxon_label": doc.get("taxon_label"),
        "reference": doc.get("reference"),
        "assigned_by": doc.get("assigned_by"),
        "bioentity_label": doc.get("bioentity_label"),
    }


def _rollup_by_ontology(
    annotations: list[dict[str, Any]],
) -> dict[str, list[dict[str, str]]]:
    """Group annotations by ontology namespace, deduping on term_id.

    A single term can back several annotations (different evidence /
    reference). The rollup collapses these so a client sees "the PO term
    set" / "the TO term set" at a glance without the per-evidence repetition.
    """
    seen: dict[str, set[str]] = {}
    grouped: dict[str, list[dict[str, str]]] = {}
    for ann in annotations:
        ontology = ann.get("ontology")
        term_id = ann.get("term_id")
        if not ontology or not term_id:
            continue
        bucket = seen.setdefault(ontology, set())
        if term_id in bucket:
            continue
        bucket.add(term_id)
        grouped.setdefault(ontology, []).append(
            {"term_id": term_id, "term_name": ann.get("term_name") or ""}
        )
    return grouped


async def lookup_locus(
    client: httpx.AsyncClient,
    locus: str,
    organism: str = organisms.DEFAULT_ORGANISM,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Fetch Plant Ontology / Trait Ontology annotations for a plant locus.

    The locus is matched across Planteome's searchable bioentity fields and
    filtered to the organism's NCBI taxon, so cross-species locus collisions
    resolve to the requested organism. ``limit`` is clamped to [1, MAX_LIMIT].

    Returns a dict with raw ``annotations[]`` plus a ``by_ontology`` rollup
    keyed on namespace (PO / TO / PECO / GO). Organisms Planteome does not
    curate return an empty annotation list rather than an error.
    """
    locus = locus.strip()
    if not locus:
        raise ValueError("locus must be a non-empty identifier")
    limit = max(1, min(limit, MAX_LIMIT))

    record = organisms.resolve(organism)
    taxid = organisms.ncbi_taxid_for(organism)
    taxon = f"NCBITaxon:{taxid}"

    params: dict[str, Any] = {
        "q": locus,
        "defType": "edismax",
        "qf": _QUERY_FIELDS,
        "fq": ['document_category:"annotation"', f'taxon:"{taxon}"'],
        "rows": limit,
        "wt": "json",
    }
    raw = await _get(client, SELECT_PATH, params=params)
    if not isinstance(raw, dict):
        raise PlantGenomicsError(
            f"Planteome {SELECT_PATH} returned non-dict payload: {type(raw).__name__}"
        )
    response = raw.get("response")
    if not isinstance(response, dict):
        raise PlantGenomicsError(
            f"Planteome {SELECT_PATH} payload missing 'response' object: got {type(response).__name__}"
        )
    docs = response.get("docs")
    if not isinstance(docs, list):
        raise PlantGenomicsError(
            f"Planteome {SELECT_PATH} response.docs is not a list: {type(docs).__name__}"
        )

    annotations = [_normalize(d) for d in docs if isinstance(d, dict)]
    return {
        "locus": locus,
        "organism": record.canonical,
        "taxon": taxon,
        "numberOfHits": int(response.get("numFound", 0)),
        "returned": len(annotations),
        "annotations": annotations,
        "by_ontology": _rollup_by_ontology(annotations),
    }
