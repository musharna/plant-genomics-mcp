# syntax=docker/dockerfile:1.7
# Minimal stdio MCP server image. Stage 1 uses uv to materialize the locked
# dependency graph (uv.lock) into a venv; stage 2 copies that venv into a
# slim runtime so the final image ships without uv or build tooling. Pinning
# through the lockfile makes the build byte-reproducible across hosts.

FROM ghcr.io/astral-sh/uv:0.11.16-python3.12-trixie-slim AS builder

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# Lock + manifest first for layer cache; sources change more often than deps.
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src

# --frozen: use exact versions from uv.lock; fail if lock is out of date.
# --no-editable: install the project as a wheel so the venv is self-contained
#                and doesn't reference /build/src in stage 2.
RUN uv sync --frozen --no-editable


FROM python:3.12-slim AS runtime

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Non-root user — MCP stdio doesn't need privileged ports.
RUN useradd --create-home --uid 10001 mcp
USER mcp
WORKDIR /home/mcp

COPY --from=builder --chown=mcp:mcp /opt/venv /opt/venv

# stdio transport — clients launch this and talk JSON-RPC over fds 0/1.
ENTRYPOINT ["plant-genomics-mcp"]
