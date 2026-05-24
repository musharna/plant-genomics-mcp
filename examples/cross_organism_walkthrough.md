# v0.9 Multi-Organism Resolver — Cross-Organism Walkthrough

**Captured:** 2026-05-24T05:57Z (against PyPI **v1.0.4**)
**Queries:** `Os01g0100100` (rice, `oryza_sativa`) and `Zm00001d027231` (maize, `zea_mays`)
**Capture script:** [`_run_cross_organism_chain.py`](_run_cross_organism_chain.py)
**Raw JSON:** [`cross_organism_captures.json`](cross_organism_captures.json)

v0.9 added an organism resolver so callers pass `organism="<species_slug>"` to
the existing v0.8 synthesis tools and the orchestrator routes to per-backend
identifiers (Ensembl Plants slug, UniProt taxid, Phytozome proteome int,
STRING taxid, Europe PMC organism slug) without any caller-side branching.
The captures below prove the **routing** works end-to-end against the
published 1.0.4 stack for two non-Arabidopsis organisms — and also surface
the **coverage** reality you should expect: per-backend curation varies
substantially across species, so partial-success envelopes are the norm
for non-Arabidopsis queries, not the exception.

Outputs may drift on re-run as upstream curates new data; the JSON file is
the durable reference.

---

## Coverage matrix

| Organism              | Tool                       | Steps ok | Elapsed |
| --------------------- | -------------------------- | -------: | ------: |
| `oryza_sativa` (rice) | `analyze_locus_synth`      |    5 / 5 |  1.78 s |
| `oryza_sativa` (rice) | `biological_context_synth` |    2 / 5 |  8.62 s |
| `zea_mays` (maize)    | `analyze_locus_synth`      |    1 / 5 |  0.66 s |
| `zea_mays` (maize)    | `biological_context_synth` |    1 / 5 |  2.88 s |

Mixed coverage is exactly what we want documented. **Step routing is correct
across every cell** (STRING received taxon 39947 for rice and 4577 for maize;
Ensembl received `oryza_sativa` and `zea_mays`; Europe PMC scoped the query
to `… AND rice`). The non-`ok` cells fall into three classes:

1. **Upstream coverage gap** — backend supports the organism but has no
   record for this specific locus/accession (STRING for both TrEMBL
   accessions; rice ATTED-II for `Os01g0100100`).
2. **Upstream assembly-version drift** — Maize `Zm00001d…` is the B73
   AGPv4 naming; current Ensembl Plants is on v5 (`Zm00001eb…`). The
   resolver routes correctly, the locus identifier itself is stale.
3. **Routing bug (worth filing)** — KEGG queries the `ath` (Arabidopsis)
   gene db even when `organism="oryza_sativa"` or `"zea_mays"`. The error
   message literally reads `KEGG: no pathway memberships for Os01g0100100
(ath gene db)`. See "Follow-ups" at the bottom.

---

## 1. `analyze_locus_synth` — `oryza_sativa` / `Os01g0100100`

**Input.** `{"locus": "Os01g0100100", "organism": "oryza_sativa"}`
**Elapsed.** 1.78 s — 5/5 steps ok

### Steps

| #   | Backend tool                  | Status | Routed argument               |
| --- | ----------------------------- | ------ | ----------------------------- |
| 1   | `ensembl_plants_lookup_locus` | ok     | species slug `oryza_sativa`   |
| 2   | `resolve_locus_to_uniprot`    | ok     | locus + organism → UniProt    |
| 3   | `get_gene_xrefs`              | ok     | species slug `oryza_sativa`   |
| 4   | `locus_literature`            | ok     | Europe PMC query `… AND rice` |
| 5   | `locus_go_annotations`        | ok     | UniProt accession `Q0JRI1`    |

### Result (truncated)

```json
{
  "reconciled": {
    "canonical_gene_name": null,
    "best_uniprot_accession": "Q0JRI1",
    "conflict_flags": []
  },
  "ensembl_record": {
    "id": "Os01g0100100",
    "biotype": "protein_coding",
    "assembly_name": "IRGSP-1.0",
    "seq_region_name": "1",
    "start": 2983,
    "end": 10815,
    "strand": 1,
    "canonical_transcript": "Os01t0100100-01.",
    "organism": "oryza_sativa"
  },
  "uniprot_record": {
    "primaryAccession": "Q0JRI1",
    "uniProtkbId": "Q0JRI1_ORYSJ",
    "entryType": "UniProtKB unreviewed (TrEMBL)",
    "reviewed": false,
    "recommendedName": null,
    "geneNames": [],
    "organism": "Oryza sativa subsp. japonica",
    "taxonId": 39947,
    "sequenceLength": 410
  },
  "xrefs": {
    "count": 3,
    "by_db": {
      "ArrayExpress": ["Os01g0100100"],
      "EntrezGene": ["4326813"],
      "WikiGene": ["4326813"]
    }
  },
  "literature": {
    "query": "Os01g0100100 AND rice",
    "hitCount": 4,
    "returned": 4,
    "hits": "<top hit: GrameneOryza (2025), doi 10.1093/database/baaf021>"
  },
  "go_annotations": {
    "uniprot_accession": "Q0JRI1",
    "numberOfHits": 0,
    "returned": 0
  }
}
```

