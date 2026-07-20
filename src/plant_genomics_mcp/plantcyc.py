"""PlantCyc / PMN metabolism client — gene → enzyme → reactions → pathways.

PlantCyc / the Plant Metabolic Network (pmn.plantcyc.org) is a BioCyc-family
collection of species metabolic pathway databases (PGDBs). Its web-services
API — ``getxml`` (fetch a frame) and ``xmlquery`` (BioVelo query) — is FREE
and open (no API key), contrary to the pre-v1.13 stub in this module which
mis-classified it as subscription-gated (that 2026-05-21 probe hit the paid
bulk-download tier, not the web services; corrected + re-probed 2026-07-19).

There is no single per-gene "pathways" endpoint, so we walk the BioCyc data
model with bounded, cached, concurrency-limited ``getxml`` hops:

    locus → (xmlquery accession-1) gene frame
         → product monomer(s)
         → catalyzed reactions
         → in-pathway pathways

A locus with no metabolic annotation (e.g. a transcription factor — the
PGDBs are metabolism-only) resolves to zero results rather than an error.
Each species has its own PGDB, addressed by an orgid (AraCyc = ``ARA``,
OryzaCyc = ``ORYZA``, …) from ``organisms.plantcyc_orgid_for``.

Endpoint: https://pmn.plantcyc.org/getxml + /<orgid>/xmlquery (ptools-XML).
"""

from __future__ import annotations

import asyncio
import re
from typing import Any
from xml.etree import ElementTree as ET

import httpx

from plant_genomics_mcp import _http, cache, organisms
from plant_genomics_mcp.errors import NotFoundError, PlantGenomicsError

BASE_URL = "https://pmn.plantcyc.org"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3

# Traversal bounds — keep a pathological hub gene from fanning out into
# hundreds of getxml calls. A gene beyond these caps still reports its true
# totals via reaction_count / pathway_count.
MAX_REACTIONS = 25
MAX_PATHWAYS = 40
# Politeness cap on concurrent getxml calls to a single PMN host (BLAST-style).
_CONCURRENCY = 6

# Identifier whitelist (same shape as the tair/phytozome guards) — reject
# obviously-bogus input before building a BioVelo query string.
_LOCUS_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# Per-module response cache. Frames (reactions, pathways) are heavily shared
# across genes, so caching getxml by frame is a large win. See
# plant_genomics_mcp.cache for env knobs.
_CACHE = cache.TTLCache()


