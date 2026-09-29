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
from collections.abc import Callable
from typing import Any, TypeVar

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
_T = TypeVar("_T")

# Re-export so existing imports (`from plant_genomics_mcp.ensembl_plants import
# PlantGenomicsError`) keep working. New code should import from
# ``plant_genomics_mcp.errors`` directly.
__all__ = ["PlantGenomicsError", "RateLimitError", "NotFoundError", "UpstreamUnavailableError"]


async def _get(
    client: httpx.AsyncClient,
    path: str,
    params: dict[str, Any] | None = None,
    not_found_400_pattern: re.Pattern[str] = NOT_FOUND_400_RE,
    *,
    shape: Callable[[object], _T],
) -> _T:
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
        shape=shape,
    )


def _lookup_shape(value: object) -> dict[str, Any]:
    """A /lookup/id record names itself: a string ``id`` and ``species``.

    Every live record has both (gene, transcript, rice; 2026-09-28), and an
    unknown id is a 400. A record without them used to pass through
    unprojected: ``{}`` was the tool's answer, breaking its schema, and
    ``get_sequence`` and ``locus_variants`` failed on it after it was stored,
    so for the whole TTL without asking again. ``project_lookup`` holds it
    too, so the batch POST, which projects each record itself, cannot answer
    with one (#196 review).
    """
    record = _http.expect_object(value)
    for key in ("id", "species"):
        if not isinstance(record.get(key), str):
            raise _http.UnreadableBody(f"no string {key!r} in {str(record)[:120]}")
    return record


