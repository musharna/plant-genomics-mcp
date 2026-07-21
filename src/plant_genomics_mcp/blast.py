"""NCBI BLAST URLAPI client — async Put/Get polling.

NCBI's BLAST URLAPI is asynchronous: a Put request submits a query and
returns a request ID (RID) plus an estimated time-to-completion (RTOE);
subsequent Get requests poll the same RID until ``Status=READY``. We wrap
that pattern in a single ``blast_sequence`` call that submits, polls with
progress notifications, and returns parsed top hits plus raw alignment
text.

NCBI etiquette (per https://blast.ncbi.nlm.nih.gov/doc/blast-help/developerinfo.html):
  - Do not contact the server more often than once every 10 seconds.
  - Do not poll for any single RID more often than once a minute.
  - Use the ``email`` and ``tool`` URL parameters on every request.

We honor the per-RID 60s floor by default (``min_poll_interval=60``) and
identify with ``tool=plant-genomics-mcp``. Caller can override the email
via ``PLANT_GENOMICS_MCP_NCBI_EMAIL`` env var (defaults to an unattributed
placeholder — NCBI accepts unattributed but flags overuse).

Put response carries the RID + RTOE inside a comment-style block:

    QBlastInfoBegin
        RID = <ID>
        RTOE = <seconds>
    QBlastInfoEnd

Get response (with FORMAT_OBJECT=SearchInfo) carries the status in the
same comment block:

    QBlastInfoBegin
        Status=WAITING            (or READY / FAILED / UNKNOWN)
    QBlastInfoEnd

The final results are fetched with FORMAT_TYPE=Text and parsed for the
"Sequences producing significant alignments" table to expose a structured
``hits[]`` list alongside the raw report.
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any

import httpx

from plant_genomics_mcp import _http, progress
from plant_genomics_mcp.errors import (
    NotFoundError,
    PlantGenomicsError,
    UpstreamUnavailableError,
)

BASE_URL = "https://blast.ncbi.nlm.nih.gov/Blast.cgi"
DEFAULT_TIMEOUT = 60.0
# Retry budget for each raw BLAST HTTP call (Put / Get). Routed through the
# shared _http.request_with_retry so a transient 429/5xx/transport blip on
# submit/poll/fetch doesn't hard-fail a multi-minute search (audit M2).
MAX_RETRIES = 3
TOOL_ID = "plant-genomics-mcp"

# Per-RID poll floor — NCBI asks for no more than one poll per minute per
# RID. The orchestrator clamps any caller-supplied interval up to this.
MIN_POLL_INTERVAL = 60.0
DEFAULT_POLL_INTERVAL = 60.0
DEFAULT_MAX_WAIT = 600.0  # 10 minutes — typical BLAST nr search

# Process-level cap on concurrent BLAST submissions. NCBI etiquette is "no
# more than one request every 10 seconds"; the public HTTP transport
# can fan out N parallel ``blast_sequence`` calls per client, so we hold
# the line here. Two simultaneous searches is a safe middle ground —
# leaves slots for synthesis fanouts (``find_homologs_synth``,
# ``consensus_homologs``) without exhausting NCBI goodwill.
MAX_CONCURRENT_BLAST = 2
_BLAST_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_BLAST)

# Reserved-TLD placeholder when the operator hasn't set the email env.
# ``.invalid`` is RFC 2606 — guaranteed unroutable; the tool name in the
# local-part lets NCBI ops pattern-match the traffic source if needed.
_UNCONFIGURED_EMAIL = "plant-genomics-mcp-unconfigured@example.invalid"

# Cap raw report bytes returned over the wire so a huge alignment doesn't
# blow the MCP payload. The full report is always available by re-fetching
# the RID directly.
RAW_REPORT_CAP_BYTES = 50_000

# Allowed BLAST programs. Database defaults are program-specific.
_PROGRAM_DEFAULTS = {
    "blastn": "core_nt",  # nucleotide vs nucleotide
    "blastp": "swissprot",  # protein vs protein (Swiss-Prot is small + curated)
    "blastx": "swissprot",  # translated-nucleotide vs protein
    "tblastn": "core_nt",  # protein vs translated-nucleotide
    "tblastx": "core_nt",  # translated vs translated
}

# RID + RTOE parsing — the QBlastInfo block uses spaces around '=' in Put
# responses but no spaces in Get/SearchInfo responses. Tolerate both.
_RE_RID = re.compile(r"\bRID\s*=\s*([A-Z0-9]+)")
_RE_RTOE = re.compile(r"\bRTOE\s*=\s*(\d+)")
_RE_STATUS = re.compile(r"\bStatus\s*=\s*([A-Z]+)")
# Hit-table row: "<accession> <description...>  <bit-score>  <evalue>  <ident%>"
# Confirmed against live NCBI BLAST output 2026-05-21 (header columns
# "(Bits)  Value  Ident"). The accession is the FIRST whitespace-delimited
# token; the trailing three columns are bit score, e-value, identity%.
# The summary block starts with "Sequences producing significant alignments:"
# and ends at the first blank line / "ALIGNMENTS" marker.


def _supported_program(program: str) -> str:
    if program not in _PROGRAM_DEFAULTS:
        raise PlantGenomicsError(
            f"BLAST program must be one of {sorted(_PROGRAM_DEFAULTS)}, got {program!r}"
        )
    return program


def _identity_params() -> dict[str, str]:
    """NCBI courtesy params — sent on every Put and Get."""
    return {
        "tool": TOOL_ID,
        "email": os.environ.get("PLANT_GENOMICS_MCP_NCBI_EMAIL", _UNCONFIGURED_EMAIL),
    }


def _parse_put_response(text: str) -> tuple[str, int]:
    """Extract (RID, RTOE) from the QBlastInfo block of a Put response."""
    m_rid = _RE_RID.search(text)
    m_rtoe = _RE_RTOE.search(text)
    if not m_rid:
        raise UpstreamUnavailableError(
            "BLAST Put response missing RID — first 200 chars: " + text[:200]
        )
    rid = m_rid.group(1)
    rtoe = int(m_rtoe.group(1)) if m_rtoe else 0
    return rid, rtoe


def _parse_status(text: str) -> str:
    """Extract the Status= value from a Get/SearchInfo response.

    Returns ``UNKNOWN`` if no Status block is present so callers can decide
    whether to keep waiting or treat the RID as expired.
    """
    m = _RE_STATUS.search(text)
    if not m:
        return "UNKNOWN"
    return m.group(1).upper()


def _parse_hit_table(text: str) -> list[dict[str, Any]]:
    """Parse the "Sequences producing significant alignments" block.

    The block sits between a header line ending in 'significant alignments:'
    and either a blank line followed by 'ALIGNMENTS' or two consecutive
    blank lines. Each row is whitespace-delimited; the layout (verified
    against live NCBI BLAST output 2026-05-21) is:

        <accession> <description...>  <bit_score>  <evalue>  <identity%>

    We surface accession (first token), identity (last token, kept as
    string because it ships with a literal "%"), e-value (second-to-last),
    bit score (third-to-last), and a description re-joined from the
    interior tokens.

    Returns an empty list if the marker isn't found — that happens for
    "no hits found" responses where the body skips straight to the
    alignments section. Caller can fall back to the raw report.
    """
    marker = "Sequences producing significant alignments:"
    idx = text.find(marker)
    if idx == -1:
        return []
    block = text[idx + len(marker) :]
    # Trim trailing alignments section.
    cut = block.find("\nALIGNMENTS")
    if cut != -1:
        block = block[:cut]
    hits: list[dict[str, Any]] = []
    for raw_line in block.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        # Skip header rows ("Description", column underlines, etc.).
        if line.lstrip().lower().startswith("description"):
            continue
        if set(line.strip()) <= {"-", " "}:
            continue
        parts = line.split()
        # Need accession + description + 3 trailing numeric columns.
        if len(parts) < 5:
            continue
        accession = parts[0]
        identity = parts[-1]
        evalue = parts[-2]
        bit_score = parts[-3]
        # Description = everything between accession and the 3 trailing
        # columns. Re-join by single space to collapse the column padding.
        desc_tokens = parts[1:-3]
        description = " ".join(desc_tokens).strip()
        # Defensive cast — accept the e-value as string if not parseable
        # (NCBI sometimes emits "0.0" / "1e-180" / "5e-04").
        try:
            evalue_num: float | str = float(evalue)
        except ValueError:
            evalue_num = evalue
        try:
            bit_score_num: float | str = float(bit_score)
        except ValueError:
            bit_score_num = bit_score
        hits.append(
            {
                "accession": accession,
                "description": description,
                "bit_score": bit_score_num,
                "evalue": evalue_num,
                "identity": identity,
            }
        )
    return hits


async def submit(
    client: httpx.AsyncClient,
    sequence: str,
    program: str,
    database: str,
    *,
    hitlist_size: int = 10,
    expect: float = 10.0,
    megablast: bool = False,
) -> tuple[str, int]:
    """Submit a BLAST search — returns (RID, RTOE in seconds).

    ``sequence`` may be raw or FASTA-formatted. ``database`` is the NCBI
    BLAST database slug (e.g. core_nt, swissprot, refseq_protein).
    ``megablast=True`` enables megablast (only meaningful for blastn).
    """
    program = _supported_program(program)
    data = {
        "CMD": "Put",
        "PROGRAM": program,
        "DATABASE": database,
        "QUERY": sequence,
        "HITLIST_SIZE": str(hitlist_size),
        "EXPECT": str(expect),
        "FORMAT_TYPE": "Text",
        **_identity_params(),
    }
    if megablast and program == "blastn":
        data["MEGABLAST"] = "on"
    resp = await _http.request_with_retry(
        client,
        "POST",
        BASE_URL,
        service="BLAST Put",
        data=data,
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
    )
    rid, rtoe = _parse_put_response(resp.text)
    await progress.notify(
        f"BLAST submitted — RID={rid}, RTOE={rtoe}s (program={program}, db={database})"
    )
    return rid, rtoe


async def poll_status(client: httpx.AsyncClient, rid: str) -> str:
    """Check the status of an in-flight BLAST RID.

    Returns one of WAITING / READY / FAILED / UNKNOWN.
    """
    params = {
        "CMD": "Get",
        "RID": rid,
        "FORMAT_OBJECT": "SearchInfo",
        **_identity_params(),
    }
    resp = await _http.request_with_retry(
        client,
        "GET",
        BASE_URL,
        service=f"BLAST Get(SearchInfo) RID={rid}",
        params=params,
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
    )
    return _parse_status(resp.text)


async def fetch_result(client: httpx.AsyncClient, rid: str) -> str:
    """Fetch the Text-format alignment report for a READY RID."""
    params = {
        "CMD": "Get",
        "RID": rid,
        "FORMAT_TYPE": "Text",
        **_identity_params(),
    }
    resp = await _http.request_with_retry(
        client,
        "GET",
        BASE_URL,
        service=f"BLAST Get(Text) RID={rid}",
        params=params,
        timeout=DEFAULT_TIMEOUT,
        max_retries=MAX_RETRIES,
    )
    return resp.text


async def blast_sequence(
    client: httpx.AsyncClient,
    sequence: str,
    program: str = "blastp",
    database: str | None = None,
    *,
    hitlist_size: int = 10,
    expect: float = 10.0,
    megablast: bool = False,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    max_wait: float = DEFAULT_MAX_WAIT,
) -> dict[str, Any]:
    """Submit + poll + fetch a BLAST search end-to-end.

    Emits a progress notification on submit, on each WAITING poll, and on
    READY transition. Honors NCBI's per-RID 60s poll floor — any caller
    interval below ``MIN_POLL_INTERVAL`` is clamped up.

    Raises:
      PlantGenomicsError on unknown program.
      UpstreamUnavailableError on HTTP failure or BLAST Status=FAILED.
      NotFoundError if the search times out (RID is still WAITING after
        max_wait); the RID is included so the caller can re-poll later.
    """
    program = _supported_program(program)
    db = database or _PROGRAM_DEFAULTS[program]
    interval = max(poll_interval, MIN_POLL_INTERVAL)
    if not os.environ.get("PLANT_GENOMICS_MCP_NCBI_EMAIL"):
        await progress.notify(
            "BLAST: PLANT_GENOMICS_MCP_NCBI_EMAIL is not set — using a "
            "placeholder address. NCBI may throttle or block requests "
            "without a real contact email; set the env var to your "
            "operator address before production use."
        )
    async with _BLAST_SEMAPHORE:
        rid, rtoe = await submit(
            client,
            sequence,
            program,
            db,
            hitlist_size=hitlist_size,
            expect=expect,
            megablast=megablast,
        )
        # NCBI's RTOE is usually a few seconds to a few minutes; wait that
        # long before the first poll to avoid hitting the server while the
        # search is still being queued. Capped to max_wait.
        initial_wait = max(0.0, min(float(rtoe), max_wait))
        if initial_wait > 0:
            await asyncio.sleep(initial_wait)
        elapsed = initial_wait
        status = "WAITING"
        while elapsed <= max_wait:
            status = await poll_status(client, rid)
            if status == "READY":
                await progress.notify(f"BLAST RID={rid} READY after {elapsed:.0f}s")
                break
            if status == "FAILED":
                raise UpstreamUnavailableError(
                    f"BLAST RID={rid} reported Status=FAILED after {elapsed:.0f}s"
                )
            if status == "UNKNOWN":
                raise NotFoundError(
                    f"BLAST RID={rid} reported Status=UNKNOWN — RID expired or never existed"
                )
            await progress.notify(f"BLAST RID={rid} {status} ({elapsed:.0f}s/{max_wait:.0f}s)")
            await asyncio.sleep(interval)
            elapsed += interval
        else:
            raise NotFoundError(
                f"BLAST RID={rid} still {status} after max_wait={max_wait:.0f}s — "
                "re-poll later via fetch_result()"
            )
        report = await fetch_result(client, rid)
    hits = _parse_hit_table(report)
    truncated = len(report.encode("utf-8")) > RAW_REPORT_CAP_BYTES
    raw_excerpt = report.encode("utf-8")[:RAW_REPORT_CAP_BYTES].decode("utf-8", "replace")
    return {
        "rid": rid,
        "program": program,
        "database": db,
        "status": "READY",
        "hitCount": len(hits),
        "hits": hits,
        "raw_report_excerpt": raw_excerpt,
        "raw_report_truncated": truncated,
        "elapsed_seconds": round(elapsed, 1),
    }
