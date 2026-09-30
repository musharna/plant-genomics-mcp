"""Tests for the AlphaFold DB structure backend.

Two tiers (mirrors the quickgo / plantcyc pattern):
  1. Unit tests with mocked HTTP via pytest-httpx. ``lookup_by_uniprot`` is
     tested against a mocked ``/api/prediction`` endpoint; ``lookup_locus`` is
     tested with ``uniprot.lookup_locus`` monkeypatched, so each test exercises
     only this module's logic.
  2. Live integration tests gated by PLANT_GENOMICS_MCP_LIVE=1.
"""

from __future__ import annotations

import os

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import alphafold, uniprot
from plant_genomics_mcp.errors import NotFoundError, PlantGenomicsError

LIVE = os.environ.get("PLANT_GENOMICS_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set PLANT_GENOMICS_MCP_LIVE=1 to run")

# One real-shaped AlphaFold prediction entry (fields trimmed to what we surface,
# key names verified against the live API 2026-07-20, acc Q9SZ92).
_PREDICTION = [
    {
        "uniprotAccession": "Q9SZ92",
        "modelEntityId": "AF-Q9SZ92-F1",
        "globalMetricValue": 90.25,
        "fractionPlddtVeryLow": 0.017,
        "fractionPlddtLow": 0.04,
        "fractionPlddtConfident": 0.243,
        "fractionPlddtVeryHigh": 0.699,
        "latestVersion": 6,
        "modelCreatedDate": "2025-08-01T00:00:00Z",
        "sequenceStart": 1,
        "sequenceEnd": 346,
        "organismScientificName": "Arabidopsis thaliana",
        "gene": "At4g09760",
        "uniprotDescription": "Probable choline kinase 3",
        "cifUrl": "https://alphafold.ebi.ac.uk/files/AF-Q9SZ92-F1-model_v6.cif",
        "pdbUrl": "https://alphafold.ebi.ac.uk/files/AF-Q9SZ92-F1-model_v6.pdb",
        "paeImageUrl": "https://alphafold.ebi.ac.uk/files/AF-Q9SZ92-F1-predicted_aligned_error_v6.png",
    }
]

_PRED_URL = f"{alphafold.BASE_URL}/api/prediction/Q9SZ92"


def _fake_uniprot(acc: str | None, calls: list[tuple[object, ...]] | None = None):
    """Return a monkeypatch stand-in for uniprot.lookup_locus.

    ``calls`` collects each call's (client, locus, organism).
    """

    async def _lookup(client, locus, organism="arabidopsis"):  # noqa: ANN001
        if calls is not None:
            calls.append((client, locus, organism))
        if acc is None:
            raise NotFoundError(f"no UniProt entry for {locus!r}")
        return {"primaryAccession": acc, "uniProtkbId": "CK3_ARATH"}

    return _lookup


# ---------- mocked unit tests: lookup_by_uniprot ----------


@pytest.mark.asyncio
async def test_lookup_by_uniprot_full(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=_PRED_URL, json=_PREDICTION)
    async with httpx.AsyncClient() as client:
        r = await alphafold.lookup_by_uniprot(client, "Q9SZ92")
    assert r["found"] is True
    assert r["accession"] == "Q9SZ92"
    assert r["model_entity_id"] == "AF-Q9SZ92-F1"
    assert r["mean_plddt"] == 90.25
    assert r["plddt_bands"] == {
        "very_low": 0.017,
        "low": 0.04,
        "confident": 0.243,
        "very_high": 0.699,
    }
    assert r["latest_version"] == 6
    assert r["residue_range"] == {"start": 1, "end": 346}
    assert r["organism"] == "Arabidopsis thaliana"
    assert r["gene"] == "At4g09760"
    assert r["cif_url"].endswith("model_v6.cif")
    assert r["pdb_url"].endswith("model_v6.pdb")
    assert r["pae_image_url"].endswith(".png")


@pytest.mark.asyncio
async def test_lookup_by_uniprot_no_model_is_graceful(httpx_mock: HTTPXMock) -> None:
    """404 = no predicted model for this (valid) accession → found=False."""
    httpx_mock.add_response(url=_PRED_URL, status_code=404, json={})
    async with httpx.AsyncClient() as client:
        r = await alphafold.lookup_by_uniprot(client, "Q9SZ92")
    assert r["found"] is False
    assert r["accession"] == "Q9SZ92"
    assert r["mean_plddt"] is None
    assert r["cif_url"] is None


