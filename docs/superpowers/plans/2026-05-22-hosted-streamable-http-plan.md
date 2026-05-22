# Hosted Streamable-HTTP Endpoint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up a publicly reachable HTTPS URL at `https://mjarnoldgt76.tail86d19d.ts.net/mcp` that serves the existing Streamable-HTTP transport, plus a `/healthz` liveness route. Image published to GHCR as `ghcr.io/mjarnold/plant-genomics-mcp-http`, deployed via Docker Compose on gt76, exposed via Tailscale Funnel.

**Architecture:** Reuse the existing `server_http.build_app()` (already builds a Starlette ASGI app mounting MCP at `/mcp`). Add a single `Route("/healthz", ...)` ahead of the mount. Build a second container image (`Dockerfile.http`, ENTRYPOINT `plant-genomics-mcp-http`, EXPOSE 8765) published from the same GHA workflow that already publishes the stdio image. Deploy via a new `~/homelab/plant-genomics-mcp/compose.yaml` on gt76, wired into the top-level `~/homelab/compose.yaml` `include:` block. Tailscale Funnel terminates TLS and proxies to `127.0.0.1:8765` — no certs, no domain registration.

**Tech Stack:** Python 3.11+, `mcp>=1.0`, `starlette`, `uvicorn`, `httpx>=0.27`, `pytest>=8.0`, `pytest-asyncio>=0.23`, GitHub Actions, `docker/build-push-action@v6`, `docker/metadata-action@v5`, Docker Compose, Tailscale ≥ 1.40 (gt76 runs 1.96.4).

**Spec:** [2026-05-22-hosted-streamable-http-design.md](../specs/2026-05-22-hosted-streamable-http-design.md) (commit `190b65a`).

---

## File Structure

| File                                        | Action | Why                                                                                                                                                                                                        |
| ------------------------------------------- | ------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `src/plant_genomics_mcp/server_http.py`     | Modify | Add `healthz` handler + `Route("/healthz", ...)` ahead of the existing `Mount("/mcp", ...)`. No new dependency.                                                                                            |
| `tests/test_http_transport.py`              | Modify | Add unit test asserting `/healthz` returns 200 + `{status, version}` with `version == plant_genomics_mcp.__version__`. Extend the existing real-execution test to also probe `/healthz` over real uvicorn. |
| `Dockerfile.http`                           | Create | Mirror `Dockerfile` (two-stage builder + slim runtime), flip ENTRYPOINT to `plant-genomics-mcp-http`, add `EXPOSE 8765`.                                                                                   |
| `.github/workflows/docker.yml`              | Modify | Add parallel meta + build-push steps for the HTTP image (`ghcr.io/${owner}/plant-genomics-mcp-http`), sharing checkout / QEMU / buildx / login with the existing stdio job.                                |
| `README.md`                                 | Modify | Insert `## Hosted endpoint` section between `## Transports` and `## Install`.                                                                                                                              |
| `CHANGELOG.md`                              | Modify | New `## v0.8.1 — 2026-05-22` section above `v0.8.0`.                                                                                                                                                       |
| `src/plant_genomics_mcp/__init__.py`        | Modify | Bump `__version__` `"0.8.0"` → `"0.8.1"`.                                                                                                                                                                  |
| `pyproject.toml`                            | Modify | Bump `version = "0.8.0"` → `"0.8.1"`.                                                                                                                                                                      |
| `~/homelab/plant-genomics-mcp/compose.yaml` | Create | On gt76 — declares the `plant-genomics-mcp-http` service against `ghcr.io/mjarnold/plant-genomics-mcp-http:latest`, binds `127.0.0.1:8765:8765`, joins the `homelab` external network.                     |
| `~/homelab/plant-genomics-mcp/README.md`    | Create | On gt76 — ops notes: env vars, restart commands, log location, Funnel command, validation snippets.                                                                                                        |
| `~/homelab/compose.yaml`                    | Modify | On gt76 — add one line to the top-level `include:` block: `- plant-genomics-mcp/compose.yaml`.                                                                                                             |

**Repo vs. gt76 split.** Tasks 1–7 are repo-local and TDD-friendly (a subagent or inline executor handles them). Tasks 8–11 are operator-driven gt76 SSH steps and a remote post-deploy validation — these are NOT subagent-safe (they mutate shared infra). Each gt76 task explicitly says "confirm with operator before executing."

**Project conventions** (carry over from prior v0.x plans):

- All v0.x changes commit directly to `main`; no feature branches.
- Lightweight tags (`git tag vX.Y.Z`, no `-a`).
- HEREDOC commits with `Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>` trailer.
- Stage by file name (no `git add -A`).
- Never push or tag-push without an explicit user instruction; the spec is approved but rollout is not.
- Iron Law: read live source before editing every file the plan touches — paths and surrounding code may have drifted since the plan was written.

---

## Task 1: `/healthz` route + tests