async def _getxml(client: httpx.AsyncClient, orgid: str, frame: str) -> ET.Element | None:
    """Fetch and parse one ptools-XML frame; return its root, or None on 404.

    404 means "no such frame in this PGDB" — a normal outcome (the locus is
    not a metabolic gene), surfaced as None rather than an exception.
    """
    key = cache.make_key("GET", BASE_URL, "/getxml", {"frame": f"{orgid}:{frame}"})
    cached = _CACHE.get(key)
    if cached is not None:
        return _parse(cached, orgid, frame)
    # getxml expects the raw ``?ORG:FRAME`` query, not a urlencoded key=value.
    url = f"{BASE_URL}/getxml?{orgid}:{frame}"
    resp = await _http.request_with_retry(
        client,
        "GET",
        url,
        service=f"PlantCyc getxml {orgid}:{frame}",
        headers={"Accept": "application/xml"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
        not_found_returns=None,
    )
    if resp is None:  # 404 sentinel
        return None
    text = resp.text
    _CACHE.set(key, text)
    return _parse(text, orgid, frame)


def _parse(text: str, orgid: str, frame: str) -> ET.Element:
    try:
        return ET.fromstring(text)
    except ET.ParseError as exc:
        raise PlantGenomicsError(
            f"PlantCyc getxml {orgid}:{frame} returned unparseable XML: {exc}"
        ) from exc


async def _resolve_gene_frame(client: httpx.AsyncClient, orgid: str, locus: str) -> str | None:
    """Resolve a locus to its PGDB gene frame id via a BioVelo accession query.

    Handles species where the gene frame id differs from the user's locus
    (in AraCyc the AGI *is* the frame id, but not universally). Returns None
    if no gene carries the locus as its ``accession-1``.
    """
    # BioVelo slot names must be UNQUOTED — quoting (x^"accession-1") returns
    # zero results (verified 2026-07-19). The locus is regex-restricted to
    # [A-Za-z0-9._-] upstream, so it needs no escaping inside the query string.
    query = f'[x:x<-{orgid.lower()}^^genes,x^accession-1="{locus}"]'
    url = f"{BASE_URL}/{orgid}/xmlquery"
    resp = await _http.request_with_retry(
        client,
        "GET",
        url,
        service=f"PlantCyc xmlquery {orgid}",
        params={"query": query},
        headers={"Accept": "application/xml"},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
    )
    root = _parse(resp.text, orgid, "xmlquery")
    # Result frames carry an ``ID`` attribute (e.g. ARA:AT3G51240); nested class
    # references (Unclassified-Genes, …) use ``resource=`` instead — skip those.
    for gene in root.findall(".//Gene"):
        if gene.get("ID"):
            return gene.get("frameid")
    return None


async def _gather_getxml(
    client: httpx.AsyncClient, orgid: str, frames: list[str]
) -> list[ET.Element | None]:
    """Fetch many frames with bounded concurrency (politeness to PMN)."""
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def one(frame: str) -> ET.Element | None:
        async with sem:
            return await _getxml(client, orgid, frame)

    return await asyncio.gather(*(one(f) for f in frames))


def _empty(locus: str, organism: str, orgid: str, gene_frame: str | None) -> dict[str, Any]:
    return {
        "locus": locus,
        "organism": organism,
        "orgid": orgid,
        "found": False,
        "gene_frame": gene_frame,
        "gene_common_name": None,
        "enzymes": [],
        "reactions": [],
        "pathways": [],
        "reaction_count": 0,
        "pathway_count": 0,
    }


async def lookup_locus(
    client: httpx.AsyncClient,
    locus: str,
    organism: str = organisms.DEFAULT_ORGANISM,
) -> dict[str, Any]:
    """Fetch metabolic enzyme / reaction / pathway annotation for a locus.

    Walks the PlantCyc/PMN data model (gene → enzyme → reactions → pathways)
    with bounded, cached getxml hops. Returns ``found=False`` with empty lists
    when the locus has no metabolic annotation in the organism's PGDB (e.g. a
    non-enzymatic gene) — not an error. ``reaction_count`` / ``pathway_count``
    report the true totals even when the returned lists are capped.
    """
    locus = locus.strip()
    if not _LOCUS_RE.match(locus):
        # Typed (not plain ValueError) so the [ClassName] wire prefix lets an
        # LLM client route on the failure — before any network call.
        raise NotFoundError(f"invalid locus {locus!r} (must match {_LOCUS_RE.pattern})")
    record = organisms.resolve(organism)
    orgid = organisms.plantcyc_orgid_for(organism)

    # Hop 0 — resolve locus → gene frame (uniform across species).
    gene_frame = await _resolve_gene_frame(client, orgid, locus)
    if gene_frame is None:
        return _empty(locus, record.canonical, orgid, None)

    # Hop 1 — gene frame → common name + product monomer(s).
    groot = await _getxml(client, orgid, gene_frame)
    if groot is None:
        return _empty(locus, record.canonical, orgid, gene_frame)
    gene = groot.find(".//Gene")
    gene_common = gene.findtext("common-name") if gene is not None else None
    monomers: list[str] = [
        fid for p in groot.findall(".//product/Protein") if (fid := p.get("frameid"))
    ]

    # Hop 2 — monomer(s) → catalyzed reactions.
    reactions: dict[str, str | None] = {}
    for mroot in await _gather_getxml(client, orgid, monomers):
        if mroot is None:
            continue
        for rxn in mroot.findall(".//catalyzes/Enzymatic-Reaction/reaction/Reaction"):
            fid = rxn.get("frameid")
            if fid:
                reactions.setdefault(fid, None)

    # Hop 3 — reactions → names + member pathways (capped, concurrency-bounded).
    reaction_ids = list(reactions)[:MAX_REACTIONS]
    pathways: dict[str, str | None] = {}
    for rid, rroot in zip(
        reaction_ids, await _gather_getxml(client, orgid, reaction_ids), strict=True
    ):
        if rroot is None:
            continue
        rnode = rroot.find(".//Reaction")
        if rnode is not None:
            reactions[rid] = rnode.findtext("common-name")
        for pw in rroot.findall(".//in-pathway/Pathway"):
            pid = pw.get("frameid")
            if pid:
                pathways.setdefault(pid, None)

    # Hop 4 — pathways → names (capped, concurrency-bounded).
    pathway_ids = list(pathways)[:MAX_PATHWAYS]
    for pid, proot in zip(
        pathway_ids, await _gather_getxml(client, orgid, pathway_ids), strict=True
    ):
        if proot is None:
            continue
        pnode = proot.find(".//Pathway")
        if pnode is not None:
            pathways[pid] = pnode.findtext("common-name")

    return {
        "locus": locus,
        "organism": record.canonical,
        "orgid": orgid,
        "found": True,
        "gene_frame": gene_frame,
        "gene_common_name": gene_common,
        "enzymes": monomers,
        "reactions": [{"id": rid, "name": reactions[rid]} for rid in reaction_ids],
        "pathways": [{"id": pid, "name": pathways[pid]} for pid in pathway_ids],
        "reaction_count": len(reactions),
        "pathway_count": len(pathways),
    }
