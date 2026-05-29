"""BAR (Bio-Analytic Resource for Plant Biology) backend — async httpx wrapper around bar.utoronto.ca/api.

BAR is the U Toronto plant bioinformatics hub: ThaleMine InterMine front-end
for Arabidopsis annotation, eFP browser microarray data, AIV curated PPIs,
and a rice PPI lane. Global Core Biodata Resource (2023); NSERC + Genome
Canada (OGI-162) funded. Keyless — no auth, no API key, no rate-limit
headers observed in probing.

Probe writeup: ``docs/superpowers/audits/2026-05-23-bar-api-probe.md``.

Endpoints this module wraps:

  GET /thalemine/gene_information/{locus} → positional JSON array:
    [agi, full_name, tair_locus_id, brief_description, symbol, synonyms,
     computational_description, curator_summary, tair_short_description]
  GET /gaia/aliases/{locus} → {species, locus, geneid, aliases:[...]}
  GET /microarray_gene_expression/world_efp/arabidopsis/{locus} → eFP world map
  GET /interactions/get_paper_by_agi/{locus} → curated Arabidopsis GRN papers
  GET /interactions/rice/{locus} → rice PPI rows

ThaleMine carries only Arabidopsis (taxon 3702); yeast/human present for
ortholog cross-reference but not plant data. Rice support is limited to
the /interactions/rice/ lane.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from plant_genomics_mcp import __version__, _http, cache, organisms, validators
from plant_genomics_mcp.errors import (
    NotFoundError,
    OrganismNotSupported,
    PlantGenomicsError,
)

BASE_URL = "https://bar.utoronto.ca/api"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3
CACHE_TTL_SECONDS = 3600.0  # 1h — BAR doesn't version-stamp releases

# Locus validation uses the shared validators.assert_valid_locus (\Z-anchored)
# so every path-interpolating backend rejects the same inputs — including a
# trailing newline, which the old local `$`-anchored regex let through (audit P2).
# BAR's accepted forms (AGI "AT1G01010", MSU rice "LOC_Os01g01080", synonyms)
# all match the shared [A-Za-z0-9._-] class.

_CACHE = cache.TTLCache(default_ttl=CACHE_TTL_SECONDS)


def _user_agent() -> str:
    return f"plant-genomics-mcp/{__version__}"


async def _get(
    client: httpx.AsyncClient,
    path: str,
    params: dict[str, Any] | None = None,
) -> Any:
    """GET JSON from BAR with retry + cache. Raises typed errors on failure."""
    key = cache.make_key("GET", BASE_URL, path, params)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    resp = await _http.request_with_retry(
        client,
        "GET",
        f"{BASE_URL}{path}",
        service=f"BAR {path}",
        params=params,
        headers={"Accept": "application/json", "User-Agent": _user_agent()},
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
    )
    try:
        result = resp.json()
    except ValueError as e:
        raise PlantGenomicsError(f"BAR {path} returned non-JSON: {resp.text[:200]}") from e
    _CACHE.set(key, result)
    return result


# BAR uses a body-level success envelope, not HTTP status. Both endpoints
# return HTTP 200 even for missing loci; the `wasSuccessful` flag carries
# the real outcome. Failure modes seen live 2026-05-23:
#   /thalemine/gene_information/AT1G99999  → 200 {"wasSuccessful":true,"results":[]}
#   /thalemine/gene_information/LOC_Os01g01080 → 400 {"wasSuccessful":false,"error":"Invalid gene id"}
#   /gaia/aliases/AT1G99999                → 200 {"wasSuccessful":false,"error":"Nothing found"}

# Positional indices into thalemine's results[0] row. Order is fixed by the
# InterMine `views` list (column ordering survives schema versions). Live
# shape (probed 2026-05-23 against AT1G01010):
#   [0] "AT1G01010"                                          → agi (Gene.primaryIdentifier)
#   [1] "NAC domain containing protein 1"                    → full_name (Gene.name)
#   [2] "locus:2200935"                                      → tair_locus_id (Gene.secondaryIdentifier)
#   [3] "NAC domain containing protein 1"                    → brief_description (Gene.briefDescription)
#   [4] "NAC001"                                              → symbol (Gene.symbol)
#   [5] "ANAC001, NAC001, NTL10"                             → synonyms (Gene.tairAliases, CSV)
#   [6] "NAC domain containing protein 1;(source:Araport11)" → computational_description (Gene.tairComputationalDescription)
#   [7] "Member of the NAC domain containing family..."      → curator_summary (Gene.tairCuratorSummary)
#   [8] "NAC domain containing protein 1"                    → tair_short_description (Gene.tairShortDescription)
_GI_AGI = 0
_GI_FULL_NAME = 1
_GI_TAIR_LOCUS_ID = 2
_GI_BRIEF = 3
_GI_SYMBOL = 4
_GI_SYNONYMS = 5
_GI_COMPUTATIONAL = 6
_GI_CURATOR = 7
_GI_TAIR_SHORT = 8


async def gene_summary(
    client: httpx.AsyncClient,
    locus: str,
) -> dict[str, Any]:
    """Fetch Arabidopsis gene summary from BAR/ThaleMine + /gaia/aliases/.

    Two BAR endpoints fetched in parallel:
      - ``/thalemine/gene_information/{locus}`` → InterMine envelope with
        ``results: [[positional]]`` carrying TAIR curator summary + Araport11
        computational description.
      - ``/gaia/aliases/{locus}`` → ``{data: [{species, locus, geneid,
        aliases:[...]}]}`` carrying NCBI Gene ID + cross-DB synonyms
        (RefSeq, UniProt, locus-model IDs).

    Arabidopsis only — BAR's ThaleMine instance carries taxon 3702 plus
    yeast/human for ortholog cross-reference. Non-Arabidopsis loci raise
    NotFoundError because thalemine 400s and gaia returns empty.

    Aliases are best-effort: a /gaia failure does not fail the call, since
    the canonical TAIR fields come from thalemine. Returns ``ncbi_gene_id:
    None`` and ``aliases: []`` in that case.
    """
    validators.assert_valid_locus(locus, backend="BAR")
    gi_env, aliases_env = await asyncio.gather(
        _get(client, f"/thalemine/gene_information/{locus}"),
        _get(client, f"/gaia/aliases/{locus}"),
        return_exceptions=True,
    )
    if isinstance(gi_env, BaseException):
        raise gi_env
    if not isinstance(gi_env, dict) or not gi_env.get("wasSuccessful"):
        err = gi_env.get("error") if isinstance(gi_env, dict) else "non-dict response"
        raise NotFoundError(f"BAR /thalemine/gene_information/{locus}: {err}")
    results = gi_env.get("results") or []
    if not results:
        raise NotFoundError(
            f"BAR /thalemine/gene_information/{locus}: no record (wasSuccessful but empty results)"
        )
    row = results[0]
    if not isinstance(row, list) or len(row) <= _GI_TAIR_SHORT:
        raise NotFoundError(
            f"BAR /thalemine/gene_information/{locus}: malformed row "
            f"({type(row).__name__} of len {len(row) if hasattr(row, '__len__') else '?'})"
        )

    ncbi_gene_id: str | None = None
    aliases_list: list[str] = []
    if (
        not isinstance(aliases_env, BaseException)
        and isinstance(aliases_env, dict)
        and aliases_env.get("wasSuccessful")
    ):
        entries = aliases_env.get("data") or []
        # /gaia/aliases/ can return multiple entries per locus (case-variant
        # rows, e.g. At1g01010 + AT1G01010 — only the uppercase one carries
        # the geneid). Prefer the exact-uppercase match, then any entry with
        # a non-null geneid, then the first entry.
        chosen = next(
            (
                e
                for e in entries
                if e.get("geneid") and (e.get("locus") or "").upper() == locus.upper()
            ),
            None,
        )
        if chosen is None:
            chosen = next((e for e in entries if e.get("geneid")), None)
        if chosen is None and entries:
            chosen = entries[0]
        if chosen is not None:
            ncbi_gene_id = chosen.get("geneid")
            aliases_list = list(chosen.get("aliases") or [])

    return {
        "locus": locus,
        "agi": row[_GI_AGI],
        "symbol": row[_GI_SYMBOL],
        "full_name": row[_GI_FULL_NAME],
        "tair_locus_id": row[_GI_TAIR_LOCUS_ID],
        "synonyms": [s.strip() for s in row[_GI_SYNONYMS].split(",")] if row[_GI_SYNONYMS] else [],
        "computational_description": row[_GI_COMPUTATIONAL],
        "curator_summary": row[_GI_CURATOR],
        "brief_description": row[_GI_BRIEF],
        "tair_short_description": row[_GI_TAIR_SHORT],
        "ncbi_gene_id": ncbi_gene_id,
        "aliases": aliases_list,
        "species": "arabidopsis_thaliana",
        "source_url": f"https://bar.utoronto.ca/api/thalemine/gene_information/{locus}",
    }


# BAR encodes ecotype provenance inline in the `id` field with literal "<br>"
# separators (the same string is rendered into the eFP browser HTML). We strip
# the first <br>-suffixed chunk so clients get a clean ecotype name without
# having to parse HTML, but keep the structured `position` lat/lng for callers
# that want the geographic dimension. Trade-off: this drops the climate
# fragment (temp, precipitation); use the source URL to recover full provenance.
def _strip_html_break(text: str) -> str:
    return text.split("<br>", 1)[0].strip() if "<br>" in text else text.strip()


async def efp_expression(
    client: httpx.AsyncClient,
    locus: str,
) -> dict[str, Any]:
    """Fetch BAR/eFP world-map natural-variation expression for an Arabidopsis locus.

    Wraps ``/microarray_gene_expression/world_efp/arabidopsis/{locus}`` —
    the world-eFP view returns expression across ~36 ecotypes (Bay-0, Col-0,
    Cvi-1, Ler-2, ...) with per-replicate values and collection lat/lng. Each
    ecotype carries the same probeset (one microarray probe per gene).

    Arabidopsis only — the endpoint hard-codes the ``arabidopsis`` species
    path component. Unknown valid AGI loci 200 with wasSuccessful=false
    ("There are no data found..."); invalid loci 200 with "Invalid gene id".
    Both surface as NotFoundError.

    Per-ecotype mean is computed from ``values`` to save the caller a pass;
    everything else passes through unmodified except ``id``, which we
    HTML-strip down to the leading ecotype label.
    """
    validators.assert_valid_locus(locus, backend="BAR")
    path = f"/microarray_gene_expression/world_efp/arabidopsis/{locus}"
    env = await _get(client, path)
    if not isinstance(env, dict) or not env.get("wasSuccessful"):
        err = env.get("error") if isinstance(env, dict) else "non-dict response"
        # Map upstream "There are no data found..." through to NotFoundError so
        # callers get a typed miss rather than a generic upstream error.
        raise NotFoundError(f"BAR {path}: {err}")
    data = env.get("data") or {}
    if not isinstance(data, dict) or not data:
        raise NotFoundError(f"BAR {path}: wasSuccessful but empty data")

    ecotypes: list[dict[str, Any]] = []
    probeset: str | None = None
    for code, entry in data.items():
        if not isinstance(entry, dict):
            continue
        values = entry.get("values") or {}
        mean = sum(values.values()) / len(values) if values else None
        if probeset is None:
            probeset = entry.get("probeset")
        ecotypes.append(
            {
                "code": entry.get("code", code),
                "name": _strip_html_break(entry.get("id") or ""),
                "samples": list(entry.get("samples") or []),
                "ctrl_samples": list(entry.get("ctrlSamples") or []),
                "values": dict(values),
                "mean": mean,
                "position": entry.get("position"),
                "source": entry.get("source"),
            }
        )

    return {
        "locus": locus,
        "probeset": probeset,
        "ecotype_count": len(ecotypes),
        "ecotypes": ecotypes,
        "species": "arabidopsis_thaliana",
        "source_url": f"https://bar.utoronto.ca/api{path}",
    }


# BAR AIV (Arabidopsis Interactions Viewer) covers two organism lanes with
# completely different response shapes, so the unified function uses a `kind`
# discriminator on the envelope:
#   - arabidopsis  /interactions/get_paper_by_agi/{locus}  →  kind="grn_papers"
#     curated GRN paper refs (pmid, title, image, comments, tags)
#   - rice         /interactions/rice/{locus}              →  kind="ppi_predictions"
#     predicted PPIs (protein_2 = partner, pcc = co-expression Pearson r)
# Both fail at HTTP 400 (not 200+wasSuccessful=false) for unknown / wrong-format
# loci, so error paths surface through _get as PlantGenomicsError rather than
# NotFoundError. Rice strictly requires the MSU LOC_Os* format; RAP-DB
# (Os01g0100100) is rejected upstream with "Invalid species or gene ID".
_AIV_SUPPORTED_ORGANISMS = ("arabidopsis_thaliana", "oryza_sativa")


def _aiv_arabidopsis_envelope(locus: str, data: list[Any]) -> dict[str, Any]:
    papers: list[dict[str, Any]] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        tags_raw = entry.get("tags") or ""
        tags = [t for t in tags_raw.split("|") if t] if isinstance(tags_raw, str) else []
        papers.append(
            {
                "source_id": entry.get("source_id"),
                "pmid": entry.get("source_name"),
                "title": entry.get("grn_title"),
                "image_url": entry.get("image_url"),
                "comments": entry.get("comments"),
                "cyjs_layout": entry.get("cyjs_layout"),
                "tags": tags,
            }
        )
    return {
        "locus": locus,
        "organism": "arabidopsis_thaliana",
        "kind": "grn_papers",
        "count": len(papers),
        "papers": papers,
        "source_url": f"https://bar.utoronto.ca/api/interactions/get_paper_by_agi/{locus}",
    }


def _aiv_rice_envelope(locus: str, data: list[Any]) -> dict[str, Any]:
    partners: list[dict[str, Any]] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        p1 = entry.get("protein_1")
        p2 = entry.get("protein_2")
        # protein_1 is the queried locus in observed responses; partner_locus
        # is the non-queried side. If protein_1 != input (defensive), fall back
        # to protein_1 as the partner.
        partner = p2 if p1 == locus else p1
        partners.append(
            {
                "partner_locus": partner,
                "protein_1": p1,
                "protein_2": p2,
                "pcc": entry.get("pcc"),
                "total_hits": entry.get("total_hits"),
                "num_species": entry.get("Num_species"),
                "quality": entry.get("Quality"),
            }
        )
    return {
        "locus": locus,
        "organism": "oryza_sativa",
        "kind": "ppi_predictions",
        "count": len(partners),
        "partners": partners,
        "source_url": f"https://bar.utoronto.ca/api/interactions/rice/{locus}",
    }


async def aiv_interactions(
    client: httpx.AsyncClient,
    locus: str,
    organism: str = "arabidopsis_thaliana",
) -> dict[str, Any]:
    """Fetch BAR AIV interactions for an Arabidopsis or rice locus.

    Dispatches to one of two BAR endpoints by canonical organism slug:

      arabidopsis_thaliana → ``/interactions/get_paper_by_agi/{locus}``
                             curated GRN paper refs (kind="grn_papers")
      oryza_sativa         → ``/interactions/rice/{locus}``
                             predicted PPI partners with PCC (kind="ppi_predictions")

    Other plant organisms in the registry have no AIV lane and raise
    OrganismNotSupported. Unknown organism strings raise OrganismNotFound.

    BAR AIV returns HTTP 400 (not 200+wasSuccessful=false) for unknown loci
    and wrong-format inputs, so those failures surface as PlantGenomicsError
    from ``_get``, with the upstream "Invalid AGI" / "no data" / "Invalid
    species or gene ID" message preserved in the exception text.

    Rice requires the MSU ``LOC_Os*`` format; RAP-DB (``Os*g*``) is rejected
    upstream — match locus format to organism before calling.
    """
    validators.assert_valid_locus(locus, backend="BAR")
    record = organisms.resolve(organism)
    canonical = record.canonical
    if canonical == "arabidopsis_thaliana":
        path = f"/interactions/get_paper_by_agi/{locus}"
        builder = _aiv_arabidopsis_envelope
    elif canonical == "oryza_sativa":
        path = f"/interactions/rice/{locus}"
        builder = _aiv_rice_envelope
    else:
        raise OrganismNotSupported(
            backend="bar_aiv",
            organism=canonical,
            supported=list(_AIV_SUPPORTED_ORGANISMS),
        )
    env = await _get(client, path)
    if not isinstance(env, dict) or not env.get("wasSuccessful"):
        err = env.get("error") if isinstance(env, dict) else "non-dict response"
        raise NotFoundError(f"BAR {path}: {err}")
    data = env.get("data") or []
    if not isinstance(data, list):
        raise NotFoundError(f"BAR {path}: malformed data ({type(data).__name__})")
    return builder(locus, data)