The existing `server_http.build_app()` mounts only `/mcp`. Add a tiny in-process `Route("/healthz", ...)` ahead of the mount that returns `200 OK` with `{"status": "ok", "version": plant_genomics_mcp.__version__}`. Trivial — no new dependency, no MCP-protocol entanglement. Lets registry indexers, Uptime Kuma, or a curl-in-cron verify liveness without sending a JSON-RPC POST.

**Files:**

- Modify: `src/plant_genomics_mcp/server_http.py` — add `healthz` async handler + insert `Route` ahead of `Mount` in the `routes=[…]` list.
- Modify: `tests/test_http_transport.py` — append one new unit test + extend the existing real-execution test with two extra assertions.

- [ ] **Step 1: Write the failing unit test**

Append to `tests/test_http_transport.py` (after the existing `test_env_flag_parses_truthy_and_falsey`):

```python
def test_healthz_returns_status_ok_with_version() -> None:
    """`GET /healthz` returns 200 with the package version.

    Lets external watchers (Uptime Kuma, Diun, curl-in-cron) verify
    liveness without sending a JSON-RPC POST. The version field doubles
    as a cheap deploy-confirmation probe.
    """
    from starlette.testclient import TestClient

    from plant_genomics_mcp import __version__

    app = server_http.build_app()
    with TestClient(app) as client:
        resp = client.get("/healthz")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"status": "ok", "version": __version__}, body
```

`TestClient` is synchronous (it spins up its own thread + event loop under the hood), so this test is a plain `def` — no `pytest.mark.asyncio`, no `await`. Same shape as the existing sync unit tests in the file.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_http_transport.py::test_healthz_returns_status_ok_with_version -v`
Expected: FAIL with a 404 / `assert 404 == 200` (`/healthz` is not yet a registered route).

- [ ] **Step 3: Implement the `/healthz` route**

Edit `src/plant_genomics_mcp/server_http.py`. Add a Starlette import for `Route` + `JSONResponse` + `Request`, define a small `healthz` handler, and insert `Route("/healthz", healthz)` ahead of the `Mount("/mcp", ...)` in `build_app`.

Edit 1 — update the imports block (currently lines ~28–37):

```python
import contextlib
import os
from collections.abc import AsyncIterator

import uvicorn
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.types import Receive, Scope, Send

from plant_genomics_mcp import __version__
from plant_genomics_mcp.server import server
```

Edit 2 — inside `build_app`, define `healthz` next to `handle_mcp`:

```python
    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "version": __version__})

    async def handle_mcp(scope: Scope, receive: Receive, send: Send) -> None:
        await session_manager.handle_request(scope, receive, send)
```

Edit 3 — replace the existing `routes=[Mount("/mcp", app=handle_mcp)],` with:

```python
        routes=[
            Route("/healthz", healthz),
            Mount("/mcp", app=handle_mcp),
        ],
```

- [ ] **Step 4: Run unit test to verify it passes**

Run: `pytest tests/test_http_transport.py::test_healthz_returns_status_ok_with_version -v`
Expected: PASS.

- [ ] **Step 5: Extend the existing real-execution test**

Edit `tests/test_http_transport.py::test_http_tools_list_via_real_uvicorn`. Inside the `async with httpx.AsyncClient(...) as client:` block, immediately before the `init_payload` assignment, add a live `/healthz` probe:

```python
            health_resp = await client.get("/healthz")
            assert health_resp.status_code == 200, health_resp.text
            assert health_resp.json()["status"] == "ok"
```

This proves the route is wired through real uvicorn (not just TestClient) — the same boundary check the existing `/mcp` probe already covers.

- [ ] **Step 6: Run the full transport-test file**

Run: `pytest tests/test_http_transport.py -v`
Expected: all tests pass (new unit + parametrized env tests + extended real-execution test).

- [ ] **Step 7: Lint**

Run: `ruff check src/plant_genomics_mcp/server_http.py tests/test_http_transport.py`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add src/plant_genomics_mcp/server_http.py tests/test_http_transport.py
git commit -m "$(cat <<'EOF'
feat(http): add GET /healthz liveness route

Returns 200 with {status, version} so registry indexers, Uptime Kuma,
and curl-in-cron can verify liveness without sending a JSON-RPC POST.
Wired ahead of the /mcp mount in build_app(); real-execution test
covers it alongside the existing tools/list probe.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `Dockerfile.http` for the HTTP entry point

The stdio `Dockerfile` is already two-stage (builder + slim runtime). For the HTTP variant, mirror that exactly — same Python base, same venv install, same non-root `mcp` user — but flip the ENTRYPOINT to `plant-genomics-mcp-http` (the entry point declared in `pyproject.toml:41`) and `EXPOSE 8765`. A distinct file (not a build-arg fork of the stdio one) keeps each image's intent legible and lets the GHA workflow's `docker/build-push-action` target each Dockerfile independently with its own metadata-action tags.

**Files:**

- Create: `Dockerfile.http`.

- [ ] **Step 1: Re-read the live stdio `Dockerfile`**

Run: `cat Dockerfile`
Confirm the two-stage structure (builder + runtime), the non-root `mcp` user setup, and the venv copy. The HTTP file mirrors this exactly except for the ENTRYPOINT line and the addition of `EXPOSE`.

- [ ] **Step 2: Write `Dockerfile.http`**

Create `Dockerfile.http` at the repo root with the following content (note: structurally identical to `Dockerfile` except for the trailing `EXPOSE` and `ENTRYPOINT` lines, and the second-to-last comment):

```dockerfile
# syntax=docker/dockerfile:1.7
# Streamable-HTTP MCP server image. Stage 1 installs the wheel into a venv;
# stage 2 copies that venv into a slim runtime so the final image ships
# without build tooling. ENTRYPOINT runs the HTTP transport instead of stdio.

FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# Copy only what the build needs first, for layer cache stability.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install .


FROM python:3.12-slim AS runtime

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Non-root user — uvicorn binds to a high port (8765), no privileged caps.
RUN useradd --create-home --uid 10001 mcp
USER mcp
WORKDIR /home/mcp

COPY --from=builder --chown=mcp:mcp /opt/venv /opt/venv

# Streamable-HTTP transport — clients POST JSON-RPC at /mcp.
EXPOSE 8765
ENTRYPOINT ["plant-genomics-mcp-http"]
```

- [ ] **Step 3: Verify the Dockerfile syntax locally**

Run: `docker build -f Dockerfile.http -t plant-genomics-mcp-http:local .` (this will pull the python:3.12-slim base + reinstall the wheel; allow 1–3 min).
Expected: build succeeds; no errors. If `docker` isn't installed on the runner host, skip this step — the GHA workflow in Task 3 will exercise the build.

- [ ] **Step 4: (Optional) Smoke-test the image locally**

Only run if Step 3 was executed. Otherwise skip — the real validation lives in Task 11.

```bash
docker run --rm -d --name pgmcp-http-smoke \
  -e PLANT_GENOMICS_MCP_HTTP_HOST=0.0.0.0 \
  -p 127.0.0.1:18765:8765 plant-genomics-mcp-http:local
sleep 2
curl -s http://127.0.0.1:18765/healthz
docker stop pgmcp-http-smoke
```

Expected `curl` output: `{"status":"ok","version":"0.8.1"}` (or `0.8.0` if Task 7 hasn't bumped yet). Then remove the local image: `docker rmi plant-genomics-mcp-http:local`.

- [ ] **Step 5: Commit**

```bash
git add Dockerfile.http
git commit -m "$(cat <<'EOF'
feat(docker): add Dockerfile.http for the streamable-HTTP image

Mirrors the stdio Dockerfile (two-stage builder + slim runtime,
non-root mcp uid 10001) but ENTRYPOINTs plant-genomics-mcp-http and
EXPOSEs 8765. Distinct image keeps each transport's intent legible
and lets the GHA workflow target each Dockerfile independently.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Extend `.github/workflows/docker.yml` to publish both images

The current workflow has one `meta` + one `build-push` step targeting `ghcr.io/${owner}/plant-genomics-mcp` from `Dockerfile`. Duplicate those two steps for the HTTP image (`ghcr.io/${owner}/plant-genomics-mcp-http` from `Dockerfile.http`), sharing the existing checkout / QEMU / buildx / login. Same triggers (`push` to `main` → `:edge`; semver tag → `:vX.Y.Z` + `:vX.Y` + `:latest`). Same `cache-from: type=gha` / `cache-to: type=gha,mode=max` so the two builds share the buildx cache.

**Files:**

- Modify: `.github/workflows/docker.yml`.

- [ ] **Step 1: Re-read the live workflow**

Run: `cat .github/workflows/docker.yml`
Confirm the current step layout (checkout → setup-qemu → setup-buildx → login → metadata-action → build-push-action). The plan extends in-place by appending two new steps after the existing build-push.

- [ ] **Step 2: Append the HTTP metadata + build-push steps**

Edit `.github/workflows/docker.yml`. Locate the existing `Build and push` step (it ends with `cache-to: type=gha,mode=max`). Immediately after that step (still inside the `steps:` list, same indentation level), insert two new steps:

```yaml
- name: Compute image tags (http)
  id: meta_http
  uses: docker/metadata-action@v5
  with:
    images: ghcr.io/${{ github.repository_owner }}/plant-genomics-mcp-http
    tags: |
      type=ref,event=branch,suffix=,enable=${{ github.ref == 'refs/heads/main' }},value=edge
      type=semver,pattern={{version}}
      type=semver,pattern={{major}}.{{minor}}
      type=raw,value=latest,enable=${{ startsWith(github.ref, 'refs/tags/v') }}

- name: Build and push (http)
  uses: docker/build-push-action@v6
  with:
    context: .
    file: Dockerfile.http
    platforms: linux/amd64,linux/arm64
    push: true
    tags: ${{ steps.meta_http.outputs.tags }}
    labels: ${{ steps.meta_http.outputs.labels }}
    cache-from: type=gha
    cache-to: type=gha,mode=max
```

