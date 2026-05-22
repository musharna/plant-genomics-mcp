# Hosted Streamable-HTTP Endpoint — Design

**Status:** Approved — ready for plan
**Date:** 2026-05-22
**Owner:** mjarnold
**Driving task:** #2 from the v0.8.0 post-release play (6 → 2 → 5 → 3 → 4)

## Goal

Ship a publicly reachable HTTPS URL that speaks MCP Streamable-HTTP, so clients (Claude Code, MCP Inspector, registry indexers) can connect without cloning the repo or installing the package. Registry-grade demo posture: low traffic, no SLA, durable enough that a registry catalog entry stays good for months.

The Streamable-HTTP transport itself already ships (`src/plant_genomics_mcp/server_http.py`, `plant-genomics-mcp-http` entrypoint — P1.14, v0.5). What's missing is the deployment: a container image, a host to run it on, and a stable public URL.

## Non-goals

- Rate limiting (slowapi / nginx / Cloudflare WAF). Defer until abuse appears — upstream backends self-throttle, and expected traffic is near zero.
- Loki / Promtail wiring for long-term log query. Defer; `docker logs` is sufficient for demo-grade.
- Custom domain / Cloudflare migration. `ts.net` subdomain is the explicit choice (lowest friction, free, leverages existing Tailscale on gt76).
- Stateful sessions / Redis-backed cache. Container-restart-loses-cache is acceptable at zero traffic.
- Registry-entry refresh (modelcontextprotocol.io / Glama / PulseMCP / Smithery — P1.15). Filed as a follow-up task to run ≥48h after deploy lands (see "Out-of-scope follow-ups").
- Auth / token gate. Open + future per-IP rate limit is the agreed posture (see "Auth posture" below).

## Architecture

```
                  Tailscale Funnel
public client  →  *.ts.net  →  gt76 host  →  127.0.0.1:8765  →  Docker container
                                                                  (uvicorn + Starlette + MCP)
                                                                  ├── /healthz   (200, JSON)
                                                                  └── /mcp       (StreamableHTTP)
                                                                       │
                                                                       └→  upstream backends
                                                                           (Ensembl, NCBI BLAST,
                                                                            UniProt, Gramene, …)
```

- **Tailscale Funnel** terminates TLS at the Tailscale edge and proxies cleartext HTTP to `127.0.0.1:8765` on gt76. No certs to manage, no domain to register.
- **Container** binds to `127.0.0.1:8765` only (never the LAN, never WireGuard) — Funnel is the only ingress path.
- **Public URL**: `https://mjarnoldgt76.tail86d19d.ts.net/mcp` (captured live from `tailscale status --json` on gt76, 2026-05-22).

## Components

### 1. `Dockerfile.http` — new image for the HTTP entrypoint

Mirror the existing stdio `Dockerfile` (two-stage builder + slim runtime), but flip the `ENTRYPOINT` to `plant-genomics-mcp-http` and `EXPOSE 8765`. Image published as `ghcr.io/mjarnold/plant-genomics-mcp-http` — a distinct image name from the stdio `ghcr.io/mjarnold/plant-genomics-mcp` so tags don't collide.

GitHub Actions: extend the existing `.github/workflows/docker-publish.yml` to build + push both images from the same trigger. Two `docker/build-push-action` steps, one per Dockerfile, sharing the buildx cache.

Image carries no env-var defaults — runtime config comes from the compose `environment:` block.

### 2. `/healthz` route in `server_http.py`

Add a Starlette `Route("/healthz", ...)` ahead of the `/mcp` mount. Returns `200 OK` with `{"status": "ok", "version": plant_genomics_mcp.__version__}`. Trivial — no new dependency, no MCP-protocol entanglement.

Lets any external watcher (Diun, Uptime Kuma, a curl in a cron) verify liveness without sending a JSON-RPC POST. Exposed at the same public URL since it carries no secrets.

### 3. `~/homelab/plant-genomics-mcp/` — compose service on gt76

New directory following the existing per-service homelab pattern:

```
~/homelab/plant-genomics-mcp/
├── compose.yaml
└── README.md          # ops notes: env vars, restart, log location, funnel cmd
```

`compose.yaml`:

```yaml
services:
  plant-genomics-mcp-http:
    image: ghcr.io/mjarnold/plant-genomics-mcp-http:latest
    container_name: plant-genomics-mcp-http
    restart: unless-stopped
    ports:
      - "127.0.0.1:8765:8765"
    environment:
      PLANT_GENOMICS_MCP_HTTP_HOST: "0.0.0.0"
      PLANT_GENOMICS_MCP_HTTP_PORT: "8765"
      PLANT_GENOMICS_MCP_HTTP_STATELESS: "1"
      PLANT_GENOMICS_MCP_HTTP_JSON: "1"
    networks: [homelab]

networks:
  homelab:
    external: true
```

Wire into `~/homelab/compose.yaml` via the existing `include:` block. Diun (already in the homelab stack) auto-watches `:latest` for new GHCR pushes — releases auto-deploy.

### 4. Tailscale Funnel wiring

One-time setup on gt76:

```bash
tailscale serve --bg --https=443 / http://127.0.0.1:8765
tailscale funnel --bg 443 on
```

Tailscale persists this to `/var/lib/tailscale/`, surviving reboots. Pre-flight: `tailscale status` confirms MagicDNS + HTTPS certs + Funnel allowed on the node (admin console). Captured live: Tailscale 1.96.4 on gt76 (well above the 1.40 minimum for Funnel).

