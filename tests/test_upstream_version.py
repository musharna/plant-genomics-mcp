"""Issue #121: one ``upstream_version`` key on every chain tool.

The ARF dossier read the field out of 248 responses and found it on 3 of 16
tools, while three others carried a release under a key of their own
(``release``, ``atted_release``, ``latest_version``). These tests lock the
contract: every chain tool's output schema declares ``upstream_version``; a
backend whose ANSWERING request or response states a release reports it
there (and keeps its old key); a backend that states nothing reports null
under the same key rather than omitting it.
"""

from __future__ import annotations

import httpx
import pytest
from pydantic import ValidationError
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import (
    alphafold,
    atted,
    gramene,
    jaspar,
    organisms,
    orthodb,
    pdbe,
    server,
)
from plant_genomics_mcp.models import GrameneHomologs, OrthoDbOrthologs

# The 13 dossier tools whose responses were null on every call (issue #121),
# plus the three that carried it under another key. gene_report and the batch
# forms inherit the key from the rows they wrap.
CHAIN_TOOLS = [
    "alphafold_structure",
    "aragwas_associations",
    "atted_coexpression",
    "ensembl_plants_lookup_locus",
    "experimental_structures",
    "gramene_homologs",
    "kegg_pathways",
    "locus_go_annotations",
    "locus_literature",
    "orthodb_orthologs",
    "panther_family",
    "string_interactions",
    "tf_binding_motifs",
]


def test_every_chain_tool_declares_upstream_version_in_its_output_schema() -> None:
    by_name = {t.name: t for t in server.TOOLS}
    missing = [
        name
        for name in CHAIN_TOOLS
        if "upstream_version" not in ((by_name[name].output_schema or {}).get("properties") or {})
    ]
    assert not missing, f"tools whose output schema omits upstream_version: {missing}"
    # Positive control: the two tools that always carried it still declare it.
    for name in ("interpro_domains", "resolve_locus_to_uniprot"):
        assert "upstream_version" in by_name[name].output_schema["properties"]


@pytest.fixture(autouse=True)
def _clear_caches():
    gramene._CACHE.clear()
    atted._CACHE.clear()
    yield
    gramene._CACHE.clear()
    atted._CACHE.clear()


_GRAMENE_RECORD = [
    {
        "_id": "AT1G01010",
        "homology": {
            "gene_tree": {"id": "EPlGT1"},
            "homologous_genes": {"ortholog_one2many": ["Os01g0100100"]},
        },
    }
]


@pytest.mark.asyncio
async def test_gramene_reports_the_release_pinned_in_the_request(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://data.gramene.org/v69/genes?idList=AT1G01010&fl=homology",
        json=_GRAMENE_RECORD,
    )
    async with httpx.AsyncClient() as client:
        r = await gramene.lookup_homologs(client, "AT1G01010")
    assert r["upstream_version"] == gramene.GRAMENE_RELEASE == "v69"
    assert r["release"] == "v69"  # the old key stays for existing callers
    GrameneHomologs.model_validate(r)


@pytest.mark.asyncio
async def test_gramene_filtered_path_validates_against_its_output_schema(
    httpx_mock: HTTPXMock,
) -> None:
    """#125 added target_organism/total_all_organisms to the result; the
    extra=forbid output model must accept them or the structured result fails
    validation over the wire."""
    httpx_mock.add_response(
        url="https://data.gramene.org/v69/genes?idList=AT1G01010&fl=homology",
        json=_GRAMENE_RECORD,
    )
    httpx_mock.add_response(
        url="https://data.gramene.org/v69/genes?idList=Os01g0100100&fl=_id%2Cxrefs%2Csystem_name&rows=1",
        json=[{"_id": "Os01g0100100", "system_name": "oryza_sativa", "xrefs": []}],
    )
    async with httpx.AsyncClient() as client:
        r = await gramene.lookup_homologs(client, "AT1G01010", target_organism="rice")
    assert r["target_organism"] == "oryza_sativa" and r["total_all_organisms"] == 1
    assert r["upstream_version"] == "v69"
    GrameneHomologs.model_validate(r)
    # Positive control: an unknown key is still rejected by the same model.
    with pytest.raises(ValidationError):
        GrameneHomologs.model_validate({**r, "not_a_field": 1})


def test_orthodb_filtered_result_validates_against_its_output_schema() -> None:
    base = orthodb._empty("AT1G01060", "arabidopsis_thaliana")
    assert base["upstream_version"] == orthodb.ORTHODB_RELEASE  # the pinned release answered
    OrthoDbOrthologs.model_validate(base)
    filtered = {
        **base,
        "found": True,
        "target_organism": "oryza_sativa",
        "member_count_all_organisms": 6,
    }
    OrthoDbOrthologs.model_validate(filtered)
    with pytest.raises(ValidationError):
        OrthoDbOrthologs.model_validate({**filtered, "not_a_field": 1})


@pytest.mark.asyncio
async def test_atted_reports_the_release_pinned_in_the_request(httpx_mock: HTTPXMock) -> None:
    release = organisms.atted_release_for("arabidopsis_thaliana")
    httpx_mock.add_response(
        url=f"https://atted.jp/api5/?gene=AT1G01010&topN=25&db={release}",
        json={
            "result_set": [
                {"type": "z", "results": [{"gene": 1, "other_id": ["AT1G01020"], "z": 4.2}]}
            ]
        },
    )
    async with httpx.AsyncClient() as client:
        r = await atted.lookup_coexpression(client, "AT1G01010", organism="arabidopsis_thaliana")
    assert r["upstream_version"] == r["atted_release"] == release


def test_alphafold_reports_the_entry_version_as_a_string() -> None:
    hit = alphafold._project("Q9SZ92", {"latestVersion": 6})
    assert hit["upstream_version"] == "6" and hit["latest_version"] == 6
    assert alphafold._project("Q9SZ92", {})["upstream_version"] is None
    assert alphafold._empty("Q9SZ92")["upstream_version"] is None


def test_backends_that_state_no_release_carry_the_key_as_null() -> None:
    """Uniform key, honest value: null, not absent, so one pass reads them all."""
    for empty in (
        pdbe._empty("Q9SZ92"),
        jaspar._empty("AT1G01010", "Q9SZ92", 3702, ["ARF5"]),
    ):
        assert "upstream_version" in empty and empty["upstream_version"] is None
