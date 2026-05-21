"""TAIR informational-stub backend — NO network calls.

TAIR's per-locus REST API requires a Phoenix Bioinformatics paid subscription
(controller-verified 2026-05-21):

  * ``https://www.arabidopsis.org/api/locus/AT1G01010`` → 403 Forbidden
  * ``https://api.arabidopsis.org/locus/AT1G01010``     → 403 Forbidden
  * ``servlets/TairObject?type=locus&name=AT1G01010``   → 403 Forbidden
  * SPA bundle inspection: only auth-gated ``org/api/*`` endpoints; the
    public surface is RSS feeds + ``/api/download-files/download`` for bulk
    archives. No free per-locus REST.

Phoenix Bioinformatics has gated per-locus queries behind paid subscriptions
since ~2014. Same Arabidopsis annotation is freely available in this MCP via
``ensembl_plants_lookup_locus`` and ``phytozome_lookup_locus`` (both
controller-verified live for AT1G01010 → NAC001 on 2026-05-21).

This module exposes a single PURE-DATA function that returns a structured
redirect record pointing users to those alternatives. It does NOT call TAIR.

We reuse ``ensembl_plants.PlantGenomicsError`` as the shared error type so
server dispatch handles one exception class for all backends.
"""

from __future__ import annotations

import re
from typing import Any

from plant_genomics_mcp.errors import NotFoundError


# Identifier whitelist — same shape as phytozome's guard. Even though we
# string-format the locus only into a URL (not XML), reject obviously-bogus
# input up front so callers fail loud rather than receive a misleading
# "valid-looking" redirect record.
_LOCUS_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# Hardcoded per brief: must NOT come from datetime.now() (would make the
# 7-key dict-shape test flaky as wall-clock drifts).
_PROBED_AT = "2026-05-21"


def lookup_locus(locus: str) -> dict[str, Any]:
    """Informational redirect for TAIR locus queries.

    TAIR's per-locus REST API requires a Phoenix Bioinformatics subscription
    (confirmed 2026-05-21). This function does NOT call TAIR — it returns a
    structured record pointing users to the free alternatives already in
    this MCP: ``ensembl_plants_lookup_locus`` and ``phytozome_lookup_locus``.
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
            "requires paid subscription."
        ),
        "alternatives": ["ensembl_plants_lookup_locus", "phytozome_lookup_locus"],
        "alternatives_note": (
            "Both alternatives return the same canonical Arabidopsis "
            "annotation; ensembl_plants_lookup_locus also covers other plant "
            "species (oryza_sativa, zea_mays, ...)."
        ),
    }
