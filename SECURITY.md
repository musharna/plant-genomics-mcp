# Security Policy

## Supported versions

`plant-genomics-mcp` is released from `main` and only the latest published
version receives security fixes. As of this writing that is **v1.8.0**.

| Version                 | Supported          |
| ----------------------- | ------------------ |
| Latest release (v1.8.0) | :white_check_mark: |
| Older releases          | :x:                |

Fixes ship in a new patch/minor release on PyPI; there are no long-term
support branches. Always upgrade to the latest version.

## Reporting a vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

Report privately through either channel:

1. **GitHub private advisory (preferred).** Go to the repository's
   **Security → Advisories → Report a vulnerability** page
   (`https://github.com/musharna/plant-genomics-mcp/security/advisories/new`).
   This opens a private advisory visible only to maintainers.
2. **Email.** mjarnold1998@gmail.com — include "plant-genomics-mcp security"
   in the subject.

Please include a description of the issue, affected version, reproduction
steps, and the impact you expect. You will get an acknowledgement; once a fix
is available it will be released and the advisory disclosed.

## Security model

Understanding the attack surface helps scope reports.

- **Outbound third-party calls.** The server makes outbound HTTPS requests to
  **11 public bioinformatics APIs** — Ensembl Plants, Phytozome (JGI
  BioMart), UniProtKB, Europe PMC, QuickGO, NCBI BLAST, Gramene, KEGG,
  STRING-DB, ATTED-II, and BAR. By default **no credentials are sent** to any
  of them; responses are untrusted upstream data. The optional
  `PLANT_GENOMICS_MCP_NCBI_EMAIL` is an etiquette contact string, not a secret.

- **Optional HTTP transport.** Besides the default stdio transport, the server
  can run a streamable-HTTP transport (`plant-genomics-mcp-http`, JSON-RPC at
  `/mcp`). When the HTTP transport is enabled it is gated by a bearer token
  (`PLANT_GENOMICS_MCP_HTTP_TOKEN`), which **must be ≥32 characters** or the
  HTTP server aborts at startup. Generate one with `openssl rand -hex 32`.
  - The default bind address is `127.0.0.1` (loopback). **Do not expose the
    HTTP transport on `0.0.0.0` / a public interface.** Bind to loopback or a
    private tailnet/VPN address and front it with TLS if remote access is
    needed. The bearer token is the only authentication layer.
  - The body cap (`PLANT_GENOMICS_MCP_HTTP_MAX_BODY`, default 2 MiB) limits
    request size; keep it conservative for internet-exposed deployments.

- **No local execution / no persistence.** The server does not execute
  user-supplied code, write to a database, or persist secrets to disk; the only
  state is an in-memory per-backend TTL+LRU response cache.

The stdio transport (the default and recommended mode) takes no network input
and needs no token.
