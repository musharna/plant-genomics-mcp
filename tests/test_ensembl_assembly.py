"""ensembl_plants_assembly: an organism -> its Ensembl assembly and seq-regions.

The family walk learned an assembly's region names and lengths only from
`/overlap/region` refusals (gap row `no-assembly-metadata`): region '6' answers
400 "No slice found for location 6:1-4000000", and a start past the end answers
400 "Cannot request a slice whose start (32000001) is greater than 30427671
for 1." Ensembl REST lists both at `/info/assembly/<species>` (live,
2026-09-26: 200 for all 12 organisms; Arabidopsis 7 regions, chromosome 1
30,427,671 bases). The fixtures below are that response's shape, cut short.
"""

from __future__ import annotations

import os
import re

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import ensembl_plants
from plant_genomics_mcp.errors import PlantGenomicsError


def _assembly_url(species: str) -> re.Pattern[str]:
    return re.compile(rf"^https://rest\.ensembl\.org/info/assembly/{species}(\?.*)?$")


def _answer(regions: list[tuple[str, int, str]], karyotype: list[str], **extra) -> dict:
    return {
        "assembly_name": "IRGSP-1.0",
        "assembly_accession": "GCA_001433935.1",
        "assembly_date": "2015-10",
        "karyotype": karyotype,
        "top_level_region": [
            {"name": name, "length": length, "coord_system": coord}
            for name, length, coord in regions
        ],
        "golden_path": 373245519,
        **extra,
    }


# Rice's shape: Ensembl lists the regions in no useful order and the karyotype
# as 1, Mt, Pt, 2, ...; the scaffolds are not in the karyotype.
RICE = [
    ("Syng_TIGR_010", 30000, "scaffold"),
    ("2", 35937250, "chromosome"),
    ("Pt", 134525, "chromosome"),
    ("1", 43270923, "chromosome"),
    ("Syng_TIGR_001", 90000, "scaffold"),
    ("Mt", 490520, "chromosome"),
    ("Syng_TIGR_002", 90000, "scaffold"),
]
RICE_KARYOTYPE = ["1", "Mt", "Pt", "2"]


@pytest.fixture(autouse=True)
def _clear_cache():
    ensembl_plants._CACHE.clear()
    yield
    ensembl_plants._CACHE.clear()


@pytest.mark.asyncio
async def test_karyotype_first_then_the_rest_longest_first(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=_assembly_url("oryza_sativa"), json=_answer(RICE, RICE_KARYOTYPE))
    async with httpx.AsyncClient() as client:
        out = await ensembl_plants.assembly(client, "rice")

    (request,) = httpx_mock.get_requests()
    assert request.url.path == "/info/assembly/oryza_sativa"

    assert out["organism"] == "oryza_sativa"
    assert (out["assembly_name"], out["assembly_accession"], out["assembly_date"]) == (
        "IRGSP-1.0",
        "GCA_001433935.1",
        "2015-10",
    )
    assert out["karyotype"] == RICE_KARYOTYPE
    assert (out["total"], out["returned"], out["truncated"]) == (7, 7, False)
    # Karyotype order as Ensembl gives it; then by length, ties by name.
    assert [r["name"] for r in out["regions"]] == [
        "1",
        "Mt",
        "Pt",
        "2",
        "Syng_TIGR_001",
        "Syng_TIGR_002",
        "Syng_TIGR_010",
    ]
    assert out["regions"][0] == {
        "name": "1",
        "length": 43270923,
        "coord_system": "chromosome",
        "in_karyotype": True,
    }
    assert out["regions"][-1]["in_karyotype"] is False
    assert out["upstream_version"] is None


