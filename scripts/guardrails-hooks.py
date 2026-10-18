#!/usr/bin/env python3
"""AST-based house-rule hooks (things a line regex cannot see).

bare-replace: a statement consisting of `x.replace(<str literal>, <str literal>)`
whose result is discarded. Strings are immutable, so this is a silent no-op;
os.replace / Path.replace are renames and take non-literal args, so the
two-string-literal shape is the discriminator.

defused-parseerror: a module that imports defusedxml but still catches only
`ParseError`. defusedxml signals an attack (entity expansion, DTD, external
reference) with DefusedXmlException, a ValueError subclass, NOT ParseError,
so `except ET.ParseError` lets the attack path escape unwrapped. The handler
must also name ValueError, DefusedXmlException, or Exception.

fixed-wait (`--fixed-wait`, hook no-fixed-wait-py): any `x.wait_for_timeout(...)`
call, and a `sleep` / `time.sleep` / `asyncio.sleep` statement when what runs
next clicks or types. "Next" is the statement that runs after the sleep, out of
its block if the sleep ends one (try -> else -> finally, loop -> else, and a
loop's next pass), and of that statement only what runs first: its header and
the first statement of each block it can enter. A def runs only its decorators
and defaults; a class body runs now, so its first statement is what runs first.
Excused by a `# qa-wait-ok: <why>` comment on any line the call spans.
This was a pygrep hook until 2026-10-17: a call the formatter wrapped over three
lines slipped past it, and --multiline reported only the first hit in a file.
"""

import ast
import io
import re
import sys
import tokenize
from pathlib import Path

N_REPLACE_ARGS = 2  # str.replace(old, new); renames never pass two literals
COVERS_DEFUSED = {"ValueError", "DefusedXmlException", "Exception", "BaseException"}
INTERACT = frozenset(
    {
        "click",
        "dblclick",
        "focus",
        "hover",
        "tap",
        "press",
        "press_sequentially",
        "type",
        "fill",
    }
)
QA_WAIT_OK = re.compile(r"qa-wait-ok:[ \t]*\S")
DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
TRY = tuple(getattr(ast, n) for n in ("Try", "TryStar") if hasattr(ast, n))
LOOPS = (ast.For, ast.AsyncFor, ast.While)
BLOCK = (ast.stmt, ast.excepthandler, ast.match_case)
EXCUSE = "(or '# qa-wait-ok: <why>' on the call)"
MSG_WAIT_FOR_TIMEOUT = "wait_for_timeout is a fixed sleep: wait for the state " + EXCUSE
MSG_SLEEP_THEN_ACT = "a sleep, then a click or key press: wait for the state " + EXCUSE


def _is_str_literal(node):
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def bare_replace(tree, _src):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        if not (isinstance(call.func, ast.Attribute) and call.func.attr == "replace"):
            continue
        old_new = call.args[:2]
        if len(old_new) == N_REPLACE_ARGS and all(_is_str_literal(a) for a in old_new):
            yield node.lineno, "bare str.replace() discards its result (silent no-op)"


def _imports_defusedxml(tree):
    # Short lines on purpose: synced repos format-check at 88 AND at 100.
    for node in ast.walk(tree):
        modules = []
        if isinstance(node, ast.Import):
            modules = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or ""]
        if any(m.split(".")[0] == "defusedxml" for m in modules):
            return True
    return False


def _exc_names(handler_type):
    """Trailing names of the exception(s) an `except` clause catches."""
    nodes = handler_type.elts if isinstance(handler_type, ast.Tuple) else [handler_type]
    out = []
    for n in nodes:
        if isinstance(n, ast.Name):
            out.append(n.id)
        elif isinstance(n, ast.Attribute):
            out.append(n.attr)
    return out


def defused_parseerror(tree, _src):
    if not _imports_defusedxml(tree):
        return
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler) or node.type is None:
            continue
        names = _exc_names(node.type)
        if "ParseError" in names and not COVERS_DEFUSED.intersection(names):
            msg = (
                "except ParseError with defusedxml imported: DefusedXmlException is a "
                "ValueError, not a ParseError; the attack path escapes unwrapped"
            )
            yield node.lineno, msg


def _excused_lines(src):
    """Lines with a real `# qa-wait-ok: <why>` comment; a string does not count."""
    lines = set()
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT and QA_WAIT_OK.search(tok.string):
            lines.add(tok.start[0])
    return lines


def _excused(node, ok_lines):
    return any(n in ok_lines for n in range(node.lineno, node.end_lineno + 1))


