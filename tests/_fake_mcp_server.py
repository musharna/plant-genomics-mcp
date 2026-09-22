"""Fake stdio MCP server — test helper only, never imported by product code.

Answers the MCP handshake and a small set of tools over stdio, stdlib only,
so tests can exercise `McpClient` and `examples/arf_family/verify_genes.py`
without spawning the real `plant-genomics-mcp` server (which always
succeeds against the network) or touching the network.

Usage: ``python3 _fake_mcp_server.py <mode>``

Modes:
  ok         — ``initialize`` succeeds. ``tools/call`` for tool ``echo``
               returns ``{"echo": <arguments>}`` as its JSON payload; any
               other tool name returns an ``isError`` result with a
               plain-text message.
  init-error — ``initialize`` responds with a JSON-RPC ``"error"`` object.
  chain      — ``initialize`` succeeds. Every ``tools/call`` returns a small
               ok payload ``{"tool": <name>, "args": <arguments>}`` except
               ``CHAIN_FAILING_TOOL``, which returns an ``isError`` result,
               for `tests/test_arf_chain.py`'s runner tests.
  arf        — ``initialize`` succeeds. ``tools/call`` serves canned
               ``interpro_domains`` / ``panther_family`` payloads keyed by
               ``arguments["locus"]`` from ``ARF_FIXTURES`` (see below), for
               `tests/test_verify_genes.py`. Any other tool name, or a
               locus not in the fixture table, returns an ``isError``
               result.
  family     — ``initialize`` succeeds. A toy two-chromosome genome
               (``FAMILY_*`` tables below) behind ``ensembl_region_query``,
               ``gramene_homologs``, ``interpro_domains``, ``panther_family``,
               ``ensembl_plants_lookup_locus`` and an empty
               ``orthodb_orthologs``, for `tests/test_enumerate_family.py`.

An unrecognised mode string is a hard error (exit 2) at startup — it must
never silently fall back to ``ok`` behaviour, which would let a typo in a
test's mode argument pass as the wrong fixture set instead of failing loud.
"""

from __future__ import annotations

import json
import sys

_MODES = {"ok", "init-error", "arf", "chain", "family"}

# `family` mode: a two-chromosome toy genome for
# `examples/arf_family/enumerate_family.py`'s closure test. Paralog edges
# are deliberately incomplete (the seed reaches AT1G00050, which alone
# reaches AT1G00060 — two hops, so a closure that stops after one round
# misses it; AT2G00010 is reachable only through the region scan) so the test can tell "closed
# over paralogs" from "found by scanning" — and AT1G00030 sits on
# chromosome 1 with a matching description but no IPR010525, so the
# InterPro arbiter has something to reject. `gramene_homologs(ortholog)`
# on the seed names one rice-shaped and one wheat-shaped locus plus one
# that is neither; the rice hit has a paralog that is not a member.
FAMILY_GENOME: dict[str, list[dict]] = {
    "1": [
        {"gene_id": "AT1G00010", "biotype": "protein_coding", "description": "auxin thing"},
        {"gene_id": "AT1G00020", "biotype": "protein_coding", "description": "unrelated"},
        {"gene_id": "AT1G00030", "biotype": "protein_coding", "description": "B3 family"},
        {"gene_id": "AT1G00040", "biotype": "lncRNA", "description": "auxin lncRNA"},
        # Matches the filter but has no InterPro fixture: its interpro_domains
        # call FAILS, which must leave it undecided, not rejected.
        {"gene_id": "AT1G00070", "biotype": "protein_coding", "description": "auxin, no record"},
    ],
    "2": [
        {"gene_id": "AT2G00010", "biotype": "protein_coding", "description": "B3 family protein"},
    ],
}
FAMILY_LENGTHS = {"1": 5_000_000, "2": 1_500_000}
FAMILY_PARALOGS: dict[str, list[str]] = {
    "AT1G00010": ["AT1G00050"],
    "AT1G00050": ["AT1G00010", "AT1G00060"],
    "AT1G00060": ["AT1G00050"],
    "Os01g0000100": ["Os01g0000200"],
    "Os01g0000200": ["Os01g0000100"],
}
FAMILY_ORTHOLOGS: dict[str, list[str]] = {
    "AT1G00010": ["Os01g0000100", "TraesCS1A02G000100", "Zm00001d000001"],
}
# Locus-id prefix -> canonical organism, for the fake `target_organism` filter.
_FAKE_ORGANISM_OF = {"AT": "arabidopsis_thaliana", "Os": "oryza_sativa", "Tr": "triticum_aestivum"}
FAMILY_INTERPRO: dict[str, list[str]] = {
    "AT1G00010": ["IPR010525", "IPR033389"],
    "AT1G00050": ["IPR010525"],
    "AT1G00060": ["IPR010525"],
    "AT1G00030": ["IPR003340"],
    "AT2G00010": ["IPR010525"],
    "Os01g0000100": ["IPR010525"],
    "Os01g0000200": ["IPR003340"],
    "TraesCS1A02G000100": ["IPR010525", "IPR033389"],
}


