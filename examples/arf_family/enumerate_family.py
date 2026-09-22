"""Enumerate the family behind `genes.tsv` through the MCP alone.

Seeds are the loci already in `genes.tsv`. Everything else is discovered
by calling the live server over stdio through `McpClient` — no gene list
is typed in here, and nothing imports the server's Python package. Each
stage is a tool the server publishes; where a stage cannot do what it
is asked, that is recorded in `family_candidates.tsv` and logged as a
gap by the caller, never patched around with a hand-written list.

Stages, in order:

1. **Paralog closure** (`gramene_homologs`, `homology_type="paralog"`):
   from every seed, then from every accepted member, until no new locus
   appears. This is the brief's algorithm. On the live server it returns
   `total: 0` for two of the three seeds, so on its own it does not reach
   the family — the reason stage 2 exists.
2. **Region scan** (`ensembl_region_query`): walk every chromosome of the
   seed organism in `WINDOW`-sized windows and collect every
   protein-coding gene whose Ensembl `description` or `external_name`
   matches `CANDIDATE_RE`. Ensembl's free-text description is not a
   family label (`gaps.jsonl`, `free-text-not-a-label`); it is used here
   only to pick candidates, and every candidate is then decided by
   InterPro. The chromosome list and each chromosome's length are not
   published by any tool: the scan starts from `region "1"` and walks
   upward until a region name is unknown, and learns each length from the
   HTTP 400 Ensembl returns for a window past the end.
3. **InterPro arbiter** (`interpro_domains`): a candidate is a member iff
   its InterPro entries include `FAMILY_ENTRY`. Every candidate is written
   to `family_candidates.tsv` with `kept` in `true` / `false` /
   `undecided` — the last for a candidate whose call failed, which is not
   a rejection — and the entries it carries, so the rejections are as
   checkable as the members.
4. **Orthologs** (`gramene_homologs` with `homology_type="ortholog"` and
   `orthodb_orthologs`): from every accepted member, keep hits that the
   locus-id shape or OrthoDB's own `organism` field places in one of
   `ORTHOLOG_ORGANISMS`; then close over in-species paralogs and
   cross-species orthologs among those; verify each with
   `interpro_domains` in its organism. The two tools' per-organism sets are
   kept side by side in `ortholog_sources.tsv` — disagreement is data.
5. **Annotation**: `panther_family` for `panther_subfamily`,
   `ensembl_plants_lookup_locus` for `symbol` (`display_name`), and
   `verify_genes._has_pb1_domain` on the InterPro payload already fetched.

`genes.tsv` is rewritten with the original rows first and byte-identical
(asserted), then every new member sorted by organism and locus. Every MCP
call is appended to `enumeration_calls.jsonl` (tool, args, ok, bytes,
elapsed), so the cost of the enumeration is on the record.
"""

from __future__ import annotations

import asyncio
import csv
import json
import re
import signal
import sys
import time
from pathlib import Path

from examples.arf_family.mcp_client import SERVER_CMD, McpClient
from examples.arf_family.verify_genes import _has_pb1_domain

HERE = Path(__file__).parent
GENES_TSV = HERE / "genes.tsv"
CANDIDATES_TSV = HERE / "family_candidates.tsv"
ORTHOLOG_SOURCES_TSV = HERE / "ortholog_sources.tsv"
ENUM_CALLS = HERE / "enumeration_calls.jsonl"

FAMILY_ENTRY = "IPR010525"
WINDOW = 4_000_000  # bases per ensembl_region_query; 5 Mb 500s, 4 Mb answers
MIN_WINDOW = 500_000  # the walk halves the window on an upstream timeout, down to this
RETRY_PAUSE_S = 10.0
WALLTIME_S = 3 * 3600

# Free text is a candidate filter only (stage 2); InterPro decides (stage 3).
# `B3` is included because some descriptions name only that, not the
# family.
CANDIDATE_RE = re.compile(r"auxin|\bARF|\bB3\b", re.IGNORECASE)

# Locus-id shapes per organism, for classifying `gramene_homologs` rows,
# which carry no per-row taxon (`gaps.jsonl`, `identifier-with-no-tool`).
LOCUS_SHAPES: dict[str, re.Pattern[str]] = {
    "arabidopsis_thaliana": re.compile(r"^AT[1-5CM]G\d{5}$"),
    "oryza_sativa": re.compile(r"^Os\d{2}g\d{7}$"),
    "triticum_aestivum": re.compile(r"^TraesCS\w+G\d+$"),
}
# OrthoDB's `organism` strings for the same three.
ORTHODB_ORGANISMS: dict[str, str] = {
    "arabidopsis_thaliana": "Arabidopsis thaliana",
    "oryza_sativa": "Oryza sativa",
    "triticum_aestivum": "Triticum aestivum",
}
ORTHOLOG_ORGANISMS = ("oryza_sativa", "triticum_aestivum")

