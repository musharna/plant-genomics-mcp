"""Every tool answer the suite produces is checked against the tool's schema.

The server sends each answer as ``structuredContent`` and publishes an
``outputSchema`` per tool, which a client validates against. The unit tests
call the backend functions directly, so until this module nothing in the
suite compared an answer to that schema: the nightly mutation run found 377
mutants that rename an output key (``"organism"`` -> ``"ORGANISM"``) and
survive, because the renamed key is just an extra key to a test that reads
three others.

`install` wraps every function `server._dispatch` routes a tool to. Each
value such a function returns, anywhere in the suite, is checked two ways:

- it validates against the tool's published ``outputSchema`` (what a
  client enforces), and
- every property the schema declares is present, recursively. A model with
  ``extra="allow"`` accepts a renamed key as an extra one and fills the
  declared field from its default, so validation alone cannot see a rename
  there; a missing declared key can.

A violation is recorded rather than raised, because batch tools catch the
exceptions of the calls they fan out; the autouse fixture in conftest.py
fails the test that produced it. The tool-to-function map is read from
``_dispatch``'s own source, so a new tool is covered without editing this
file, and `validated_calls` lets the session report any tool the suite
never exercised.
"""

from __future__ import annotations

import ast
import functools
import importlib
import inspect
import re
import textwrap
from collections import Counter
from collections.abc import Callable, Iterator
from typing import Any

import jsonschema

from plant_genomics_mcp import server
from plant_genomics_mcp.models import RegionFeature

violations: list[str] = []
validated_calls: Counter[str] = Counter()

_WITH_FILTER = "written only when the call filters by organism"

# Answers built by copying an upstream record: this code never writes these
# keys, so it cannot rename one, and which of them the record carries varies
# (live 2026-09-29, one gene per organism: ten have no description or
# display_name, maize's has no description; every xref row carried all seven
# keys). Presence would test the fixture, not the code.
# test_output_contract checks that the named producers write none of the keys;
# test_live_every_organisms_lookup_and_xrefs_keep_the_contract checks the
# lists against each organism's real record.
PASSTHROUGH: dict[str, tuple[tuple[str, ...], str, tuple[str, ...]]] = {
    "ensembl_plants_lookup_locus": (
        ("ensembl_plants.lookup_locus", "ensembl_plants.project_lookup"),
        "$.",
        (
            "assembly_name",
            "biotype",
            "canonical_transcript",
            "db_type",
            "description",
            "display_name",
            "end",
            "logic_name",
            "object_type",
            "seq_region_name",
            "source",
            "start",
            "strand",
        ),
    ),
    "get_gene_xrefs": (
        ("ensembl_plants.lookup_xrefs",),
        "$.xrefs[].",
        (
            "db_display_name",
            "description",
            "display_id",
            "info_text",
            "info_type",
            "synonyms",
            "version",
        ),
    ),
    # Every feature row is Ensembl's record as sent, so every declared field is
    # copied, and which a row carries varies by organism (live 2026-09-29: no
    # tomato gene in CM001064.4:100000-200000 has external_name). The list the
    # Arabidopsis fixture's gaps gave (three keys) failed the live tomato call.
    "ensembl_region_query": (
        ("ensembl_plants.region_query",),
        "$.features[].",
        tuple(RegionFeature.model_fields),
    ),
}

# Declared keys an answer may leave out, by tool and path ("[]" for any row),
# each with the reason. Everything else a schema declares must be present.
ABSENT_OK: dict[str, dict[str, str]] = {
    **{
        tool: {f"{prefix}{key}": "copied from the upstream record" for key in keys}
        for tool, (_, prefix, keys) in PASSTHROUGH.items()
    },
    "gramene_homologs": {
        "$.homologs[].organism": "written only with with_organism or target_organism",
        "$.target_organism": _WITH_FILTER,
        "$.total_all_organisms": _WITH_FILTER,
    },
    "orthodb_orthologs": {
        "$.target_organism": _WITH_FILTER,
        "$.member_count_all_organisms": _WITH_FILTER,
    },
    "kegg_pathways": {
        "$.entrez_gene_id": "written only when the Ensembl-to-Entrez bridge ran",
    },
    "bar_aiv_interactions": {
        "$.papers": "one of papers/partners per kind (the model says the other is empty)",
        "$.partners": "one of papers/partners per kind (the model says the other is empty)",
    },
}

# Keys a found=false answer may leave out (it echoes the request only).
ABSENT_OK_WHEN_NOT_FOUND: dict[str, frozenset[str]] = {
    "vep_annotate": frozenset(
        {"$.input", "$.seq_region_name", "$.start", "$.end", "$.allele_string"}
    ),
}