Two specifics matter:

- `id: meta_http` (must differ from the existing `id: meta`).
- `file: Dockerfile.http` (the existing build-push step relies on the default `Dockerfile`; this one points explicitly at the HTTP file).

- [ ] **Step 3: Verify YAML parses**

Run: `python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/docker.yml')); print('ok')"`
Expected: prints `ok`. (No `yamllint` dependency required — the python stdlib equivalent catches indentation breakage.)

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/docker.yml
git commit -m "$(cat <<'EOF'
ci(docker): publish plant-genomics-mcp-http alongside the stdio image

Adds parallel metadata-action + build-push-action steps targeting
Dockerfile.http and ghcr.io/owner/plant-genomics-mcp-http, sharing
the existing checkout / QEMU / buildx / login + buildx cache. Same
trigger surface — push to main → :edge, semver tag → :vX.Y.Z +
:vX.Y + :latest.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Version bump + CHANGELOG entry

The repo currently sits at `0.8.0`. The hosted endpoint is a user-visible shipping change (new container image, new public URL, new README section), so cut a patch release `v0.8.1`. The Diun watcher on gt76 only repulls `:latest`, and `:latest` is only assigned on a semver-tag push — so without this bump the new HTTP image never lands at a tag that flips `:latest`.

**Files:**

- Modify: `src/plant_genomics_mcp/__init__.py`.
- Modify: `pyproject.toml`.
- Modify: `CHANGELOG.md`.

- [ ] **Step 1: Re-read the live `__init__.py` to confirm the exact line**

Run: `cat src/plant_genomics_mcp/__init__.py`
Expected current content: `__version__ = "0.8.0"`. If a prior run already bumped it, skip the Edit and confirm `0.8.1` is in place.

- [ ] **Step 2: Bump `__version__` in `src/plant_genomics_mcp/__init__.py`**

Change the `__version__` line from `"0.8.0"` to `"0.8.1"`. Leave the surrounding docstring untouched (the v0.8.0 docstring still accurately describes the tool surface — no tools added in v0.8.1).

- [ ] **Step 3: Bump `version` in `pyproject.toml`**

Run: `grep -n '^version = ' pyproject.toml` to find the exact line. Change from `version = "0.8.0"` to `version = "0.8.1"`. (Only that field — no other pyproject keys change in this release.)

- [ ] **Step 4: Prepend the CHANGELOG entry**

Edit `CHANGELOG.md`. Insert a new section immediately above `## v0.8.0 — 2026-05-22`:

```markdown
## v0.8.1 — 2026-05-22

Hosted endpoint release — no new tools or backends. Adds a public Streamable-HTTP deployment so MCP clients and registry indexers can connect without cloning the repo. Image published to GHCR as a parallel artifact alongside the existing stdio image.

- **`GET /healthz` route** added to `server_http.build_app()` ahead of the `/mcp` mount. Returns `200 {"status":"ok","version":<__version__>}`. No new dependency, no MCP-protocol entanglement — drop-in target for Uptime Kuma, Diun, or curl-in-cron.
- **`Dockerfile.http` + `ghcr.io/mjarnold/plant-genomics-mcp-http`** new image (two-stage builder + slim runtime, non-root mcp uid 10001, EXPOSE 8765, ENTRYPOINT `plant-genomics-mcp-http`). The existing `plant-genomics-mcp` stdio image is unchanged.
- **`.github/workflows/docker.yml` publishes both images** from the same trigger via parallel `metadata-action` + `build-push-action` steps sharing the buildx GHA cache. Same tag policy on both — push to `main` → `:edge`; semver tag → `:vX.Y.Z` + `:vX.Y` + `:latest`.
- **Hosted instance** at `https://mjarnoldgt76.tail86d19d.ts.net/mcp` (Tailscale Funnel → gt76 → Docker on `127.0.0.1:8765`). Open access — no token, no IP allowlist; upstream backends self-rate-limit. Best-effort uptime, demo-grade. README has the full `claude mcp add` recipe.
```

- [ ] **Step 5: Verify the version bump propagates**

Run:

```bash
python -c "from plant_genomics_mcp import __version__; print(__version__)"
grep -n '^version = ' pyproject.toml
head -4 CHANGELOG.md
```

Expected: `0.8.1` from the python import, `version = "0.8.1"` from pyproject, and `## v0.8.1 — 2026-05-22` as the first dated heading.

- [ ] **Step 6: Run the full test suite (sanity)**

Run: `pytest -q`
Expected: full suite green. Pay special attention to `tests/test_http_transport.py` — the unit test from Task 1 asserts the version field matches `__version__`, so the bump propagates there automatically.

- [ ] **Step 7: Commit**

