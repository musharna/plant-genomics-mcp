"""Run one perf workload in this process and print what it cost, as JSON.

    python -m tests.perf.harness <workload>            # replay its recorded responses
    python -m tests.perf.harness <workload> --record   # call the real upstreams, save them

A workload is one tool call taken through the path ``server._call_tool``
takes (rename deprecated arguments, validate, dispatch), with every HTTP
request answered from ``fixtures/<workload>.json``. Two things are counted,
neither by editing the code under test:

- **Python calls**: a ``sys.setprofile`` hook counts each call of a function
  whose source is under ``src/plant_genomics_mcp``, keyed
  ``relpath:qualname``. A coroutine or generator is counted when it starts,
  not each time it resumes after an ``await``: how often a coroutine resumes
  depends on asyncio's own scheduling, which differs between Python versions.
  Comprehensions are not counted: they are functions of their own on 3.11
  and inlined from 3.12 (PEP 709). Nothing outside the package is counted, so
  stdlib and dependency internals cannot leak into the numbers.
- **HTTP requests per host**, as the mock transport saw them.

The tool's answer is hashed (``output_sha256``) so the ratchet can check the
harness ran the workload it was asked for, not a default or an error.

Each workload runs in its own interpreter (``ratchet.py`` spawns one per
workload), so caches, lazy imports and ``lru_cache`` warm-up in one workload
cannot lower another's count.
"""

from __future__ import annotations

import asyncio
import base64
import dis
import hashlib
import inspect
import json
import sys
import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import CodeType, FrameType
from typing import Any

import httpx

import plant_genomics_mcp
from plant_genomics_mcp import server

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
SRC = Path(plant_genomics_mcp.__file__).resolve().parent

# One call per main tool: the tools the agent benchmark's server runs used
# (benchmarks/agent_qa), plus enrichment, GO, literature, the batch form and
# the synthesis fan-out. Arguments are fixed, so the requests are too.
WORKLOADS: dict[str, tuple[str, dict[str, Any]]] = {
    "lookup_locus": ("ensembl_plants_lookup_locus", {"locus": "AT5G43460"}),
    "get_sequence": ("get_sequence", {"locus": "AT2G13680", "seq_type": "protein"}),
    "resolve_uniprot": ("resolve_locus_to_uniprot", {"locus": "AT3G23430"}),
    "gene_xrefs": ("get_gene_xrefs", {"locus": "AT3G23430"}),
    "gramene_homologs": (
        "gramene_homologs",
        {"locus": "AT1G19850", "target_organism": "oryza_sativa"},
    ),
    "assembly": ("ensembl_plants_assembly", {"organism": "arabidopsis_thaliana", "limit": 10}),
    "entry_members": ("entry_members", {"entry": "IPR010525", "page_size": 20}),
    "gene_tree_members": ("gene_tree_members", {"locus": "AT1G19850", "limit": 20}),
    "bar_gene_summary": ("bar_gene_summary", {"locus": "AT1G19850"}),
    "string_interactions": ("string_interactions", {"locus": "AT1G19850", "limit": 10}),
    "go_enrichment": (
        "go_enrichment",
        {
            "loci": [
                "AT1G19850",
                "AT1G30330",
                "AT2G33860",
                "AT5G62000",
                "AT1G19220",
                "AT5G20730",
                "AT1G59750",
                "AT2G28350",
                "AT4G30080",
                "AT3G61830",
            ],
            "top_n": 10,
        },
    ),  # fmt: skip
    "locus_go_annotations": ("locus_go_annotations", {"locus": "AT1G19850", "limit": 20}),
    "locus_literature": ("locus_literature", {"locus": "AT1G19850", "size": 5}),
    "batch_lookup": (
        "batch_ensembl_plants_lookup_locus",
        {"loci": ["AT1G19850", "AT5G43460", "AT3G23430"]},
    ),
    "analyze_locus_synth": ("analyze_locus_synth", {"locus": "AT1G19850"}),
}

# The answer may carry "now" (synthesis envelopes, release probes): pin it.
FROZEN_NOW = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)
# Headers httpx recomputes for the decoded body we store; replaying them would lie.
_DROP_HEADERS = {"content-encoding", "content-length", "transfer-encoding", "date", "set-cookie"}
_GEN_FLAGS = (
    inspect.CO_GENERATOR
    | inspect.CO_COROUTINE
    | inspect.CO_ASYNC_GENERATOR
    | inspect.CO_ITERABLE_COROUTINE
)
_COMPREHENSIONS = {"<listcomp>", "<dictcomp>", "<setcomp>"}


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz: Any = None) -> _FrozenDatetime:
        return cls.fromtimestamp(FROZEN_NOW.timestamp(), tz)


class _FrozenTime:
    """The ``time`` module with its clocks stopped (``elapsed_s`` reads 0.0)."""

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)

    def time(self) -> float:
        return FROZEN_NOW.timestamp()

    def monotonic(self) -> float:
        return 1000.0

    def perf_counter(self) -> float:
        return 1000.0


def _freeze_clock() -> None:
    """Point each package module's ``datetime`` and ``time`` at stopped clocks.

    Answers carry "now" and step timings (synthesis envelopes, release
    probes), and the TTL cache reads the monotonic clock. The package's own
    functions still run and are counted; only the stdlib names they call
    through are replaced.
    """
    frozen_time = _FrozenTime()
    for name, mod in list(sys.modules.items()):
        if not name.startswith("plant_genomics_mcp"):
            continue
        if getattr(mod, "datetime", None) is datetime:
            mod.datetime = _FrozenDatetime  # type: ignore[attr-defined]
        if getattr(mod, "time", None) is time:
            mod.time = frozen_time  # type: ignore[attr-defined]