GENES_FIELDS = ["locus", "symbol", "organism", "panther_subfamily", "has_pb1_domain"]
CANDIDATE_FIELDS = ["locus", "organism", "source", "kept", "interpro_entries"]
ORTHOLOG_SOURCE_FIELDS = ["query_locus", "tool", "organism", "hits", "hits_elsewhere"]

_PAST_END_RE = re.compile(r"greater than (\d+) for ")
_NO_SLICE_RE = re.compile(r"No slice found for location")
_UNAVAILABLE_RE = re.compile(r"\[UpstreamUnavailableError\]")


class EnumerationError(Exception):
    """A stage could not run at all — the run stops rather than continuing
    on a partial candidate set that would read as 'the family is small'."""


def organism_of_locus(locus: str) -> str | None:
    for organism, shape in LOCUS_SHAPES.items():
        if shape.match(locus):
            return organism
    return None


def interpro_entries(payload: dict) -> list[str]:
    """Sorted, de-duplicated InterPro accessions in an `interpro_domains` payload."""
    found: set[str] = set()
    for d in payload.get("domains", []):
        for key in ("interpro", "accession"):
            v = d.get(key)
            if isinstance(v, str) and v.startswith("IPR"):
                found.add(v)
    return sorted(found)


class Enumerator:
    def __init__(self, client: McpClient, calls_log) -> None:
        self.c = client
        self.log = calls_log
        # locus -> row of family_candidates.tsv
        self.candidates: dict[str, dict] = {}
        # locus -> interpro payload for accepted members (for pb1)
        self.interpro: dict[str, dict] = {}
        self.ortholog_sources: list[dict] = []

    async def call(self, tool: str, args: dict):
        res = await self.c.call(tool, args)
        self.log.write(
            json.dumps(
                {
                    "tool": tool,
                    "args": args,
                    "ok": res.ok,
                    "error": res.error,
                    "elapsed_s": round(res.elapsed_s, 2),
                    "n_bytes": res.n_bytes,
                    "ts": int(time.time()),
                }
            )
            + "\n"
        )
        self.log.flush()
        print(f"  {tool}({args}) -> {'ok' if res.ok else 'ERR'} {res.n_bytes}B", file=sys.stderr)
        return res

    # -- stage 1 / 4a: paralogs -------------------------------------------

    async def paralogs(self, locus: str) -> list[str]:
        res = await self.call("gramene_homologs", {"locus": locus, "homology_type": "paralog"})
        if not res.ok:
            raise EnumerationError(f"gramene_homologs(paralog) failed on {locus}: {res.error}")
        return [h["target_locus"] for h in res.payload["homologs"]]

    async def paralog_closure(self, start: set[str], organism: str, source: str) -> None:
        """Add paralogs of `start` and of every accepted new member until
        the accepted set stops growing. Only loci whose id shape belongs to
        `organism` are candidates; the rest are recorded as rejected with
        the shape mismatch as their reason."""
        frontier = sorted(start)
        queried: set[str] = set()
        while frontier:
            nxt: list[str] = []
            for locus in frontier:
                if locus in queried:
                    continue
                queried.add(locus)
                for hit in await self.paralogs(locus):
                    if hit in self.candidates:
                        continue
                    if organism_of_locus(hit) != organism:
                        self.candidates[hit] = {
                            "locus": hit,
                            "organism": organism,
                            "source": f"{source}:{locus}",
                            "kept": "false",
                            "interpro_entries": "not queried: locus id shape is not " + organism,
                        }
                        continue
                    if await self.decide(hit, organism, f"{source}:{locus}"):
                        nxt.append(hit)
            frontier = nxt

    # -- stage 2: region scan --------------------------------------------

    async def region_window(self, organism: str, region: str, start: int, end: int):
        return await self.call(
            "ensembl_region_query",
            {"region": region, "start": start, "end": end, "organism": organism, "feature": "gene"},
        )

    async def scan_chromosome(self, organism: str, region: str) -> bool:
        """Walk one chromosome. Returns False if `region` is not a
        seq-region of this assembly (the walk over region names stops)."""
        start = 1
        window = WINDOW
        while True:
            res = await self.region_window(organism, region, start, start + window - 1)
            if not res.ok:
                m = _PAST_END_RE.search(res.error or "")
                if m and start > int(m.group(1)):
                    return True  # walked off the end: chromosome done
                if start == 1 and _NO_SLICE_RE.search(res.error or ""):
                    return False  # no such region name
                if _UNAVAILABLE_RE.search(res.error or "") and window > MIN_WINDOW:
                    # The tool already retried three times. A 25-minute walk
                    # must not die on one upstream timeout: halve the window
                    # and ask again; the failed call stays in the log.
                    window //= 2
                    await asyncio.sleep(RETRY_PAUSE_S)
                    continue
                raise EnumerationError(
                    f"ensembl_region_query {organism} {region}:{start}-{start + window - 1}: "
                    f"{res.error}"
                )
            for f in res.payload.get("features", []):
                if f.get("biotype") != "protein_coding":
                    continue
                text = f"{f.get('description') or ''} {f.get('external_name') or ''}"
                if not CANDIDATE_RE.search(text):
                    continue
                locus = f["gene_id"]
                if locus in self.candidates:
                    continue
                await self.decide(locus, organism, f"region:{region}:{start}")
            start += window

    async def scan_genome(self, organism: str) -> None:
        region = 1
        while await self.scan_chromosome(organism, str(region)):
            region += 1
        if region == 1:
            raise EnumerationError(f"{organism}: region '1' is not a seq-region; nothing scanned")

    # -- stage 3: InterPro arbiter ---------------------------------------

    async def decide(self, locus: str, organism: str, source: str) -> bool:
        """Query InterPro for `locus`, record the candidate row, return kept."""
        res = await self.call("interpro_domains", {"locus": locus, "organism": organism})
        if not res.ok:
            # A failed call is not a "no": the candidate stays undecided,
            # and the member count carries that margin.
            self.candidates[locus] = {
                "locus": locus,
                "organism": organism,
                "source": source,
                "kept": "undecided",
                "interpro_entries": f"call failed: {res.error}"[:300].strip(),
            }
            return False
        entries = interpro_entries(res.payload)
        kept = FAMILY_ENTRY in entries
        self.candidates[locus] = {
            "locus": locus,
            "organism": organism,
            "source": source,
            "kept": "true" if kept else "false",
            "interpro_entries": ",".join(entries) or "none",
        }
        if kept:
            self.interpro[locus] = res.payload
        return kept

    def members(self, organism: str) -> list[str]:
        return sorted(
            r["locus"]
            for r in self.candidates.values()
            if r["kept"] == "true" and r["organism"] == organism
        )

    # -- stage 4: orthologs ----------------------------------------------

    async def ortholog_hits(self, locus: str) -> dict[str, dict[str, set[str]]]:
        """Per-organism hit sets from both ortholog tools for one query locus."""
        out: dict[str, dict[str, set[str]]] = {
            "gramene_homologs": {o: set() for o in ORTHOLOG_ORGANISMS},
            "orthodb_orthologs": {o: set() for o in ORTHOLOG_ORGANISMS},
        }
        elsewhere = {"gramene_homologs": 0, "orthodb_orthologs": 0}
        res = await self.call("gramene_homologs", {"locus": locus, "homology_type": "ortholog"})
        if res.ok:
            for h in res.payload["homologs"]:
                org = organism_of_locus(h["target_locus"])
                if org in ORTHOLOG_ORGANISMS:
                    out["gramene_homologs"][org].add(h["target_locus"])
                else:
                    elsewhere["gramene_homologs"] += 1
        res = await self.call(
            "orthodb_orthologs", {"locus": locus, "organism": organism_of_locus(locus)}
        )
        if res.ok:
            for m in res.payload.get("members", []):
                org = next(
                    (
                        o
                        for o, name in ORTHODB_ORGANISMS.items()
                        if m.get("organism", "").startswith(name)
                    ),
                    None,
                )
                if org in ORTHOLOG_ORGANISMS:
                    out["orthodb_orthologs"][org].add(m["gene_id"])
                else:
                    elsewhere["orthodb_orthologs"] += 1
        for tool, per_org in out.items():
            for org, hits in per_org.items():
                self.ortholog_sources.append(
                    {
                        "query_locus": locus,
                        "tool": tool,
                        "organism": org,
                        "hits": ",".join(sorted(hits)) or "none",
                        "hits_elsewhere": elsewhere[tool],
                    }
                )
        return out

    async def orthologs(self, seeds: list[str]) -> None:
        frontier = list(seeds)
        queried: set[str] = set()
        while frontier:
            nxt: list[str] = []
            for locus in frontier:
                if locus in queried:
                    continue
                queried.add(locus)
                hits = await self.ortholog_hits(locus)
                for org in ORTHOLOG_ORGANISMS:
                    for tool in hits:
                        for hit in sorted(hits[tool][org]):
                            if hit in self.candidates:
                                continue
                            if organism_of_locus(hit) != org:
                                self.candidates[hit] = {
                                    "locus": hit,
                                    "organism": org,
                                    "source": f"{tool}:{locus}",
                                    "kept": "false",
                                    "interpro_entries": "not queried: id is not a locus of " + org,
                                }
                                continue
                            if await self.decide(hit, org, f"{tool}:{locus}"):
                                nxt.append(hit)
            frontier = nxt
        for org in ORTHOLOG_ORGANISMS:
            await self.paralog_closure(set(self.members(org)), org, "paralog")

    # -- stage 5: annotation ---------------------------------------------

    async def annotate(self, locus: str, organism: str) -> dict:
        p = await self.call("panther_family", {"locus": locus, "organism": organism})
        e = await self.call("ensembl_plants_lookup_locus", {"locus": locus, "organism": organism})
        if not p.ok or not e.ok:
            raise EnumerationError(
                f"annotation failed for {locus}: panther={p.error} ensembl={e.error}"
            )
        return {
            "locus": locus,
            "symbol": e.payload.get("display_name") or locus,
            "organism": organism,
            "panther_subfamily": p.payload.get("subfamily_id") or "",
            "has_pb1_domain": "true" if _has_pb1_domain(self.interpro[locus]) else "false",
        }