```bash
git add src/plant_genomics_mcp/__init__.py pyproject.toml CHANGELOG.md
git commit -m "$(cat <<'EOF'
chore: release v0.8.1 — hosted endpoint

Bumps __version__ + pyproject to 0.8.1 and adds the CHANGELOG entry
documenting /healthz, Dockerfile.http, dual-image CI publishing, and
the public Tailscale Funnel deployment.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: README — add `## Hosted endpoint` section

Insert a new top-level section between `## Transports` (line ~98) and `## Install` (line ~115). Two reasons it's its own section rather than a subsection of Transports: (a) it's about _deployment_, not transport mechanics — which env vars to flip; (b) Smithery / Glama / modelcontextprotocol.io indexers grep the README for explicit "hosted" / "public URL" callouts, and a section heading is the most reliable signal.

**Files:**

- Modify: `README.md`.

- [ ] **Step 1: Re-read the live README to find the exact slot**

Run: `grep -n '^## ' README.md`
Confirm `## Transports` and `## Install` are still adjacent (with `## Transports` first). The new section goes between them.

- [ ] **Step 2: Insert the section**

Edit `README.md`. Immediately before the `## Install` line, insert this block (note the trailing blank line — markdown headings need to be separated by blank lines):

```markdown
## Hosted endpoint

A read-only public deployment runs at:
```

https://mjarnoldgt76.tail86d19d.ts.net/mcp

````

Liveness probe:

```bash
curl https://mjarnoldgt76.tail86d19d.ts.net/healthz
# {"status":"ok","version":"0.8.1"}
````

Connect from Claude Code:

```bash
claude mcp add --transport http plant-genomics-mcp \
  https://mjarnoldgt76.tail86d19d.ts.net/mcp
```

No auth required. Best-effort uptime — upstream backends (Ensembl, NCBI BLAST, UniProt, Gramene, …) self-rate-limit, so a misbehaving client mostly hurts itself. For production workloads, run the stdio entry point locally (see [Install](#install)) — the hosted endpoint is for evaluation, registry indexers, and one-off interactive use.

````

(Yes — the inner code fences are intentional. Markdown renderers handle nested fenced blocks fine inside a section that starts with the outer `## Hosted endpoint` heading.)

- [ ] **Step 3: Verify the section renders cleanly**

Run: `python -c "import re; t=open('README.md').read(); assert '## Hosted endpoint' in t and '## Install' in t.split('## Hosted endpoint')[1]; print('order ok')"`
Expected: prints `order ok`. (Sanity-checks that the Hosted section sits ahead of Install.)

- [ ] **Step 4: Lint markdown (if a linter is configured)**

Run: `pre-commit run --files README.md 2>/dev/null || echo "no pre-commit configured — skipping"`
Expected: pre-commit either succeeds or the script prints the skip message. No new errors.

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "$(cat <<'EOF'
docs(readme): add Hosted endpoint section

Calls out the public Tailscale Funnel deployment + the
'claude mcp add --transport http' recipe so registry indexers and
walk-up users can connect without cloning. Lives between Transports
and Install — it's a deployment fact, not a transport-mechanic detail.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
````

---

## Task 6: Full-suite green light + ruff

Final sweep before tagging. Confirm the full test matrix is clean (not just the http file), confirm ruff is clean across `src/` and `tests/`, and confirm the stdio smoke still passes — the new Starlette route is non-stdio territory but a regression on the shared `server` module would surface as a stdio smoke failure too.

**Files:** none modified — verification only.

- [ ] **Step 1: Full pytest**

Run: `PLANT_GENOMICS_MCP_STDIO_SMOKE=1 pytest -q`
Expected: all tests pass (199+ from the v0.8.0 baseline + 1 new from Task 1 — exact count depends on the parametrized env-flag matrix). No skips except the live-gated tests (those require `PLANT_GENOMICS_MCP_LIVE=1`).

- [ ] **Step 2: Ruff**

Run: `ruff check .`
Expected: `All checks passed!`.

- [ ] **Step 3: Build the package (sanity)**

Run: `python -m build --wheel --sdist 2>/dev/null | tail -5 || echo "build not installed — skip"`
Expected: either a wheel + sdist land in `dist/`, or the script prints the skip message. The wheel build catches any pyproject syntax error from the Task 4 bump.

- [ ] **Step 4: Confirm no uncommitted noise**

Run: `git status --short`
Expected: empty output. Anything left here means a prior task half-committed; reconcile before tagging.

- [ ] **Step 5: STOP — ask the user before tagging or pushing**

The repo is green. Per project convention, the next steps are `git tag v0.8.1` (lightweight) and `git push --atomic main v0.8.1`. **Do not run these unless the user has explicitly authorized.** Surface the current state with a message like:

> Tasks 1–6 complete. v0.8.1 ready to tag. `git log --oneline -5` shows the new commits. To trigger the GHCR build + Diun auto-deploy, I'll need to run `git tag v0.8.1 && git push --atomic origin main v0.8.1`. Authorize?

