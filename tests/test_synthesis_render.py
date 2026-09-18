"""Golden renders for the gene_report Markdown dossier.

The renderer is pure (rows in, Markdown out), so the honest oracle is the whole
document: exact string equality over a fixture that exercises every branch. A
substring check ("NAC001" in md) cannot tell a truncated section from a full
one, a swapped separator from the right one, or a dropped line from a kept
one -- the nightly mutation run left 261 surviving mutants in this function
under substring checks alone.

Two documents: every section populated (with more rows than ``top_n`` so each
cap is exercised, a duplicate xref so the dedup is exercised, and a minus-strand
locus), and a sparse one where each section takes a different fallback path
(error row, skipped row, ok row with empty data, ok row with null fields).
"""

from __future__ import annotations

import textwrap

from plant_genomics_mcp.models import StepRow
from plant_genomics_mcp.synthesis import _render_gene_report_md


def _ok(tool: str, result: dict) -> StepRow:
    return StepRow(step=1, tool=tool, status="ok", elapsed_s=0.1, result=result)


def _err(tool: str, msg: str) -> StepRow:
    return StepRow(step=1, tool=tool, status="error", elapsed_s=0.1, error=msg)


def _skip(tool: str, why: str) -> StepRow:
    return StepRow(step=1, tool=tool, status="skipped", elapsed_s=None, error=why)


def _full_rows() -> dict[str, StepRow]:
    return {
        "annotation": _ok(
            "annotation",
            {
                "biotype": "protein_coding",
                "seq_region_name": "1",
                "start": 3631,
                "end": 5899,
                "strand": -1,
                "assembly_name": "TAIR10",
                "description": "NAC domain containing protein 1",
            },
        ),
        "protein": _ok(
            "protein",
            {
                "primaryAccession": "Q0WV96",
                "recommendedName": "NAC domain-containing protein 1",
                "uniProtkbId": "NAC1_ARATH",
                "entryType": "UniProtKB reviewed (Swiss-Prot)",
                "sequenceLength": 429,
                "web_url": "https://www.uniprot.org/uniprotkb/Q0WV96",
            },
        ),
        "domains": _ok(
            "domains",
            {
                "domains": [
                    {
                        "name": "NAM",
                        "accession": "PF02365",
                        "type": "domain",
                        "source_database": "pfam",
                        "locations": [{"start": 8, "end": 140}],
                    },
                    {"accession": "IPR003441", "type": "family", "locations": []},
                    {"name": "THIRD", "locations": [{"start": 1, "end": 2}]},  # past top_n
                ]
            },
        ),
        "go_annotations": _ok(
            "go_annotations",
            {
                "annotations": [
                    {
                        "goId": "GO:0003700",
                        "goName": "DNA-binding transcription factor activity",
                        "goAspect": "molecular_function",
                        "goEvidence": "IEA",
                    },
                    {
                        "goId": "GO:0006355",
                        "goName": "regulation of DNA-templated transcription",
                        "goAspect": "biological_process",
                    },
                    {
                        "goId": "GO:0005634",
                        "goName": "nucleus",
                        "goAspect": "cellular_component",
                        "goEvidence": "IDA",
                    },
                    {
                        "goId": "GO:0000001",
                        "goName": "extra bp",
                        "goAspect": "biological_process",
                        "goEvidence": "IEA",
                    },
                    {"goId": "GO:0000002", "goName": "third bp", "goAspect": "biological_process"},
                    {"goId": "GO:0000009", "goName": "weird", "goAspect": "other"},
                ]
            },
        ),
        "pathways": _ok(
            "pathways",
            {
                "pathways": [
                    {
                        "id": "ath00010",
                        "name": "Glycolysis / Gluconeogenesis",
                        "pathway_class": "Metabolism; Carbohydrate metabolism",
                    },
                    {"id": "ath00020", "name": "TCA"},
                    {"id": "ath00030", "name": "third"},
                ]
            },
        ),
        "interactions": _ok(
            "interactions",
            {
                "partners": [
                    {"preferred_name": "NAC3", "string_id": "3702.AT3G15500.1", "score": 0.85},
                    {"string_id": "3702.AT1G00001.1", "score": 0.5},
                    {"preferred_name": "THIRD", "score": 0.1},
                ]
            },
        ),
        "xrefs": _ok(
            "xrefs",
            {
                "xrefs": [
                    {"db_display_name": "UniProtKB Gene Name", "primary_id": "Q0WV96"},
                    {"dbname": "TAIR_LOCUS", "primary_id": "AT1G01010"},
                    {"db_display_name": "UniProtKB Gene Name", "primary_id": "Q0WV96"},  # dup
                    {"dbname": "EntrezGene", "display_id": "839580"},  # past top_n
                ]
            },
        ),
        "literature": _ok(
            "literature",
            {
                "hitCount": 40,
                "hits": [
                    {
                        "title": "Spaceflight transcriptome patterns in Arabidopsis.",
                        "authorString": "Seo D, Paul AL, Ferl RJ.",
                        "pmid": "41152268",
                        "doi": "10.1038/x",
                    },
                    {"title": "Second paper", "authorString": "A B.", "pmid": "2"},
                    {"title": "Third", "authorString": "C D."},
                ],
            },
        ),
    }


