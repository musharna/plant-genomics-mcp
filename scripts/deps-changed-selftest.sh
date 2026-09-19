#!/usr/bin/env bash
# Negative controls for scripts/deps_changed.sh on real git histories.
set -uo pipefail
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR
here=$(cd "$(dirname "$0")" && pwd)
t=$(mktemp -d)
trap 'rm -rf "$t"' EXIT
n=0
f=0
G() { git -c user.name=t -c user.email=t@t -c commit.gpgsign=false "$@" >/dev/null 2>&1; }
want() { # name want-rc got-rc
	n=$((n + 1))
	if [ "$2" = "$3" ]; then echo "ok   $1 (exit $3)"; else
		echo "FAIL $1: want exit $2, got $3"
		f=$((f + 1))
	fi
}
run() {
	env "$@" bash "$here/deps_changed.sh" >/dev/null 2>&1
	echo $?
}
mkdir -p "$t/r/src" && cd "$t/r" || exit 1
G init -q -b main
echo 'x = 1' >src/app.py
echo 'anyio==4.13.0' >requirements.txt
G add -A && G commit -q -m base
base=$(git rev-parse HEAD)

echo 'x = 2' >src/app.py
G add -A && G commit -q -m "code only"
want "PR touching only code: no audit" 1 "$(run EVENT=pull_request BASE="$base")"
want "push touching only code: no audit" 1 "$(run EVENT=push BEFORE="$base")"

mkdir -p vendor
echo 'x' >vendor/notuv.lock
echo 'x' >vendor/xpyproject.toml
G add -A && G commit -q -m "names that only END like a dependency file"
want "notuv.lock / xpyproject.toml are not dependency files: no audit" 1 "$(run EVENT=pull_request BASE="$base")"

echo 'anyio==4.15.1' >requirements.txt
G add -A && G commit -q -m "bump"
want "PR changing requirements.txt: audit" 0 "$(run EVENT=pull_request BASE="$base")"
want "push changing requirements.txt: audit" 0 "$(run EVENT=push BEFORE="$base")"

echo '[project]' >pyproject.toml
G add -A && G commit -q -m "pyproject"
mid=$(git rev-parse HEAD~1)
want "push changing pyproject.toml: audit" 0 "$(run EVENT=push BEFORE="$mid")"

want "push with no usable before (new branch): audit, do not guess" 0 "$(run EVENT=push BEFORE=0000000000000000000000000000000000000000)"
want "workflow_dispatch: audit unconditionally" 0 "$(run EVENT=workflow_dispatch)"

echo "$n cases, $f failed"
[ "$f" = 0 ]
