"""Every optional tool argument reaches its backend: the schema default when
omitted, the caller's value when given.

Why this exists (nightly mutation run, 2026-09-16, #96): ``server.py`` had 508
surviving mutants, 346 of them behaviour changes, and most were of two shapes:

    organism=args.get("organism", "arabidopsis_thaliana")   ->   organism=None
    size=args.get("size", europe_pmc.DEFAULT_PAGE_SIZE)     ->   (dropped)

``test_server_dispatch.py`` proves the identifier and the default organism
route; nothing proved the other 70-odd optional arguments did. A test per
argument would be 74 hand-written tests that drift; instead the tool schema
is the oracle. Each optional property carries its default in the schema, so
"the backend must see the schema default when the caller omits it" and "the
backend must see the caller's value when given" are checkable for every tool
from the catalog itself — and a schema default that disagrees with the
dispatcher's literal is caught as a bonus.
"""

from __future__ import annotations

from typing import Any

import pytest

from plant_genomics_mcp import server
from tests.test_server_dispatch import DISPATCH_SPECS, Spec, _Env, _make_recorder

# Properties the dispatcher consumes itself or renames before the backend call.
# Each entry is a fact about server.py, not an exemption: keep it short.
_CONSUMED: dict[str, set[str]] = {}
_RENAMED: dict[str, dict[str, str]] = {}
# Arms that forward ``limit=None`` on purpose and let the backend resolve it:
# the invariant is then "the backend's resolution of None IS the schema default".
_BACKEND_RESOLVES: dict[str, dict[str, str]] = {
    "locus_variants": {"limit": "ensembl_variation"},
    "orthodb_orthologs": {"limit": "orthodb"},
    "gramene_homologs": {"limit": "gramene"},
}


def _schema(tool: str) -> dict[str, Any]:
    t = next(t for t in server.TOOLS if t.name == tool)
    return t.input_schema or {}


def _optional_with_defaults(tool: str) -> dict[str, Any]:
    """Every optional property -> the value the backend must see when it is
    omitted: the schema default, or None for the three with no default
    (go_enrichment.sources/background, blast_sequence.database), which the
    dispatcher forwards as ``args.get(name)``."""
    s = _schema(tool)
    required = set(s.get("required", []))
    return {
        k: v.get("default")
        for k, v in s.get("properties", {}).items()
        if k not in required and k not in _CONSUMED.get(tool, set())
    }


