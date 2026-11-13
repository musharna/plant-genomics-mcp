#!/usr/bin/env bash
# Audit every package in uv.lock against OSV, for every Python version and
# platform the lock covers, not only the ones this runner would install.
#
#   bash scripts/lock_audit.sh [project-dir]
#   exit 0 = clean, 1 = known vulnerabilities, 2 = the audit did not run
#   stdout: the findings; stderr: uv's progress and its `Found ...` summary line
#
# Until 2026-11-01 the template ran `uv export | pip-audit -r`. pip-audit
# evaluates each line's marker against the auditing interpreter, so a pin for
# another Python was never audited: figcite and ldraw-mcp resolved pillow
# 12.3.0 (59 OSV advisories) for Python 3.10 and both audited clean on 3.12.
# `uv audit` reads the lock itself. It is a preview feature, and its --help
# says --python-version defaults to the discovered interpreter;
# lock-audit-selftest.sh fails loud if it ever stops seeing a pin this
# runner's Python does not select.
#
# No --frozen: with no committed uv.lock (ldraw-mcp) uv locks first, as
# `uv export` did. Adverse project statuses (deprecated, archived) are printed
# but exit 0.
set -uo pipefail
cd "${1:-.}" || exit 2
exec uv audit --preview-features audit