def _is_sleep(call):
    f = call.func
    if isinstance(f, ast.Name):
        return f.id == "sleep"
    return (
        isinstance(f, ast.Attribute)
        and f.attr == "sleep"
        and isinstance(f.value, ast.Name)
        and f.value.id in {"time", "asyncio"}
    )


def _stmt_call(stmt):
    """The call a bare expression statement makes, `await` unwrapped; else None."""
    if not isinstance(stmt, ast.Expr):
        return None
    value = stmt.value.value if isinstance(stmt.value, ast.Await) else stmt.value
    return value if isinstance(value, ast.Call) else None


def _runs_first(node):
    """What `node` runs first: a def its decorators and defaults (its body runs
    later); a try its body; any other node its expressions and the first
    statement of each block it can enter."""
    if isinstance(node, DEFS):
        a = node.args
        deco = getattr(node, "decorator_list", [])
        return [*deco, *a.defaults, *(d for d in a.kw_defaults if d)]
    out = []
    for field, value in ast.iter_fields(node):
        if isinstance(value, ast.AST):
            out.append(value)
        elif not isinstance(value, list):
            continue  # a name, constant or flag: nothing runs
        elif value and isinstance(value[0], ast.stmt):
            if field == "body" or not isinstance(node, TRY):
                out.append(value[0])
        elif field != "handlers":
            out.extend(v for v in value if isinstance(v, ast.AST))
    return out


def _interacts(stmt):
    """Does what `stmt` runs first click or type?"""
    stack = [stmt]
    while stack:
        node = stack.pop()
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in INTERACT
        ):
            return True
        stack.extend(_runs_first(node))
    return False


def _positions(tree):
    """Each statement, handler and match case -> (parent, field, index)."""
    where = {}
    for parent in ast.walk(tree):
        for field, value in ast.iter_fields(parent):
            if isinstance(value, list):
                for i, child in enumerate(value):
                    if isinstance(child, BLOCK):
                        where[child] = (parent, field, i)
    return where


def _then(parent, field):
    """Blocks of `parent` that run when its `field` block ends normally."""
    if isinstance(parent, LOOPS) and field == "body":
        return ("orelse",)
    if isinstance(parent, TRY):
        then = {"body": ("orelse", "finalbody"), "orelse": ("finalbody",)}
        return then.get(field, ("finalbody",) if field == "handlers" else ())
    return ()


def _successors(stmt, where):
    """The statements that can run after `stmt` ends normally: the next one, and
    the first statement of every loop whose body it ends (the next pass). Past
    the end of a function or the module, the caller resumes, unseen."""
    out = []
    node = stmt
    while node in where:
        parent, field, i = where[node]
        block = getattr(parent, field)
        if isinstance(node, ast.stmt) and i + 1 < len(block):
            return [*out, block[i + 1]]
        if isinstance(parent, DEFS):
            return out
        if isinstance(parent, LOOPS) and field == "body":
            out.append(block[0])
        for f in _then(parent, field):
            if getattr(parent, f):
                return [*out, getattr(parent, f)[0]]
        node = parent
    return out


def fixed_wait(tree, src):
    ok = _excused_lines(src)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "wait_for_timeout"
            and not _excused(node, ok)
        ):
            yield node.lineno, MSG_WAIT_FOR_TIMEOUT
    where = _positions(tree)
    for stmt in where:
        call = _stmt_call(stmt)
        if not (call and _is_sleep(call)) or _excused(stmt, ok):
            continue
        if any(_interacts(nxt) for nxt in _successors(stmt, where)):
            yield stmt.lineno, MSG_SLEEP_THEN_ACT


CHECKS = (bare_replace, defused_parseerror)
FIXED_WAIT_CHECKS = (fixed_wait,)


def check_files(paths, checks=CHECKS):
    rc = 0
    for p in paths:
        src = Path(p).read_text(encoding="utf-8")
        try:
            tree = ast.parse(src, filename=p)
        except SyntaxError as e:  # let ruff report syntax; do not mask it as a rule hit
            print(f"{p}: skipped (SyntaxError: {e.msg})", file=sys.stderr)
            continue
        hits = sorted(hit for check in checks for hit in check(tree, src))
        for line, msg in hits:
            print(f"{p}:{line}: {msg}")
            rc = 1
    return rc


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["--fixed-wait"]:
        sys.exit(check_files(args[1:], FIXED_WAIT_CHECKS))
    sys.exit(check_files(args))
