"""Schema-contract invariants over the live ``server.TOOLS`` registry.

These are fast, no-subprocess assertions on the tool catalog itself (not the
stdio transport). They lock in the uniform reject-unknown-args contract added
in the v1.6.0 audit remediation (finding P4): every advertised tool — both the
synthesis tools (which already set it) and the single-locus/batch tools — must
declare ``additionalProperties: false`` so a misspelled or spurious arg key
(e.g. ``organsim=``, or an ``organism=`` sent to a tool that does not accept
it) is rejected at the boundary rather than silently dropped by the
``args.get(...)`` dispatcher.
"""

from __future__ import annotations

from plant_genomics_mcp import server


def test_all_tools_have_object_input_schema() -> None:
    assert server.TOOLS, "tool catalog is empty"
    for tool in server.TOOLS:
        schema = tool.inputSchema or {}
        assert schema.get("type") == "object", (
            f"{tool.name} inputSchema is not type=object: {schema.get('type')!r}"
        )


def test_every_tool_rejects_unknown_args() -> None:
    """P4: uniform ``additionalProperties: false`` across the whole surface."""
    offenders = [
        tool.name
        for tool in server.TOOLS
        if (tool.inputSchema or {}).get("additionalProperties") is not False
    ]
    assert not offenders, (
        "these tools omit additionalProperties:false and would silently accept "
        f"unknown args: {offenders}"
    )
