"""PANTHER protein-family client — locus → family / subfamily + GO + protein class.

PANTHER (pantherdb.org) classifies proteins into evolutionary families and
subfamilies, each carrying curated GO terms (by aspect), a PANTHER protein
class, and pathways. Its ``geneinfo`` service takes a native gene identifier
plus an NCBI ``organism`` taxid and returns the family assignment directly — no
UniProt hop needed. Plant loci (AT1G01060, Os01g0100100, …) map natively.

The ``organism`` taxid is NOT always the species taxid: barley is indexed under
the subspecies taxid 112509, so we route through
``organisms.panther_taxid_for`` (see the ``panther_taxid`` registry slot).

A gene PANTHER cannot classify returns ``found=False`` (a normal outcome).

Endpoint (https only — http 301-redirects via Cloudflare):
    https://pantherdb.org/services/oai/pantherdb/geneinfo
        ?geneInputList={locus}&organism={taxid}
"""

from __future__ import annotations

from typing import Any

import httpx

from plant_genomics_mcp import _http, cache, organisms, validators
from plant_genomics_mcp.errors import PlantGenomicsError

BASE_URL = "https://pantherdb.org"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3

_CACHE = cache.TTLCache()

# PANTHER encodes each annotation block's kind in its ``content`` field. The
# three GO aspects use the aspect root-term accession; protein class and pathway
# use ANNOT_TYPE ids.
_GO_ASPECTS = {
    "GO:0003674": "go_molecular_function",
    "GO:0008150": "go_biological_process",
    "GO:0005575": "go_cellular_component",
}
_PROTEIN_CLASS = "ANNOT_TYPE_ID_PANTHER_PC"
_PATHWAY = "ANNOT_TYPE_ID_PANTHER_PATHWAY"


def _as_list(x: Any) -> list[Any]:
    """Normalize PANTHER's obj-or-array fields to a list."""
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def _empty(locus: str) -> dict[str, Any]:
    """Result for a gene PANTHER could not classify."""
    return {
        "locus": locus,
        "found": False,
        "accession": None,
        "family_id": None,
        "family_name": None,
        "subfamily_id": None,
        "subfamily_name": None,
        "go_molecular_function": [],
        "go_biological_process": [],
        "go_cellular_component": [],
        "protein_class": [],
        "pathways": [],
    }


def _terms(block: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract ``[{id, name}]`` from one annotation block."""
    out: list[dict[str, Any]] = []
    ann_list = block.get("annotation_list") or {}
    for ann in _as_list(ann_list.get("annotation")):
        if isinstance(ann, dict):
            out.append({"id": ann.get("id"), "name": ann.get("name")})
    return out


def _project(locus: str, gene: dict[str, Any]) -> dict[str, Any]:
    """Project one mapped PANTHER gene to the surfaced field set."""
    result = _empty(locus)
    result["found"] = True
    result["accession"] = gene.get("accession")
    result["family_id"] = gene.get("family_id")
    result["family_name"] = gene.get("family_name")
    result["subfamily_id"] = gene.get("sf_id")
    result["subfamily_name"] = gene.get("sf_name")
    ann_types = (gene.get("annotation_type_list") or {}).get("annotation_data_type")
    for block in _as_list(ann_types):
        if not isinstance(block, dict):
            continue
        content = block.get("content")
        if content in _GO_ASPECTS:
            result[_GO_ASPECTS[content]] = _terms(block)
        elif content == _PROTEIN_CLASS:
            result["protein_class"] = _terms(block)
        elif content == _PATHWAY:
            result["pathways"] = _terms(block)
    return result


async def lookup_locus(
    client: httpx.AsyncClient,
    locus: str,
    organism: str | int = organisms.DEFAULT_ORGANISM,
) -> dict[str, Any]:
    """Fetch PANTHER family / GO classification for a locus.

    Resolves ``organism`` to its PANTHER taxid (raising ``OrganismNotSupported``
    for an organism absent from PANTHER). Returns ``found=False`` when PANTHER
    cannot map the locus to a family.
    """
    validators.assert_valid_locus(locus, backend="PANTHER")
    taxid = organisms.panther_taxid_for(organism)
    path = "/services/oai/pantherdb/geneinfo"
    params = {"geneInputList": locus, "organism": taxid}
    key = cache.make_key("GET", BASE_URL, path, params)
    cached = _CACHE.get(key)
    if cached is None:
        resp = await _http.request_with_retry(
            client,
            "GET",
            f"{BASE_URL}{path}",
            service="PANTHER geneinfo",
            params=params,
            headers={"Accept": "application/json"},
            timeout=DEFAULT_TIMEOUT,
            max_retries=MAX_RETRIES,
        )
        cached = resp.json()
        _CACHE.set(key, cached)
    if not isinstance(cached, dict):
        raise PlantGenomicsError(
            f"PANTHER geneinfo returned unexpected payload: {type(cached).__name__}"
        )
    mapped = (cached.get("search") or {}).get("mapped_genes") or {}
    gene = mapped.get("gene")
    if isinstance(gene, list):
        gene = gene[0] if gene else None
    if not isinstance(gene, dict):
        return _empty(locus)
    return _project(locus, gene)
