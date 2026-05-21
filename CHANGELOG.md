# Changelog

## Unreleased

- **`blast_sequence` tool** (P2.16): NCBI BLAST URLAPI client — async Put/Get polling against `https://blast.ncbi.nlm.nih.gov/Blast.cgi`. Supports blastn / blastp / blastx / tblastn / tblastx; honors NCBI etiquette (per-RID 60s poll floor, `tool=` + `email=` identity params, `PLANT_GENOMICS_MCP_NCBI_EMAIL` env override). Emits `notifications/progress` on each poll. Returns parsed top hits + raw text report excerpt (capped at 50 KB). Long searches that exceed `max_wait` raise `[NotFoundError]` with the RID preserved so the client can re-poll.

## v0.5.0 — 2026-05-21

Publishability milestone — closes P0 readiness work.

- **Differentiated exception subclasses** (`plant_genomics_mcp.errors`): `PlantGenomicsError` base + `RateLimitError` / `NotFoundError` / `UpstreamUnavailableError` / `SubscriptionGatedError` subclasses. The base `__str__` prepends `[ClassName]` so the SDK error-result serializer preserves type info on the wire — clients can route on failure kind without parsing the message.
- **Pydantic output models** (`plant_genomics_mcp.models`): `EnsemblPlantsLocus`, `PhytozomeLocus`, `TairLocusInfo`, `PlantCycLocusInfo`. Each `Tool` entry now publishes `outputSchema = Model.model_json_schema()`. The SDK now returns both `structuredContent` (dict) and `content[]` (JSON-stringified text) and validates against the schema. `EnsemblPlantsLocus` keeps only `id` + `species` required (extra="allow") to absorb sparse/future Ensembl payloads without raising.
- **EDAM ontology tags** on every tool's `_meta`: `operation_2422` (Data retrieval); topic `topic_0780` (Plant biology) + `topic_0114` (Gene structure). Smithery / Glama / bio.tools can categorize.
- **Stdio end-to-end smoke test** (`tests/test_server_stdio.py`, opt-in via `PLANT_GENOMICS_MCP_STDIO_SMOKE=1`): spawns the server as a subprocess, drives `initialize` + `list_tools` + `call_tool` over real stdio. Anti-rot guard verifies the `[NotFoundError]` wire prefix actually surfaces. CI runs the smoke on every push/PR.
- **Dockerfile + GHCR publish workflow**: two-stage `python:3.12-slim` image (~140 MB, runs as uid 10001), multi-arch `linux/amd64,linux/arm64` via QEMU+Buildx. Tags `:edge` on `main`, `:vX.Y.Z` + `:vX.Y` + `:latest` on `v*.*.*` tag push.
- **README rewrite** to the registry-discoverable scaffold popular MCP servers use: tool-count headline, category table, transport matrix, install paths (pipx + GHCR + source), per-tool usage examples, error-prefix table, chain recipes, CI/Docker/Python/License shield badges.

## v0.4.0 — 2026-05-21

- Add `plantcyc_locus_info` tool — informational stub. BioCyc PLANT orgid returns 404 for per-locus REST (live probe 2026-05-21); SRI/Phoenix subscription required. Returns a structured redirect to `ensembl_plants_lookup_locus` and `phytozome_lookup_locus`. MetaCyc parent (`META` orgid) is publicly accessible but lacks Arabidopsis gene mappings.

## v0.3.0 — 2026-05-21

- Add `tair_locus_info` tool — informational stub. TAIR's free per-locus REST API was retired (live probe 2026-05-21: public `arabidopsis.org` is a Vue SPA shell; `/api/*` endpoints return 403, gated by Phoenix Bioinformatics subscription). Returns a structured redirect to the live Ensembl Plants and Phytozome backends, which cover the same Arabidopsis annotation.

## v0.2.0 — 2026-05-21

- Add `phytozome_lookup_locus` tool — async Phytozome BioMart XML POST client (`phytozome-next.jgi.doe.gov`). Default `organism_id=167` (Arabidopsis thaliana TAIR10, live-verified). `KNOWN_ORGANISMS` dict ships 9 additional unverified hints (Glycine max, Sorghum bicolor, Brachypodium distachyon, Manihot esculenta, Eucalyptus grandis, Populus trichocarpa, Phaseolus vulgaris, Chlamydomonas reinhardtii, Daucus carota). Detects BioMart's `Query ERROR:` and empty-results idioms.

## v0.1.0 — 2026-05-21

- Initial release. `ensembl_plants_lookup_locus` tool — async httpx client for `rest.ensembl.org/lookup/id/{locus}?species={species}` with 429/5xx retry and exponential backoff. Default species `arabidopsis_thaliana`.
