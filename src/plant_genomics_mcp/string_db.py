"""STRING-DB interaction-partners backend — async httpx wrapper around string-db.org.

STRING is the EMBL-hosted protein-protein interaction database. We query
``/api/json/interaction_partners`` to retrieve the first-neighbor network
for a protein, scored by predicted + curated + experimental confidence.

Input shape: tools accept either a UniProt accession (``Q0WV96``,
``P12345``) or a locus identifier (``AT1G01010``, ``Os01g0100100``). Both
go to STRING's own identifier resolver, which picks the canonical
species-scoped accession, in the spelling STRING's alias table carries
(:func:`string_query_id`): whole, never cut to a prefix. Pre-resolving loci through
UniProt produces accession-choice mismatches when a locus has multiple
valid UniProt accessions and STRING canonicalizes on a different one
(observed v1.1.0 with rice Os01g0100100 → UniProt Q0JRI1 vs STRING
A0A0P0UX28), so v1.1.1 removed the UniProt pre-resolution step.

STRING etiquette: pass ``caller_identity`` to identify the caller. We
hardcode ``plant-genomics-mcp``.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from plant_genomics_mcp import _http, cache, organisms, uniprot, validators
from plant_genomics_mcp.errors import (
    NotFoundError,
    PlantGenomicsError,
)

BASE_URL = "https://string-db.org"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3
CACHE_TTL_SECONDS = 3600.0  # 1h — matches uniprot._CACHE TTL for cache-stats uniformity.

DEFAULT_LIMIT = 20
MAX_LIMIT = 500
CALLER_IDENTITY = "plant-genomics-mcp"

_CACHE = cache.TTLCache(default_ttl=CACHE_TTL_SECONDS)

# Community locus spellings STRING's alias table does not carry, and the
# UniProt ORF-name spelling it does, per organism. Each pair was probed live
# on /api/json/get_string_ids (2026-09-22): the left form resolves to nothing,
# the right form to the gene's own protein. An optional ``.N`` transcript
# suffix is dropped (STRING indexes genes' proteins, not transcripts).
_STRING_LOCUS_SPELLING: dict[str, tuple[re.Pattern[str], str]] = {
    # Glyma.04G220900 -> GLYMA_04G220900 (K7KLM4)
    "glycine_max": (re.compile(r"^Glyma\.(\d{2}G\d{6})(?:\.\d+)?\Z", re.I), "GLYMA_{0}"),
    # Sobic.001G000100 -> SORBI_3001G000100 (C5WR12)
    "sorghum_bicolor": (re.compile(r"^Sobic\.(\d{3}G\d{6})(?:\.\d+)?\Z", re.I), "SORBI_3{0}"),
    # Bradi1g00200 -> BRADI_1g00200v3 (I1GKD6)
    "brachypodium_distachyon": (
        re.compile(r"^Bradi(\dg\d{5})(?:\.\d+)?\Z", re.I),
        "BRADI_{0}v3",
    ),
}

# Species whose STRING proteins carry no locus alias at all, only their UniProt
# accession: a locus is resolved through UniProt before it goes to STRING
# (#155). Wheat, probed on /api/json/get_string_ids (2026-09-25): the IWGSC
# gene (TraesCS3A02G449300), its transcript (.1) and its RefSeq name
# (LOC123063246) resolve to nothing; its accession A0A3B6EQF8 resolves to
# itself, as do 48 of the 66 wheat ARF accessions in examples/arf_family.
_STRING_KEYED_BY_UNIPROT = frozenset({"triticum_aestivum"})


def string_query_id(locus_or_accession: str, organism: str | int) -> str:
    """The identifier to send STRING for ``locus_or_accession`` in ``organism``.

    Audit 2026-09-22 H1: this used to be ``split(".", 1)[0]`` for every input,
    meant to drop a UniProt ``.N`` version. On a dotted locus it kept only the
    prefix — soybean ``Glyma.04G220900`` went out as ``Glyma``, which STRING
    resolved to an unrelated protein and answered for with confidence.
    Now a suffix is dropped only where it is a version (UniProt accession) or
    a transcript (AGI), a per-organism spelling is applied where STRING's
    aliases differ from the community form, and everything else goes whole.
    ``locus_or_accession`` must already be validated (canonical case).
    """
    if uniprot._looks_like_uniprot_accession(locus_or_accession):
        return locus_or_accession.partition(".")[0]
    if validators.AGI_RE.match(locus_or_accession):
        return locus_or_accession.partition(".")[0]
    spelling = _STRING_LOCUS_SPELLING.get(organisms.resolve(organism).canonical)
    if spelling is not None:
        pattern, template = spelling
        match = pattern.match(locus_or_accession)
        if match:
            return template.format(match.group(1))
    return locus_or_accession


async def _get(
    client: httpx.AsyncClient,
    path: str,
    params: dict[str, Any] | None = None,
) -> Any:
    return await _http.cached_get(
        client,
        _CACHE,
        f"{BASE_URL}{path}",
        service=f"STRING {path}",
        params=params,
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
    )


def _normalize(row: dict[str, Any], query_accession: str) -> dict[str, Any]:
    """Project one STRING interaction row to the surfaced field set.

    STRING returns symmetric A/B columns; the query protein is always on
    the A side, so we surface the B-side fields as the partner.

    This function owns the ``string_id: str | None`` invariant that
    synthesis._string_partner_locus consumes (#95): a non-string
    ``stringId_B`` is an upstream contract violation and raises here, so it
    lands as a status="error" step instead of an AttributeError downstream.
    """
    string_id = row.get("stringId_B")
    if string_id is not None and not isinstance(string_id, str):
        raise PlantGenomicsError(
            f"STRING interaction row for {query_accession!r}: stringId_B must be a string, "
            f"got {type(string_id).__name__} {string_id!r}"
        )
    return {
        "string_id": string_id,
        "accession": string_id,  # partner's stringId; UniProt mapping not always trivial
        "preferred_name": row.get("preferredName_B"),
        "score": row.get("score"),
        "escore": row.get("escore"),
        "dscore": row.get("dscore"),
        "tscore": row.get("tscore"),
        "pscore": row.get("pscore"),
    }


async def lookup_partners(
    client: httpx.AsyncClient,
    locus_or_accession: str,
    limit: int = DEFAULT_LIMIT,
    organism: str | int = organisms.DEFAULT_ORGANISM,
) -> dict[str, Any]:
    """Fetch STRING first-neighbor interactors for a protein.

    Accepts either a UniProt accession or a locus identifier; both go to
    STRING's ``/api/json/interaction_partners`` endpoint in the spelling its
    alias table carries (:func:`string_query_id`), except that a locus of a
    species STRING keys by UniProt accession alone (wheat) is resolved through
    UniProt first (#155). STRING's internal resolver picks the species-canonical
    accession and returns it in ``stringId_A`` (taxid-prefixed); we surface
    the bare accession as ``accession`` on the result. ``organism`` accepts
    any form the resolver supports (slug, scientific/common name, taxid).
    """
    # Validate before the value reaches cache.make_key / the wire — STRING is
    # the lone locus-accepting backend that previously skipped this, so a
    # caller identifier containing cache-key separators ('&', '=', '|') could
    # slip through (audit P6). UniProt accessions and loci both match the
    # [A-Za-z0-9._-] class, so this rejects only genuinely malformed input.
    locus_or_accession = validators.assert_valid_locus(locus_or_accession, backend="STRING")
    limit = max(1, min(limit, MAX_LIMIT))
    record = organisms.resolve(organism)
    taxid = organisms.string_taxid_for(organism)
    query = locus_or_accession
    identifier = string_query_id(locus_or_accession, organism)
    if record.canonical in _STRING_KEYED_BY_UNIPROT and not uniprot._looks_like_uniprot_accession(
        identifier
    ):
        try:
            entry = await uniprot.lookup_locus(client, identifier, organism=organism)
        except NotFoundError as exc:
            raise NotFoundError(
                f"STRING indexes {record.canonical} proteins by UniProt accession "
                f"only, and UniProt resolves no accession for {query}: {exc.args[0]}"
            ) from exc
        identifier = entry["primaryAccession"]

    try:
        raw = await _get(
            client,
            "/api/json/interaction_partners",
            params={
                "identifiers": identifier,
                "species": taxid,
                "limit": limit,
                "caller_identity": CALLER_IDENTITY,
            },
        )
    except NotFoundError as exc:
        raise NotFoundError(
            f"STRING has no protein for {query} in {record.canonical} "
            f"(queried as {identifier}): {exc.args[0]}"
        ) from exc
    if not isinstance(raw, list):
        raise PlantGenomicsError(
            f"STRING /api/json/interaction_partners returned non-list: {type(raw).__name__}"
        )
    if not raw:
        raise NotFoundError(
            f"STRING: no interaction partners for {query} (queried as {identifier})"
        )

    # STRING returns stringId_A as "{taxid}.{accession}"; the accession is
    # STRING's species-canonical pick, which may differ from the input
    # (e.g. locus → accession resolution, or one of several UniProt accessions).
    string_id_a = raw[0].get("stringId_A", "") if isinstance(raw[0], dict) else ""
    canonical_accession = string_id_a.split(".", 1)[1] if "." in string_id_a else query

    partners = [_normalize(r, canonical_accession) for r in raw if isinstance(r, dict)]
    return {
        "query": query,
        "accession": canonical_accession,
        "organism": record.canonical,
        # STRING's network API returns the top `limit` partners and states no total.
        **_http.counted(None, partners),
        "partners": partners,
        # Issue #121: uniform key; null because this backend states no release on
        # the answering response (headers probed live 2026-09-22).
        "upstream_version": None,
    }