@pytest.mark.asyncio
async def test_lookup_by_uniprot_empty_array_is_graceful(httpx_mock: HTTPXMock) -> None:
    """A 200 with an empty array (no entries) is also found=False."""
    httpx_mock.add_response(url=_PRED_URL, json=[])
    async with httpx.AsyncClient() as client:
        r = await alphafold.lookup_by_uniprot(client, "Q9SZ92")
    assert r["found"] is False


@pytest.mark.asyncio
async def test_lookup_by_uniprot_malformed_raises(httpx_mock: HTTPXMock) -> None:
    """A 200 whose body is not a JSON list → typed PlantGenomicsError."""
    httpx_mock.add_response(url=_PRED_URL, json={"unexpected": "object"})
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlantGenomicsError, match="unexpected payload"):
            await alphafold.lookup_by_uniprot(client, "Q9SZ92")


# Rows that are not all objects (#96). The list check passed them, the body
# was stored, and every call for the TTL leaked ``AttributeError: 'int' object
# has no attribute 'get'`` from the projection without asking again.
_BAD_ROWS = {
    "only such a row": ([1], "row 0 is int"),
    "beside a real row": ([_PREDICTION[0], "junk"], "row 1 is str"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(_BAD_ROWS))
async def test_a_row_that_is_not_an_object_is_refused_and_not_stored(
    httpx_mock: HTTPXMock, case: str
) -> None:
    """Checked before the store. Positive control, same cache: a readable
    answer is then asked for and served."""
    rows, problem = _BAD_ROWS[case]
    httpx_mock.add_response(url=_PRED_URL, json=rows)
    httpx_mock.add_response(url=_PRED_URL, json=_PREDICTION)
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlantGenomicsError) as err:
            await alphafold.lookup_by_uniprot(client, "Q9SZ92")
        assert str(err.value) == (
            "AlphaFold /api/prediction/Q9SZ92 returned unexpected payload: "
            f"{problem}, not an object"
        )
        r = await alphafold.lookup_by_uniprot(client, "Q9SZ92")
    assert (r["found"], r["model_entity_id"]) == (True, "AF-Q9SZ92-F1")
    assert len(httpx_mock.get_requests()) == 2


@pytest.mark.asyncio
async def test_lookup_by_uniprot_no_residue_range(httpx_mock: HTTPXMock) -> None:
    """An entry without sequenceStart yields residue_range=None (L11)."""
    entry = {k: v for k, v in _PREDICTION[0].items() if k != "sequenceStart"}
    httpx_mock.add_response(url=_PRED_URL, json=[entry])
    async with httpx.AsyncClient() as client:
        r = await alphafold.lookup_by_uniprot(client, "Q9SZ92")
    assert r["found"] is True
    assert r["residue_range"] is None


# ---------- mocked unit tests: lookup_locus (uniprot monkeypatched) ----------


@pytest.mark.asyncio
async def test_lookup_locus_wraps_with_locus(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(uniprot, "lookup_locus", _fake_uniprot("Q9SZ92"))
    httpx_mock.add_response(url=_PRED_URL, json=_PREDICTION)
    async with httpx.AsyncClient() as client:
        r = await alphafold.lookup_locus(client, "AT4G09760", "arabidopsis")
    assert r["locus"] == "AT4G09760"
    assert r["accession"] == "Q9SZ92"
    assert r["found"] is True
    assert r["mean_plddt"] == 90.25


@pytest.mark.asyncio
async def test_lookup_locus_unresolvable_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """A locus with no UniProt entry → NotFoundError propagates (typed)."""
    monkeypatch.setattr(uniprot, "lookup_locus", _fake_uniprot(None))
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError):
            await alphafold.lookup_locus(client, "NOSUCHLOCUS", "arabidopsis")


# ---------- live integration (real-execution check) ----------


@live_only
@pytest.mark.asyncio
async def test_live_arabidopsis_has_structure() -> None:
    """Real AlphaFold call — AT4G09760 (Q9SZ92) has a predicted model."""
    async with httpx.AsyncClient() as client:
        r = await alphafold.lookup_locus(client, "AT4G09760", "arabidopsis")
    assert r["found"] is True
    assert r["model_entity_id"].startswith("AF-")
    assert isinstance(r["mean_plddt"], (int, float))
    assert r["cif_url"].startswith("https://alphafold.ebi.ac.uk/")


# ---------- negative caching (audit 2026-07-22, M2) ----------