Wait for an explicit "yes" / "approved" / "tag and push" before proceeding. Ambiguous replies ("ok", "sure", "continue") are a stop sign — restate the exact command and re-ask.

---

## Task 7: gt76 — create `~/homelab/plant-genomics-mcp/` compose stub

**Operator-driven.** This task runs on gt76 (host `100.113.204.41`, SSH alias `mjarnold@100.113.204.41`). Confirm with the operator before each shell-mutating step. None of these commands run on the laptop; all are `ssh mjarnold@100.113.204.41 '…'` (or paste into an SSH session).

**Files (on gt76):**

- Create: `~/homelab/plant-genomics-mcp/compose.yaml`.
- Create: `~/homelab/plant-genomics-mcp/README.md`.

- [ ] **Step 1: Verify the GHCR image actually exists**

Cannot proceed until the Task 6 push (and the GHA workflow it triggered) has completed and published `ghcr.io/mjarnold/plant-genomics-mcp-http:latest`. Probe from the operator's laptop or gt76:

```bash
docker manifest inspect ghcr.io/mjarnold/plant-genomics-mcp-http:latest 2>&1 | head -20
```

Expected: a JSON manifest with `linux/amd64` and `linux/arm64` entries. If it returns `manifest unknown`, the GHA workflow hasn't finished; wait and re-probe.

- [ ] **Step 2: Create the service directory on gt76**

```bash
ssh mjarnold@100.113.204.41 'mkdir -p ~/homelab/plant-genomics-mcp && ls -la ~/homelab/plant-genomics-mcp'
```

Expected: empty directory listing.

- [ ] **Step 3: Write `compose.yaml`**

Write the following to `~/homelab/plant-genomics-mcp/compose.yaml` on gt76 (via `scp`, `ssh + cat <<EOF`, or editor of choice):

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

Two specifics:

- Port binding is `127.0.0.1:8765:8765` (laptop side : container side). Funnel proxies from the Tailscale edge directly to this loopback address — no LAN exposure, no WireGuard exposure.
- Container env binds uvicorn to `0.0.0.0` _inside_ the container — that's just so the Docker port-publish forwards. The host port is still loopback-only.

- [ ] **Step 4: Write `README.md` (ops notes for the next operator)**

Write the following to `~/homelab/plant-genomics-mcp/README.md` on gt76:

````markdown
# plant-genomics-mcp (hosted MCP)

Streamable-HTTP transport for plant-genomics-mcp, published at:

https://mjarnoldgt76.tail86d19d.ts.net/mcp

Health: https://mjarnoldgt76.tail86d19d.ts.net/healthz

## How it serves traffic

Tailscale Funnel terminates TLS at the Tailscale edge and forwards cleartext
to `127.0.0.1:8765` on gt76 (this host). That maps to the
`plant-genomics-mcp-http` container's exposed port 8765. The container runs
`uvicorn` against the Starlette app from `server_http.build_app()`.

## Restart / redeploy

```bash
cd ~/homelab
docker compose pull plant-genomics-mcp-http
docker compose up -d plant-genomics-mcp-http
```
````

Diun watches `:latest` and auto-deploys on the next release push.

## Logs

```bash
docker logs -f plant-genomics-mcp-http
docker logs plant-genomics-mcp-http --tail 200
```

No external log shipping yet (Loki/Promtail wiring is filed as a follow-up).

## Funnel state

```bash
tailscale serve status
tailscale funnel status
```

Persisted at `/var/lib/tailscale/`, survives reboots.

## Env knobs

Set in `compose.yaml`:

- `PLANT_GENOMICS_MCP_HTTP_HOST=0.0.0.0` — uvicorn bind inside the container
- `PLANT_GENOMICS_MCP_HTTP_PORT=8765`
- `PLANT_GENOMICS_MCP_HTTP_STATELESS=1` — no per-client session state
- `PLANT_GENOMICS_MCP_HTTP_JSON=1` — JSON response (not SSE) by default

Flip `STATELESS=0` if a client ever needs durable sessions (resumable SSE, long-running tools that hold sessions open).

````

- [ ] **Step 5: Confirm files landed**

