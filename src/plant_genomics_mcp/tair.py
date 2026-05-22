"""TAIR informational-redirect backend — NO network calls.

This module makes NO upstream calls. ``lookup_locus`` returns a structured
redirect record pointing callers at the free Arabidopsis annotation
backends already in this MCP (``ensembl_plants_lookup_locus`` and
``phytozome_lookup_locus``).

Why a stub instead of a live wrapper: TAIR's per-locus REST API is
gated behind a paid Phoenix Bioinformatics subscription
(controller-verified 2026-05-21):

  * ``https://www.arabidopsis.org/api/locus/AT1G01010`` → 403 Forbidden
  * ``https://api.arabidopsis.org/locus/AT1G01010``     → 403 Forbidden
  * ``servlets/TairObject?type=locus&name=AT1G01010``   → 403 Forbidden
  * SPA bundle inspection: only auth-gated ``org/api/*`` endpoints; the
    public surface is RSS feeds + ``/api/download-files/download`` for
    bulk archives. No free per-locus REST.

Phoenix Bioinformatics has gated per-locus queries since ~2014. The
canonical Arabidopsis annotation TAIR would return is freely available
in this MCP via ``ensembl_plants_lookup_locus`` and
``phytozome_lookup_locus`` (both controller-verified live for AT1G01010
→ NAC001 on 2026-05-21), which is why ``tair_locus_info`` exists as a
discoverability stub rather than a live wrapper.
"""

from __future__ import annotations

import re
from typing import Any

from plant_genomics_mcp.errors import NotFoundError


# Identifier whitelist — same shape as phytozome's guard. Reject obviously-
# bogus input up front so callers fail loud rather than receive a misleading
# "valid-looking" redirect record.
_LOCUS_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# Hardcoded per brief: must NOT come from datetime.now() (would make the
# dict-shape tests flaky as wall-clock drifts).
_PROBED_AT = "2026-05-21"


def lookup_locus(locus: str) -> dict[str, Any]:
    """Return a structured redirect record for a TAIR locus.

    Makes no network call. Always returns the same shape: locus, web URL,
    status, probed date, rationale, and the list of free alternative
    tools in this MCP that cover the same annotation.
    """
    if not _LOCUS_RE.match(locus):
        raise NotFoundError(f"TAIR: invalid locus {locus!r} (must match {_LOCUS_RE.pattern})")

    return {
        "locus": locus,
        "tair_web_url": f"https://www.arabidopsis.org/locus/{locus}",
        "status": "subscription_required",
        "probed_at": _PROBED_AT,
        "rationale": (
            "TAIR per-locus REST endpoints return 403; Phoenix Bioinformatics "
            "requires a paid subscription. This MCP does not ship a live TAIR "
            "client — use the alternatives below for the same annotation."
        ),
        "alternatives": ["ensembl_plants_lookup_locus", "phytozome_lookup_locus"],
        "alternatives_note": (
            "Both alternatives return the same canonical Arabidopsis "
            "annotation; ensembl_plants_lookup_locus also covers other "
            "plant species (oryza_sativa, zea_mays, ...)."
        ),
    }
