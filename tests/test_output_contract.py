"""The output-contract check in tests/_output_contract.py, tested on its own.

The check runs around every test (conftest.py), so a check that passes
everything would leave the whole suite green and prove nothing. Each test
here shows a real tool schema rejecting a broken answer AND accepting the
correct one.
"""

from __future__ import annotations

import inspect
from typing import Any

import jsonschema
import pytest

from plant_genomics_mcp import server
from tests import _output_contract as oc

SCHEMAS = {t.name: t.output_schema for t in server.TOOLS}


def _aragwas() -> dict[str, Any]:
    return {
        "next_cursor": None,
        "total": 1,
        "locus": "AT1G19850",
        "organism": "arabidopsis_thaliana",
        "found": True,
        "association_count": 1,
        "returned": 1,
        "truncated": False,
        "associations": [{"score": 7.2}],
        "upstream_version": None,
    }


def _gramene(row: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "next_cursor": None,
        "returned": 1,
        "locus": "AT1G19850",
        "release": "v69",
        "total": 1,
        "truncated": False,
        "homologs": [
            row
            if row is not None
            else {
                "target_locus": "Os01g0100100",
                "type": "ortholog_one2one",
                "gene_tree_id": "GT1",
                "organism": "oryza_sativa",
            }
        ],
        "target_organism": "oryza_sativa",
        "total_all_organisms": 1,
        "excluded_categories": {},
        "upstream_version": "v69",
    }


def _renamed(d: dict[str, Any], key: str) -> dict[str, Any]:
    return {(k.upper() if k == key else k): v for k, v in d.items()}


def test_the_dispatch_map_accounts_for_every_tool() -> None:
    targets, other = oc.dispatch_targets()
    # Every published tool is either wrapped or named as not wrappable, so a
    # tool cannot drop out of the check unnoticed.
    assert set(targets) | set(other) == set(SCHEMAS)
    assert not set(targets) & set(other)
    assert len(targets) > 40, targets  # the map was really read
    for module_name, fn_name in targets.values():
        module = __import__(f"plant_genomics_mcp.{module_name}", fromlist=[fn_name])
        fn = getattr(module, fn_name)
        # conftest's fixture has wrapped it for this test: the check engaged.
        assert getattr(fn, "__contract_wrapped__", False), (module_name, fn_name)
        assert inspect.iscoroutinefunction(fn), (module_name, fn_name)


def test_a_renamed_key_in_a_strict_model_is_rejected() -> None:
    schema = SCHEMAS["aragwas_associations"]
    assert oc.check("aragwas_associations", _aragwas(), schema) == []

    problems = oc.check("aragwas_associations", _renamed(_aragwas(), "organism"), schema)
    assert any("Additional properties" in p for p in problems), problems
    assert "aragwas_associations: declared key absent: $.organism" in problems


def test_a_renamed_key_in_a_permissive_model_is_caught_only_by_presence() -> None:
    """A row model with extra="allow" accepts a renamed key as an extra one.

    Schema validation alone passes it; the declared-key check is what fails.
    """
    schema = SCHEMAS["gramene_homologs"]
    assert oc.check("gramene_homologs", _gramene(), schema) == []

    row = _renamed(_gramene()["homologs"][0], "type")
    renamed = _gramene(row)
    validator = jsonschema.validators.validator_for(schema)(schema)
    assert list(validator.iter_errors(renamed)) == []  # why presence is needed
    assert oc.check("gramene_homologs", renamed, schema) == [
        "gramene_homologs: declared key absent: $.homologs[0].type"
    ]


def test_an_exempt_absence_is_exempt_only_at_its_own_path() -> None:
    schema = SCHEMAS["gramene_homologs"]
    no_organism = dict(_gramene()["homologs"][0])
    del no_organism["organism"]
    assert oc.check("gramene_homologs", _gramene(no_organism), schema) == []

    no_tree = dict(_gramene()["homologs"][0])
    del no_tree["gene_tree_id"]
    assert oc.check("gramene_homologs", _gramene(no_tree), schema) == [
        "gramene_homologs: declared key absent: $.homologs[0].gene_tree_id"
    ]


