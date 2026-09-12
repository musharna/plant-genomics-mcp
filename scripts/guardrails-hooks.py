#!/usr/bin/env python3
"""AST-based house-rule hooks (things a line regex cannot see).

bare-replace: a statement consisting of `x.replace(<str literal>, <str literal>)`
whose result is discarded. Strings are immutable, so this is a silent no-op;
os.replace / Path.replace are renames and take non-literal args, so the
two-string-literal shape is the discriminator.
"""

import ast
import sys
from pathlib import Path

N_REPLACE_ARGS = 2  # str.replace(old, new); renames never pass two literals


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


def check_files(paths):
    rc = 0
    for p in paths:
        try:
            tree = ast.parse(Path(p).read_text(encoding="utf-8"), filename=p)
        except SyntaxError as e:  # let ruff report syntax; do not mask it as a rule hit
            print(f"{p}: skipped (SyntaxError: {e.msg})", file=sys.stderr)
            continue
        for line, msg in bare_replace(tree):
            print(f"{p}:{line}: {msg}")
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(check_files(sys.argv[1:]))
