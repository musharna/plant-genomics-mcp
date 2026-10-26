"""Tell an upstream outage from a regression in the live verify_genes check.

The live check (`test_verify_genes_over_real_stdio_...`) failed 4 of its
first 60 CI runs, every time on `[UpstreamUnavailableError]`: PANTHER
timing out three times, UniProt answering 503 once. The server's own error
cannot tell those apart from a regression that makes its calls fail (a
wrong host, path or timeout): both come back as the same class. So a
failing tool's backends are asked directly, with URLs written here rather
than imported from `src/` — an imported constant would share the
regression it is meant to rule out.

`outage` returns a skip reason only when every flagged reason is a
`[UpstreamUnavailableError]` call (the planted control row aside), and at
least one backend of each failing tool also fails the direct probe. If
every backend answers, the server's outage is its own and the check stays
red. An outage that ends between the failed calls and the probe reads as a
regression: this errs toward red, as the check did before.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Iterable
from pathlib import Path

import httpx

from examples.arf_family.verify_genes import Flag, verify_flags

OUTAGE = "UpstreamUnavailableError"

# The backends each tool verify_genes calls reaches (interpro_domains goes
# through a UniProt search to InterPro), one known-good request each.
BACKENDS: dict[str, tuple[tuple[str, str], ...]] = {
    "interpro_domains": (
        (
            "UniProt search",
            "https://rest.uniprot.org/uniprotkb/search?query=accession:P93024&fields=accession&size=1",
        ),
        ("InterPro", "https://www.ebi.ac.uk/interpro/api/entry/all/protein/uniprot/P93024/"),
    ),
    "panther_family": (
        (
            "PANTHER geneinfo",
            "https://pantherdb.org/services/oai/pantherdb/geneinfo?geneInputList=AT1G19850&organism=3702",
        ),
    ),
}


# InterPro's error object. Its overloaded database answers ``{"Error":1040}``
# on a 200 or a 404 (2026-09-28), which the status alone read as up, or as a
# broken probe. Written here rather than imported, like the URLs above.
ERROR_BODY = re.compile(r'\A\s*\{\s*"Error"\s*:')


class ProbeBroken(AssertionError):
    """A probe got an answer that is neither up nor down: the probe is wrong."""


def probe(url: str, timeout_s: float = 30.0) -> str | None:
    """None when the backend answers 200 with JSON; why it is down on a 5xx,
    a timeout, no connection, a 200 whose body is not JSON (PANTHER's empty
    200s, 2026-09-28, read as up), or a body naming the service's own
    error at any status. Anything else raises `ProbeBroken`."""
    try:
        resp = httpx.get(url, timeout=timeout_s)
    except httpx.TransportError as e:
        return f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
    if ERROR_BODY.search(resp.text):
        return f"HTTP {resp.status_code} {' '.join(resp.text.split())[:100]}"
    if resp.status_code == 200:
        try:
            resp.json()
        except ValueError:
            return f"HTTP 200 non-JSON {' '.join(resp.text.split())[:100]!r}"
        return None
    if resp.status_code >= 500:
        return f"HTTP {resp.status_code}"
    raise ProbeBroken(f"{url} answered HTTP {resp.status_code}; the probe needs fixing")


def outage(
    flags: Iterable[Flag],
    planted_locus: str,
    probe: Callable[[str], str | None] = probe,
) -> tuple[str | None, str]:
    """(skip reason, note). The skip reason is None unless this run failed
    on an upstream outage and nothing else; the note, for a failure report,
    says why it is not one."""
    flags = list(flags)
    outage_calls = [(f, r) for f in flags for r in f.reasons if r.error_class == OUTAGE]
    if not outage_calls:
        return None, ""
    for f in flags:
        if f.locus == planted_locus:
            continue
        for r in f.reasons:
            if r.error_class != OUTAGE:
                return None, f"not only an outage: {f.locus} {r.text}"
    planted = [f for f in flags if f.locus == planted_locus]
    if not planted:
        return None, f"not only an outage: the planted row {planted_locus} was not flagged"
    planted_unchecked = any(
        r.tool == "interpro_domains" and r.error_class == OUTAGE for r in planted[0].reasons
    )
    if not planted_unchecked and not any("IPR010525 absent" in r.text for r in planted[0].reasons):
        return None, f"not only an outage: the planted row read {planted[0].reason!r}"

    down: list[str] = []
    for tool in sorted({r.tool for _, r in outage_calls if r.tool is not None}):
        answers = [(name, probe(url)) for name, url in BACKENDS[tool]]
        why = [f"{name} {reason}" for name, reason in answers if reason is not None]
        if not why:
            names = ", ".join(name for name, _ in answers)
            return None, (
                f"not an outage: the server reports {OUTAGE} for {tool}, "
                f"but {names} answer directly"
            )
        down.append(f"{tool} ({'; '.join(why)})")
    skip = f"upstream outage on {len(outage_calls)} calls: {', '.join(down)}"
    if planted_unchecked:
        skip += "; the planted-row control was not checked"
    return skip, ""


# The live-smoke job is killed at 30 min (.github/workflows/test.yml); a full
# run takes 3-4 min. A backend that hangs rather than erroring costs each row
# ~93 s (3 attempts at PANTHER's 30 s timeout, 1 s + 2 s backoff), ~3 h for
# the manifest's 115 rows, so the job was killed before `outage` ran
# (2026-10-26, run 40293758282, twice). The deadline leaves the rest of the
# job time for the probes and the client's live test.
LIVE_DEADLINE_S = 20 * 60.0


def verify_within(
    genes_path: Path, server_cmd: list[str], deadline_s: float = LIVE_DEADLINE_S
) -> list[Flag] | None:
    """`verify_flags`, or None when it has not returned by the deadline.

    Only this deadline reads as None: a TimeoutError raised inside (the
    client's own per-call timeout) propagates. Either way `verify_flags`
    terminates and reaps the server on the way out."""

    async def bounded() -> list[Flag] | None:
        try:
            async with asyncio.timeout(deadline_s) as cm:
                return await verify_flags(genes_path, server_cmd)
        except TimeoutError:
            if cm.expired():
                return None
            raise

    return asyncio.run(bounded())


def deadline_outage(
    deadline_s: float, probe: Callable[[str], str | None] = probe
) -> tuple[str | None, str]:
    """(skip reason, note) for a live check that passed its deadline: a skip
    only when a direct probe finds a backend down. If every backend answers,
    the time went in the server or the host, and the check stays red."""
    down = []
    for tool, backends in BACKENDS.items():
        for name, url in backends:
            reason = probe(url)
            if reason is not None:
                down.append(f"{tool} ({name} {reason})")
    if down:
        return (
            f"upstream outage: the live check passed its {deadline_s:.0f} s deadline; "
            f"down: {', '.join(down)}"
        ), ""
    return None, (
        f"not an outage: the live check passed its {deadline_s:.0f} s deadline "
        "and every backend answers directly"
    )
