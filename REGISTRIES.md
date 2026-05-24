# Registry submissions

Status of submissions to the four primary MCP server registries. **PyPI
and the official MCP registry are live as of v1.0.4** —
`pip install plant-genomics-mcp` (or `pipx install plant-genomics-mcp`)
works directly, and the namespace `io.github.musharna/plant-genomics-mcp`
is listed at registry.modelcontextprotocol.io. The GHCR Docker image
stays pinned at v1.0.3 (no rebuild for the metadata-only 1.0.4 cut); the
`oci` package entry was dropped from `server.json` for the 1.0.4 publish
and will be re-added in v1.0.5 once the image carries
`LABEL io.modelcontextprotocol.server.name=io.github.musharna/plant-genomics-mcp`.

## registry.modelcontextprotocol.io (official) — LIVE

**Status:** **published at v1.0.4 (2026-05-24).** Listing visible at
[`https://registry.modelcontextprotocol.io/v0/servers?search=plant-genomics-mcp`](https://registry.modelcontextprotocol.io/v0/servers?search=plant-genomics-mcp).

**Artifact:** [`server.json`](./server.json) — registers under the
namespace `io.github.musharna/plant-genomics-mcp`. GitHub-auth verifies
the namespace owns the repo; PyPI ownership-verification reads the
literal `mcp-name: io.github.musharna/plant-genomics-mcp` token from
the package README (rendered into wheel METADATA, see footer of
[`README.md`](./README.md)).

**Republishing on subsequent releases** (e.g., v1.0.5+):

```bash
# 1. Install the publisher CLI (Go binary; build from source or pre-built)
git clone https://github.com/modelcontextprotocol/registry /tmp/mcp-registry
make -C /tmp/mcp-registry publisher
sudo install /tmp/mcp-registry/bin/mcp-publisher /usr/local/bin/

# 2. Authenticate against GitHub (device-code browser flow; JWT is short-lived)
mcp-publisher login github

# 3. Bump versions in server.json (top-level + each package entry), then
mcp-publisher validate
mcp-publisher publish
```

**Currently published packages:**

- PyPI: https://pypi.org/project/plant-genomics-mcp/1.0.4/
- GHCR (not in registry yet): `ghcr.io/musharna/plant-genomics-mcp:1.0.3`

## smithery.ai

**Artifact:** [`smithery.yaml`](./smithery.yaml) — stdio launcher that
runs the GHCR image via `docker run --rm -i`.

**Submission steps:**

1. Browse to https://smithery.ai/new and sign in with GitHub.
2. Point Smithery at the GitHub repo `musharna/plant-genomics-mcp`.
3. Smithery reads `smithery.yaml` from the repo root and builds the
   sandbox listing. No additional fields to fill in.
4. Smoke-test the in-browser inspector run against a default locus
   (`AT1G01010`) before flipping the listing to public.

**Blocked-on:** none.

## glama.ai

Glama auto-discovers public GitHub repos that contain MCP server code
and indexes their tool schemas. No active submission required — the
listing appears once the crawler picks up the repo.

**Submission steps:**

1. Verify the listing exists at
   `https://glama.ai/mcp/servers/musharna/plant-genomics-mcp` (may take
   24–72h after the repo first goes public).
2. (Optional) Sign in with GitHub and **claim** the server from the
   listing page — claiming unlocks the admin panel (set categories,
   featured screenshots, etc.).

**Blocked-on:** crawler latency only.

## pulsemcp.com

PulseMCP has a manual submission form on
https://www.pulsemcp.com/servers — click the **Submit** button in the
top-right of any directory page.

**Submission steps:**

1. Navigate to https://www.pulsemcp.com/servers and click **Submit**.
2. Fill in:
   - GitHub URL: `https://github.com/musharna/plant-genomics-mcp`
   - Description: see the `description` field in `server.json`.
   - Classification: community
   - Tags: `plant-biology`, `genomics`, `bioinformatics`, `ensembl`,
     `uniprot`, `phytozome`, `europe-pmc`, `quickgo`, `gramene`, `kegg`,
     `string-db`, `atted-ii`, `blast`, `bar`
3. Submit and wait for moderation (typically <48h).

**Blocked-on:** none.

## Submission state

- **registry.modelcontextprotocol.io** — LIVE as of v1.0.4 (2026-05-24).
- **smithery.ai** — local `smithery.yaml` committed; manual Submit click
  deferred to a human-in-the-loop call.
- **glama.ai** — auto-discovered from the public GitHub repo on a
  ~24–72h crawler latency; no active submission step.
- **pulsemcp.com** — local artifacts ready; manual Submit form deferred
  to a human-in-the-loop call.

The two manual-submit steps (Smithery + PulseMCP) are public,
shared-state actions visible to other people — per the durable
instruction to confirm such actions before firing, the local artifacts
ship now and the actual submission clicks are deferred to a
human-in-the-loop call.