@pytest.mark.asyncio
async def test_404_is_cached_so_a_repeat_lookup_stays_off_the_wire(
    httpx_mock: HTTPXMock,
) -> None:
    """One mock, two calls: a second request would fail as unexpected."""
    httpx_mock.add_response(url=_PRED_URL, status_code=404, json={})
    async with httpx.AsyncClient() as client:
        first = await alphafold.lookup_by_uniprot(client, "Q9SZ92")
        second = await alphafold.lookup_by_uniprot(client, "Q9SZ92")
    assert first == second
    assert second["found"] is False
    assert len(httpx_mock.get_requests()) == 1


# ---------- issue #135: the bands say where they divide ----------


@pytest.mark.asyncio
async def test_every_reported_band_carries_its_plddt_range(httpx_mock: HTTPXMock) -> None:
    """Four fractions named very_low..very_high shipped with no cutoffs (#135)."""
    httpx_mock.add_response(url=_PRED_URL, json=_PREDICTION)
    httpx_mock.add_response(url=_PRED_URL, status_code=404, json={})
    async with httpx.AsyncClient() as client:
        found = await alphafold.lookup_by_uniprot(client, "Q9SZ92")
        alphafold._CACHE.clear()
        missing = await alphafold.lookup_by_uniprot(client, "Q9SZ92")

    ranges = found["plddt_band_ranges"]
    assert set(ranges) == set(found["plddt_bands"])  # one range per reported band
    # The ranges tile the 0-100 pLDDT scale in band order, no gap, no overlap.
    ordered = [ranges[b] for b in ("very_low", "low", "confident", "very_high")]
    assert ordered[0][0] == 0 and ordered[-1][1] == 100
    assert all(lo[1] == hi[0] for lo, hi in zip(ordered, ordered[1:], strict=False))
    assert ranges["confident"] == [70, 90]  # EMBL-EBI: "90 > pLDDT > 70"
    # A definition, not data: the no-model answer states it too.
    assert missing["found"] is False and missing["plddt_band_ranges"] == ranges


# ---------- the tool's whole answer (#96 mutation triage) ----------
# The no-model tests above call lookup_by_uniprot, which conftest's output-
# contract check does not wrap (the tool's dispatch target is lookup_locus),
# and they read two or three keys: 20 mutants renaming a key of the no-model
# answer survived, and 8 reading modelCreatedDate / uniprotDescription under
# another name. These go through lookup_locus, so every answer is also checked
# against the tool's schema (extra="forbid"), and compare the answer whole.
# _PREDICTION's values match the live Q9SZ92 entry field for field (2026-09-27).

_RANGES = {"very_low": [0, 50], "low": [50, 70], "confident": [70, 90], "very_high": [90, 100]}

# Written out by hand from _PREDICTION, not produced by the code under test.
_FOUND = {
    "locus": "AT4G09760",
    "accession": "Q9SZ92",
    "found": True,
    "model_entity_id": "AF-Q9SZ92-F1",
    "mean_plddt": 90.25,
    "plddt_bands": {"very_low": 0.017, "low": 0.04, "confident": 0.243, "very_high": 0.699},
    "plddt_band_ranges": _RANGES,
    "latest_version": 6,
    "model_created": "2025-08-01T00:00:00Z",
    "residue_range": {"start": 1, "end": 346},
    "organism": "Arabidopsis thaliana",
    "gene": "At4g09760",
    "description": "Probable choline kinase 3",
    "cif_url": "https://alphafold.ebi.ac.uk/files/AF-Q9SZ92-F1-model_v6.cif",
    "pdb_url": "https://alphafold.ebi.ac.uk/files/AF-Q9SZ92-F1-model_v6.pdb",
    "pae_image_url": "https://alphafold.ebi.ac.uk/files/AF-Q9SZ92-F1-predicted_aligned_error_v6.png",
    "upstream_version": "6",
}

_NO_MODEL = {
    "locus": "AT4G09760",
    "accession": "Q9SZ92",
    "found": False,
    "model_entity_id": None,
    "mean_plddt": None,
    "plddt_bands": None,
    "plddt_band_ranges": _RANGES,
    "latest_version": None,
    "model_created": None,
    "residue_range": None,
    "organism": None,
    "gene": None,
    "description": None,
    "cif_url": None,
    "pdb_url": None,
    "pae_image_url": None,
    "upstream_version": None,
}


