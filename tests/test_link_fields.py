"""Issue #136: a URL in a payload says that it is only a link.

Structures, PAE plots, motif logos and entry pages come back as URLs that no
tool on this server dereferences, and nothing said so: a caller could not tell
a link to follow in a browser from an identifier another tool takes. The rule
is on the field name, not a list of fields, so a URL added to a new tool's
output schema is held to it without anyone remembering this test.
"""

from __future__ import annotations

import re
from typing import Any

from plant_genomics_mcp import server
from plant_genomics_mcp.models import LINK_NOTE

URL_NAME = re.compile(r"(^|_)(url|logo)$")


def _url_fields(schema: dict[str, Any], defs: dict[str, Any], path: str, out: dict) -> None:
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        if f"{path}@{name}" not in out.setdefault("_seen", set()):
            out["_seen"].add(f"{path}@{name}")
            _url_fields(defs.get(name, {}), defs, path, out)
        return
    for key, sub in (schema.get("properties") or {}).items():
        if URL_NAME.search(key):
            out[f"{path}.{key}"] = sub.get("description") or ""
        _url_fields(sub, defs, f"{path}.{key}", out)
    for key in ("items", "additionalProperties"):
        if isinstance(schema.get(key), dict):
            _url_fields(schema[key], defs, path, out)
    for key in ("anyOf", "oneOf", "allOf"):
        for sub in schema.get(key) or []:
            _url_fields(sub, defs, path, out)


def _all_url_fields() -> dict[str, str]:
    out: dict[str, Any] = {}
    for tool in server.TOOLS:
        schema = tool.output_schema or {}
        _url_fields(schema, schema.get("$defs", {}), str(tool.name), out)
    out.pop("_seen", None)
    return out


def test_every_url_field_in_an_output_schema_says_no_tool_dereferences_it() -> None:
    fields = _all_url_fields()
    # Positive control: the walk reaches nested and $ref'd fields, so an empty
    # or shallow walk cannot pass this test by finding nothing to check.
    assert {
        "alphafold_structure.cif_url",
        "alphafold_structure.pae_image_url",
        "tf_binding_motifs.motifs.sequence_logo",
        "locus_literature.hits.web_url",
        "bar_aiv_interactions.papers.image_url",
    } <= set(fields), sorted(fields)
    assert len(fields) >= 16

    unmarked = {path: desc for path, desc in fields.items() if LINK_NOTE not in desc}
    assert not unmarked, unmarked