@pytest.mark.parametrize(("found", "reported"), [(False, False), (True, True)])
def test_the_not_found_exemption_needs_found_false(found: bool, reported: bool) -> None:
    payload = {
        "organism": "arabidopsis_thaliana",
        "region": "1:100-100:1",
        "allele": "C",
        "found": found,
        "most_severe_consequence": None,
        "assembly_name": None,
        "seq_region_name": None,
        "start": None,
        "end": None,
        "allele_string": None,
        "transcript_consequences": [],
    }
    problems = oc.check("vep_annotate", payload, SCHEMAS["vep_annotate"])
    assert (problems == ["vep_annotate: declared key absent: $.input"]) is reported
    assert (problems == []) is not reported


@pytest.mark.asyncio
async def test_the_wrapper_records_each_answer_and_its_problems() -> None:
    async def answers(payload: dict[str, Any]) -> dict[str, Any]:
        return payload

    wrapped = oc._wrap(answers, "aragwas_associations", SCHEMAS["aragwas_associations"])
    before = oc.validated_calls["aragwas_associations"]
    start = len(oc.violations)

    assert await wrapped(_aragwas()) == _aragwas()
    assert oc.violations[start:] == []
    await wrapped(_renamed(_aragwas(), "organism"))
    recorded = oc.violations[start:]
    # Taken back out, or conftest's fixture would fail this test for them.
    del oc.violations[start:]

    assert "aragwas_associations: declared key absent: $.organism" in recorded
    assert oc.validated_calls["aragwas_associations"] == before + 2


# project_lookup rewrites canonical_transcript in place when the record
# carries it (strips Ensembl's trailing "."); it never creates the key.
_REWRITTEN_IN_PLACE = {("ensembl_plants.project_lookup", "canonical_transcript")}


def test_passthrough_keys_are_never_written_by_the_code_that_answers() -> None:
    """An exempt passthrough key is exempt because this code cannot rename it.

    That holds only while the functions that build the answer never write
    the key themselves; the day one does, a rename there becomes possible
    and the exemption has to go.
    """

    def build(raw: dict[str, Any]) -> dict[str, Any]:
        out = {"biotype": raw.get("x")}
        out["strand"] = 1
        return out

    assert oc.keys_written(build) == {"biotype", "strand"}  # the scanner sees writes

    for tool, (producers, _, keys) in oc.PASSTHROUGH.items():
        for producer in producers:
            module_name, fn_name = producer.split(".")
            module = __import__(f"plant_genomics_mcp.{module_name}", fromlist=[fn_name])
            fn = getattr(module, fn_name)
            fn = getattr(fn, "__wrapped__", fn)  # conftest's wrapper, if any
            written = {
                k
                for k in oc.keys_written(fn) & set(keys)
                if (producer, k) not in _REWRITTEN_IN_PLACE
            }
            assert written == set(), (tool, producer, written)


def test_a_union_answer_is_checked_against_the_branch_it_is() -> None:
    """A correct answer of one shape must not be held to another shape's keys."""
    schema = {
        "$defs": {
            "A": {
                "type": "object",
                "properties": {"kind": {"const": "a"}, "x": {"type": "integer"}},
                "required": ["kind"],
            },
            "B": {
                "type": "object",
                "properties": {"kind": {"const": "b"}, "y": {"type": "integer"}},
                "required": ["kind"],
            },
        },
        "anyOf": [{"$ref": "#/$defs/A"}, {"$ref": "#/$defs/B"}],
    }
    assert oc.missing_declared({"kind": "a", "x": 1}, schema, schema, "$") == []
    assert oc.missing_declared({"kind": "b", "y": 2}, schema, schema, "$") == []
    assert oc.missing_declared({"kind": "a"}, schema, schema, "$") == ["$.x"]