def _family_call(name: str, args: dict) -> dict:
    locus = str(args.get("locus", ""))
    if name == "gramene_homologs":
        table = FAMILY_PARALOGS if args.get("homology_type") == "paralog" else FAMILY_ORTHOLOGS
        hits = table.get(locus, [])
        result = {
            "locus": locus,
            "release": "fake",
            "total": len(hits),
            "truncated": False,
            "homologs": [{"target_locus": h, "type": "x", "gene_tree_id": "f"} for h in hits],
        }
        target = args.get("target_organism")
        if target:
            # Mirror the real filter (#125): keep the target organism's rows,
            # tagged with it, and report the pre-filter total beside them.
            kept = [h for h in hits if _FAKE_ORGANISM_OF.get(h[:2]) == target]
            result.update(
                target_organism=target,
                total=len(kept),
                total_all_organisms=len(hits),
                homologs=[
                    {"target_locus": h, "type": "x", "gene_tree_id": "f", "organism": target}
                    for h in kept
                ],
            )
        return _text_result(result)
    if name == "orthodb_orthologs":
        # Wheat-shaped loci have no group: found=False carries no filter keys,
        # exactly as the real tool answers (a re-run died on that KeyError).
        if locus.startswith("Traes"):
            return _text_result({"locus": locus, "found": False, "member_count": 0, "members": []})
        result = {"locus": locus, "found": True, "member_count": 0, "members": []}
        if args.get("target_organism"):
            result.update(target_organism=args["target_organism"], member_count_all_organisms=0)
        return _text_result(result)
    if name == "ensembl_region_query":
        region, start, end = args["region"], args["start"], args["end"]
        if region not in FAMILY_GENOME:
            return _error_result(f"HTTP 400: No slice found for location {region}:{start}-{end}")
        if start > FAMILY_LENGTHS[region]:
            return _error_result(
                f"HTTP 400: Cannot request a slice whose start ({start}) is greater than "
                f"{FAMILY_LENGTHS[region]} for {region}."
            )
        if region == "2" and end - start + 1 >= 4_000_000:
            # Chromosome 2 times out at the full window and answers at a
            # smaller one, so the walk's halve-and-retry is exercised.
            return _error_result(
                "[UpstreamUnavailableError] Ensembl Plants /overlap/region exhausted 3 retries"
            )
        feats = FAMILY_GENOME[region] if start == 1 else []
        return _text_result({"region": region, "count": len(feats), "features": feats})
    if name == "interpro_domains":
        if locus not in FAMILY_INTERPRO:
            return _error_result(f"[NotFoundError] fake: no InterPro record for {locus}")
        return _text_result(
            {
                "locus": locus,
                "found": True,
                "domains": [{"accession": e, "interpro": e} for e in FAMILY_INTERPRO[locus]],
            }
        )
    if name == "panther_family":
        return _text_result({"locus": locus, "found": True, "subfamily_id": f"PTHR0:{locus}"})
    if name == "ensembl_plants_lookup_locus":
        return _text_result({"id": locus, "display_name": f"SYM_{locus}"})
    return _error_result(f"unknown tool: {name}")


# `chain` mode: every tools/call succeeds with a tiny stub echoing its
# arguments, EXCEPT this one tool, which returns an isError result. The runner
# must therefore produce exactly one auto gap row per gene per run, beside
# fifteen calls that produce none — so a test can assert both that a failure
# IS logged and that a success is NOT, rather than only that some rows exist.
CHAIN_FAILING_TOOL = "kegg_pathways"
# ...and any chain-mode call whose `organism` is this string is refused
# with the tag the live server uses for an organism a backend does not
# cover, in both the single (isError) and the batch (per-locus `errors`)
# shape — so the runner's "expected, not a gap" classification is testable.
CHAIN_UNSUPPORTED_ORGANISM = "fake_unsupported"
_UNSUPPORTED = "[OrganismNotSupported] backend 'fake' has no ID for 'fake_unsupported'"

