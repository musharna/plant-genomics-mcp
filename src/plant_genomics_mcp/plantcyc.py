"""PlantCyc informational-stub backend — NO network calls.

PlantCyc's per-locus REST endpoints require a paid SRI/Phoenix subscription
(controller-verified 2026-05-21):

  * ``https://websvc.biocyc.org/PLANT/foreignid?ids=NCBI-GENE-ID:AT1G01010`` → 404
  * ``https://websvc.biocyc.org/getxml?id=PLANT:AT1G01010``                  → 404
  * ``https://pmn.plantcyc.org/META/NEW-IMAGE?type=GENE&object=AT1G01010``   → 404

The MetaCyc parent DB IS public (``getxml?id=META:PWY-7194`` returns a valid
17 KB pathway XML), but Arabidopsis-specific gene→pathway mappings live in
the gated PLANT orgid — ``META/foreignid?ids=TAIR:AT1G01010`` returns ``0``
matches. A future ``metacyc_*`` tool could wrap MetaCyc pathway lookups
directly; this stub does not attempt that.

This module exposes a single PURE-DATA function that returns a structured
redirect record pointing users to free Arabidopsis annotation backends
already in this MCP. It does NOT call PlantCyc.

We reuse ``ensembl_plants.PlantGenomicsError`` as the shared error type so
server dispatch handles one exception class for all backends.
"""

from __future__ import annotations

import re
from typing import Any

from plant_genomics_mcp.errors import NotFoundError


# Identifier whitelist — same shape as tair/phytozome guards. Even though
# we only string-format the locus into a URL (not XML), reject obviously-
# bogus input up front so callers fail loud rather than receive a
# misleading "valid-looking" redirect record.
_LOCUS_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# Hardcoded per brief: must NOT come from datetime.now() (would make the
# 7-key dict-shape test flaky as wall-clock drifts).
_PROBED_AT = "2026-05-21"


def lookup_locus(locus: str) -> dict[str, Any]:
    """Informational redirect for PlantCyc locus queries.

    PlantCyc (PLANT orgid in BioCyc) requires a paid SRI/Phoenix
    subscription; per-locus REST endpoints return 404 without auth
    (confirmed 2026-05-21). This function does NOT call PlantCyc — it
    returns a structured record pointing users to the free alternatives
    already in this MCP: ``ensembl_plants_lookup_locus`` and
    ``phytozome_lookup_locus``. MetaCyc (META orgid) is publicly
    accessible but does not contain Arabidopsis-specific gene mappings
    (loci stay in the gated PLANT db); a future ``metacyc_*`` tool could
    wrap MetaCyc pathway lookups directly.
    """
    if not _LOCUS_RE.match(locus):
        raise NotFoundError(f"PlantCyc: invalid locus {locus!r} (must match {_LOCUS_RE.pattern})")

    return {
        "locus": locus,
        "plantcyc_web_url": (f"https://pmn.plantcyc.org/PLANT/NEW-IMAGE?type=GENE&object={locus}"),
        "status": "subscription_required",
        "probed_at": _PROBED_AT,
        "rationale": (
            "BioCyc PLANT orgid returns 404 for per-locus REST; "
            "SRI/Phoenix paid subscription required."
        ),
        "alternatives": ["ensembl_plants_lookup_locus", "phytozome_lookup_locus"],
        "alternatives_note": (
            "Both alternatives return canonical Arabidopsis annotation. "
            "PlantCyc's value-add is metabolic pathway membership — not "
            "covered by alternatives. MetaCyc parent DB is public but "
            "lacks Arabidopsis gene mappings (per 2026-05-21 probe of "
            "META/foreignid)."
        ),
    }
