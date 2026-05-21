# syntax=docker/dockerfile:1.7
# Minimal stdio MCP server image. Stage 1 installs the wheel into a venv;
# stage 2 copies that venv into a slim runtime so the final image ships
# without build tooling.

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

# Non-root user — MCP stdio doesn't need privileged ports.
RUN useradd --create-home --uid 10001 mcp
USER mcp
WORKDIR /home/mcp

COPY --from=builder --chown=mcp:mcp /opt/venv /opt/venv

# stdio transport — clients launch this and talk JSON-RPC over fds 0/1.
ENTRYPOINT ["plant-genomics-mcp"]
