#!/usr/bin/env bash
# lock_audit.sh can fail: a vulnerable pin that only ANOTHER Python selects is
# found, a clean lock passes, and an audit that cannot reach OSV exits 2, never
# 0. Real uv against real OSV: a canned report would encode the belief this
# checks, and the belief that failed (2026-11-01) was "the audit sees every pin".
#
#   bash scripts/lock-audit-selftest.sh [audit-script]   (default: lock_audit.sh)
#
# The planted pin is pillow 12.3.0, marked python_full_version < '3.11'. Its
# advisories are published and stay published. `uv export | pip-audit -r` on
# this lock prints "No known vulnerabilities found" (exit 0) on Python 3.12.
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
audit=$(realpath "${1:-$here/lock_audit.sh}")
t=$(mktemp -d)
trap 'rm -rf "$t"' EXIT
n=0
f=0
want() { # name want got
	n=$((n + 1))
	if [ "$2" = "$3" ]; then echo "ok   $1 ($3)"; else
		echo "FAIL $1: want '$2', got '$3'"
		f=$((f + 1))
	fi
}

fixture() { # dir with_pillow(0|1): a project and a lock, as uv writes them, minus wheels
	local d=$1 dep="" pkg="" meta="" req='"iniconfig==2.1.0"'
	mkdir -p "$d"
	if [ "$2" = 1 ]; then
		req=$req$', "pillow==12.3.0 ; python_full_version < \'3.11\'"'
		dep=$'\n    { name = "pillow", marker = "python_full_version < \'3.11\'" },'
		meta=$'\n    { name = "pillow", marker = "python_full_version < \'3.11\'", specifier = "==12.3.0" },'
		pkg=$'\n[[package]]\nname = "pillow"\nversion = "12.3.0"\nsource = { registry = "https://pypi.org/simple" }\n'
	fi
	cat >"$d/pyproject.toml" <<-EOF
		[project]
		name = "lock-audit-fixture"
		version = "0"
		requires-python = ">=3.10"
		dependencies = [$req]
	EOF
	cat >"$d/uv.lock" <<-EOF
		version = 1
		revision = 3
		requires-python = ">=3.10"

		[[package]]
		name = "iniconfig"
		version = "2.1.0"
		source = { registry = "https://pypi.org/simple" }

		[[package]]
		name = "lock-audit-fixture"
		version = "0"
		source = { virtual = "." }
		dependencies = [
		    { name = "iniconfig" },$dep
		]

		[package.metadata]
		requires-dist = [
		    { name = "iniconfig", specifier = "==2.1.0" },$meta
		]
		$pkg
	EOF
}
audit_in() { # dir [env...]: run the audit there; stdout/stderr to files, echo the exit status
	local d=$1
	shift
	(cd "$d" && env "$@" bash "$audit" . >"$d/stdout" 2>"$d/stderr")
	echo $?
}

fixture "$t/planted" 1
fixture "$t/clean" 0

# The planted fork must be one this runner's Python does NOT select, or a
# per-interpreter audit would find it too and this test would prove nothing.
py=$(cd "$t/planted" && uv python find 2>/dev/null)
want "the runner's Python is not in the planted fork (< 3.11)" ok \
	"$("$py" -c 'import sys; print("ok" if sys.version_info >= (3, 11) else "%d.%d" % sys.version_info[:2])' 2>&1)"

# 1. the planted pin, for a Python this runner is not, is found
want "a pin only Python 3.10 selects: exit 1" 1 "$(audit_in "$t/planted")"
want "  ... naming it" 1 "$(grep -cE '^pillow 12\.3\.0 has [0-9]+ known vulnerabilit' "$t/planted/stdout")"
want "  ... with uv's summary line on stderr" 1 "$(grep -cE '^Found [0-9]+ known vulnerabilit' "$t/planted/stderr")"

# 2. positive control: the same lock without it is clean
want "the same lock without it: exit 0" 0 "$(audit_in "$t/clean")"
want "  ... saying so" 1 "$(grep -c '^Found no known vulnerabilities' "$t/clean/stderr")"

# 3. no network and nothing cached: exit 2, which the steps read as "did not run"
want "OSV unreachable: exit 2, not clean" 2 "$(audit_in "$t/planted" UV_OFFLINE=1 UV_CACHE_DIR="$t/empty-cache")"

if [ "$f" -ne 0 ]; then
	for d in planted clean; do echo "--- $d stderr:"; cat "$t/$d/stderr"; done
fi
echo "$((n - f))/$n passed"
[ "$f" -eq 0 ]
