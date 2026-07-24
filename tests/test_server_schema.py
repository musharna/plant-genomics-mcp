"""Schema-contract invariants over the live ``server.TOOLS`` registry.

These are fast, no-subprocess assertions on the tool catalog itself (not the
stdio transport). They lock in the uniform reject-unknown-args contract added
in the v1.6.0 audit remediation (finding P4): every advertised tool — both the
synthesis tools (which already set it) and the single-locus/batch tools — must
declare ``additionalProperties: false`` so a misspelled or spurious arg key
(e.g. ``organsim=``, or an ``organism=`` sent to a tool that does not accept
it) is rejected at the boundary rather than silently dropped by the
``args.get(...)`` dispatcher.

Also locks the annotation contract: every advertised tool must declare
``readOnlyHint``/``openWorldHint`` and a display ``title``. Hosts treat an
OMITTED annotation block as destructive + open-world, so a tool added without
one silently regains the confirmation friction the annotations removed.
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


def test_every_tool_declares_annotations() -> None:
    """No tool may ship without behaviour hints — hosts default them to destructive."""
    offenders = [tool.name for tool in server.TOOLS if tool.annotations is None]
    assert not offenders, (
        "these tools omit annotations and hosts will assume they are "
        f"destructive + open-world: {offenders}"
    )


def test_whole_catalog_is_read_only_and_open_world() -> None:
    """Every backend is an external public database we only ever read from."""
    for tool in server.TOOLS:
        annotations = tool.annotations
        assert annotations is not None  # covered by the test above
        assert annotations.readOnlyHint is True, f"{tool.name} is not marked read-only"
        assert annotations.destructiveHint is False, f"{tool.name} is marked destructive"
        assert annotations.openWorldHint is True, (
            f"{tool.name} is not marked open-world despite hitting an external API"
        )


def test_blast_is_the_only_non_idempotent_tool() -> None:
    """Each blast_sequence call enqueues a NEW rate-limited job at NCBI (Put -> RID).

    Every other tool is a plain lookup that can be repeated freely. If this
    list grows, the new tool needs the same deliberate justification.
    """
    non_idempotent = {
        tool.name
        for tool in server.TOOLS
        if tool.annotations is not None and tool.annotations.idempotentHint is False
    }
    assert non_idempotent == {"blast_sequence"}


def test_every_tool_has_a_unique_display_title() -> None:
    missing = [tool.name for tool in server.TOOLS if not tool.title]
    assert not missing, f"these tools have no display title: {missing}"
    titles = [tool.title for tool in server.TOOLS if tool.title]
    duplicates = sorted({t for t in titles if titles.count(t) > 1})
    assert not duplicates, f"display titles must disambiguate tools: {duplicates}"