def read_genes(path: Path) -> list[dict]:
    with open(path) as f:
        return list(csv.DictReader(f, delimiter="\t"))


def write_tsv(path: Path, fields: list[str], rows: list[dict]) -> None:
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, delimiter="\t", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def _walltime_guard(*_: object) -> None:
    sys.stderr.write(f"aborting: walltime guard fired after {WALLTIME_S}s\n")
    sys.exit(2)


async def enumerate_family(
    seeds: list[dict],
    client: McpClient,
    calls_log,
    *,
    scan: bool = True,
    orthologs: bool = True,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Returns (genes rows, candidate rows, ortholog-source rows).

    `seeds` are rows of `genes.tsv`; they are returned first, unchanged.
    """
    seed_orgs = {r["organism"] for r in seeds}
    if len(seed_orgs) != 1:
        raise EnumerationError(f"seeds must share one organism, got {sorted(seed_orgs)}")
    organism = seed_orgs.pop()
    en = Enumerator(client, calls_log)
    seed_loci = [r["locus"] for r in seeds]
    for locus in seed_loci:
        if not await en.decide(locus, organism, "seed"):
            raise EnumerationError(f"seed {locus} does not carry {FAMILY_ENTRY}")
    await en.paralog_closure(set(seed_loci), organism, "paralog")
    if scan:
        await en.scan_genome(organism)
        await en.paralog_closure(set(en.members(organism)), organism, "paralog")
    if orthologs:
        await en.orthologs(en.members(organism))

    rows = list(seeds)
    have = {r["locus"] for r in seeds}
    for org in (organism, *ORTHOLOG_ORGANISMS):
        for locus in en.members(org):
            if locus not in have:
                rows.append(await en.annotate(locus, org))
    candidates = [en.candidates[k] for k in sorted(en.candidates)]
    return rows, candidates, en.ortholog_sources


async def main(server_cmd: list[str] = SERVER_CMD, here: Path = HERE) -> int:
    signal.signal(signal.SIGALRM, _walltime_guard)
    signal.alarm(WALLTIME_S)
    seeds = read_genes(here / "genes.tsv")
    original = (here / "genes.tsv").read_text()
    c = McpClient(server_cmd)
    await c.start()
    try:
        with open(here / "enumeration_calls.jsonl", "w") as log:
            rows, candidates, sources = await enumerate_family(seeds, c, log)
    finally:
        await c.close()
        signal.alarm(0)
    write_tsv(here / "genes.tsv", GENES_FIELDS, rows)
    new_text = (here / "genes.tsv").read_text()
    if not new_text.startswith(original):
        raise EnumerationError("the original genes.tsv rows are no longer a byte-identical prefix")
    write_tsv(here / "family_candidates.tsv", CANDIDATE_FIELDS, candidates)
    write_tsv(here / "ortholog_sources.tsv", ORTHOLOG_SOURCE_FIELDS, sources)
    by_org: dict[str, int] = {}
    for r in rows:
        by_org[r["organism"]] = by_org.get(r["organism"], 0) + 1
    print(f"members per organism: {by_org}; candidates: {len(candidates)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
