"""TAIR locus info — silent upgrade: now served via BAR ThaleMine.

Wave A.6.8 promotes ``tair_locus_info`` from a v0.9 ``subscription_required``
redirect stub into a real Arabidopsis Curator Summary tool. The MCP tool
name is preserved so existing clients keep working; the body delegates to
``bar.gene_summary`` (BAR/ThaleMine + GAIA aliases), which carries the same
TAIR curator-vetted annotation that the paid TAIR REST API would have
returned.

Why the upgrade: TAIR's free per-locus REST endpoints (probed 2026-05-21)
return 403 — Phoenix Bioinformatics has gated per-locus queries since
~2014. BAR (U Toronto, Global Core Biodata Resource as of 2023) is the
upstream curator's free mirror for that annotation. See
``docs/superpowers/audits/2026-05-23-bar-api-probe.md``.

Arabidopsis only — ThaleMine carries taxon 3702 plus yeast/human for
ortholog cross-reference. Non-Arabidopsis loci raise NotFoundError.
"""

from __future__ import annotations

from typing import Any

import httpx

from plant_genomics_mcp import bar


async def lookup_locus(client: httpx.AsyncClient, locus: str) -> dict[str, Any]:
    """Return the BAR ThaleMine + GAIA-aliases summary for an Arabidopsis locus.

    Direct alias for ``bar.gene_summary`` — see that function for the full
    return-shape contract (BarGeneSummary). Locus regex validation, BAR
    upstream error mapping, and best-effort GAIA aliases all happen there.
    """
    return await bar.gene_summary(client, locus)