# Keyed by locus. Shapes mirror the real interpro_domains / panther_family
# tool payloads closely enough for `verify_genes.py` to read: `domains` (list
# of dicts carrying `accession`/`interpro` InterPro ids, checked as a JSON
# substring the same way the real-stdio test in test_arf_mcp_client.py does)
# and `subfamily_id` (a single structured field). `None` for a tool means
# that tool call fails for this locus (isError), for the "a failed call must
# not read as absence" case. A `list` value is a legitimately-decoded,
# ok=True non-dict payload (verify_genes.py's payload-shape defect).
ARF_FIXTURES: dict[str, dict[str, dict | list | None]] = {
    "GOOD_ARF_PB1": {
        "interpro_domains": {
            "locus": "GOOD_ARF_PB1",
            "found": True,
            "domain_count": 2,
            "truncated": False,
            "domains": [
                {"accession": "IPR010525", "interpro": "IPR010525"},
                {"accession": "IPR033389", "interpro": "IPR033389"},
            ],
            "count_by_type": {},
        },
        "panther_family": {
            "locus": "GOOD_ARF_PB1",
            "found": True,
            "family_id": "PTHR31384",
            "subfamily_id": "PTHR31384:SF10",
        },
    },
    "GOOD_ARF_NO_PB1": {
        "interpro_domains": {
            "locus": "GOOD_ARF_NO_PB1",
            "found": True,
            "domain_count": 1,
            "truncated": False,
            "domains": [{"accession": "IPR010525", "interpro": "IPR010525"}],
            "count_by_type": {},
        },
        "panther_family": {
            "locus": "GOOD_ARF_NO_PB1",
            "found": True,
            "family_id": "PTHR31384",
            "subfamily_id": "PTHR31384:SF193",
        },
    },
    "NOT_ARF": {
        "interpro_domains": {
            "locus": "NOT_ARF",
            "found": True,
            "domain_count": 1,
            "truncated": False,
            "domains": [{"accession": "IPR005225", "interpro": "IPR005225"}],
            "count_by_type": {},
        },
        "panther_family": {
            "locus": "NOT_ARF",
            "found": True,
            "family_id": "PTHR11711",
            "subfamily_id": "PTHR11711:SF462",
        },
    },
    "CALL_FAILS": {
        "interpro_domains": None,
        "panther_family": None,
    },
    # `payload` is `dict | None` per CallResult's own type, but every helper
    # in verify_genes.py assumed `dict`. A `list` here is not the "call
    # failed" sentinel (that's Python `None`, used by CALL_FAILS above) — it
    # is a legitimately-decoded, ok=True JSON value that just isn't an
    # object, which `json.loads` on a server response can produce.
    "INTERPRO_NON_DICT_PAYLOAD": {
        "interpro_domains": ["not", "a", "dict"],
        "panther_family": {
            "locus": "INTERPRO_NON_DICT_PAYLOAD",
            "found": True,
            "family_id": "PTHR31384",
            "subfamily_id": "PTHR31384:SF10",
        },
    },
    # A rice-shaped locus that answers ONLY when the call names its
    # organism (`ARF_FIXTURE_ORGANISM`): the live tools resolve a locus
    # within the organism given, so a manifest row's organism must reach
    # the call. Any other organism gets an isError result.
    "Os01g0000100": {
        "interpro_domains": {
            "locus": "Os01g0000100",
            "found": True,
            "domain_count": 1,
            "truncated": False,
            "domains": [{"accession": "IPR010525", "interpro": "IPR010525"}],
            "count_by_type": {},
        },
        "panther_family": {
            "locus": "Os01g0000100",
            "found": True,
            "family_id": "PTHR31384",
            "subfamily_id": "PTHR31384:SF50",
        },
    },
    "PANTHER_NON_DICT_PAYLOAD": {
        "interpro_domains": {
            "locus": "PANTHER_NON_DICT_PAYLOAD",
            "found": True,
            "domain_count": 2,
            "truncated": False,
            "domains": [
                {"accession": "IPR010525", "interpro": "IPR010525"},
                {"accession": "IPR033389", "interpro": "IPR033389"},
            ],
            "count_by_type": {},
        },
        "panther_family": ["not", "a", "dict"],
    },
}


