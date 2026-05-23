# plant-genomics-mcp v0.9.0 — Security Audit

**Date:** 2026-05-23
**Auditor:** feature-dev:code-reviewer (opus)
**Scope:** Full pre-1.0 security review — HTTP transport, outbound client safety, input validation, cache/state isolation, dependency posture, secrets handling, logging leakage, resource exhaustion, process safety, MCP protocol safety.
**Deployment context:** stdio binary (local Claude Code use) + Streamable-HTTP server intended for gt76 behind Tailscale Funnel — publicly reachable from the internet with no auth.

---

## Executive Summary

Codebase is structurally clean: no shell injection, no subprocess calls, no eval/exec, no hardcoded secrets, correct TLS posture throughout. SSRF surface is zero (all outbound URLs are hardcoded constants). Primary risk is the public HTTP transport: ships with **no authentication, no rate limiting, no request-size cap** — trivially abusable as an NCBI BLAST launcher and a resource-exhaustion target. Secondary risk: `Retry-After` header fed directly to `asyncio.sleep()` across all 9 backends without bounds — a malicious or misbehaving upstream can hold coroutines for arbitrary durations. **Totals: 2 BLOCKER, 4 IMPORTANT, 3 POLISH.**

---

## Findings

### BLOCKER

#### B-1: HTTP transport has no authentication

- **Files:** `src/plant_genomics_mcp/server_http.py:82-88` (Starlette routes), spec `docs/superpowers/specs/2026-05-22-hosted-streamable-http-design.md:141` (auth explicitly deferred).
- **Threat model:** Any internet client with the `.ts.net` URL can invoke all 27 MCP tools. Notably: `blast_sequence`, `find_homologs_synth`, and `consensus_homologs` auto-submit NCBI BLAST jobs under the server's IP (NCBI may block the host). All 10 batch tools fan out 50 parallel upstream requests per call. No credentials needed — the URL is in the README and any future registry listing.
- **Fix:** Bearer-token Starlette middleware in front of `/mcp` (let `/healthz` pass). Token in `PLANT_GENOMICS_MCP_HTTP_TOKEN` env var; startup aborts if missing or shorter than 32 chars.
  ```python
  class BearerAuthMiddleware(BaseHTTPMiddleware):
      async def dispatch(self, request, call_next):
          if request.url.path == "/healthz":
              return await call_next(request)
          token = os.environ.get("PLANT_GENOMICS_MCP_HTTP_TOKEN", "")
          auth = request.headers.get("Authorization", "")
          if not token or auth != f"Bearer {token}":
              return JSONResponse({"error": "unauthorized"}, status_code=401)
          return await call_next(request)
  ```
- **Effort:** S

#### B-2: Unbounded `Retry-After` from upstream servers passed directly to `asyncio.sleep()`

- **Files (all 9 backends):** `ensembl_plants.py:74`, `uniprot.py:122,218,274`, `europe_pmc.py:85`, `gramene.py:63`, `quickgo.py:85`, `phytozome.py:114`, `kegg.py:65`, `string_db.py:88`, `atted.py:90`.
- **Pattern:** `retry_after = float(resp.headers.get("Retry-After", delay)); await asyncio.sleep(retry_after)`
- **Threat model:** A malicious or misbehaving upstream returns `Retry-After: 86400` → coroutine sleeps 24h holding an httpx client + Starlette connection slot. Real risk scenario: legitimate upstream maintenance window with a large `Retry-After`. With 50-locus batch calls, 50 coroutines sleep simultaneously. Combined with B-1, drains uvicorn capacity.
- **Fix:** Cap at 60 seconds across all 9 backends:
  ```python
  MAX_RETRY_AFTER = 60.0
  retry_after = min(float(resp.headers.get("Retry-After", delay)), MAX_RETRY_AFTER)
  ```
- **Effort:** S (mechanical, 9 identical sites)

---

### IMPORTANT

#### I-1: No HTTP request body size limit

- **File:** `src/plant_genomics_mcp/server_http.py` (no body-size middleware, uvicorn launched without `limit_concurrency` / body-size limits).
- **Threat model:** A POST to `/mcp` with a 500 MB `sequence` parameter is buffered in memory before the tool dispatch layer can reject. With no auth, several such concurrent requests OOM the container. The tool layer then POSTs the entire payload to NCBI.
- **Fix:**
  1. Add a Starlette body-size middleware capping body at 1 MB.
  2. Add `"maxLength": 100000` to the `sequence` field in `blast_sequence` and `find_homologs_synth` `inputSchema`.
- **Effort:** S

#### I-2: `consensus_homologs` and `find_homologs_synth` submit BLAST jobs unconditionally with no throttle