def _key(request: httpx.Request) -> str:
    body = request.content
    digest = hashlib.sha256(body).hexdigest()[:16] if body else "-"
    return f"{request.method} {request.url} {digest}"


def _first_resume(code: CodeType) -> int:
    """Offset of the RESUME a fresh generator/coroutine frame starts at."""
    for ins in dis.get_instructions(code):
        if ins.opname == "RESUME":
            return ins.offset
    raise RuntimeError(f"no RESUME in {code.co_qualname}")  # pragma: no cover


class CallCounter:
    """``sys.setprofile`` hook counting calls into the package's own source."""

    def __init__(self, root: Path) -> None:
        self.prefix = str(root) + "/"
        self.repo = root.parent.parent
        self.counts: Counter[str] = Counter()
        self._rel: dict[str, str | None] = {}
        self._start: dict[CodeType, int] = {}

    def _relpath(self, filename: str) -> str | None:
        if filename not in self._rel:
            path = Path(filename).resolve()
            inside = str(path).startswith(self.prefix)
            self._rel[filename] = path.relative_to(self.repo).as_posix() if inside else None
        return self._rel[filename]

    def __call__(self, frame: FrameType, event: str, arg: object) -> None:
        if event != "call":
            return
        code = frame.f_code
        rel = self._relpath(code.co_filename)
        if rel is None or code.co_name in _COMPREHENSIONS:
            return
        if code.co_flags & _GEN_FLAGS:
            start = self._start.get(code)
            if start is None:
                start = self._start[code] = _first_resume(code)
            if frame.f_lasti != start:
                return  # a resume after an await/yield, not a new call
        self.counts[f"{rel}:{code.co_qualname}"] += 1


class FixtureMiss(BaseException):
    """A request the workload's recording does not hold."""


class Replay:
    """Mock transport handler: answer each request from the recorded fixture.

    A request the recording does not hold raises :class:`FixtureMiss`, a
    ``BaseException`` the package's ``except Exception`` handlers cannot turn
    into a retry or a quieter answer, and the run fails on it.
    """

    def __init__(self, recorded: dict[str, list[dict[str, Any]]]) -> None:
        self.recorded = recorded
        self.served: Counter[str] = Counter()
        self.hosts: Counter[str] = Counter()
        self.misses: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        key = _key(request)
        self.hosts[request.url.host] += 1
        answers = self.recorded.get(key)
        if not answers:
            self.misses.append(key)
            raise FixtureMiss(key)
        a = answers[min(self.served[key], len(answers) - 1)]
        self.served[key] += 1
        body = base64.b64decode(a["body_b64"]) if "body_b64" in a else a["body"].encode()
        return httpx.Response(a["status"], headers=a["headers"], content=body)


class Recorder(httpx.AsyncBaseTransport):
    """Real transport that keeps each request's response for the fixture."""

    def __init__(self) -> None:
        self.inner = httpx.AsyncHTTPTransport()
        self.recorded: dict[str, list[dict[str, Any]]] = {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        resp = await self.inner.handle_async_request(request)
        body = await resp.aread()
        headers = [(k, v) for k, v in resp.headers.multi_items() if k not in _DROP_HEADERS]
        entry: dict[str, Any] = {"status": resp.status_code, "headers": headers}
        try:
            entry["body"] = body.decode("utf-8")
        except UnicodeDecodeError:
            entry["body_b64"] = base64.b64encode(body).decode()
        self.recorded.setdefault(_key(request), []).append(entry)
        return httpx.Response(resp.status_code, headers=headers, content=body, request=request)


def _install_transport(transport: httpx.AsyncBaseTransport) -> None:
    """Make every ``httpx.AsyncClient()`` the package opens use ``transport``."""
    real = httpx.AsyncClient

    def client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    httpx.AsyncClient = client  # type: ignore[misc,assignment]


async def _call(tool: str, args: dict[str, Any]) -> Any:
    # The steps of server._call_tool, minus the MCP request context.
    args = server._rename_deprecated_arguments(tool, args)
    server._validate_arguments(tool, args)
    return await server._dispatch(tool, args)


def run(name: str, *, record: bool = False) -> dict[str, Any]:
    tool, args = WORKLOADS[name]
    _freeze_clock()
    fixture = FIXTURES / f"{name}.json"
    replay: Replay | None = None
    recorder: Recorder | None = None
    if record:
        recorder = Recorder()
        _install_transport(recorder)
    else:
        replay = Replay(json.loads(fixture.read_text())["responses"])
        _install_transport(httpx.MockTransport(replay))
    counter = CallCounter(SRC)
    profile: Callable[[FrameType, str, object], None] = counter
    sys.setprofile(profile)
    try:
        payload = asyncio.run(_call(tool, dict(args)))
    finally:
        sys.setprofile(None)
    if recorder is not None:
        FIXTURES.mkdir(exist_ok=True)
        fixture.write_text(
            json.dumps({"tool": tool, "args": args, "responses": recorder.recorded}, indent=1)
            + "\n"
        )
    if replay is not None and replay.misses:
        raise SystemExit(f"{name}: requests not in {fixture.name}: {replay.misses}")
    out = json.dumps(payload, sort_keys=True, default=str)
    return {
        "tool": tool,
        "output_sha256": hashlib.sha256(out.encode()).hexdigest(),
        "calls_total": sum(counter.counts.values()),
        "http": dict(sorted(replay.hosts.items())) if replay else {},
        "calls": dict(sorted(counter.counts.items())),
    }


def main(argv: list[str]) -> None:
    if not argv or argv[0] not in WORKLOADS:
        raise SystemExit(f"usage: harness.py <workload> [--record]; workloads: {list(WORKLOADS)}")
    print(json.dumps(run(argv[0], record="--record" in argv[1:])))


if __name__ == "__main__":
    main(sys.argv[1:])