```bash
ssh mjarnold@100.113.204.41 'ls -la ~/homelab/plant-genomics-mcp/ && head -5 ~/homelab/plant-genomics-mcp/compose.yaml'
````

Expected: both files present; compose.yaml starts with `services:`.

---

## Task 8: gt76 — wire the new compose file into the top-level `include:`

**Operator-driven.** Same SSH discipline as Task 7. The top-level `~/homelab/compose.yaml` already uses an `include:` block (confirmed live 2026-05-22 — entries: `stacks/apps.yaml`, `stacks/monitoring.yaml`, `stacks/ml.yaml`, `stacks/homepage-mockups.yaml`). Add one more line pointing at the new service directory.

**Files (on gt76):**

- Modify: `~/homelab/compose.yaml`.

- [ ] **Step 1: Re-read the live top-level compose**

```bash
ssh mjarnold@100.113.204.41 'cat ~/homelab/compose.yaml'
```

Expected: `include:` block with the 4 existing `stacks/*.yaml` entries.

- [ ] **Step 2: Add the new include line**

Edit `~/homelab/compose.yaml`. Inside the existing `include:` block, append (preserving the existing entries — do not replace them):

```yaml
- plant-genomics-mcp/compose.yaml
```

Final block should look like:

```yaml
include:
  - stacks/apps.yaml
  - stacks/monitoring.yaml
  - stacks/ml.yaml
  - stacks/homepage-mockups.yaml
  - plant-genomics-mcp/compose.yaml
```

- [ ] **Step 3: Validate the full compose config**

```bash
ssh mjarnold@100.113.204.41 'cd ~/homelab && docker compose config | head -40'
```

Expected: `docker compose` prints the resolved merged config with the new `plant-genomics-mcp-http` service visible. Errors here usually mean YAML indentation in the new include line — fix and retry.

- [ ] **Step 4: Bring the service up**

```bash
ssh mjarnold@100.113.204.41 'cd ~/homelab && docker compose pull plant-genomics-mcp-http && docker compose up -d plant-genomics-mcp-http'
```

Expected: docker pulls the `:latest` tag, creates the container, prints `Container plant-genomics-mcp-http  Started`.

- [ ] **Step 5: Local liveness probe**

```bash
ssh mjarnold@100.113.204.41 'curl -s http://127.0.0.1:8765/healthz'
```

Expected: `{"status":"ok","version":"0.8.1"}`. If it returns nothing or a connect-refused, check `docker logs plant-genomics-mcp-http --tail 50` for uvicorn startup errors.

---

## Task 9: gt76 — one-time Tailscale Funnel wiring

**Operator-driven.** Funnel exposes the local `127.0.0.1:8765` listener to the public internet via the Tailscale edge. Tailscale 1.96.4 is already installed on gt76 (well above the 1.40 minimum). The persistence layer is `/var/lib/tailscale/` — the config survives reboots. Funnel must be allowed for this node in the Tailscale admin console (per-node ACL).

**Files:** none on disk — Tailscale persists state internally. No repo files touched.

- [ ] **Step 1: Pre-flight — confirm MagicDNS, HTTPS certs, and Funnel are enabled**

```bash
ssh mjarnold@100.113.204.41 'tailscale status | head -3 && tailscale cert mjarnoldgt76.tail86d19d.ts.net 2>&1 | head -5 || true'
```

Expected: `tailscale status` shows the node online; the cert command either prints the existing cert files or attempts to provision them. If MagicDNS / HTTPS certs are off, fix in the Tailscale admin console (`https://login.tailscale.com/admin/dns`) before continuing.

- [ ] **Step 2: Wire the serve mapping**

```bash
ssh mjarnold@100.113.204.41 'sudo tailscale serve --bg --https=443 / http://127.0.0.1:8765'
```

Expected: prints the live URL (`https://mjarnoldgt76.tail86d19d.ts.net/`). Confirms the cleartext-to-loopback proxy is set up.

- [ ] **Step 3: Flip Funnel on**

```bash
ssh mjarnold@100.113.204.41 'sudo tailscale funnel --bg 443 on'
```

Expected: prints `Available on the internet:` followed by the public URL. If it errors `funnel not allowed for this node`, open the Tailscale admin console → Funnel → enable for `mjarnoldgt76`, then retry.

- [ ] **Step 4: Verify persistence**

```bash
ssh mjarnold@100.113.204.41 'tailscale serve status && echo "---" && tailscale funnel status'
```

Expected: both subcommands report active mappings. These are persisted by `tailscaled` — no systemd unit to add.

- [ ] **Step 5: First public probe (from gt76)**

```bash
ssh mjarnold@100.113.204.41 'curl -s https://mjarnoldgt76.tail86d19d.ts.net/healthz'
```

Expected: same JSON as Task 8 Step 5. If the public probe fails but the loopback probe in Task 8 succeeded, the Funnel layer didn't activate — re-run Step 3 and check the admin-console allow-list.

---

## Task 10: External end-to-end validation (from off-tailnet)

**Operator-driven** — must run from a network _not_ on the tailnet (the laptop's normal Wi-Fi is fine, but only if Tailscale is paused; an LTE hotspot is the cleanest signal). Mirrors the 6-step checklist in the spec.

**Files:** none — verification only.

- [ ] **Step 1: Public health probe**

Run from off-tailnet:

```bash
curl -s https://mjarnoldgt76.tail86d19d.ts.net/healthz
```

Expected: `{"status":"ok","version":"0.8.1"}`.

- [ ] **Step 2: MCP `tools/list` JSON-RPC POST**

```bash
curl -s -X POST https://mjarnoldgt76.tail86d19d.ts.net/mcp/ \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"manual-probe","version":"0.0.1"}}}' \
| head -c 200
```

Expected: a JSON-RPC envelope with `"result":{"serverInfo":{"name":"plant-genomics-mcp"…}}`. Note the trailing `/` on `/mcp/` — the existing real-execution test uses that form (line 110 of `tests/test_http_transport.py`).

Then issue `tools/list` against the same endpoint (without `initialize` for this stateless probe — stateless mode treats every POST as its own session):

```bash
curl -s -X POST https://mjarnoldgt76.tail86d19d.ts.net/mcp/ \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
| python -c "import json,sys; d=json.load(sys.stdin); print(len(d['result']['tools']))"
```

Expected: prints `27` (matches the v0.8.x catalog).

- [ ] **Step 3: Claude Code MCP registration**

```bash
claude mcp add --transport http plant-genomics-mcp-hosted \
  https://mjarnoldgt76.tail86d19d.ts.net/mcp
```

Expected: command exits 0; `claude mcp list` shows the new entry as `plant-genomics-mcp-hosted` with transport `http`. (Use a distinct local name to avoid collision with any existing stdio install.)

- [ ] **Step 4: Real-tool round-trip**

In a fresh Claude Code session, invoke:

```
ensembl_plants_lookup_locus { locus: "AT1G01010" }
```

Expected: response with `gene_id`, `description`, etc. — same shape as a local stdio call. If it errors `tool not found`, the dispatch wiring is wrong; if it errors with an upstream timeout, the hosted backend has a network issue but the MCP layer is OK.

- [ ] **Step 5: Container-side log inspection**

```bash
ssh mjarnold@100.113.204.41 'docker logs plant-genomics-mcp-http --tail 50'
```

Expected: uvicorn `INFO:` lines for each request, no tracebacks. Each public POST should show a `127.0.0.1` source (Funnel terminates at loopback, so requests appear local to the container).

- [ ] **Step 6: 48-hour soak**

Wait ≥48h after Task 9 Step 5 (the first public probe). Re-run Steps 1 + 2 from a fresh network. If both still return correctly, ship it: the endpoint is registry-grade.

---

## Task 11: Tag the release + push (gated by Task 6 Step 5 approval)

**Operator-driven.** Only execute after the user has explicitly approved Task 6 Step 5. This is the trigger that fires the GHA workflow + populates `:latest` on GHCR + lets Diun on gt76 see the new image. (Tasks 7–10 cannot meaningfully start without this.)

**Files:** none — git state only.

- [ ] **Step 1: Confirm clean state**

```bash
git status --short
git log --oneline -8
```

Expected: empty `status`; the last ~6 commits are the Task 1–5 commits in order.

- [ ] **Step 2: Tag + push**

Exactly as the user authorized:

```bash
git tag v0.8.1
git push --atomic origin main v0.8.1
```

Expected: push succeeds; GHCR Actions tab shows the `docker` workflow firing within ~30s.

- [ ] **Step 3: Watch the GHA workflow**

```bash
gh run watch $(gh run list --workflow=docker.yml --limit 1 --json databaseId --jq '.[0].databaseId')
```

Expected: both `Build and push` and `Build and push (http)` steps green. The whole run takes ~3–6 min (multi-arch builds dominate). If either build fails, the tag has already been pushed — fix forward with a v0.8.2 follow-up; don't try to re-point or delete the v0.8.1 tag remotely (that breaks any downstream cache that pulled it).

- [ ] **Step 4: Confirm both manifests landed**

```bash
docker manifest inspect ghcr.io/mjarnold/plant-genomics-mcp:latest      | jq '.manifests | length'
docker manifest inspect ghcr.io/mjarnold/plant-genomics-mcp-http:latest | jq '.manifests | length'
```

Expected: both print `2` (one for amd64, one for arm64). If the HTTP manifest is missing, the `(http)` build-push step in the workflow didn't fire — re-check the YAML from Task 3.

After Step 4 passes, hand off to Task 7 (gt76 deployment).

---

## Out-of-scope follow-ups (NOT in this plan)

Filed for later, do NOT slip into this rollout:

- **Registry-entry refresh (P1.15 re-poll)** — update modelcontextprotocol.io / Glama / PulseMCP / Smithery entries to advertise the hosted URL alongside the stdio path. Trigger: ≥48h of green uptime per Task 10 Step 6. Open a separate task after the soak window.
- **Per-IP rate limit (slowapi)** — drops in as a Starlette middleware _if_ abuse appears.
- **Loki/Promtail wiring** — add a `loki:` logging driver + label the container for the homelab Loki stack if log query becomes useful.
- **Uptime monitoring** — Uptime Kuma probe or a cron-curl into ntfy. Defer until the first observed outage.
- **Pinned image tag** — switch from `:latest` to `:v0.X.Y` in compose if a release ever breaks production.
- **Custom domain / Cloudflare migration** — only if Tailscale Funnel's 1 GB/day bandwidth quota becomes a real ceiling.

These are explicitly _out of scope_ — adding any of them mid-rollout dilutes the spec's "registry-grade demo, low traffic, no SLA" framing.