ARF_FIXTURE_ORGANISM: dict[str, str] = {"Os01g0000100": "oryza_sativa"}
# A member PANTHER does not classify: `subfamily_id` is null.
ARF_FIXTURES["UNCLASSIFIED_ARF"] = {
    "interpro_domains": ARF_FIXTURES["GOOD_ARF_NO_PB1"]["interpro_domains"],
    "panther_family": {"locus": "UNCLASSIFIED_ARF", "found": False, "subfamily_id": None},
}


def _respond(req_id: object, *, result: dict | None = None, error: dict | None = None) -> None:
    resp: dict = {"jsonrpc": "2.0", "id": req_id}
    if error is not None:
        resp["error"] = error
    else:
        resp["result"] = result
    sys.stdout.write(json.dumps(resp) + "\n")
    sys.stdout.flush()


def _text_result(payload: object) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload)}], "isError": False}


def _error_result(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def main() -> None:
    if len(sys.argv) <= 1:
        sys.stderr.write("_fake_mcp_server.py: a mode argument is required\n")
        raise SystemExit(2)
    mode = sys.argv[1]
    if mode not in _MODES:
        sys.stderr.write(
            f"_fake_mcp_server.py: unknown mode {mode!r}, expected one of {sorted(_MODES)}\n"
        )
        raise SystemExit(2)

    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        req = json.loads(line)
        method = req.get("method")
        req_id = req.get("id")

        if req_id is None:
            # Notification (e.g. notifications/initialized) — no response.
            continue

        if method == "initialize":
            if mode == "init-error":
                _respond(
                    req_id,
                    error={"code": -32000, "message": "fake init failure"},
                )
            else:
                _respond(
                    req_id,
                    result={
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "serverInfo": {"name": "fake-mcp-server", "version": "0"},
                    },
                )
        elif method == "tools/call":
            params = req.get("params", {})
            name = params.get("name")
            args = params.get("arguments", {})
            if mode == "family":
                _respond(req_id, result=_family_call(name, args))
            elif mode == "chain":
                if name.startswith("batch_"):
                    # A batch envelope: every locus succeeds, except that the
                    # batch form of CHAIN_FAILING_TOOL puts every locus in
                    # `errors`, the way the live batch tools report a
                    # per-locus failure inside an ok envelope.
                    base = name[len("batch_") :]
                    loci = args.get("loci") or args.get("loci_or_accessions") or []
                    if args.get("organism") == CHAIN_UNSUPPORTED_ORGANISM:
                        envelope = {
                            "tool": base,
                            "count": len(loci),
                            "results": {},
                            "errors": {lo: _UNSUPPORTED for lo in loci},
                        }
                    elif base == CHAIN_FAILING_TOOL:
                        envelope = {
                            "tool": base,
                            "count": len(loci),
                            "results": {},
                            "errors": {lo: f"fake chain failure for {base}" for lo in loci},
                        }
                    else:
                        envelope = {
                            "tool": base,
                            "count": len(loci),
                            "results": {lo: {"tool": base, "locus": lo} for lo in loci},
                            "errors": {},
                        }
                    _respond(req_id, result=_text_result(envelope))
                elif args.get("organism") == CHAIN_UNSUPPORTED_ORGANISM:
                    _respond(req_id, result=_error_result(_UNSUPPORTED))
                elif name == CHAIN_FAILING_TOOL:
                    _respond(req_id, result=_error_result(f"fake chain failure for {name}"))
                else:
                    _respond(req_id, result=_text_result({"tool": name, "args": args}))
            elif mode == "arf" and name in ("interpro_domains", "panther_family"):
                locus = args.get("locus")
                fixture = ARF_FIXTURES.get(locus, {}).get(name)
                wanted = ARF_FIXTURE_ORGANISM.get(locus)
                if wanted is not None and args.get("organism") != wanted:
                    _respond(
                        req_id,
                        result=_error_result(
                            f"[NotFoundError] fake: {locus} is not a locus of "
                            f"{args.get('organism')!r}"
                        ),
                    )
                elif fixture is None:
                    _respond(req_id, result=_error_result(f"no fixture for {name}({locus!r})"))
                else:
                    _respond(req_id, result=_text_result(fixture))
            elif name == "echo":
                text = json.dumps({"echo": args})
                _respond(
                    req_id,
                    result={"content": [{"type": "text", "text": text}], "isError": False},
                )
            else:
                _respond(
                    req_id,
                    result={
                        "content": [{"type": "text", "text": f"unknown tool: {name}"}],
                        "isError": True,
                    },
                )
        else:
            _respond(req_id, error={"code": -32601, "message": f"method not found: {method}"})


if __name__ == "__main__":
    main()