- **Files:** `synthesis.py:887-893` (`consensus_homologs` hardcodes `hitlist_size=50` raw_top), `synthesis.py:365-368` (`find_homologs_synth` phase 1).
- **Threat model:** Each call ties up NCBI BLAST for ~10 min. Unauthenticated callers can fire N parallel BLAST jobs, risking IP blacklisting under NCBI's ToS. Running as `anonymous@example.org` (P-2) compounds this.
- **Fix:**
  1. Process-level semaphore in `blast.py`: `_BLAST_SEMAPHORE = asyncio.Semaphore(2)`; wrap `blast_sequence`.
  2. Set real operator email in deployment compose `PLANT_GENOMICS_MCP_NCBI_EMAIL`.
  3. `maxLength: 100000` on `sequence` inputSchemas (overlaps with I-1).
- **Effort:** S–M

#### I-3: No CORS policy on the HTTP transport

- **File:** `server_http.py:82-88` (no `CORSMiddleware`).
- **Threat model:** Today with no auth: any web page in a visitor's browser can POST to the `/mcp` endpoint cross-origin, using the visitor as a proxy. Post-B-1 this is mitigated by the bearer token (the page can't know it), but an explicit deny-all CORS is best practice regardless.
- **Fix:** Add `CORSMiddleware(allow_origins=[], allow_methods=["POST", "GET"])`. Restrict to specific MCP-client origins if known.
- **Effort:** S

#### I-4: `locus` and `organism` inputs reach upstream URL paths without regex validation (Ensembl, KEGG, Gramene)

- **Files:** `ensembl_plants.py:120` (`f"/lookup/id/{locus}"`), `ensembl_plants.py:143` (`f"/xrefs/id/{locus}"`), `kegg.py:139` (`f"/link/pathway/{gene_id}"`), `gramene.py:132` (params).
- **Phytozome and TAIR already validate** (`phytozome.py:49` `_LOCUS_RE = re.compile(r"^[A-Za-z0-9._-]+$")`). The inconsistency is the risk surface — httpx percent-encoding makes a real exploit unlikely but defense-in-depth wants uniform validation at the boundary.
- **Fix:** Extract `_LOCUS_RE` to `validators.py`; apply in Ensembl, KEGG, Gramene before URL construction.
- **Effort:** S

---

### POLISH

#### P-1: `/healthz` leaks exact version string to unauthenticated callers

- **File:** `server_http.py:71-73` — `{"status": "ok", "version": "0.9.0"}`. Post-B-1, `/healthz` stays unauthenticated for liveness probes; consider dropping `version` or returning a hash.
- **Effort:** XS

#### P-2: NCBI email defaults to `anonymous@example.org`

- **File:** `blast.py:104`. NCBI policy requires a real email; placeholder risks throttling/blocking. Set in deployment compose env. Document as required.
- **Effort:** XS

#### P-3: `pyproject.toml` uses minimum-version pins only; no lock file committed

- **File:** `pyproject.toml:18-24`. No CVEs found in the constrained ranges at audit time, but absence of `uv.lock` / `requirements.txt --exact` makes the Docker image non-reproducible across builds — a transitive-dep compromise would silently land on next `docker build`.
- **Effort:** S

---

## Affirmations

- **Shell/process safety:** Zero matches for `subprocess`, `os.system`, `shell=True`, `eval(`, `exec(`, `pickle`, `__import__`. Clean.
- **TLS verification:** Zero `verify=False`. Default `verify=True` everywhere. Clean.
- **SSRF:** All outbound URLs are hardcoded module-level constants. No user-supplied URL fragments reach the httpx client. Clean.
- **Secrets:** No hardcoded API keys, tokens, or credentials. `PLANT_GENOMICS_MCP_NCBI_EMAIL` correctly reads from env with safe fallback. Project is intentionally keyless. Clean.
- **Cache key isolation:** Per-module `TTLCache` instances share no keyspace. Keys include method, base URL, path, sorted params. Non-200 responses are never cached. Bounded at 256 entries with LRU. Clean.
- **MCP resource URI parsing:** `resources.read_resource()` dispatches on hardcoded literals. Unknown URIs raise typed errors. No traversal/injection surface. Clean.
- **Batch size limits:** `batch.MAX_BATCH = 50` enforced before fanout. `inputSchema` `maxItems: 50` on all batch `loci`. Clean.
- **Phytozome XML injection prevention:** `_LOCUS_RE` pre-flight reject at `phytozome.py:49` before string-templated XML query. Clean.

---

## Open Questions

1. **Tailscale Funnel bandwidth quota as the only DoS ceiling.** Documented as "~1 GB/day" — verify actual quota for the gt76 plan before relying on it as primary abuse backstop.
2. **MCP SDK `inputSchema` validation timing.** Unclear whether the SDK validates actual call args against `inputSchema` server-side, or whether schema validation is purely client-side advisory. If server-side validation is absent, `maxItems: 50` is advisory only — server-side enforcement in dispatch should be strengthened for all numeric/length bounds.
3. **`httpx.AsyncClient` lifetime per tool dispatch.** `server.py:1031` opens a fresh client per request — no cross-request connection pooling. Compounds resource-exhaustion exposure under sustained traffic. Adding a process-level client pool reduces this and improves performance.