def dispatch_targets() -> tuple[dict[str, tuple[str, str]], list[str]]:
    """Map each tool to the (module, function) its `_dispatch` case awaits.

    Returns the map and the tools whose case is not a single
    ``return await module.function(...)``, which cannot be wrapped here.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(server._dispatch)))
    targets: dict[str, tuple[str, str]] = {}
    other: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.match_case):
            continue
        pattern = node.pattern
        if not (
            isinstance(pattern, ast.MatchValue)
            and isinstance(pattern.value, ast.Constant)
            and isinstance(pattern.value.value, str)
        ):
            continue
        tool = pattern.value.value
        body = node.body
        call = (
            body[0].value.value
            if len(body) == 1
            and isinstance(body[0], ast.Return)
            and isinstance(body[0].value, ast.Await)
            else None
        )
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
        ):
            targets[tool] = (call.func.value.id, call.func.attr)
        else:
            other.append(tool)
    return targets, other


def _resolve(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        schema = root["$defs"][name]
    return schema


def _branches(schema: dict[str, Any], root: dict[str, Any]) -> Iterator[dict[str, Any]]:
    schema = _resolve(schema, root)
    alternatives = schema.get("anyOf") or schema.get("oneOf")
    if alternatives:
        for alt in alternatives:
            yield from _branches(alt, root)
    else:
        yield schema


def missing_declared(
    value: Any, schema: dict[str, Any], root: dict[str, Any], path: str
) -> list[str]:
    """Paths of properties the schema declares that `value` does not carry."""
    branches = list(_branches(schema, root))
    if len(branches) > 1:
        # A union: check the answer against the branch it is, not against
        # every alternative. Among the branches it validates against, the one
        # it is missing least from; if it matches none, validation reports it.
        defs = {"$defs": root.get("$defs", {})}
        fits = [
            b
            for b in branches
            if jsonschema.validators.validator_for(b)({**b, **defs}).is_valid(value)
        ]
        results = [missing_declared(value, b, root, path) for b in fits or branches]
        return min(results, key=len)
    out: list[str] = []
    for branch in branches:
        if isinstance(value, dict) and "properties" in branch:
            for key, sub in branch["properties"].items():
                if key not in value:
                    out.append(f"{path}.{key}")
                else:
                    out.extend(missing_declared(value[key], sub, root, f"{path}.{key}"))
        elif isinstance(value, list) and "items" in branch:
            for i, item in enumerate(value):
                out.extend(missing_declared(item, branch["items"], root, f"{path}[{i}]"))
    return out


def keys_written(fn: Callable[..., Any]) -> set[str]:
    """String keys `fn` writes: dict-literal keys, `dict(key=...)` keywords
    and `x["key"] = ...` targets.

    A key computed at run time (`x[name] = ...`, a spread of a built dict)
    cannot be seen here; a producer that writes one is not checkable by this
    scan and does not belong in PASSTHROUGH.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            out |= {
                k.value
                for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "dict"
        ):
            out |= {kw.arg for kw in node.keywords if kw.arg is not None}
        elif (
            isinstance(node, ast.Subscript)
            and isinstance(node.ctx, ast.Store)
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            out.add(node.slice.value)
    return out


def check(tool: str, payload: Any, schema: dict[str, Any]) -> list[str]:
    """Every way `payload` breaks the contract `schema` publishes for `tool`."""
    problems: list[str] = []
    validator = jsonschema.validators.validator_for(schema)(schema)
    for err in validator.iter_errors(payload):
        where = "/".join(str(p) for p in err.absolute_path) or "(root)"
        problems.append(f"{tool}: {where}: {err.message[:200]}")
    ok = set(ABSENT_OK.get(tool, {}))
    if isinstance(payload, dict) and payload.get("found") is False:
        ok |= ABSENT_OK_WHEN_NOT_FOUND.get(tool, frozenset())
    for miss in missing_declared(payload, schema, schema, "$"):
        if re.sub(r"\[\d+\]", "[]", miss) not in ok:
            problems.append(f"{tool}: declared key absent: {miss}")
    return problems


def _wrap(fn: Callable[..., Any], tool: str, schema: dict[str, Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        payload = await fn(*args, **kwargs)
        violations.extend(check(tool, payload, schema))
        validated_calls[tool] += 1
        return payload

    wrapper.__contract_wrapped__ = True  # type: ignore[attr-defined]
    return wrapper


def install(setattr_: Callable[[Any, str, Any], None]) -> None:
    """Wrap every dispatch target, using `setattr_` (monkeypatch.setattr)."""
    schemas = {t.name: t.output_schema for t in server.TOOLS}
    targets, _ = dispatch_targets()
    by_function: dict[tuple[str, str], list[str]] = {}
    for tool, target in targets.items():
        by_function.setdefault(target, []).append(tool)
    for (mod_name, fn_name), tools in by_function.items():
        module = importlib.import_module(f"plant_genomics_mcp.{mod_name}")
        fn = getattr(module, fn_name)
        if getattr(fn, "__contract_wrapped__", False):
            continue
        # One function serving two tools would be checked against one schema
        # only; there is none today, and the test for this module says so.
        tool = tools[0]
        schema = schemas.get(tool)
        if schema is None:
            continue
        setattr_(module, fn_name, _wrap(fn, tool, schema))
