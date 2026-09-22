# `alphafold_structure` reports four pLDDT bands and never says where they divide

**Filed as [#135](https://github.com/musharna/plant-genomics-mcp/issues/135) — open.** Written from the ARF family dossier run in
[`examples/arf_family/`](../), server version `1.21.0`, 2026-09-21
(29 genes x 16 tools, 248 MCP calls; the family itself from a 516-call
enumeration through the same tools). Each section below is one row of
[`gaps.jsonl`](../gaps.jsonl), quoted as logged; `Check` names the
captured response to read it back from.

Covers 1 row of `gaps.jsonl`.

## `undefined-bands`

- **Attempted:** read alphafold_structure's plddt_bands
- **Returned:** four fractions named very_low / low / confident / very_high (0.516 / 0.027 / 0.120 / 0.338 for AT1G19850) beside mean_plddt 61.19. src/plant_genomics_mcp/alphafold.py:62 builds these names from upstream's fractionPlddtVeryLow/.../fractionPlddtConfident keys. Neither the payload nor the tool description ('the per-band pLDDT distribution') gives the pLDDT cutoffs the four bands divide on.
- **Expected:** the numeric cutoffs alongside the fractions
- **Check:** `raw/AT1G19850__alphafold_structure.json`
