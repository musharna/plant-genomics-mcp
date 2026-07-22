"""Tests for the JASPAR TF-binding-motif backend.

Two tiers (mirrors the pdbe / interpro pattern):
  1. Unit tests with mocked HTTP via pytest-httpx; ``uniprot.lookup_locus`` is
     monkeypatched for the locus paths.
  2. Live integration tests gated by PLANT_GENOMICS_MCP_LIVE=1 — including the
     two upstream behaviours this backend is built around (fuzzy name search
     returning a different gene, and the 500-on-unknown-base-id defect).
"""

from __future__ import annotations

import os

import httpx
import pytest
from pytest_httpx import HTTPXMock

from plant_genomics_mcp import jaspar, uniprot
from plant_genomics_mcp.errors import NotFoundError, PlantGenomicsError

LIVE = os.environ.get("PLANT_GENOMICS_MCP_LIVE") == "1"
live_only = pytest.mark.skipif(not LIVE, reason="set PLANT_GENOMICS_MCP_LIVE=1 to run")

# Real-shaped matrix detail (fields verified live 2026-07-21 against MA0570.1).
_DETAIL = {
    "matrix_id": "MA0570.1",
    "name": "ABF1",
    "collection": "CORE",
    "base_id": "MA0570",
    "version": 1,
    "class": ["Basic leucine zipper factors (bZIP)"],
    "family": ["Group A"],
    "type": "SELEX",
    "species": [{"tax_id": 3702, "name": "Arabidopsis thaliana"}],
    "uniprot_ids": ["Q9M7Q5"],
    "pubmed_ids": ["10636868"],
    "sequence_logo": "https://jaspar.elixir.no/static/logos/svg/MA0570.1.svg",
    # ACG — one unambiguous base per column.
    "pfm": {"A": [10, 0, 0], "C": [0, 10, 0], "G": [0, 0, 10], "T": [0, 0, 0]},
}


def _detail_url(matrix_id: str) -> str:
    return f"{jaspar.BASE_URL}{jaspar.API_PREFIX}/matrix/{matrix_id}/"


def _versions_url(base_id: str) -> str:
    return f"{jaspar.BASE_URL}{jaspar.API_PREFIX}/matrix/{base_id}/versions/"


def _search_url(name: str, tax_id: int = 3702, page_size: int | None = None) -> str:
    ps = jaspar.MAX_CANDIDATES if page_size is None else page_size
    return (
        f"{jaspar.BASE_URL}{jaspar.API_PREFIX}/matrix/?search={name}&tax_id={tax_id}&page_size={ps}"
    )


def _fake_uniprot(acc: str | None, genes: list[str] | None = None, taxon: int | None = 3702):
    async def _lookup(client, locus, organism="arabidopsis_thaliana"):  # noqa: ANN001, ARG001
        if acc is None:
            raise NotFoundError(f"no UniProt entry for {locus!r}")
        return {
            "primaryAccession": acc,
            "geneNames": ["ABF1"] if genes is None else genes,
            "taxonId": taxon,
        }

    return _lookup


# ---------- _consensus ----------


@pytest.mark.parametrize(
    ("pfm", "expected"),
    [
        ({"A": [10, 0, 0], "C": [0, 10, 0], "G": [0, 0, 10], "T": [0, 0, 0]}, "ACG"),
        # A dominant but C below the 0.25 floor → unambiguous A.
        ({"A": [8], "C": [2], "G": [0], "T": [0]}, "A"),
        # Two bases tie at 0.5 → IUPAC M.
        ({"A": [5], "C": [5], "G": [0], "T": [0]}, "M"),
        # Three bases clear the floor → IUPAC V.
        ({"A": [4], "C": [4], "G": [4], "T": [0]}, "V"),
        # Flat column → N.
        ({"A": [1], "C": [1], "G": [1], "T": [1]}, "N"),
        # Zero-count column → N rather than a division error.
        ({"A": [0], "C": [0], "G": [0], "T": [0]}, "N"),
    ],
)
def test_consensus_shapes(pfm: dict, expected: str) -> None:
    assert jaspar._consensus(pfm) == expected