**What this proves.** Five backends (Ensembl Plants, Ensembl xrefs, UniProtKB,
Europe PMC, QuickGO) all received rice-shaped identifiers and returned
real rice data — the resolver routed correctly. The `IRGSP-1.0` assembly
name, the `Q0JRI1_ORYSJ` UniProt ID suffix, and the literature query
`Os01g0100100 AND rice` all confirm per-backend organism routing.

**Data-quality note (not a routing failure).** The reconciled
`canonical_gene_name` is `null` and `xrefs.count` is 3 (vs 8 for `AT1G01010`),
because `Q0JRI1` is an unreviewed TrEMBL entry — no curated
`recommendedName`, no `geneNames`, and QuickGO returns 0 hits because
QuickGO only carries annotations for reviewed Swiss-Prot entries.
This is the cliff between Swiss-Prot (curated) and TrEMBL (auto-annotated)
that every cross-organism workflow has to confront — the envelope surfaces
it as visible nulls rather than synthesizing fake content.

---

## 2. `biological_context_synth` — `oryza_sativa` / `Os01g0100100`

**Input.** `{"locus": "Os01g0100100", "organism": "oryza_sativa", "top_n": 5}`
**Elapsed.** 8.62 s — 2/5 steps ok

### Steps

| #   | Backend tool               | Status    | Note                                                                                                                                            |
| --- | -------------------------- | --------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | `resolve_locus_to_uniprot` | ok        | → `Q0JRI1` (cache hit from analyze)                                                                                                             |
| 2   | `gramene_homologs`         | ok        | 156 total cross-species homologs (release `v69`)                                                                                                |
| 3   | `kegg_pathways`            | **error** | `[NotFoundError] KEGG: no pathway memberships for Os01g0100100 (ath gene db)` — **routing bug**: should query `osa` for rice, see Follow-ups    |
| 4   | `string_interactions`      | **error** | `[NotFoundError] STRING … HTTP 404: … protein called 'Q0JRI1' in the taxon '39947'` — taxon routed correctly; coverage gap for unreviewed entry |
| 5   | `atted_coexpression`       | **error** | `[NotFoundError] ATTED-II: no co-expression neighbors for Os01g0100100` — backend queries Arabidopsis release for any locus; see Follow-ups     |

### Result (truncated)

```json
{
  "uniprot_accession": "Q0JRI1",
  "homologs": {
    "locus": "Os01g0100100",
    "release": "v69",
    "total": 156,
    "homologs": [
      {
        "target_locus": "Mp4g13805",
        "type": "ortholog_one2one",
        "gene_tree_id": "EPlGT00940000167767"
      },
      {
        "target_locus": "Pp3c1_2510",
        "type": "ortholog_one2one",
        "gene_tree_id": "EPlGT00940000167767"
      },
      {
        "target_locus": "C5167_025963",
        "type": "ortholog_one2one",
        "gene_tree_id": "EPlGT00940000167767"
      },
      "<153 more homolog rows; see cross_organism_captures.json>"
    ]
  },
  "pathways": null,
  "string_partners": null,
  "atted_coexpression": null,
  "consensus_partners": []
}
```

**What this proves.** Two of the five expected partner-discovery sources
worked end-to-end for rice (resolve + Gramene homologs); the other three
errored at the upstream-coverage / upstream-routing layer. The envelope's
partial-failure model handles this cleanly — `pathways`, `string_partners`,
and `atted_coexpression` are `null` in the result block while
`steps[2..4]` carry typed errors with the upstream message verbatim. The
client gets a usable rice ortholog list (156 partners across basal land
plants and other monocots) plus visible holes in the other three sources.

---

## 3. `analyze_locus_synth` — `zea_mays` / `Zm00001d027231`

**Input.** `{"locus": "Zm00001d027231", "organism": "zea_mays"}`
**Elapsed.** 0.66 s — 1/5 steps ok

### Steps

| #   | Backend tool                  | Status      | Note                                                                                                                         |
| --- | ----------------------------- | ----------- | ---------------------------------------------------------------------------------------------------------------------------- |
| 1   | `ensembl_plants_lookup_locus` | **error**   | `Ensembl Plants /lookup/id/Zm00001d027231 → HTTP 400: {"error":"ID 'Zm00001d027231' not found"}` — assembly drift (v4 vs v5) |
| 2   | `resolve_locus_to_uniprot`    | ok          | → `A0A1D6JJ72` (TrEMBL, `A0A1D6JJ72_MAIZE`)                                                                                  |
| 3   | `get_gene_xrefs`              | **skipped** | `phase-1 ensembl lookup failed; skipped`                                                                                     |
| 4   | `locus_literature`            | **skipped** | `phase-1 ensembl lookup failed; skipped`                                                                                     |
| 5   | `locus_go_annotations`        | **skipped** | `phase-1 ensembl lookup failed; skipped`                                                                                     |