@pytest.mark.asyncio
async def test_limit_truncates_after_counting(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=_assembly_url("oryza_sativa"), json=_answer(RICE, RICE_KARYOTYPE))
    async with httpx.AsyncClient() as client:
        out = await ensembl_plants.assembly(client, "oryza_sativa", limit=5)
        with pytest.raises(ValueError, match="limit"):
            await ensembl_plants.assembly(client, "oryza_sativa", limit=0)
        with pytest.raises(ValueError, match="limit"):
            await ensembl_plants.assembly(client, "oryza_sativa", limit=2001)

    assert (out["total"], out["returned"], out["truncated"]) == (7, 5, True)
    assert [r["name"] for r in out["regions"]] == ["1", "Mt", "Pt", "2", "Syng_TIGR_001"]


@pytest.mark.parametrize(
    ("regions", "karyotype", "match"),
    [
        pytest.param(
            RICE, ["1", "Mt", "Pt", "2", "3"], "absent from the top-level", id="karyotype"
        ),
        pytest.param([*RICE, ("2", 5, "chromosome")], RICE_KARYOTYPE, "repeated", id="dupes"),
        pytest.param([*RICE, ("3", 0, "chromosome")], RICE_KARYOTYPE, "unusable", id="length"),
    ],
)
@pytest.mark.asyncio
async def test_an_inconsistent_answer_is_refused(
    httpx_mock: HTTPXMock, regions: list, karyotype: list, match: str
) -> None:
    """A karyotype naming a region the list lacks, a repeated name, or a
    zero length would each hand a region walk a wrong map. The well-formed
    answer for another organism beside it is the positive control."""
    httpx_mock.add_response(url=_assembly_url("oryza_sativa"), json=_answer(regions, karyotype))
    httpx_mock.add_response(
        url=_assembly_url("arabidopsis_thaliana"),
        json=_answer([("1", 30427671, "chromosome")], ["1"]),
    )
    async with httpx.AsyncClient() as client:
        good = await ensembl_plants.assembly(client)
        with pytest.raises(PlantGenomicsError, match=match):
            await ensembl_plants.assembly(client, "oryza_sativa")
    assert [r["name"] for r in good["regions"]] == ["1"]


@pytest.mark.asyncio
async def test_a_region_without_length_is_refused(httpx_mock: HTTPXMock) -> None:
    answer = _answer(RICE, RICE_KARYOTYPE)
    answer["top_level_region"].append({"name": "3", "coord_system": "chromosome"})
    httpx_mock.add_response(url=_assembly_url("oryza_sativa"), json=answer)
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlantGenomicsError, match="without name or length"):
            await ensembl_plants.assembly(client, "oryza_sativa")


TOMATO = "solanum_lycopersicum_gca000188115v5cm"


@pytest.mark.asyncio
async def test_tomato_regions_keep_ensembl_names(httpx_mock: HTTPXMock) -> None:
    """SL4.0 goes to Ensembl under the registry's tomato slug, and every region
    is a `primary_assembly` region, chromosomes named by accession (live,
    2026-09-26: 152 regions, karyotype CM001064.4 ...). The karyotype, not the
    coordinate-system label, marks the chromosomes."""
    httpx_mock.add_response(
        url=_assembly_url(TOMATO),
        json=_answer(
            [
                ("AEKE04000064.1", 20000, "primary_assembly"),
                ("CM001064.4", 90863682, "primary_assembly"),
            ],
            ["CM001064.4"],
            assembly_name="SL4.0",
            assembly_date=None,
        ),
    )
    async with httpx.AsyncClient() as client:
        out = await ensembl_plants.assembly(client, "solanum_lycopersicum")
    assert out["assembly_date"] is None
    assert [(r["name"], r["in_karyotype"]) for r in out["regions"]] == [
        ("CM001064.4", True),
        ("AEKE04000064.1", False),
    ]
    assert {r["coord_system"] for r in out["regions"]} == {"primary_assembly"}