FULL_EXPECTED = textwrap.dedent(
    """\
    # NAC001 — `AT1G01010`

    *Arabidopsis thaliana* · protein_coding · 1:3,631–5,899 (-) · TAIR10

    NAC domain containing protein 1

    ## Protein
    **NAC domain-containing protein 1**
    NAC1_ARATH · UniProtKB reviewed (Swiss-Prot) · 429 aa
    UniProt: [Q0WV96](https://www.uniprot.org/uniprotkb/Q0WV96)

    ## Protein domains
    - **NAM** (domain, pfam) [8–140]
    - **IPR003441** (family)

    ## GO annotations
    **Molecular Function**
    - [GO:0003700] DNA-binding transcription factor activity (IEA)
    **Biological Process**
    - [GO:0006355] regulation of DNA-templated transcription
    - [GO:0000001] extra bp (IEA)
    **Cellular Component**
    - [GO:0005634] nucleus (IDA)

    ## Pathways (KEGG)
    - `ath00010` Glycolysis / Gluconeogenesis — Metabolism; Carbohydrate metabolism
    - `ath00020` TCA

    ## Interaction partners (STRING, top 2)
    | Partner | Score |
    | --- | --- |
    | NAC3 | 0.85 |
    | 3702.AT1G00001.1 | 0.5 |

    ## Cross-references
    - UniProtKB Gene Name: Q0WV96
    - TAIR_LOCUS: AT1G01010

    ## Literature
    40 hits total; showing top 2.
    - **Spaceflight transcriptome patterns in Arabidopsis** — Seo D, Paul AL, Ferl RJ. (PMID:41152268; doi:10.1038/x)
    - **Second paper** — A B. (PMID:2)
    """
)


def test_render_gene_report_md_full_dossier_is_byte_exact():
    md = _render_gene_report_md(
        "AT1G01010", "Arabidopsis thaliana", "NAC001", _full_rows(), top_n=2
    )
    assert md == FULL_EXPECTED


def test_render_gene_report_md_top_n_caps_every_list_section():
    """The same fixture at top_n=3 shows the third row in every capped section
    and the literature line reports 3, so the cap is the fixture's, not a
    hard-coded 2 hiding in the renderer."""
    md = _render_gene_report_md(
        "AT1G01010", "Arabidopsis thaliana", "NAC001", _full_rows(), top_n=3
    )
    assert "- **THIRD** [1–2]" in md
    assert "- [GO:0000002] third bp" in md
    assert "- `ath00030` third" in md
    assert "| THIRD | 0.1 |" in md
    assert "- EntrezGene: 839580" in md
    assert "40 hits total; showing top 3." in md
    assert "- **Third** — C D." in md
    assert "## Interaction partners (STRING, top 3)" in md
    # Positive control for the dedup: the duplicate xref still appears once.
    assert md.count("- UniProtKB Gene Name: Q0WV96") == 1


