"""PlantCyc informational-stub backend — NO network calls (yet).

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

Config slot (P2.20)
-------------------
``PLANT_GENOMICS_MCP_PLANTCYC_TOKEN`` env var. When set, the returned
record flips ``status`` from ``subscription_required`` to
``configured_live_not_implemented`` and adds a ``note_for_subscribers``
field. The HTTP wiring against the documented SRI/BioCyc auth flow is
intentionally deferred — the auth scheme is undocumented in the public
surface, and shipping an un-real-execution-checked client would mislead
the first subscriber-with-credentials. A subscriber PR is welcome; the
entry point is ``_call_live_if_configured`` below (returns ``None``
today).

We reuse ``ensembl_plants.PlantGenomicsError`` as the shared error type so
server dispatch handles one exception class for all backends.
"""

from __future__ import annotations

import os
import re
from typing import Any

from plant_genomics_mcp.errors import NotFoundError


# Identifier whitelist — same shape as tair/phytozome guards. Even though
# we only string-format the locus into a URL (not XML), reject obviously-
# bogus input up front so callers fail loud rather than receive a
# misleading "valid-looking" redirect record.
_LOCUS_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# Hardcoded per brief: must NOT come from datetime.now() (would make the
# dict-shape tests flaky as wall-clock drifts).
_PROBED_AT = "2026-05-21"

# Env-var slot for the optional SRI/BioCyc subscription token. Read at
# call time (not import time) so test monkeypatching works and so
# operators can rotate the token without restarting the server.
PLANTCYC_TOKEN_ENV = "PLANT_GENOMICS_MCP_PLANTCYC_TOKEN"


def _token_present() -> bool:
    """Return True when a non-empty PlantCyc subscription token is configured.

    Empty string and unset are treated as "not configured" — matching the
    convention used elsewhere in this server for similar env-var slots.
    """
    return bool(os.environ.get(PLANTCYC_TOKEN_ENV))


def _call_live_if_configured(locus: str) -> dict[str, Any] | None:
    """Subscriber-PR entry point for wiring real SRI/BioCyc auth.

    Returns a populated locus record on success, or ``None`` to fall
    through to the redirect stub. Today this ALWAYS returns ``None`` —
    the auth scheme is undocumented in the public surface and shipping
    an unverifiable HTTP client would mislead callers. A subscriber with
    credentials can drop in the real ``httpx`` call here.
    """
    return None


def lookup_locus(locus: str) -> dict[str, Any]:
    """Informational redirect (or live record if subscriber wiring is added).

    PlantCyc (PLANT orgid in BioCyc) requires a paid SRI/Phoenix
    subscription; per-locus REST endpoints return 404 without auth
    (confirmed 2026-05-21). This function does NOT call PlantCyc by
    default — it returns a structured record pointing users to the free
    alternatives already in this MCP: ``ensembl_plants_lookup_locus``
    and ``phytozome_lookup_locus``. MetaCyc (META orgid) is publicly
    accessible but does not contain Arabidopsis-specific gene mappings
    (loci stay in the gated PLANT db); a future ``metacyc_*`` tool could
    wrap MetaCyc pathway lookups directly.

    When ``PLANT_GENOMICS_MCP_PLANTCYC_TOKEN`` is set in the environment
    the function still does not make a network call (live wiring
    deferred — see module docstring), but the returned record flips
    ``status`` to ``configured_live_not_implemented`` and adds a
    ``note_for_subscribers`` field so a credentialed user can see their
    token was detected.
    """
    if not _LOCUS_RE.match(locus):
        raise NotFoundError(f"PlantCyc: invalid locus {locus!r} (must match {_LOCUS_RE.pattern})")

    live = _call_live_if_configured(locus)
    if live is not None:
        return live

    if _token_present():
        return {
            "locus": locus,
            "plantcyc_web_url": (
                f"https://pmn.plantcyc.org/PLANT/NEW-IMAGE?type=GENE&object={locus}"
            ),
            "status": "configured_live_not_implemented",
            "probed_at": _PROBED_AT,
            "auth_configured": True,
            "rationale": (
                f"{PLANTCYC_TOKEN_ENV} is set, but the SRI/BioCyc auth "
                "scheme is undocumented in the public surface and the live "
                "HTTP wiring is intentionally deferred until a subscriber-"
                "with-credentials can ground-truth it."
            ),
            "note_for_subscribers": (
                "Wire the real call in plant_genomics_mcp.plantcyc."
                "_call_live_if_configured — it currently returns None. "
                "A working PR should include a real-execution test against "
                "your account."
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

    return {
        "locus": locus,
        "plantcyc_web_url": (f"https://pmn.plantcyc.org/PLANT/NEW-IMAGE?type=GENE&object={locus}"),
        "status": "subscription_required",
        "probed_at": _PROBED_AT,
        "auth_configured": False,
        "rationale": (
            "BioCyc PLANT orgid returns 404 for per-locus REST; "
            f"SRI/Phoenix paid subscription required. Set {PLANTCYC_TOKEN_ENV} "
            "once a subscriber-implemented live wiring lands."
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