### 5. README — `## Hosted endpoint` section

Above the existing `## Quickstart`:

```markdown
## Hosted endpoint

A read-only public deployment is available at:

https://mjarnoldgt76.tail86d19d.ts.net/mcp

Health check:

curl https://mjarnoldgt76.tail86d19d.ts.net/healthz

Connect from Claude Code:

claude mcp add --transport http plant-genomics-mcp \
 https://mjarnoldgt76.tail86d19d.ts.net/mcp

No auth required. Best-effort uptime — upstream backends self-rate-limit,
so a misbehaving client mostly hurts itself. For production use, run the
stdio entrypoint locally (see Quickstart).
```

## Data flow

Single hop, no state crossing the boundary:

1. Client opens HTTPS to `mjarnoldgt76.tail86d19d.ts.net`
2. Tailscale Funnel terminates TLS, forwards cleartext to gt76's local `127.0.0.1:8765`
3. Docker port mapping delivers the connection to the container's uvicorn
4. Starlette routes:
   - `GET /healthz` → in-process JSON response
   - `POST /mcp` → `StreamableHTTPSessionManager` → low-level MCP `Server` → backend module → upstream HTTP
5. Backend response bubbles back the same path; stateless, no session bookkeeping

## Auth posture

Open access. No bearer token, no IP allowlist. The threat model is "registry catalogs and curious developers"; the actual abuse vector (NCBI BLAST or Ensembl getting flooded _through_ my endpoint) is mitigated by:

- Upstream backends' own rate limits (Ensembl ~15 req/s; NCBI BLAST returns 429s under abuse; UniProt unmetered but slow)
- Tailscale Funnel's bandwidth quota (~1 GB/day per node — natural ceiling)
- Per-IP rate limit drops in as a slowapi middleware **if** abuse is observed (out of scope for v1)

## Error handling

No new error paths beyond what the existing MCP server already emits. Two new surfaces:

- `/healthz` — never raises; always returns 200 if uvicorn is up
- Funnel-side errors (Tailscale edge rejects connection) return a Tailscale-branded HTML error page — not a JSON-RPC envelope. Acceptable for liveness probes; documented in the README ops section.

## Testing

### Unit (`tests/test_http_transport.py`)

- New: `GET /healthz → 200 OK`, body has keys `{"status", "version"}`, `version` equals `__version__`
- Existing tests continue to assert `/mcp` round-trips

### Real-execution validation (post-deploy checklist)

Run from a laptop _not_ on the tailnet:

1. `curl https://mjarnoldgt76.tail86d19d.ts.net/healthz` → 200 OK with `{"status":"ok","version":"0.8.0"}`
2. `curl -X POST https://mjarnoldgt76.tail86d19d.ts.net/mcp -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'` → JSON-RPC response listing all 27 tools
3. `claude mcp add --transport http plant-genomics-mcp https://mjarnoldgt76.tail86d19d.ts.net/mcp` → no error, MCP advertises tools
4. `claude` → call `ensembl_plants_lookup_locus { locus: "AT1G01010" }` → result matches local stdio invocation
5. `docker logs plant-genomics-mcp-http --tail 50` (on gt76) → no tracebacks, request lines visible
6. After 48h: re-run steps 1+2 from a fresh network. Endpoint still up → success.

## Risks + mitigations

| Risk                                                               | Mitigation                                                                                                                                                                                     |
| ------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Tailscale Funnel bandwidth quota (1 GB/day)                        | Monitor `tailscale funnel status` weekly. At zero baseline traffic this is no concern; if it ever 429s, that signals success-mode traffic and triggers re-design (custom domain + Cloudflare). |
| gt76 home internet outage = endpoint down                          | Acceptable for demo-grade. Documented in README.                                                                                                                                               |
| `:latest` GHCR tag + Diun auto-deploy = breaking change auto-ships | Mitigation: pin to `:v0.X.Y` in compose, bump manually on each release. Trade-off: loses auto-deploy. **Decision: ship `:latest` for v1; revisit if a release breaks the endpoint.**           |
| Funnel exposes the gt76 hostname to public DNS                     | Already exposed (Tailscale MagicDNS suffix is per-tailnet but not secret). Acceptable.                                                                                                         |
| Container's in-memory TTL cache evicts on restart                  | Cold-start latency for first caller after restart. Acceptable at demo scale.                                                                                                                   |

## Out-of-scope follow-ups (filed for later)

- **Registry refresh (P1.15 re-poll)**: update modelcontextprotocol.io / Glama / PulseMCP / Smithery entries to advertise the hosted URL alongside the stdio path. Trigger: ≥48h of green uptime.
- **Per-IP rate limit (slowapi)**: drops in as a Starlette middleware if abuse appears.
- **Loki/Promtail wiring**: add a `loki:` logging driver and label the container for the homelab Loki stack if log query becomes useful.
- **Uptime monitoring**: Uptime Kuma or a cron-curl into ntfy alerts. Defer until first outage observed.
- **Pinned image tag**: switch from `:latest` to `:v0.X.Y` if a release breaks production.

## Open questions

None. All clarifying-question answers captured in this spec:

- Purpose: registry-grade demo
- Host: gt76 + Tailscale Funnel
- Auth: open + future per-IP rate limit
- Domain: `ts.net` subdomain (no custom domain)
