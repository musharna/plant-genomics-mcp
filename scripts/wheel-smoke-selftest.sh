#!/usr/bin/env bash
# Negative controls for scripts/wheel_smoke.py: real packages, really built and
# installed. Each broken fixture WORKS from its source tree (so an editable test
# install would never see it) and is broken only as a wheel.
set -uo pipefail
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR
here=$(cd "$(dirname "$0")" && pwd)
t=$(mktemp -d)
trap 'rm -rf "$t"' EXIT
n=0
f=0
want() { # name want-rc got-rc [pattern that must appear in the output]
	n=$((n + 1))
	if [ "$2" = "$3" ] && { [ -z "${4:-}" ] || grep -qE "$4" "$t/out"; }; then echo "ok   $1 (exit $3)"; else
		echo "FAIL $1: want exit $2${4:+ and /$4/}, got $3"
		sed 's/^/     /' "$t/out"
		f=$((f + 1))
	fi
}
run() { # dir [env...]
	d=$1
	shift
	env "$@" python3 -B "$here/wheel_smoke.py" "$d" >"$t/out" 2>&1
	echo $?
}
# pkg <dir> <packages-list> <script-target> [package-data line]
pkg() {
	rm -rf "$1"
	mkdir -p "$1/demo/sub" "$1/scripts"
	cat >"$1/pyproject.toml" <<EOF
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"
[project]
name = "demo-smoke"
version = "0.0.1"
[project.scripts]
demo = "$3"
[tool.setuptools]
packages = $2
include-package-data = false
${4:-}
EOF
	echo 'from demo.sub import helper  # eager: the package needs its subpackage' >"$1/demo/__init__.py"
	printf 'def main():\n    return 0\n' >"$1/demo/cli.py"
	echo 'def helper(): return 1' >"$1/demo/sub/__init__.py"
	git -C "$1" init -q
	git -C "$1" add -A
}

pkg "$t/good" '["demo", "demo.sub"]' 'demo.cli:main'
want "a correct package: clear" 0 "$(run "$t/good")" 'base install: imported 1 .*loaded 1 console'
want "same wheel, source tree on PYTHONPATH: refused as not the wheel" 1 "$(run "$t/good" PYTHONPATH="$t/good")" 'resolved into the source tree'

pkg "$t/nosub" '["demo"]' 'demo.cli:main'
want "subpackage left out of the wheel (imports fine from source)" 1 "$(run "$t/nosub")" "No module named 'demo.sub'"

pkg "$t/badep" '["demo", "demo.sub"]' 'demo.cli:mian'
want "console script names a function that does not exist" 1 "$(run "$t/badep")" 'console script demo'

pkg "$t/data" '["demo", "demo.sub"]' 'demo.cli:main'
echo '{}' >"$t/data/demo/schema.json"
git -C "$t/data" add -A
want "tracked data file not in the wheel" 1 "$(run "$t/data")" 'not in the wheel: demo/schema.json'
echo 'demo/schema.json  # dev-only fixture, read by no shipped code' >"$t/data/scripts/wheel-smoke-ignore.txt"
want "the same file, ignored with a reason: clear" 0 "$(run "$t/data")"
pkg "$t/data2" '["demo", "demo.sub"]' 'demo.cli:main' $'[tool.setuptools.package-data]\ndemo = ["*.json"]'
echo '{}' >"$t/data2/demo/schema.json"
git -C "$t/data2" add -A
want "the same file, declared as package data: clear" 0 "$(run "$t/data2")"

pkg "$t/host" '["demo", "demo.sub"]' 'demo.cli:main'
echo 'import host_only_module_zz' >"$t/host/demo/inhost.py"
echo 'import host_only_module_zz' >"$t/host/demo/other.py"
git -C "$t/host" add -A
want "lazily-used module that cannot import: caught by the full walk" 1 "$(run "$t/host")" 'import demo.inhost'
printf 'module:demo.inhost  # runs inside the host application only\n' >"$t/host/scripts/wheel-smoke-ignore.txt"
want "a module ignore covers ONLY the module it names" 1 "$(run "$t/host")" 'import demo.other'
grep -q 'ignored demo.inhost' "$t/out" && ! grep -q 'import demo.inhost' "$t/out"
want "  ...and the named one was skipped, not reported" 0 $?
printf 'module:demo.inhost  # host only\nmodule:demo.other  # host only\n' >"$t/host/scripts/wheel-smoke-ignore.txt"
want "both named: clear" 0 "$(run "$t/host")"

mkdir -p "$t/nobuild" && printf '[project]\nname = "x"\nversion = "0"\n' >"$t/nobuild/pyproject.toml"
want "no [build-system]: skipped, and says so" 0 "$(run "$t/nobuild")" '^skipped:'
pkg "$t/broken" '["demo", "no_such_package"]' 'demo.cli:main'
want "a wheel that does not build is a finding, not a skip" 1 "$(run "$t/broken")" 'does not build'

echo "$n cases, $f failed"
[ "$f" = 0 ]
