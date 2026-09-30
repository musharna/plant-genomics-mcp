"""Planteome client — Plant Ontology (PO) + Trait Ontology (TO) annotations.

Planteome (planteome.org) is the reference database for the plant-specific
ontologies — PO (plant anatomy + developmental stages), TO (traits), and
PECO (experimental conditions). Its browser is AmiGO2/GOlr-backed, so the
open Solr ``/select`` endpoint returns structured annotation records with
no API key. This complements ``quickgo.py``: QuickGO serves GO (the
species-agnostic ontology); Planteome serves the plant-specific ones.

We query by locus across the searchable bioentity fields and filter by
``taxon`` (NCBI taxid), so a locus that exists in more than one species
resolves to the requested organism. Planteome names genes by our locus ids
for arabidopsis, rice, wheat and tomato only (``organisms.planteome_id_form``);
the others it indexes under other ids and are refused, since asking by ours
answered "0 annotations" for every gene.

Solr endpoint: https://browser.planteome.org/solr/select (AmiGO2 GOlr).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, TypeVar

import httpx

from plant_genomics_mcp import _http, cache, organisms
from plant_genomics_mcp.errors import NotFoundError

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

# Characters the Lucene query syntax reads as operators.
_LUCENE_SPECIAL = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\/\s])')


def _query(gene: str) -> str:
    """The gene over the query fields, or any transcript label ``<gene>.N``.

    Wheat and most tomato genes are held only as transcripts
    (TraesCS6D02G130400.2, Solyc01g005000.2.1) that the gene id does not
    match as a word: live 2026-09-30, Solyc01g005000 0 annotations by the id
    alone, 27 with its transcripts; AT5G16970 36 and 37.
    """
    escaped = _LUCENE_SPECIAL.sub(r"\\\1", gene)
    return f'"{escaped}" OR bioentity_label:{escaped}.*'


# Per-module response cache. See plant_genomics_mcp.cache for env knobs.
_CACHE = cache.TTLCache()
_T = TypeVar("_T")


def _select_shape(raw: object) -> dict[str, Any]:
    """The ``response`` object of a /select body, holding a ``docs`` list."""
    response = _http.expect_object(raw).get("response")
    if not isinstance(response, dict):
        raise _http.UnreadableBody(f"no 'response' object: got {type(response).__name__}")
    if not isinstance(response.get("docs"), list):
        raise _http.UnreadableBody(
            f"response.docs is not a list: {type(response.get('docs')).__name__}"
        )
    # The count too, before the store: read afterwards, a body without one
    # was stored first and failed the call for the whole TTL. Every live
    # answer states it, zero included (2026-09-28).
    return _http.expect_count("numFound")(response)


async def _get(
    client: httpx.AsyncClient,
    path: str,
    params: dict[str, Any] | None = None,
    *,
    shape: Callable[[object], _T],
) -> _T:
    """GET a Planteome Solr endpoint with retry on 429/5xx."""
    return await _http.cached_get(
        client,
        _CACHE,
        f"{BASE_URL}{path}",
        service=f"Planteome {path}",
        params=params,
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
        shape=shape,
    )


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
    keyed on namespace (PO / TO / PECO / GO). An organism Planteome does not
    name by our ids is :class:`OrganismNotSupported`; a gene it has no record
    of is :class:`NotFoundError`; a gene it has with no annotation answers
    an empty list.
    """
    locus = locus.strip()
    if not locus:
        raise ValueError("locus must be a non-empty identifier")
    limit = max(1, min(limit, MAX_LIMIT))

    record = organisms.resolve(organism)
    form = organisms.planteome_id_form_for(organism)
    taxid = organisms.ncbi_taxid_for(organism)
    taxon = f"NCBITaxon:{taxid}"
    gene = re.sub(r"\.\d+$", "", locus) if form == "unversioned" else locus

    def params(category: str, rows: int) -> dict[str, Any]:
        return {
            "q": _query(gene),
            "defType": "edismax",
            "qf": _QUERY_FIELDS,
            "fq": [f'document_category:"{category}"', f'taxon:"{taxon}"'],
            "rows": rows,
            "wt": "json",
        }

    service = f"Planteome {SELECT_PATH}"
    response = await _get(client, SELECT_PATH, params("annotation", limit), shape=_select_shape)
    docs = response["docs"]

    annotations = [_normalize(d) for d in docs if isinstance(d, dict)]
    total = _http.stated_count(response, "numFound", service=service)
    if total == 0:
        # No annotation: a gene Planteome holds without one, or none at all
        # (live 2026-09-30: AT1G01010 is a bioentity, AT1G99990 is not).
        known = await _get(client, SELECT_PATH, params("bioentity", 0), shape=_select_shape)
        if _http.stated_count(known, "numFound", service=service) == 0:
            raise NotFoundError(f"Planteome has no gene {locus!r} for {record.canonical}")
    return {
        "locus": locus,
        "organism": record.canonical,
        "taxon": taxon,
        "numberOfHits": total,
        **_http.counted(total, annotations),
        "annotations": annotations,
        "by_ontology": _rollup_by_ontology(annotations),
    }