@pytest.mark.parametrize(
    "pfm",
    [
        None,
        "not-a-dict",
        {"A": [1], "C": [1], "G": [1]},  # missing T
        {"A": "nope", "C": [1], "G": [1], "T": [1]},  # row not a list
        {"A": ["x"], "C": [1], "G": [1], "T": [1]},  # non-numeric entry
        {"A": [], "C": [], "G": [], "T": []},  # zero width
        {"A": [1, 1], "C": [1], "G": [1], "T": [1]},  # ragged rows
    ],
)
def test_consensus_malformed_returns_none(pfm: object) -> None:
    """A missing consensus beats a wrong one — malformed input yields None."""
    assert jaspar._consensus(pfm) is None


# ---------- _project ----------


def test_project_full() -> None:
    p = jaspar._project(_DETAIL)
    assert p["matrix_id"] == "MA0570.1"
    assert p["name"] == "ABF1"
    assert p["tf_class"] == ["Basic leucine zipper factors (bZIP)"]
    assert p["tf_family"] == ["Group A"]
    assert p["data_type"] == "SELEX"
    assert p["consensus"] == "ACG"
    assert p["length"] == 3
    assert p["uniprot_ids"] == ["Q9M7Q5"]
    assert p["web_url"] == "https://jaspar.elixir.no/matrix/MA0570.1/"


def test_project_sparse_record() -> None:
    """No matrix_id and no PFM → null web_url / consensus / length, not a crash."""
    p = jaspar._project({})
    assert p["web_url"] is None
    assert p["consensus"] is None
    assert p["length"] is None
    assert p["uniprot_ids"] == []
    assert p["pubmed_ids"] == []


# ---------- fetch_matrix / lookup_matrix ----------


@pytest.mark.asyncio
async def test_fetch_matrix_404_returns_none(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=_detail_url("MA9999.9"), status_code=404, text="Not Found")
    async with httpx.AsyncClient() as client:
        assert await jaspar.fetch_matrix(client, "MA9999.9") is None


@pytest.mark.asyncio
async def test_fetch_matrix_malformed_raises(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=_detail_url("MA0570.1"), json=["unexpected", "list"])
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlantGenomicsError, match="unexpected payload"):
            await jaspar.fetch_matrix(client, "MA0570.1")


@pytest.mark.asyncio
async def test_lookup_matrix_versioned(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=_detail_url("MA0570.1"), json=_DETAIL)
    async with httpx.AsyncClient() as client:
        r = await jaspar.lookup_matrix(client, "MA0570.1")
    assert r["matrix_id"] == "MA0570.1"
    assert r["consensus"] == "ACG"
    assert r["pfm"] == _DETAIL["pfm"]
    assert r["species"] == [{"tax_id": 3702, "name": "Arabidopsis thaliana"}]


@pytest.mark.asyncio
async def test_lookup_matrix_is_cached(httpx_mock: HTTPXMock) -> None:
    """A repeat fetch is served from the module cache, not a second HTTP call."""
    httpx_mock.add_response(url=_detail_url("MA0570.1"), json=_DETAIL)
    async with httpx.AsyncClient() as client:
        await jaspar.lookup_matrix(client, "MA0570.1")
        await jaspar.lookup_matrix(client, "MA0570.1")
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.asyncio
async def test_lookup_matrix_invalid_id_raises_before_network() -> None:
    """A path-traversal id is rejected before any HTTP call (no mock registered)."""
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="invalid matrix id"):
            await jaspar.lookup_matrix(client, "../etc/passwd")


@pytest.mark.asyncio
async def test_lookup_matrix_unknown_versioned_id_raises(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=_detail_url("MA9999.9"), status_code=404, text="Not Found")
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="no matrix with id"):
            await jaspar.lookup_matrix(client, "MA9999.9")


# ---------- unversioned id → versions/ resolution (routes around the 500 bug) ----------


@pytest.mark.asyncio
async def test_lookup_matrix_unversioned_resolves_newest(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=_versions_url("MA0570"),
        json={
            "count": 3,
            "results": [
                {"matrix_id": "MA0570.1", "version": 1},
                {"matrix_id": "MA0570.3", "version": 3},
                {"matrix_id": "MA0570.2", "version": 2},
            ],
        },
    )
    httpx_mock.add_response(url=_detail_url("MA0570.3"), json={**_DETAIL, "matrix_id": "MA0570.3"})
    async with httpx.AsyncClient() as client:
        r = await jaspar.lookup_matrix(client, "MA0570")
    assert r["matrix_id"] == "MA0570.3"


