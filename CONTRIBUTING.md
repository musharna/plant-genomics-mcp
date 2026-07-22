# Contributing

Thanks for helping improve `plant-genomics-mcp` — an MCP server exposing 50
tools across 23 live plant-genomics backends. Contributions are welcome:
bug fixes, new backends, new tools, and additions to the organism coverage
matrix.

## Dev setup

Python ≥3.11 is required.

```bash
git clone https://github.com/musharna/plant-genomics-mcp.git
cd plant-genomics-mcp
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

The `[dev]` extra installs `pytest`, `pytest-asyncio`, `pytest-httpx`,
`pytest-cov`, `ruff`, and `mypy`.

If you prefer `uv`, the extra is **not** installed by default — and a bare
`uv sync` will actively _remove_ `pytest` from an existing environment, after
which `uv run pytest` silently falls through to whatever `pytest` is on your
`PATH` and fails with `ModuleNotFoundError: plant_genomics_mcp`. Always ask
for the extra:

```bash
uv sync --extra dev
uv run pytest -q
```

## Running tests

The default suite is fully mocked (no network) and must pass with the coverage
floor enforced:

```bash
.venv/bin/pytest -q --cov=src --cov-report=term
```

Coverage is gated: `[tool.coverage.report] fail_under = 92` in
`pyproject.toml`, and `pytest-cov` honors it, so `pytest --cov` **fails the
build if total coverage drops below 92%**. Measured total is ~94%, so there is
a little slack — but new code should land with tests.

Two optional test groups are gated behind environment variables and are **not**
run in CI (live ones avoid upstream flakiness):

```bash
PLANT_GENOMICS_MCP_LIVE=1        .venv/bin/pytest -q   # adds live-network probes against the 11 backends
PLANT_GENOMICS_MCP_STDIO_SMOKE=1 .venv/bin/pytest -q   # adds a stdio init / tools/list smoke test
```

CI runs the mocked suite + stdio smoke on Python 3.11 and 3.12. If you add or
change a backend, please run the live gate locally and (where relevant) the
scientific-validation sweep:

```bash
.venv/bin/python scripts/benchmark_annotations.py      # full live drift sweep (~3-5 min)
```

## Linting

CI runs ruff exactly as:

```bash
.venv/bin/ruff check .
```

Line length is 100, target version `py311` (see `[tool.ruff]` in
`pyproject.toml`). Run `ruff check --fix .` to auto-fix what it can.

## Running the server locally

```bash
# stdio (default transport) from a checkout:
.venv/bin/plant-genomics-mcp

# register with Claude Code (local scope):
claude mcp add plant-genomics --scope local -- "$(pwd)/.venv/bin/plant-genomics-mcp"
```

Or install the published package globally with `pipx install plant-genomics-mcp`
and run the `plant-genomics-mcp` entry point. The optional HTTP transport is
`plant-genomics-mcp-http`; see the README for transport and env-var details.

## Pull request expectations

Before opening a PR, confirm all of the following pass locally:

- `ruff check .` is clean.
- `pytest -q --cov=src --cov-report=term` passes, including the **92% coverage
  floor** (new code comes with tests).
- For user-facing changes, `CHANGELOG.md` is updated.
- Docs (README / `docs/`) are updated when behavior, tools, env vars, or the
  coverage matrix change.

Use the PR template that pre-fills when you open the PR. Keep changes focused
and explain the motivation in the description.
