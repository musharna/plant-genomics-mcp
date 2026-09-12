#!/usr/bin/env python3
"""AST-based house-rule hooks (things a line regex cannot see).

bare-replace: a statement consisting of `x.replace(<str literal>, <str literal>)`
whose result is discarded. Strings are immutable, so this is a silent no-op;
os.replace / Path.replace are renames and take non-literal args, so the
two-string-literal shape is the discriminator.
"""
import ast
import sys


def bare_replace(tree):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        if not (isinstance(call.func, ast.Attribute) and call.func.attr == "replace"):
            continue
        if len(call.args) >= 2 and all(
            isinstance(a, ast.Constant) and isinstance(a.value, str) for a in call.args[:2]
        ):
            yield node.lineno, "bare str.replace() discards its result (silent no-op)"


def main(paths):
    rc = 0
    for p in paths:
        try:
            tree = ast.parse(open(p, encoding="utf-8").read(), filename=p)
        except SyntaxError as e:  # let ruff report syntax; do not mask it as a rule hit
            print(f"{p}: skipped (SyntaxError: {e.msg})", file=sys.stderr)
            continue
        for line, msg in bare_replace(tree):
            print(f"{p}:{line}: {msg}")
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