@pytest.mark.asyncio
async def test_lookup_matrix_unversioned_unknown_is_not_found(httpx_mock: HTTPXMock) -> None:
    """The upstream 500-on-unknown-base-id is never triggered: versions/ answers 200/count-0.

    Regression guard for the defect this indirection exists for — a typo'd base
    id must surface as NotFoundError, not as UpstreamUnavailableError after
    burning the retry budget.
    """
    httpx_mock.add_response(url=_versions_url("MA9999"), json={"count": 0, "results": []})
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="no matrix with id"):
            await jaspar.lookup_matrix(client, "MA9999")
    # One call only: no retry storm.
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.asyncio
async def test_lookup_matrix_unversioned_malformed_versions_payload(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=_versions_url("MA0570"), json={"results": "not-a-list"})
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="no matrix with id"):
            await jaspar.lookup_matrix(client, "MA0570")


@pytest.mark.asyncio
async def test_lookup_matrix_unversioned_row_without_matrix_id(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=_versions_url("MA0570"), json={"results": [{"version": 1}]})
    async with httpx.AsyncClient() as client:
        with pytest.raises(PlantGenomicsError, match="no matrix_id"):
            await jaspar.lookup_matrix(client, "MA0570")


# ---------- _search_candidates ----------


@pytest.mark.asyncio
async def test_search_candidates_filters_junk(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=_search_url("ABF1"), json={"count": 2, "results": [{"matrix_id": "MA0570.1"}, "junk"]}
    )
    async with httpx.AsyncClient() as client:
        rows = await jaspar._search_candidates(client, "ABF1", 3702)
    assert rows == [{"matrix_id": "MA0570.1"}]


@pytest.mark.asyncio
async def test_search_candidates_non_dict_payload(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=_search_url("ABF1"), json=["unexpected"])
    async with httpx.AsyncClient() as client:
        assert await jaspar._search_candidates(client, "ABF1", 3702) == []


@pytest.mark.asyncio
async def test_search_candidates_results_not_a_list(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=_search_url("ABF1"), json={"results": None})
    async with httpx.AsyncClient() as client:
        assert await jaspar._search_candidates(client, "ABF1", 3702) == []


# ---------- lookup_locus ----------


