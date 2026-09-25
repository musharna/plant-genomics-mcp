"""Ensembl Plants REST client — async httpx wrapper around rest.ensembl.org.

Ensembl Plants uses the same REST host as Ensembl (``rest.ensembl.org``); plant
species (``arabidopsis_thaliana``, ``oryza_sativa``, ``zea_mays``, ...) live
alongside vertebrates in the same lookup namespace. We constrain calls to
plant species via the ``species=`` query parameter.

Endpoints documented at https://rest.ensembl.org. No auth required. Server
asks for a ~15 req/sec ceiling per IP for sustained use; bursts above are
tolerated. We retry on 429 and 5xx with exponential backoff.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from plant_genomics_mcp import _http, cache, organisms, validators
from plant_genomics_mcp.errors import (
    NotFoundError,
    PlantGenomicsError,
    RateLimitError,
    UpstreamUnavailableError,
)

BASE_URL = "https://rest.ensembl.org"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3

# Ensembl answers an unknown identifier with `400 {"error":"ID '...' not
# found"}` rather than 404, so the shared helper's 404 -> NotFoundError mapping
# never fires and callers get a generic PlantGenomicsError instead. Passing this
# to `_http.request_with_retry(not_found_400_pattern=...)` restores the typed
# error, letting a caller tell "no such gene" from "the backend is broken".
#
# Matched on the body, NOT applied to every 400, because Ensembl overloads the
# status: an oversized region span is also a 400 (see `region_query`). Calling
# that "not found" would just be a different wrong answer.
#
# Defined here and imported by the other Ensembl-backed modules so there is one
# definition to keep correct rather than three copies drifting apart.
NOT_FOUND_400_RE = re.compile(r"\bnot found\b", re.IGNORECASE)

# Per-module response cache. See plant_genomics_mcp.cache for env knobs.
_CACHE = cache.TTLCache()

# Re-export so existing imports (`from plant_genomics_mcp.ensembl_plants import
# PlantGenomicsError`) keep working. New code should import from
# ``plant_genomics_mcp.errors`` directly.
__all__ = ["PlantGenomicsError", "RateLimitError", "NotFoundError", "UpstreamUnavailableError"]


async def _get(
    client: httpx.AsyncClient,
    path: str,
    params: dict[str, Any] | None = None,
    not_found_400_pattern: re.Pattern[str] = NOT_FOUND_400_RE,
) -> Any:
    """GET an Ensembl REST endpoint with retry on 429/5xx.

    Thin cache wrapper over the shared :func:`_http.request_with_retry`
    helper. The retry/cap/error-classification policy is shared with the
    other 8 backends.
    """
    return await _http.cached_get(
        client,
        _CACHE,
        f"{BASE_URL}{path}",
        service=f"Ensembl Plants {path}",
        params=params,
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
        not_found_400_pattern=not_found_400_pattern,
    )


def project_lookup(raw: dict[str, Any]) -> dict[str, Any]:
    """One /lookup/id record, as both tool forms return it (issue #137).

    The single and batch forms used to project separately, and the batch form
    passed Ensembl's record through raw: ``species`` not renamed, no
    ``upstream_version``. Built fresh rather than mutating ``raw`` (audit P5).
    """
    out = {**raw}
    if "species" in out:
        out["organism"] = out.pop("species")
    # Ensembl spells canonical_transcript "<id>.<version>", and plant genes
    # carry version None, so the id arrives as "AT1G19850.1." (live, 2026-09-22)
    # while aragwas_associations spells the same transcript "AT1G19850.1".
    transcript = out.get("canonical_transcript")
    if isinstance(transcript, str) and out.get("version") is None:
        out["canonical_transcript"] = transcript.removesuffix(".")
    # Issue #121: uniform key; Ensembl states no release on the answering
    # response (headers probed live 2026-09-22).
    out["upstream_version"] = None
    return out


def wire_id(locus: str, organism: str | int) -> str:
    """The id Ensembl indexes ``locus`` under in ``organism`` — the one projection.

    Validates ``locus`` (canonical case) and prepends the registry's wire-only
    stable-id prefix (tomato SL4.0: ``gene-``). Every Ensembl call keyed on a
    user locus goes through this, the batch POST included: the batch used to
    send the bare locus, so every tomato batch lookup was NotFound while the
    single lookup answered (audit 2026-09-22 H2).
    """
    locus = validators.assert_valid_locus(locus, backend="Ensembl Plants")
    return organisms.ensembl_id_prefix_for(organism) + locus


async def lookup_locus(
    client: httpx.AsyncClient,
    locus: str,
    organism: str | int = organisms.DEFAULT_ORGANISM,
) -> dict[str, Any]:
    """Fetch metadata for a plant locus identifier.

    ``locus`` is the species-specific gene identifier — e.g. TAIR locus
    ``AT1G01010`` for Arabidopsis, ``Os01g0100100`` for rice. Ensembl
    looks these up via ``/lookup/id/{locus}`` with the ``species=`` query
    parameter constraining the namespace. ``organism=`` accepts any alias
    or NCBI taxid the resolver understands; we translate to the Ensembl
    slug before hitting the wire.
    """
    locus = validators.assert_valid_locus(locus, backend="Ensembl Plants")
    slug = organisms.ensembl_slug_for(organism)
    wire = wire_id(locus, organism)
    params: dict[str, Any] = {"species": slug, "expand": 0}
    raw = await _get(client, f"/lookup/id/{wire}", params=params)
    if isinstance(raw, dict) and "species" in raw:
        return project_lookup(raw)
    return raw


async def lookup_xrefs(
    client: httpx.AsyncClient,
    locus: str,
    organism: str | int = organisms.DEFAULT_ORGANISM,
) -> dict[str, Any]:
    """Fetch cross-references (UniProt, NCBI Gene, TAIR, etc.) for a locus.

    Ensembl ``/xrefs/id/{locus}`` returns a list of records mapping the
    locus to other databases. We wrap the raw array in an object so the
    MCP outputSchema can validate it (top-level must be type=object) and
    add a ``by_db`` rollup keyed on Ensembl's ``dbname`` for quick lookup
    without walking the full list. ``organism=`` accepts any alias or
    NCBI taxid the resolver understands; we translate to the Ensembl
    slug before hitting the wire.
    """
    locus = validators.assert_valid_locus(locus, backend="Ensembl Plants")
    slug = organisms.ensembl_slug_for(organism)
    wire = wire_id(locus, organism)
    params: dict[str, Any] = {"species": slug}
    raw = await _get(client, f"/xrefs/id/{wire}", params=params)
    if not isinstance(raw, list):
        raise PlantGenomicsError(
            f"Ensembl /xrefs/id/{locus} returned non-list payload: {type(raw).__name__}"
        )
    by_db: dict[str, list[str]] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        dbname = entry.get("dbname")
        primary_id = entry.get("primary_id")
        if dbname and primary_id:
            by_db.setdefault(dbname, []).append(primary_id)
    return {
        "locus": locus,
        "organism": slug,
        "count": len(raw),
        "xrefs": raw,
        "by_db": by_db,
    }


SEQUENCE_TYPES = ("genomic", "cds", "cdna", "protein")


async def _product_id(client: httpx.AsyncClient, locus: str, organism: str | int) -> str:
    """The Ensembl id whose cds / cdna / protein ``get_sequence`` returns.

    A protein, CDS or cDNA belongs to a transcript, not a gene: asked for one
    on a gene id, Ensembl answers 400 ("N sequences detected ... specify the
    multiple_sequences parameter") whenever the gene has more than one
    transcript — most real genes (audit 2026-09-22 M3). A gene resolves to
    its canonical transcript, through the same lookup and projection
    ``ensembl_plants_lookup_locus`` returns; a transcript id is used as given.
    """
    record = await lookup_locus(client, locus, organism=organism)
    if record.get("object_type") != "Gene":
        target = record.get("id")
    else:
        target = record.get("canonical_transcript")
        if not target:
            raise NotFoundError(
                f"Ensembl Plants: {locus} has no canonical transcript, so no cds/cdna/protein"
            )
    if not isinstance(target, str):
        raise PlantGenomicsError(
            f"Ensembl Plants /lookup/id/{locus} returned no usable id: {target!r}"
        )
    # Upstream data spliced into a path: held to the same shape as user input.
    return validators.assert_valid_locus(target, backend="Ensembl Plants")


async def get_sequence(
    client: httpx.AsyncClient,
    locus: str,
    organism: str | int = organisms.DEFAULT_ORGANISM,
    seq_type: str = "protein",
) -> dict[str, Any]:
    """Fetch a locus's sequence from Ensembl ``/sequence/id/{locus}``.

    ``seq_type`` is one of ``genomic`` / ``cds`` / ``cdna`` / ``protein``.
    ``protein`` / ``cds`` / ``cdna`` return the gene's canonical-transcript
    product; ``genomic`` returns the gene's genomic span. This is the fetch
    half of the ``lookup → fetch → BLAST`` loop — feed the returned
    ``sequence`` straight to ``blast_sequence`` (use ``protein`` for
    ``blastp``, ``cds``/``cdna`` for ``blastn``). ``organism=`` accepts any
    alias or NCBI taxid the resolver understands.
    """
    locus = validators.assert_valid_locus(locus, backend="Ensembl Plants")
    if seq_type not in SEQUENCE_TYPES:
        raise ValueError(f"seq_type {seq_type!r} not in {list(SEQUENCE_TYPES)}")
    slug = organisms.ensembl_slug_for(organism)
    wire = wire_id(locus, organism)
    if seq_type != "genomic":
        wire = await _product_id(client, locus, organism)
    params: dict[str, Any] = {"species": slug, "type": seq_type}
    raw = await _get(client, f"/sequence/id/{wire}", params=params)
    if not isinstance(raw, dict) or "seq" not in raw:
        raise PlantGenomicsError(
            f"Ensembl /sequence/id/{locus} (type={seq_type}) returned unexpected payload: "
            f"{type(raw).__name__}"
        )
    seq = raw.get("seq") or ""
    return {
        "locus": locus,
        "organism": slug,
        "type": seq_type,
        "molecule": raw.get("molecule"),
        "ensembl_id": raw.get("id"),
        "description": raw.get("desc"),
        "version": raw.get("version"),
        "length": len(seq),
        "sequence": seq,
    }


REGION_FEATURES = ("gene", "transcript", "cds", "exon")


async def region_query(
    client: httpx.AsyncClient,
    region: str,
    start: int,
    end: int,
    organism: str | int = organisms.DEFAULT_ORGANISM,
    feature: str = "gene",
) -> dict[str, Any]:
    """List features overlapping a genomic interval via ``/overlap/region``.

    ``region`` is the seq-region name (chromosome / contig, e.g. ``"1"``);
    ``start`` / ``end`` are 1-based inclusive coordinates. ``feature`` is one
    of ``gene`` / ``transcript`` / ``cds`` / ``exon``. Answers "what genes are
    in this QTL interval / assembly window" without a per-locus lookup. Ensembl
    caps the span (oversized regions 400 → ``PlantGenomicsError``). ``organism=``
    accepts any alias or NCBI taxid the resolver understands.
    """
    if feature not in REGION_FEATURES:
        raise ValueError(f"feature {feature!r} not in {list(REGION_FEATURES)}")
    if start < 1:
        raise ValueError(f"start must be >=1, got {start}")
    if end < start:
        raise ValueError(f"end {end} must be >= start {start}")
    # ``region`` is caller input spliced into the request path — guard it against
    # ``?``/``/``/``#``/``&``/``%``/whitespace exactly as ``vep_annotate`` does for
    # its own path-templated ``region``; otherwise a value like ``1?feature=exon``
    # would inject/override query params on rest.ensembl.org.
    validators.assert_no_path_metachars(region, field="region", backend="Ensembl Plants")
    slug = organisms.ensembl_slug_for(organism)
    region_str = f"{region}:{start}-{end}"
    raw = await _get(client, f"/overlap/region/{slug}/{region_str}", params={"feature": feature})
    if not isinstance(raw, list):
        raise PlantGenomicsError(
            f"Ensembl /overlap/region/{region_str} returned non-list payload: {type(raw).__name__}"
        )
    return {
        "organism": slug,
        "region": region_str,
        "feature": feature,
        "count": len(raw),
        "features": raw,
    }


GENE_TREE_ID_RE = re.compile(r"^EPlGT\d{14}$")
# An unknown tree is `400 {"error":"No GeneTree found for ID ..."}` (live,
# 2026-09-25): no "not found" in it, so the module pattern misses it.
_GENE_TREE_NOT_FOUND_RE = re.compile(r"No GeneTree found|\bnot found\b", re.IGNORECASE)
GENE_TREE_MEMBERS_DEFAULT_LIMIT = 100
GENE_TREE_MEMBERS_MAX_LIMIT = 1000


def _gene_tree_leaves(tree: dict[str, Any]) -> list[dict[str, Any]]:
    """Every leaf of a genetree node, at any depth (iterative: trees run deep)."""
    leaves: list[dict[str, Any]] = []
    stack = [tree]
    while stack:
        node = stack.pop()
        children = node.get("children")
        if children:
            stack.extend(children)
        else:
            leaves.append(node)
    return leaves


def _gene_tree_member(leaf: dict[str, Any], gene_tree_id: str) -> dict[str, Any]:
    try:
        gene = leaf["id"]["accession"]
        taxid = leaf["taxonomy"]["id"]
        species = leaf["taxonomy"]["scientific_name"]
    except (KeyError, TypeError) as exc:
        raise PlantGenomicsError(
            f"Ensembl genetree {gene_tree_id}: leaf without gene id or taxonomy ({exc!r}): {leaf!r}"
        ) from exc
    record = organisms.by_compara_taxid(taxid)
    prefix = (record.ensembl_id_prefix or "") if record else ""
    proteins = (leaf.get("sequence") or {}).get("id") or []
    return {
        "locus": gene.removeprefix(prefix) if prefix else gene,
        "protein_id": proteins[0]["accession"] if proteins else None,
        "organism": record.canonical if record else None,
        "species": species,
        "taxid": taxid,
    }


async def gene_tree_members(
    client: httpx.AsyncClient,
    gene_tree_id: str,
    target_organism: str | int | None = None,
    limit: int = GENE_TREE_MEMBERS_DEFAULT_LIMIT,
) -> dict[str, Any]:
    """The member genes of an Ensembl Compara (plants) gene tree (issue #130).

    ``gene_tree_id`` is the ``EPlGT...`` id ``gramene_homologs`` returns.
    ``target_organism=None`` lists every species in the tree; otherwise only the
    leaves Compara tags with that organism's taxid. Members are sorted by
    species then locus; ``total`` counts them before ``limit`` cuts.
    """
    if not isinstance(gene_tree_id, str) or not GENE_TREE_ID_RE.match(gene_tree_id):
        raise NotFoundError(f"invalid gene_tree_id {gene_tree_id!r}: expected EPlGT + 14 digits")
    if not 1 <= limit <= GENE_TREE_MEMBERS_MAX_LIMIT:
        raise ValueError(f"limit must be in 1..{GENE_TREE_MEMBERS_MAX_LIMIT}, got {limit}")
    wanted = organisms.resolve(target_organism) if target_organism is not None else None
    raw = await _get(
        client,
        f"/genetree/id/{gene_tree_id}",
        params={"compara": "plants", "aligned": 0, "sequence": "none"},
        not_found_400_pattern=_GENE_TREE_NOT_FOUND_RE,
    )
    tree = raw.get("tree") if isinstance(raw, dict) else None
    if not isinstance(tree, dict):
        raise PlantGenomicsError(
            f"Ensembl genetree {gene_tree_id} returned no tree: {type(raw).__name__}"
        )
    members = [_gene_tree_member(leaf, gene_tree_id) for leaf in _gene_tree_leaves(tree)]
    if wanted is not None:
        members = [m for m in members if m["organism"] == wanted.canonical]
    members.sort(key=lambda m: (m["species"], m["locus"]))
    return {
        "gene_tree_id": gene_tree_id,
        "target_organism": wanted.canonical if wanted else None,
        "total": len(members),
        "returned": min(len(members), limit),
        "truncated": len(members) > limit,
        "members": members[:limit],
        "upstream_version": None,
    }
