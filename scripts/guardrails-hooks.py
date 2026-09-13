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
"""

import ast
import sys
from pathlib import Path

N_REPLACE_ARGS = 2  # str.replace(old, new); renames never pass two literals
COVERS_DEFUSED = {"ValueError", "DefusedXmlException", "Exception", "BaseException"}


def _is_str_literal(node):
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def bare_replace(tree):
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
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            a.name.split(".")[0] == "defusedxml" for a in node.names
        ):
            return True
        if (
            isinstance(node, ast.ImportFrom)
            and (node.module or "").split(".")[0] == "defusedxml"
        ):
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


def defused_parseerror(tree):
    if not _imports_defusedxml(tree):
        return
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler) or node.type is None:
            continue
        names = _exc_names(node.type)
        if "ParseError" in names and not COVERS_DEFUSED.intersection(names):
            yield (
                node.lineno,
                "except ParseError with defusedxml imported: DefusedXmlException is a "
                "ValueError, not a ParseError; the attack path escapes unwrapped",
            )


CHECKS = (bare_replace, defused_parseerror)


def check_files(paths):
    rc = 0
    for p in paths:
        try:
            tree = ast.parse(Path(p).read_text(encoding="utf-8"), filename=p)
        except SyntaxError as e:  # let ruff report syntax; do not mask it as a rule hit
            print(f"{p}: skipped (SyntaxError: {e.msg})", file=sys.stderr)
            continue
        for check in CHECKS:
            for line, msg in check(tree):
                print(f"{p}:{line}: {msg}")
                rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(check_files(sys.argv[1:]))