@pytest.mark.asyncio
async def test_lookup_locus_confirmed_motif(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(uniprot, "lookup_locus", _fake_uniprot("Q9M7Q5"))
    httpx_mock.add_response(url=_search_url("ABF1"), json={"results": [{"matrix_id": "MA0570.1"}]})
    httpx_mock.add_response(url=_detail_url("MA0570.1"), json=_DETAIL)
    async with httpx.AsyncClient() as client:
        r = await jaspar.lookup_locus(client, "AT1G49720")
    assert r["locus"] == "AT1G49720"
    assert r["accession"] == "Q9M7Q5"
    assert r["tax_id"] == 3702
    assert r["gene_names_searched"] == ["ABF1"]
    assert r["found"] is True
    assert r["motif_count"] == 1
    assert r["truncated"] is False
    assert r["motifs"][0]["matrix_id"] == "MA0570.1"
    assert r["name_only_matches"] == []


@pytest.mark.asyncio
async def test_lookup_locus_quarantines_other_gene(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The load-bearing behaviour: a fuzzy name hit for a DIFFERENT gene never
    lands in ``motifs``.

    Mirrors the live case ``?search=CCA1&tax_id=3702`` → MA1187.1 (RVE4).
    """
    monkeypatch.setattr(uniprot, "lookup_locus", _fake_uniprot("P92973", genes=["CCA1"]))
    httpx_mock.add_response(
        url=_search_url("CCA1"),
        json={"results": [{"matrix_id": "MA0972.1"}, {"matrix_id": "MA1187.1"}]},
    )
    httpx_mock.add_response(
        url=_detail_url("MA0972.1"),
        json={**_DETAIL, "matrix_id": "MA0972.1", "name": "CCA1", "uniprot_ids": ["P92973"]},
    )
    httpx_mock.add_response(
        url=_detail_url("MA1187.1"),
        json={**_DETAIL, "matrix_id": "MA1187.1", "name": "RVE4", "uniprot_ids": ["Q6R0G4"]},
    )
    async with httpx.AsyncClient() as client:
        r = await jaspar.lookup_locus(client, "AT2G46830")
    assert [m["matrix_id"] for m in r["motifs"]] == ["MA0972.1"]
    assert r["motif_count"] == 1
    assert r["name_only_matches"] == [
        {"matrix_id": "MA1187.1", "name": "RVE4", "uniprot_ids": ["Q6R0G4"]}
    ]


@pytest.mark.asyncio
async def test_lookup_locus_no_candidates(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-TF gene: search matches nothing → found=False, not an error."""
    monkeypatch.setattr(uniprot, "lookup_locus", _fake_uniprot("Q93V56", genes=["PPA1"]))
    httpx_mock.add_response(url=_search_url("PPA1"), json={"count": 0, "results": []})
    async with httpx.AsyncClient() as client:
        r = await jaspar.lookup_locus(client, "AT1G01050")
    assert r["found"] is False
    assert r["motifs"] == []
    assert r["motif_count"] == 0
    assert r["gene_names_searched"] == ["PPA1"]


@pytest.mark.asyncio
async def test_lookup_locus_no_gene_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """No gene symbol → JASPAR's name index has no key; empty result, no HTTP."""
    monkeypatch.setattr(uniprot, "lookup_locus", _fake_uniprot("A0A123", genes=[]))
    async with httpx.AsyncClient() as client:
        r = await jaspar.lookup_locus(client, "AT1G01010")
    assert r["found"] is False
    assert r["gene_names_searched"] == []


@pytest.mark.asyncio
async def test_lookup_locus_falls_back_to_registry_taxid(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A UniProt record with no taxonId falls back to the organism registry."""
    monkeypatch.setattr(uniprot, "lookup_locus", _fake_uniprot("Q9M7Q5", taxon=None))
    httpx_mock.add_response(url=_search_url("ABF1"), json={"results": []})
    async with httpx.AsyncClient() as client:
        r = await jaspar.lookup_locus(client, "AT1G49720", organism="arabidopsis_thaliana")
    assert r["tax_id"] == 3702


@pytest.mark.asyncio
async def test_lookup_locus_dedupes_across_gene_names(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two symbols hitting the same matrix fetch its detail once."""
    monkeypatch.setattr(uniprot, "lookup_locus", _fake_uniprot("Q9M7Q5", genes=["ABF1", "BZIP35"]))
    httpx_mock.add_response(url=_search_url("ABF1"), json={"results": [{"matrix_id": "MA0570.1"}]})
    httpx_mock.add_response(
        url=_search_url("BZIP35"), json={"results": [{"matrix_id": "MA0570.1"}]}
    )
    httpx_mock.add_response(url=_detail_url("MA0570.1"), json=_DETAIL)
    async with httpx.AsyncClient() as client:
        r = await jaspar.lookup_locus(client, "AT1G49720")
    assert r["motif_count"] == 1
    assert len(httpx_mock.get_requests()) == 3  # 2 searches + 1 detail


@pytest.mark.asyncio
async def test_lookup_locus_skips_vanished_detail(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A candidate whose detail 404s is dropped rather than failing the call."""
    monkeypatch.setattr(uniprot, "lookup_locus", _fake_uniprot("Q9M7Q5"))
    httpx_mock.add_response(url=_search_url("ABF1"), json={"results": [{"matrix_id": "MA0570.9"}]})
    httpx_mock.add_response(url=_detail_url("MA0570.9"), status_code=404, text="Not Found")
    async with httpx.AsyncClient() as client:
        r = await jaspar.lookup_locus(client, "AT1G49720")
    assert r["found"] is False
    assert r["name_only_matches"] == []


@pytest.mark.asyncio
async def test_lookup_locus_truncates(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jaspar, "MAX_MOTIFS", 1)
    monkeypatch.setattr(uniprot, "lookup_locus", _fake_uniprot("Q9M7Q5"))
    httpx_mock.add_response(
        url=_search_url("ABF1"),
        json={"results": [{"matrix_id": "MA0570.1"}, {"matrix_id": "MA0570.2"}]},
    )
    httpx_mock.add_response(url=_detail_url("MA0570.1"), json=_DETAIL)
    httpx_mock.add_response(url=_detail_url("MA0570.2"), json={**_DETAIL, "matrix_id": "MA0570.2"})
    async with httpx.AsyncClient() as client:
        r = await jaspar.lookup_locus(client, "AT1G49720")
    assert r["motif_count"] == 2
    assert r["truncated"] is True
    assert len(r["motifs"]) == 1


@pytest.mark.asyncio
async def test_lookup_locus_bad_locus_raises_before_network() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError, match="invalid locus"):
            await jaspar.lookup_locus(client, "AT1G49720/x")


@pytest.mark.asyncio
async def test_lookup_locus_unresolvable_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(uniprot, "lookup_locus", _fake_uniprot(None))
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError):
            await jaspar.lookup_locus(client, "NOSUCHLOCUS")


# ---------- live integration (real-execution check) ----------


@live_only
@pytest.mark.asyncio
async def test_live_abf1_binds_the_g_box() -> None:
    """AT1G49720 (ABF1) must yield a CACGTG-containing consensus — the G-box/ABRE core."""
    async with httpx.AsyncClient() as client:
        r = await jaspar.lookup_locus(client, "AT1G49720")
    assert r["found"] is True
    assert any("CACGTG" in (m["consensus"] or "") for m in r["motifs"])
    assert all("Q9M7Q5" in m["uniprot_ids"] for m in r["motifs"])


@live_only
@pytest.mark.asyncio
async def test_live_cca1_quarantines_fuzzy_hits() -> None:
    """CCA1's search returns other genes' profiles; only P92973 may reach `motifs`."""
    async with httpx.AsyncClient() as client:
        r = await jaspar.lookup_locus(client, "AT2G46830")
    assert all("P92973" in m["uniprot_ids"] for m in r["motifs"])
    assert r["name_only_matches"], "expected JASPAR's fuzzy search to surface other genes"


@live_only
@pytest.mark.asyncio
async def test_live_non_tf_is_graceful() -> None:
    async with httpx.AsyncClient() as client:
        r = await jaspar.lookup_locus(client, "AT1G01050")
    assert r["found"] is False


@live_only
@pytest.mark.asyncio
async def test_live_unknown_base_id_is_not_found_not_outage() -> None:
    """Guards the upstream 500-on-unknown-base-id defect end to end."""
    async with httpx.AsyncClient() as client:
        with pytest.raises(NotFoundError):
            await jaspar.lookup_matrix(client, "MA9999")


# ---------- negative caching + version ordering (audit 2026-07-22, M2 / L2) ----------


@pytest.mark.asyncio
async def test_404_is_cached_so_a_repeat_fetch_stays_off_the_wire(
    httpx_mock: HTTPXMock,
) -> None:
    """One mock, two calls: a second request would fail as unexpected."""
    httpx_mock.add_response(url=_detail_url("MA9999.9"), status_code=404, text="Not Found")
    async with httpx.AsyncClient() as client:
        assert await jaspar.fetch_matrix(client, "MA9999.9") is None
        assert await jaspar.fetch_matrix(client, "MA9999.9") is None
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.asyncio
async def test_unversioned_resolution_orders_versions_numerically(
    httpx_mock: HTTPXMock,
) -> None:
    """Version 10 must beat version 9 even when upstream sends them as strings.

    A lexicographic compare would pick "9" and silently resolve the id to a
    stale matrix — wrong data, no error.
    """
    httpx_mock.add_response(
        url=_versions_url("MA0570"),
        json={
            "count": 2,
            "results": [
                {"matrix_id": "MA0570.10", "version": "10"},
                {"matrix_id": "MA0570.9", "version": "9"},
            ],
        },
    )
    httpx_mock.add_response(
        url=_detail_url("MA0570.10"), json={**_DETAIL, "matrix_id": "MA0570.10"}
    )
    async with httpx.AsyncClient() as client:
        r = await jaspar.lookup_matrix(client, "MA0570")
    assert r["matrix_id"] == "MA0570.10"


def test_version_key_tolerates_unparseable_values() -> None:
    """A malformed row sorts lowest instead of raising and sinking the lookup."""
    assert jaspar._version_key({"version": "not-a-number"}) == 0
    assert jaspar._version_key({}) == 0
    assert jaspar._version_key({"version": 3}) == 3