def _leaves(value: object) -> list[object]:
    if isinstance(value, dict):
        return [leaf for v in value.values() for leaf in _leaves(v)]
    return [value]


@pytest.mark.asyncio
async def test_the_tool_answers_a_model_whole(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(uniprot, "lookup_locus", _fake_uniprot("Q9SZ92"))
    # method + match_headers: a request of another method or without the JSON
    # Accept header gets no response.
    httpx_mock.add_response(
        url=_PRED_URL,
        method="GET",
        match_headers={"Accept": "application/json"},
        json=_PREDICTION,
    )
    async with httpx.AsyncClient() as client:
        r = await alphafold.lookup_locus(client, "AT4G09760")
    assert r == _FOUND
    (request,) = httpx_mock.get_requests()
    assert request.extensions["timeout"]["read"] == alphafold.DEFAULT_TIMEOUT
    # The fixture can fail: no expected value is null, so a mutant that reads
    # a key the entry lacks (null) cannot match by accident.
    assert None not in _leaves(_FOUND)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [{"status_code": 404, "json": {}}, {"json": []}],  # live Q8WZ42 sends the 404
    ids=["404", "empty-array"],
)
async def test_the_tool_answers_no_model_whole(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch, response: dict[str, object]
) -> None:
    monkeypatch.setattr(uniprot, "lookup_locus", _fake_uniprot("Q9SZ92"))
    httpx_mock.add_response(url=_PRED_URL, **response)  # type: ignore[arg-type]
    async with httpx.AsyncClient() as client:
        r = await alphafold.lookup_locus(client, "AT4G09760")
    assert r == _NO_MODEL
    # Positive control: the same answer from the cache, still whole.
    async with httpx.AsyncClient() as client:
        assert await alphafold.lookup_locus(client, "AT4G09760") == _NO_MODEL
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.asyncio
async def test_each_accession_is_cached_under_its_own_key(httpx_mock: HTTPXMock) -> None:
    """One response each, not reusable: a refetch would find no response, and
    a shared key would answer one accession with the other's model."""
    other = f"{alphafold.BASE_URL}/api/prediction/Q8WZ42"
    httpx_mock.add_response(url=_PRED_URL, json=_PREDICTION)
    httpx_mock.add_response(url=other, status_code=404, json={})
    async with httpx.AsyncClient() as client:
        answers = [
            await alphafold.lookup_by_uniprot(client, acc)
            for acc in ("Q9SZ92", "Q8WZ42", "Q9SZ92", "Q8WZ42")
        ]
    assert [(a["accession"], a["found"]) for a in answers] == [
        ("Q9SZ92", True),
        ("Q8WZ42", False),
        ("Q9SZ92", True),
        ("Q8WZ42", False),
    ]
    assert answers[0] == answers[2] and answers[1] == answers[3]
    assert len(httpx_mock.get_requests()) == 2


@pytest.mark.asyncio
async def test_the_locus_and_organism_reach_uniprot_as_given(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(uniprot, "lookup_locus", _fake_uniprot("Q9SZ92", calls))
    httpx_mock.add_response(url=_PRED_URL, json=_PREDICTION)
    async with httpx.AsyncClient() as client:
        r = await alphafold.lookup_locus(client, "Os01g0100100", organism="oryza_sativa")
        assert calls == [(client, "Os01g0100100", "oryza_sativa")]
    assert r["locus"] == "Os01g0100100" and r["found"] is True


def test_version_str() -> None:
    assert alphafold._version_str(6) == "6"
    assert alphafold._version_str("") is None
    assert alphafold._version_str(None) is None


@live_only
@pytest.mark.asyncio
async def test_live_every_field_of_a_model_is_filled() -> None:
    """_PREDICTION is Q9SZ92 as AlphaFold DB sent it on one day; this is what
    notices a renamed field later (projected as null)."""
    async with httpx.AsyncClient() as client:
        r = await alphafold.lookup_locus(client, "AT4G09760", "arabidopsis")
    assert r["found"] is True
    assert None not in _leaves(r), r


@live_only
@pytest.mark.asyncio
async def test_live_an_accession_without_a_model_is_found_false() -> None:
    """Human titin (Q8WZ42) has no AlphaFold DB model: a 404 (live 2026-09-27)."""
    async with httpx.AsyncClient() as client:
        r = await alphafold.lookup_by_uniprot(client, "Q8WZ42")
    assert (r["accession"], r["found"], r["model_entity_id"]) == ("Q8WZ42", False, None)
