# Registry submissions

Status of submissions to the four primary MCP server registries. The
local artifacts (`server.json`, `smithery.yaml`) are committed; the
external submission steps still require an authenticated push and a
human-in-the-loop click. PyPI publication is deliberately deferred until
the final-publish gate, so all current submissions reference the GHCR
Docker image (`ghcr.io/musharna/plant-genomics-mcp:0.5.0`) rather than a
`pip install`-able package.

## registry.modelcontextprotocol.io (official)

**Artifact:** [`server.json`](./server.json) — registers under the
namespace `io.github.musharna/plant-genomics-mcp` (GitHub-auth verifies
the namespace owns the repo).

**Submission steps:**

```bash
# 1. Install the publisher CLI (Go binary; build from source or pre-built)
git clone https://github.com/modelcontextprotocol/registry /tmp/mcp-registry
make -C /tmp/mcp-registry publisher
sudo install /tmp/mcp-registry/bin/mcp-publisher /usr/local/bin/

# 2. Authenticate against GitHub (browser flow)
mcp-publisher login github

# 3. Validate and publish from the repo root
mcp-publisher publish
```

Verify at `https://registry.modelcontextprotocol.io/v0/servers?search=plant-genomics-mcp`.

**Blocked-on:** none — Docker package on GHCR is live (`0.5.0` tag
shipped via the P0.4 publish workflow). PyPI entry will be added to
`server.json` when we cut the PyPI release.

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
     `uniprot`, `phytozome`, `europe-pmc`, `quickgo`
3. Submit and wait for moderation (typically <48h).

**Blocked-on:** none.

## Why no submissions have fired yet

These four steps are public, shared-state actions visible to other
people — per the durable instruction to confirm such actions before
firing, the local artifacts ship now and the actual submission clicks /
`mcp-publisher publish` runs are deferred to a human-in-the-loop call.