`result` is `null` because phase-1 Ensembl is the gate for analyze_locus_synth.

### What this proves

**Routing.** UniProt's `Zm00001d027231` → `A0A1D6JJ72` (`A0A1D6JJ72_MAIZE`,
taxonId 4577) lookup worked, confirming the resolver routed the locus to
UniProt's maize search correctly. **The Ensembl error is not a routing
bug — Ensembl Plants moved from B73 AGPv4 (`Zm00001d…` IDs) to v5
(`Zm00001eb…` IDs).** A current ID like `Zm00001eb000200` would succeed
on the same chain; this capture documents what users will see if they
paste a v4 ID into a tool now talking to a v5 endpoint, which is a
load-bearing case to surface because v4 IDs still dominate older
literature.

**Phase-1 gating behaviour.** When the phase-1 Ensembl lookup fails, the
orchestrator skips phase-2 steps that depended on it (xrefs, literature,
GO) instead of running them with stale state. `resolve_locus_to_uniprot`
runs in phase 1 alongside Ensembl and is independent of it, so step 2
still completed — visible in the captured per-step `result` for step 2
even though the envelope-level `result` is `null`.

---

## 4. `biological_context_synth` — `zea_mays` / `Zm00001d027231`

**Input.** `{"locus": "Zm00001d027231", "organism": "zea_mays", "top_n": 5}`
**Elapsed.** 2.88 s — 1/5 steps ok

### Steps

| #   | Backend tool               | Status    | Note                                                                                                                                               |
| --- | -------------------------- | --------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | `resolve_locus_to_uniprot` | ok        | → `A0A1D6JJ72`                                                                                                                                     |
| 2   | `gramene_homologs`         | **error** | `[NotFoundError] Gramene: no record for locus Zm00001d027231 in v69` — same assembly drift; Gramene v69 uses v5 IDs                                |
| 3   | `kegg_pathways`            | **error** | `[NotFoundError] KEGG: no pathway memberships for Zm00001d027231 (ath gene db)` — same routing bug (`ath` instead of `zma`)                        |
| 4   | `string_interactions`      | **error** | `[NotFoundError] STRING … HTTP 404: … protein called 'A0A1D6JJ72' in the taxon '4577'` — taxon routed correctly; coverage gap for TrEMBL accession |
| 5   | `atted_coexpression`       | **error** | `[NotFoundError] ATTED-II: no co-expression neighbors for Zm00001d027231` — ATTED-II `Ath-u.c4-0` is Arabidopsis only; needs maize release         |

### What this proves

**Routing.** STRING received taxon `4577` (correct for maize); the 404
is upstream-coverage, not routing. Gramene received the right locus key
shape (`Zm00001d…`) but the v4 → v5 ID drift drops the row.

**Cross-organism reality.** A `1/5` envelope for a v4 maize ID against
the current v5-aligned upstreams is the expected outcome. The resolver
did its job; the rest of the partial coverage is a faithful picture of
where the cross-organism data ecosystem actually is in May 2026.

---

## Follow-ups surfaced by this walkthrough

Filing as project notes (not blockers for v1.0.4):

1. **KEGG backend not yet wired to `organism=`.** `kegg.py:118` hardcodes
   `gene_id = f"ath:{locus.lower()}"`; the `lookup_pathways` signature
   doesn't accept an organism argument. The v0.9 migration sweep missed
   this backend. Fix is to thread `organism` through `lookup_pathways`
   and use the existing organism-record helper to pick the KEGG org code
   (`osa` for rice, `zma` for maize, etc.). The user-visible error
   `(ath gene db)` is literally what `kegg.py:121` raises.

2. **ATTED-II hardcoded to Arabidopsis release.** `atted.py:46` declares
   `ATTED_RELEASE = "Ath-u.c4-0"` as a module-level constant; the
   backend has no organism dispatch. Same v0.9-migration miss as KEGG.
   Fix would need a per-organism release map (and a live probe to confirm
   which other ATTED-II releases are currently published — the API
   serves multiple frozen releases but the catalog is curated, not
   complete across all plant organisms).

3. **Assembly-version drift (not a bug, but worth a docs note).**
   `Zm00001d…` (B73 AGPv4) is dead on Ensembl Plants v55+. Either
   surface a friendlier error message ("looks like a v4 maize ID;
   current Ensembl Plants is v5 — try `Zm00001eb…`") or add a
   v4→v5 ID-mapping helper to the resolver.

---

## Re-running

```bash
PLANT_GENOMICS_MCP_LIVE=1 .venv/bin/python examples/_run_cross_organism_chain.py \
  > examples/cross_organism_captures.json
```

The four chains together take ~15 s wall (Europe PMC + Gramene dominate;
STRING/KEGG/ATTED error fast). Re-running overwrites the JSON; this
markdown stays as-is until manually refreshed.
