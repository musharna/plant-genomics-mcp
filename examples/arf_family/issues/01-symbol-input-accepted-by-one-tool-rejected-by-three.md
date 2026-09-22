# A gene symbol in a `locus` argument is answered by one tool and rejected by three

**Draft — not filed.** Written from the ARF family dossier run in
[`examples/arf_family/`](../), server version `1.21.0`, 2026-09-21
(29 genes x 16 tools, 248 MCP calls; the family itself from a 516-call
enumeration through the same tools). Each section below is one row of
[`gaps.jsonl`](../gaps.jsonl), quoted as logged; `Check` names the
captured response to read it back from.

Both rows are the same input — a symbol where an AGI locus code is expected — and the same missing behaviour: nothing on the tool surface decides whether a symbol is a legal `locus` value.

Covers 2 rows of `gaps.jsonl`.

## `shared-symbol`

- **Attempted:** resolve_locus_to_uniprot({'locus': 'ARF1', 'organism': 'arabidopsis_thaliana'})
- **Returned:** ok=true and one scalar answer: primaryAccession Q8L7G0 / uniProtkbId ARFA_ARATH / recommendedName 'Auxin response factor 1', with locus_query echoed back as 'ARF1'. src/plant_genomics_mcp/uniprot.py:290 returns \_normalize(results[0], ...) - the first hit of a multi-hit search, with no count of the rest. Nothing in the payload says the input was a symbol rather than an AGI locus code, or that the symbol is shared: the same file's probe of AT2G47170 returns uniProtkbId ARF1_ARATH, recommendedName 'ADP-ribosylation factor 1', geneNames ['ARF1'] - a second Arabidopsis gene carrying the same UniProt gene name. AT1G23490 probes as ARF2A_ARATH / 'ADP-ribosylation factor 2-A' / geneNames ['ARF2-A'].
- **Expected:** either the loci the symbol matches, or a refusal; a single ok=true accession is indistinguishable from an unambiguous hit
- **Check:** `raw/_symbol_probe_ARF1.json`

## `symbol-rejected-elsewhere`

- **Attempted:** pass the gene symbol 'ARF1' as the `locus` argument to the four chain-adjacent tools that take one
- **Returned:** three reject it, verbatim: [NotFoundError] Ensembl Plants /lookup/id/ARF1 → HTTP 400 (not found): {"error":"ID 'ARF1' not found"} ; BAR /thalemine/gene_information/ARF1 → HTTP 400: {"wasSuccessful": false, "error": "Invalid gene id"} ; [NotFoundError] Phytozome: locus ARF1 not found for organism_id 167. resolve_locus_to_uniprot, with the same argument name, accepts it and answers.
- **Expected:** one behaviour for a symbol in a `locus` argument across tools that share the argument name
- **Check:** `raw/_symbol_probe_ARF1.json`