def _positional_checks(spec: Spec, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
    """The caller's client goes first, and every REQUIRED schema property's
    value reaches the backend (positionally or by name). Survivors this kills:
    ``client`` -> ``None`` / dropped, ``args["start"]`` -> ``None``."""
    import httpx

    if not spec.sync:
        assert args and isinstance(args[0], httpx.AsyncClient), (
            f"{spec.tool}: first positional must be the AsyncClient, got {args[:1]!r}"
        )
    for name in _schema(spec.tool).get("required", []):
        value = spec.args[name]
        assert value in args or kwargs.get(name) == value, (
            f"{spec.tool}: required {name}={value!r} did not reach the backend: {args!r} {kwargs!r}"
        )


def _sentinel_for(prop: dict[str, Any], default: Any) -> Any:
    """A value the schema allows that differs from the default."""
    typ = prop.get("type")
    types = typ if isinstance(typ, list) else [typ]
    if default is None:  # no schema default: any allowed value is a sentinel
        if "array" in types:
            return ["sentinel-a", "sentinel-b"]
        if "integer" in types or "number" in types:
            return 3
        return "sentinel"
    if isinstance(default, bool):
        return not default
    if isinstance(default, int | float):
        lo: float | None = prop.get("minimum")
        hi: float | None = prop.get("maximum")
        cand: float = default + 1
        if hi is not None and cand > hi:
            cand = default - 1
        if lo is not None and cand < lo:
            assert hi is not None and hi != default, f"no room for a sentinel in {prop}"
            cand = hi
        return cand
    if "enum" in prop:
        return next(v for v in prop["enum"] if v != default)
    if "array" in types:
        return ["sentinel-a", "sentinel-b"]
    return f"sentinel-{default}"


def _kw_name(tool: str, prop: str) -> str:
    return _RENAMED.get(tool, {}).get(prop, prop)


_SPECS = [s for s in DISPATCH_SPECS if _optional_with_defaults(s.tool)]


@pytest.mark.parametrize("spec", _SPECS, ids=lambda s: s.tool)
@pytest.mark.asyncio
async def test_omitted_optional_args_reach_the_backend_as_their_schema_defaults(
    spec: Spec, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _make_recorder(_Env() if spec.synth else {"stub": True}, sync=spec.sync)
    monkeypatch.setattr(spec.module, spec.attr, rec)
    await server._dispatch(spec.tool, dict(spec.args))
    assert rec.calls, f"{spec.tool}: backend never called"
    args, kwargs = rec.calls[0]
    _positional_checks(spec, args, kwargs)
    expected = {_kw_name(spec.tool, k): v for k, v in _optional_with_defaults(spec.tool).items()}
    got = {k: kwargs.get(k, "<missing>") for k in expected}
    for prop, module_name in _BACKEND_RESOLVES.get(spec.tool, {}).items():
        import importlib

        resolve = importlib.import_module(f"plant_genomics_mcp.{module_name}")._resolve_limit
        assert got[prop] is None, f"{spec.tool}: expected the arm to forward {prop}=None"
        assert resolve(None) == expected[prop], (
            f"{spec.tool}: schema says {prop} defaults to {expected[prop]}, "
            f"but {module_name}._resolve_limit(None) gives {resolve(None)}"
        )
        got[prop] = expected[prop]
    assert got == expected, f"{spec.tool}: schema defaults {expected} vs backend saw {got}"


@pytest.mark.parametrize("spec", _SPECS, ids=lambda s: s.tool)
@pytest.mark.asyncio
async def test_supplied_optional_args_reach_the_backend_unchanged(
    spec: Spec, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _make_recorder(_Env() if spec.synth else {"stub": True}, sync=spec.sync)
    monkeypatch.setattr(spec.module, spec.attr, rec)
    props = _schema(spec.tool)["properties"]
    supplied = {
        k: _sentinel_for(props[k], d) for k, d in _optional_with_defaults(spec.tool).items()
    }
    await server._dispatch(spec.tool, {**spec.args, **supplied})
    assert rec.calls, f"{spec.tool}: backend never called"
    args, kwargs = rec.calls[0]
    _positional_checks(spec, args, kwargs)
    expected = {_kw_name(spec.tool, k): v for k, v in supplied.items()}
    got = {k: kwargs.get(k, "<missing>") for k in expected}
    assert got == expected, f"{spec.tool}: supplied {expected} vs backend saw {got}"


@pytest.mark.parametrize("spec", DISPATCH_SPECS, ids=lambda s: s.tool)
@pytest.mark.asyncio
async def test_every_arm_passes_the_client_first(
    spec: Spec, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Also the arms with no optional argument (tair, jaspar, efp, bar batch), which
    the two tests above skip. Survivor: ``fn(client, ...)`` -> ``fn(None, ...)`` /
    ``fn(...)`` in exactly those four arms."""
    rec = _make_recorder(_Env() if spec.synth else {"stub": True}, sync=spec.sync)
    monkeypatch.setattr(spec.module, spec.attr, rec)
    await server._dispatch(spec.tool, dict(spec.args))
    args, kwargs = rec.calls[0]
    _positional_checks(spec, args, kwargs)


@pytest.mark.asyncio
async def test_call_tool_wire_contract_on_error_and_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 2.x boundary builds both halves of the result itself. Survivors:
    ``is_error=True`` -> False / None, ``TextContent(type="text")`` -> None,
    ``text=str(exc)`` -> None, ``content=None``, ``json.dumps(payload)`` ->
    ``json.dumps(None)``. Error first, success as the positive control."""
    import json
    from typing import cast

    from plant_genomics_mcp import ensembl_plants
    from plant_genomics_mcp.errors import NotFoundError
    from tests.test_progress_bridge import _Ctx, _params, _RecordingSession

    ctx = _Ctx(session=_RecordingSession(), meta={})

    async def failing(client: Any, locus: str, **kwargs: Any) -> dict[str, Any]:
        raise NotFoundError(f"no gene {locus}")

    monkeypatch.setattr(ensembl_plants, "lookup_locus", failing)
    err = await server._call_tool(
        cast(Any, ctx), _params("ensembl_plants_lookup_locus", {"locus": "AT9G99999"})
    )
    assert err.is_error is True
    assert len(err.content) == 1 and err.content[0].type == "text"
    assert err.content[0].text == "[NotFoundError] no gene AT9G99999"
    assert err.structured_content is None

    payload = {"locus": "AT1G01010", "symbol": "NAC001"}

    async def ok(client: Any, locus: str, **kwargs: Any) -> dict[str, Any]:
        return payload

    monkeypatch.setattr(ensembl_plants, "lookup_locus", ok)
    res = await server._call_tool(
        cast(Any, ctx), _params("ensembl_plants_lookup_locus", {"locus": "AT1G01010"})
    )
    assert not res.is_error
    assert res.structured_content == payload
    assert len(res.content) == 1 and res.content[0].type == "text"
    assert json.loads(res.content[0].text) == payload
    assert res.content[0].text == json.dumps(payload)