def _sparse_rows() -> dict[str, StepRow]:
    return {
        # start without end: no location rendered; strand ignored; no biotype/assembly/description
        "annotation": _ok("annotation", {"strand": -1, "seq_region_name": "1", "start": 10}),
        "protein": _err("protein", "[HTTPError] boom"),
        "domains": _ok("domains", {"domains": []}),
        "go_annotations": _skip("go_annotations", "phase 1 failed"),
        "pathways": _ok("pathways", {}),
        "interactions": _err("interactions", "[HTTPError] boom"),
        "xrefs": _ok("xrefs", {"xrefs": []}),
        # Europe PMC sends null title/authorString for some records: no "None" in
        # prose and no dangling " — " separator.
        "literature": _ok("literature", {"hits": [{"title": None, "authorString": None}]}),
    }


SPARSE_EXPECTED = textwrap.dedent(
    """\
    # AT1G01010 — `AT1G01010`

    *Arabidopsis thaliana*

    ## Protein
    _Unavailable — [HTTPError] boom_

    ## Protein domains
    _No annotated protein domains found._

    ## GO annotations
    _Unavailable — phase 1 failed_

    ## Pathways (KEGG)
    _No KEGG pathway memberships found._

    ## Interaction partners (STRING, top 3)
    _Unavailable — [HTTPError] boom_

    ## Cross-references
    _No cross-references found._

    ## Literature
    - ****
    """
)


def test_render_gene_report_md_sparse_dossier_takes_every_fallback():
    md = _render_gene_report_md("AT1G01010", "Arabidopsis thaliana", None, _sparse_rows(), top_n=3)
    assert md == SPARSE_EXPECTED


def test_render_gene_report_md_no_uniprot_record_vs_unavailable_are_distinct():
    """An ok protein row with no hit and an errored protein row read differently:
    "not found" is a fact about the gene, "unavailable" is a fact about the run."""
    rows = _sparse_rows()
    rows["protein"] = _ok("protein", {})
    md = _render_gene_report_md("X", "Org", None, rows, top_n=3)
    assert "_No UniProt record found._" in md
    assert "Unavailable — [HTTPError] boom_\n\n## Protein domains" not in md


def test_render_gene_report_md_plus_strand_and_accession_only_protein():
    rows = _sparse_rows()
    rows["annotation"] = _ok(
        "annotation", {"seq_region_name": "2", "start": 1, "end": 1000, "strand": 1}
    )
    rows["protein"] = _ok("protein", {"primaryAccession": "P12345"})
    md = _render_gene_report_md("X", "Org", None, rows, top_n=3)
    assert "*Org* · 2:1–1,000 (+)" in md
    # No recommendedName / uniProtkbId: the bold line falls through to the accession,
    # and with no metadata bits and no web_url nothing else is emitted.
    assert "## Protein\n**P12345**\n\n## Protein domains" in md


def test_render_gene_report_md_every_list_section_can_be_unavailable():
    """Each list section has its own note lookup; an error row in every one of
    them must render its reason, and a literature/xrefs/pathways/domains row
    that is None must not be dereferenced."""
    rows = _sparse_rows()
    for name in ("domains", "pathways", "interactions", "xrefs", "literature", "go_annotations"):
        rows[name] = _err(name, f"[ReadTimeout] {name} slow")
    md = _render_gene_report_md("X", "Org", None, rows, top_n=3)
    for name in ("domains", "pathways", "interactions", "xrefs", "literature", "go_annotations"):
        assert f"_Unavailable — [ReadTimeout] {name} slow_" in md
    assert "None" not in md
    # protein ok with only an accession, no primaryAccession at all: bold line is empty-safe
    rows["protein"] = _ok("protein", {"uniProtkbId": "X_ARATH"})
    md = _render_gene_report_md("X", "Org", None, rows, top_n=3)
    assert "## Protein\n**X_ARATH**\nX_ARATH\n" in md