@pytest.mark.asyncio
async def test_a_call_without_organism_is_refused_before_ensembl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The organism is this tool's identifier, so it has no default: a caller
    who forgets it is refused, not handed Arabidopsis's assembly. The call
    that names one, through the same handler, is the positive control."""
    from tests.test_tool_argument_validation import _call, _text

    calls: list[tuple[object, dict]] = []

    async def recorder(client: object, organism: object, **kwargs: object) -> dict:
        calls.append((organism, kwargs))
        return {"stub": True}

    monkeypatch.setattr(ensembl_plants, "assembly", recorder)
    refused = await _call("ensembl_plants_assembly", {})
    assert refused.is_error is True
    assert _text(refused).startswith("[InvalidArguments] "), _text(refused)
    assert "organism" in _text(refused)
    assert calls == []

    answered = await _call("ensembl_plants_assembly", {"organism": "rice"})
    assert answered.is_error is not True, _text(answered)
    assert calls == [("rice", {"limit": 100})]


LIVE = pytest.mark.skipif(
    os.environ.get("PLANT_GENOMICS_MCP_LIVE") != "1",
    reason="set PLANT_GENOMICS_MCP_LIVE=1 to hit rest.ensembl.org",
)


@LIVE
@pytest.mark.asyncio
async def test_live_lengths_and_names_are_the_ones_region_query_enforces() -> None:
    """The gap row's refusals, now predicted: region_query serves a start at a
    listed length and refuses one base past it, and refuses a name the list
    lacks (Arabidopsis '6'). Each refusal sits beside the answer it bounds."""
    async with httpx.AsyncClient(timeout=60) as client:
        out = await ensembl_plants.assembly(client)
        lengths = {r["name"]: r["length"] for r in out["regions"]}
        assert out["assembly_name"] == "TAIR10"
        assert list(lengths) == ["1", "2", "3", "4", "5", "Mt", "Pt"]
        assert lengths["1"] == 30427671
        for name in ("1", "Mt"):
            end = lengths[name]
            served = await ensembl_plants.region_query(client, name, end, end)
            assert served["region"] == f"{name}:{end}-{end}"
            with pytest.raises(PlantGenomicsError, match="greater than"):
                await ensembl_plants.region_query(client, name, end + 1, end + 1)
        assert "6" not in lengths
        with pytest.raises(PlantGenomicsError, match="No slice found"):
            await ensembl_plants.region_query(client, "6", 1, 1000)


@LIVE
@pytest.mark.asyncio
async def test_live_soybean_is_truncated_with_its_chromosomes_first() -> None:
    async with httpx.AsyncClient(timeout=60) as client:
        out = await ensembl_plants.assembly(client, "glycine_max")
    assert out["total"] > 1100, out["total"]
    assert (out["returned"], out["truncated"]) == (100, True)
    assert len(out["karyotype"]) == 20
    assert [r["name"] for r in out["regions"][:20]] == out["karyotype"]
    assert all(r["in_karyotype"] for r in out["regions"][:20])
    rest = [r["length"] for r in out["regions"][20:]]
    assert rest == sorted(rest, reverse=True) and not any(
        r["in_karyotype"] for r in out["regions"][20:]
    )


@LIVE
@pytest.mark.asyncio
async def test_live_tomato_chromosome_names_are_accepted_by_region_query() -> None:
    async with httpx.AsyncClient(timeout=60) as client:
        out = await ensembl_plants.assembly(client, "solanum_lycopersicum", limit=2000)
        assert not out["truncated"]
        # Every region is `primary_assembly`; only the karyotype marks the 12
        # chromosomes, and they lead the list whatever their lengths.
        assert len(out["karyotype"]) == 12
        assert [r["name"] for r in out["regions"][:12]] == out["karyotype"]
        assert all(r["in_karyotype"] for r in out["regions"][:12])
        first = out["regions"][0]
        assert first["name"].startswith("CM"), first
        genes = await ensembl_plants.region_query(
            client, first["name"], 1, 100000, organism="solanum_lycopersicum"
        )
    assert genes["count"] > 0, genes
