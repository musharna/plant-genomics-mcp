"""Verify `genes.tsv` against the live MCP server — fail loud on any mismatch.

Each row of `genes.tsv` carries two MCP-verifiable per-gene facts,
`panther_subfamily` and `has_pb1_domain`, and each is checked against two
MCP tools, through `McpClient`, never by eye and never by importing the
server's backend modules directly:

- ``interpro_domains``: InterPro entry IPR010525 must be present (the ARF
  family discriminator — Ensembl's free-text gene description is
  unreliable, e.g. ARF5's description does not match a
  "auxin response factor" regex; AT1G23490, an ADP-ribosylation factor that
  shares the symbol "ARF1", lacks IPR010525 and is the negative control).
  The row's declared ``has_pb1_domain`` must match whether IPR033389 or
  PF02309 (the PB1 domain) is present.
- ``panther_family``: the row's declared ``panther_subfamily`` must match
  the live ``subfamily_id``.

A failed call (``CallResult.ok is False``) is reported as its own failure
reason — it is never treated as "the marker is absent", which would let a
network error or a typo'd locus silently pass as "not an ARF".
"""

from __future__ import annotations

import asyncio
import csv
import json
import sys
from pathlib import Path

from examples.arf_family.mcp_client import SERVER_CMD, McpClient

GENES_TSV = Path(__file__).parent / "genes.tsv"

REQUIRED_COLUMNS = {"locus", "symbol", "organism", "panther_subfamily", "has_pb1_domain"}

_VALID_BOOL_STRINGS = {"true", "false"}


class ManifestError(Exception):
    """`genes.tsv` itself is structurally wrong (bad header) — not a single row.

    Raised before any MCP call is made, so a broken manifest never spawns
    the server subprocess.
    """


def _has_pb1_domain(interpro_payload: dict) -> bool:
    blob = json.dumps(interpro_payload)
    return "IPR033389" in blob or "PF02309" in blob


def _is_arf_family(interpro_payload: dict) -> bool:
    return "IPR010525" in json.dumps(interpro_payload)


def _validate_row(row: dict) -> str | None:
    """Structural / value checks that need no network call.

    Returns a `BAD MANIFEST ROW:` reason string, or None if the row is fit
    to be checked against the live server. A `csv.DictReader` row with fewer
    tab-separated fields than the header fills the missing trailing keys
    with `None` (a structurally short line) rather than raising — that must
    be reported like any other bad row, not crash with a raw
    `AttributeError` the first time a `None` hits `.strip()`.
    """
    missing = sorted(col for col in REQUIRED_COLUMNS if row.get(col) is None)
    if missing:
        return f"row is missing value(s) for: {', '.join(missing)} (structurally short line)"
    raw_pb1 = row["has_pb1_domain"].strip().lower()
    if raw_pb1 not in _VALID_BOOL_STRINGS:
        return (
            "has_pb1_domain must be 'true' or 'false', got "
            f"{row['has_pb1_domain']!r} — an unrecognised value is not a valid False"
        )
    return None


async def verify(genes_path: Path, server_cmd: list[str]) -> list[tuple[str, str, str]]:
    """Check every row of `genes_path` against the live server.

    Returns a list of (locus, symbol, reason) for rows that failed
    verification; an empty list means every row is clean. Raises
    `ManifestError` if the header itself is missing or has extra columns.
    """
    with open(genes_path) as f:
        reader = csv.DictReader(f, delimiter="\t")
        if set(reader.fieldnames or []) != REQUIRED_COLUMNS:
            raise ManifestError(
                f"{genes_path}: header must be exactly {sorted(REQUIRED_COLUMNS)}, "
                f"got {reader.fieldnames}"
            )
        rows = list(reader)

    bad: list[tuple[str, str, str]] = []
    to_check: list[dict] = []
    for row in rows:
        reason = _validate_row(row)
        if reason is None:
            to_check.append(row)
        else:
            locus = row.get("locus") or "<missing locus>"
            symbol = row.get("symbol") or "<missing symbol>"
            bad.append((locus, symbol, reason))

    if not to_check:
        return bad

    c = McpClient(server_cmd)
    await c.start()
    try:
        for row in to_check:
            locus, symbol, organism = row["locus"], row["symbol"], row["organism"]
            declared_pb1 = row["has_pb1_domain"].strip().lower() == "true"
            declared_subfamily = row["panther_subfamily"]
            reasons: list[str] = []

            interpro_res = await c.call("interpro_domains", {"locus": locus, "organism": organism})
            if not interpro_res.ok:
                reasons.append(f"interpro_domains call failed: {interpro_res.error}")
            elif not isinstance(interpro_res.payload, dict):
                reasons.append(
                    f"interpro_domains returned a non-dict payload: {interpro_res.payload!r}"
                )
            elif not _is_arf_family(interpro_res.payload):
                reasons.append("not ARF family: InterPro entry IPR010525 absent")
            else:
                live_pb1 = _has_pb1_domain(interpro_res.payload)
                if live_pb1 != declared_pb1:
                    reasons.append(
                        f"has_pb1_domain mismatch: declared={declared_pb1} live={live_pb1}"
                    )

            panther_res = await c.call("panther_family", {"locus": locus, "organism": organism})
            if not panther_res.ok:
                reasons.append(f"panther_family call failed: {panther_res.error}")
            elif not isinstance(panther_res.payload, dict):
                reasons.append(
                    f"panther_family returned a non-dict payload: {panther_res.payload!r}"
                )
            else:
                live_subfamily = panther_res.payload.get("subfamily_id")
                if live_subfamily != declared_subfamily:
                    reasons.append(
                        "panther_subfamily mismatch: "
                        f"declared={declared_subfamily!r} live={live_subfamily!r}"
                    )

            if reasons:
                bad.append((locus, symbol, "; ".join(reasons)))
    finally:
        await c.close()
    return bad


async def main(genes_path: Path = GENES_TSV, server_cmd: list[str] = SERVER_CMD) -> int:
    bad = await verify(genes_path, server_cmd)
    for locus, symbol, reason in bad:
        print(f"BAD MANIFEST ROW: {locus} {symbol} — {reason}", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
