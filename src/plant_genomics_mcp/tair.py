"""TAIR informational-stub backend — NO network calls (yet).

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

Config slot (P2.20)
-------------------
``PLANT_GENOMICS_MCP_TAIR_TOKEN`` env var. When set, the returned record
flips ``status`` from ``subscription_required`` to
``configured_live_not_implemented`` and adds a ``note_for_subscribers``
field. The HTTP wiring against the documented Phoenix auth flow is
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


# Identifier whitelist — same shape as phytozome's guard. Even though we
# string-format the locus only into a URL (not XML), reject obviously-bogus
# input up front so callers fail loud rather than receive a misleading
# "valid-looking" redirect record.
_LOCUS_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# Hardcoded per brief: must NOT come from datetime.now() (would make the
# dict-shape tests flaky as wall-clock drifts).
_PROBED_AT = "2026-05-21"

# Env-var slot for the optional Phoenix Bioinformatics subscription token.
# Read at call time (not import time) so test monkeypatching works and so
# operators can rotate the token without restarting the server.
TAIR_TOKEN_ENV = "PLANT_GENOMICS_MCP_TAIR_TOKEN"


def _token_present() -> bool:
    """Return True when a non-empty TAIR subscription token is configured.

    Empty string and unset are treated as "not configured" — matching the
    convention used elsewhere in this server for similar env-var slots.
    """
    return bool(os.environ.get(TAIR_TOKEN_ENV))


def _call_live_if_configured(locus: str) -> dict[str, Any] | None:
    """Subscriber-PR entry point for wiring real Phoenix/TAIR auth.

    Returns a populated locus record on success, or ``None`` to fall
    through to the redirect stub. Today this ALWAYS returns ``None`` —
    the auth scheme is undocumented in the public surface and shipping
    an unverifiable HTTP client would mislead callers. A subscriber with
    credentials can drop in the real ``httpx`` call here.
    """
    return None


def lookup_locus(locus: str) -> dict[str, Any]:
    """Informational redirect (or live record if subscriber wiring is added).

    TAIR's per-locus REST API requires a Phoenix Bioinformatics subscription
    (confirmed 2026-05-21). This function does NOT call TAIR by default —
    it returns a structured record pointing users to the free alternatives
    already in this MCP: ``ensembl_plants_lookup_locus`` and
    ``phytozome_lookup_locus``.

    When ``PLANT_GENOMICS_MCP_TAIR_TOKEN`` is set in the environment the
    function still does not make a network call (live wiring deferred —
    see module docstring), but the returned record flips ``status`` to
    ``configured_live_not_implemented`` and adds a
    ``note_for_subscribers`` field so a credentialed user can see their
    token was detected.
    """
    if not _LOCUS_RE.match(locus):
        raise NotFoundError(f"TAIR: invalid locus {locus!r} (must match {_LOCUS_RE.pattern})")

    live = _call_live_if_configured(locus)
    if live is not None:
        return live

    if _token_present():
        return {
            "locus": locus,
            "tair_web_url": f"https://www.arabidopsis.org/locus/{locus}",
            "status": "configured_live_not_implemented",
            "probed_at": _PROBED_AT,
            "auth_configured": True,
            "rationale": (
                f"{TAIR_TOKEN_ENV} is set, but the Phoenix Bioinformatics "
                "auth scheme is undocumented in the public surface and the "
                "live HTTP wiring is intentionally deferred until a "
                "subscriber-with-credentials can ground-truth it."
            ),
            "note_for_subscribers": (
                "Wire the real call in plant_genomics_mcp.tair."
                "_call_live_if_configured — it currently returns None. "
                "A working PR should include a real-execution test against "
                "your account."
            ),
            "alternatives": ["ensembl_plants_lookup_locus", "phytozome_lookup_locus"],
            "alternatives_note": (
                "Both alternatives return the same canonical Arabidopsis "
                "annotation; ensembl_plants_lookup_locus also covers other "
                "plant species (oryza_sativa, zea_mays, ...)."
            ),
        }

    return {
        "locus": locus,
        "tair_web_url": f"https://www.arabidopsis.org/locus/{locus}",
        "status": "subscription_required",
        "probed_at": _PROBED_AT,
        "auth_configured": False,
        "rationale": (
            "TAIR per-locus REST endpoints return 403; Phoenix Bioinformatics "
            f"requires paid subscription. Set {TAIR_TOKEN_ENV} once a "
            "subscriber-implemented live wiring lands."
        ),
        "alternatives": ["ensembl_plants_lookup_locus", "phytozome_lookup_locus"],
        "alternatives_note": (
            "Both alternatives return the same canonical Arabidopsis "
            "annotation; ensembl_plants_lookup_locus also covers other plant "
            "species (oryza_sativa, zea_mays, ...)."
        ),
    }
