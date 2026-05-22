"""PlantCyc informational-redirect backend — NO network calls.

This module makes NO upstream calls. ``lookup_locus`` returns a structured
redirect record pointing callers at the free Arabidopsis annotation
backends already in this MCP. PlantCyc's value-add — metabolic pathway
membership — is not substituted by alternatives; callers should treat
the redirect as discoverability metadata rather than equivalent data.

Why a stub instead of a live wrapper: PlantCyc's per-locus REST
endpoints require a paid SRI/Phoenix subscription (controller-verified
2026-05-21):

  * ``https://websvc.biocyc.org/PLANT/foreignid?ids=NCBI-GENE-ID:AT1G01010`` → 404
  * ``https://websvc.biocyc.org/getxml?id=PLANT:AT1G01010``                  → 404
  * ``https://pmn.plantcyc.org/META/NEW-IMAGE?type=GENE&object=AT1G01010``   → 404

The MetaCyc parent DB IS public (``getxml?id=META:PWY-7194`` returns a
valid 17 KB pathway XML), but Arabidopsis-specific gene→pathway mappings
live in the gated PLANT orgid — ``META/foreignid?ids=TAIR:AT1G01010``
returns ``0`` matches. A future ``metacyc_*`` tool could wrap MetaCyc
pathway lookups directly; this stub does not attempt that.
"""

from __future__ import annotations

import re
from typing import Any

from plant_genomics_mcp.errors import NotFoundError


# Identifier whitelist — same shape as tair/phytozome guards. Reject
# obviously-bogus input up front so callers fail loud rather than receive a
# misleading "valid-looking" redirect record.
_LOCUS_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# Hardcoded per brief: must NOT come from datetime.now() (would make the
# dict-shape tests flaky as wall-clock drifts).
_PROBED_AT = "2026-05-21"


def lookup_locus(locus: str) -> dict[str, Any]:
    """Return a structured redirect record for a PlantCyc locus.

    Makes no network call. Always returns the same shape: locus, web URL,
    status, probed date, rationale, and the list of free alternative
    tools in this MCP that cover the same annotation (pathway membership
    is not substituted).
    """
    if not _LOCUS_RE.match(locus):
        raise NotFoundError(f"PlantCyc: invalid locus {locus!r} (must match {_LOCUS_RE.pattern})")

    return {
        "locus": locus,
        "plantcyc_web_url": (f"https://pmn.plantcyc.org/PLANT/NEW-IMAGE?type=GENE&object={locus}"),
        "status": "subscription_required",
        "probed_at": _PROBED_AT,
        "rationale": (
            "BioCyc PLANT orgid returns 404 for per-locus REST without auth; "
            "SRI/Phoenix paid subscription required. This MCP does not ship "
            "a live PlantCyc client — use the alternatives below for the "
            "canonical Arabidopsis annotation."
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