def project_lookup(raw: dict[str, Any]) -> dict[str, Any]:
    """One /lookup/id record, as both tool forms return it (issue #137).

    The single and batch forms used to project separately, and the batch form
    passed Ensembl's record through raw: ``species`` not renamed, no
    ``upstream_version``. Built fresh rather than mutating ``raw`` (audit P5).
    A record that does not name itself raises :class:`_http.UnreadableBody`.
    """
    out = {**_lookup_shape(raw)}
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
    raw = await _get(client, f"/lookup/id/{wire}", params=params, shape=_lookup_shape)
    return project_lookup(raw)


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
    rows = await _get(client, f"/xrefs/id/{wire}", params=params, shape=_http.object_rows)
    by_db: dict[str, list[str]] = {}
    for entry in rows:
        dbname = entry.get("dbname")
        primary_id = entry.get("primary_id")
        if dbname and primary_id:
            by_db.setdefault(dbname, []).append(primary_id)
    return {
        "locus": locus,
        "organism": slug,
        "count": len(rows),
        "xrefs": rows,
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


def _sequence_shape(raw: object) -> dict[str, Any]:
    body = _http.expect_object(raw)
    if "seq" not in body:
        raise _http.UnreadableBody(f"no seq in {str(body)[:120]}")
    return body


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
    raw = await _get(client, f"/sequence/id/{wire}", params=params, shape=_sequence_shape)
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
    rows = await _get(
        client,
        f"/overlap/region/{slug}/{region_str}",
        params={"feature": feature},
        shape=_http.object_rows,
    )
    return {
        "organism": slug,
        "region": region_str,
        "feature": feature,
        "count": len(rows),
        "features": rows,
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
    stack: list[object] = [tree]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            raise _http.UnreadableBody(f"tree node is {type(node).__name__}, not an object")
        children = node.get("children")
        if children is not None and not isinstance(children, list):
            raise _http.UnreadableBody(f"children is {type(children).__name__}, not a list")
        if children:
            stack.extend(children)
        else:
            leaves.append(node)
    return leaves


def _gene_tree_member(leaf: dict[str, Any]) -> dict[str, Any]:
    try:
        gene = leaf["id"]["accession"]
        taxid = leaf["taxonomy"]["id"]
        species = leaf["taxonomy"]["scientific_name"]
    except (KeyError, TypeError) as exc:
        raise _http.UnreadableBody(
            f"leaf without gene id or taxonomy ({exc!r}): {leaf!r:.200}"
        ) from None
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


def _gene_tree_shape(raw: object) -> list[dict[str, Any]]:
    """A /genetree body read to its members, before the store: a leaf read
    afterwards failed from the cache for the whole TTL without asking again."""
    body = _http.expect_object(raw)
    if not isinstance(body.get("tree"), dict):
        raise _http.UnreadableBody(f"no tree object in {str(body)[:120]}")
    return [_gene_tree_member(leaf) for leaf in _gene_tree_leaves(body["tree"])]


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
    stored = await _get(
        client,
        f"/genetree/id/{gene_tree_id}",
        params={"compara": "plants", "aligned": 0, "sequence": "none"},
        not_found_400_pattern=_GENE_TREE_NOT_FOUND_RE,
        shape=_gene_tree_shape,
    )
    members = sorted(
        (m for m in stored if wanted is None or m["organism"] == wanted.canonical),
        key=lambda m: (m["species"], m["locus"]),
    )
    return {
        "gene_tree_id": gene_tree_id,
        "target_organism": wanted.canonical if wanted else None,
        "total": len(members),
        "returned": min(len(members), limit),
        "truncated": len(members) > limit,
        "members": members[:limit],
        "upstream_version": None,
    }


PARALOGS_DEFAULT_LIMIT = 100
PARALOGS_MAX_LIMIT = 1000


def _paralog(row: object, slug: str, prefix: str) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise _http.UnreadableBody(f"row is {type(row).__name__}, not an object")
    try:
        target = row["target"]
        gene = target["id"]
        species = target["species"]
        kind = row["type"]
    except (KeyError, TypeError) as exc:
        raise _http.UnreadableBody(
            f"row without target id, species or type ({exc!r}): {row!r:.200}"
        ) from None
    # A paralogue is same-species by Ensembl's definition; another species here
    # means the answer is not the one this tool describes.
    if species != slug:
        raise _http.UnreadableBody(f"row names species {species!r} ({gene}), not {slug}")
    return {
        "locus": gene.removeprefix(prefix) if prefix else gene,
        "type": kind,
        "taxonomy_level": row.get("taxonomy_level"),
        "perc_id": target.get("perc_id"),
        "perc_pos": target.get("perc_pos"),
        "protein_id": target.get("protein_id"),
    }


def _paralogues_shape(slug: str, prefix: str) -> Callable[[object], dict[str, Any]]:
    """A /homology body read to its paralogues, before the store: a row read
    afterwards failed from the cache for the whole TTL without asking again.
    ``found`` is False for Compara's ``{"data": []}``."""

    def shape(raw: object) -> dict[str, Any]:
        body = _http.expect_object(raw)
        data = body.get("data")
        if not isinstance(data, list):
            raise _http.UnreadableBody(f"no data list in {str(body)[:120]}")
        if not data:
            return {"found": False, "rows": []}
        if not (isinstance(data[0], dict) and isinstance(data[0].get("homologies"), list)):
            raise _http.UnreadableBody(f"no homologies list in {data[0]!r:.120}")
        return {"found": True, "rows": [_paralog(r, slug, prefix) for r in data[0]["homologies"]]}

    return shape


async def paralogs(
    client: httpx.AsyncClient,
    locus: str,
    organism: str | int = organisms.DEFAULT_ORGANISM,
    limit: int = PARALOGS_DEFAULT_LIMIT,
) -> dict[str, Any]:
    """The paralogues Ensembl Compara (plants) records for a locus.

    Gramene v69's projection of Compara keeps ``within_species_paralog`` and
    drops ``other_paralog`` (Ensembl's "ancient paralogues", inferred across
    a super tree), so a gene can have paralogues here and none in
    ``gramene_homologs``. Rows are sorted closest first (``perc_id``
    descending, then locus); ``total`` and ``counts_by_type`` count every row
    before ``limit`` cuts.

    Compara answers ``{"data": []}`` both for an id Ensembl does not know and
    for a real gene it keeps no homology for, so on that answer one
    ``/lookup/id`` call decides: unknown is ``NotFoundError``, known is
    ``found=False``.
    """
    if not 1 <= limit <= PARALOGS_MAX_LIMIT:
        raise ValueError(f"limit must be in 1..{PARALOGS_MAX_LIMIT}, got {limit}")
    locus = validators.assert_valid_locus(locus, backend="Ensembl Plants")
    slug = organisms.ensembl_slug_for(organism)
    prefix = organisms.ensembl_id_prefix_for(organism)
    stored = await _get(
        client,
        f"/homology/id/{slug}/{wire_id(locus, organism)}",
        params={"compara": "plants", "type": "paralogues", "sequence": "none"},
        shape=_paralogues_shape(slug, prefix),
    )
    found = stored["found"]
    if not found:
        # Raises NotFoundError for an id Ensembl does not know.
        await lookup_locus(client, locus, organism=organism)
    rows = sorted(stored["rows"], key=lambda p: (-(p["perc_id"] or 0.0), p["locus"]))
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["type"]] = counts.get(row["type"], 0) + 1
    return {
        "locus": locus,
        "organism": organisms.resolve(organism).canonical,
        "found": found,
        "total": len(rows),
        "returned": min(len(rows), limit),
        "truncated": len(rows) > limit,
        "counts_by_type": counts,
        "paralogs": rows[:limit],
        "upstream_version": None,
    }


ASSEMBLY_DEFAULT_LIMIT = 100
ASSEMBLY_MAX_LIMIT = 2000


def _region(row: Any) -> tuple[str, int, str | None]:
    try:
        name = row["name"]
        length = row["length"]
    except (KeyError, TypeError) as exc:
        raise _http.UnreadableBody(
            f"top-level region without name or length ({exc!r}): {row!r:.200}"
        ) from None
    if not isinstance(name, str) or not isinstance(length, int) or length < 1:
        raise _http.UnreadableBody(f"region with unusable name or length: {row!r:.200}")
    return name, length, row.get("coord_system")


def _assembly_shape(raw: object) -> dict[str, Any]:
    """An /info/assembly body read to its regions, before the store: a region
    read afterwards failed from the cache for the whole TTL without asking
    again. Names are unique, and every karyotype name is a top-level region."""
    body = _http.expect_object(raw)
    top_level = body.get("top_level_region")
    if not isinstance(top_level, list) or not top_level:
        raise _http.UnreadableBody(f"no top-level regions in {str(body)[:120]}")
    karyotype = body.get("karyotype") or []
    if not isinstance(karyotype, list) or not all(isinstance(n, str) for n in karyotype):
        raise _http.UnreadableBody(f"karyotype is not a list of names: {karyotype!r:.120}")
    regions = [_region(row) for row in top_level]
    names = [name for name, _, _ in regions]
    if len(set(names)) != len(names):
        dupes = sorted({name for name in names if names.count(name) > 1})
        raise _http.UnreadableBody(f"repeated region names {dupes}")
    missing = [name for name in karyotype if name not in set(names)]
    if missing:
        raise _http.UnreadableBody(
            f"karyotype names regions absent from the top-level list: {missing}"
        )
    return {**body, "karyotype": karyotype, "top_level_region": regions}


async def assembly(
    client: httpx.AsyncClient,
    organism: str | int = organisms.DEFAULT_ORGANISM,
    limit: int = ASSEMBLY_DEFAULT_LIMIT,
) -> dict[str, Any]:
    """An organism's Ensembl assembly: its name, accession, karyotype, and every
    top-level seq-region with its length, via ``/info/assembly``.

    The names are the ``region`` values ``region_query`` takes; ``/overlap/region``
    refuses a start past a region's length (an end past it is answered). Karyotype regions
    come first, in Ensembl's karyotype order, then the rest by length
    (longest first). ``total`` counts every top-level region before ``limit``
    cuts. The coordinate-system label differs between assemblies (tomato's
    chromosomes are ``primary_assembly`` regions named ``CM001064.4`` ...), so
    ``in_karyotype``, not ``coord_system``, says which regions are chromosomes.
    """
    if not 1 <= limit <= ASSEMBLY_MAX_LIMIT:
        raise ValueError(f"limit must be in 1..{ASSEMBLY_MAX_LIMIT}, got {limit}")
    slug = organisms.ensembl_slug_for(organism)
    raw = await _get(client, f"/info/assembly/{slug}", shape=_assembly_shape)
    karyotype: list[str] = raw["karyotype"]
    order = {name: i for i, name in enumerate(karyotype)}
    regions = sorted(
        raw["top_level_region"],
        key=lambda r: (0, order[r[0]], 0, "") if r[0] in order else (1, 0, -r[1], r[0]),
    )
    rows = [
        {"name": name, "length": length, "coord_system": coord, "in_karyotype": name in order}
        for name, length, coord in regions
    ]
    return {
        "organism": organisms.resolve(organism).canonical,
        "assembly_name": raw.get("assembly_name"),
        "assembly_accession": raw.get("assembly_accession"),
        "assembly_date": raw.get("assembly_date"),
        "karyotype": list(karyotype),
        "total": len(rows),
        "returned": min(len(rows), limit),
        "truncated": len(rows) > limit,
        "regions": rows[:limit],
        "upstream_version": None,
    }
