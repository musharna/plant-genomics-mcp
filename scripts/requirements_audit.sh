#!/usr/bin/env bash
# Audit the packages a project's requirements files name against OSV, for every
# Python version and platform the project supports.
#
#   bash scripts/requirements_audit.sh [project-dir]
#   exit 0 = clean, or no requirements file; 1 = known vulnerabilities;
#        2 = a file was not audited
#   stdout: the findings, under a "## <file>" line for each file that has any
#           (a file whose findings repeat an earlier file's names that file)
#   stderr: uv's progress and its `Found ...` summary line, once per file
#
# lock_audit.sh reads uv.lock. A project whose dependencies live in requirements
# files has no lock and a pyproject that names no dependency, so that audit
# passes on nothing: taxon3d's nightly reported "no known vulnerabilities ... in
# 0 packages" while pillow 12.3.0 sat in its requirements.txt with 62 advisories.
#
# Each requirements*.txt at the project root is resolved into a lock of its own
# (`uv add -r`, in a scratch project with this project's requires-python) and
# lock_audit.sh audits that lock: the same tool and the same output, for every
# fork, with `-r` includes followed. One lock per file because the files need
# not resolve together (an optional backend's pins). Nothing is installed.
#
# A file that names a requirement and has no package audited exits 2: that is
# the audit not seeing, which must not read as clean.
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
cd "${1:-.}" || exit 2
shopt -s nullglob
found=(requirements*.txt)
if [ ${#found[@]} -eq 0 ]; then
	echo "requirements_audit: no requirements*.txt here: uv.lock is the whole audit" >&2
	exit 0
fi
# requirements.txt first: the files that include it then point at its findings.
files=()
if [ -f requirements.txt ]; then files=(requirements.txt); fi
for f in "${found[@]}"; do
	if [ "$f" != requirements.txt ]; then files+=("$f"); fi
done

floor=""
if [ -f pyproject.toml ]; then
	# Parsed as TOML, and a file that does not parse stops the audit: a line
	# pattern reads one way of writing the key (a single-quoted value is as valid),
	# and a floor it missed became this runner's Python, which hides the pins only
	# an older supported Python selects.
	reader=$(uv python find --no-project '>=3.11' 2>/dev/null) || {
		echo "requirements_audit: no Python 3.11+ to read pyproject.toml with, so nothing was audited" >&2
		exit 2
	}
	floor=$("$reader" -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb")).get("project", {}).get("requires-python", ""))') || {
		echo "requirements_audit: pyproject.toml did not parse, so its requires-python is unknown and nothing was audited" >&2
		exit 2
	}
fi
if [ -z "$floor" ]; then # no declared floor: the Python uv would run this project on
	py=$(uv python find 2>/dev/null) || {
		echo "requirements_audit: no requires-python and no Python to take one from" >&2
		exit 2
	}
	floor=">=$("$py" -c 'import sys; print("%d.%d" % sys.version_info[:2])')" || exit 2
	echo "requirements_audit: the project declares no requires-python: auditing for this runner's Python and newer ($floor)" >&2
fi

t=$(mktemp -d)
trap 'rm -rf "$t"' EXIT
worst=0
shown=()
for f in "${files[@]}"; do
	d="$t/$f"
	mkdir -p "$d"
	printf '[project]\nname = "requirements-audit"\nversion = "0"\nrequires-python = "%s"\ndependencies = []\n' "$floor" >"$d/pyproject.toml"
	echo "requirements_audit: $f (requires-python $floor)" >&2
	if ! uv add --project "$d" --no-sync -r "$f" >"$d/add.log" 2>&1; then
		cat "$d/add.log" >&2
		echo "requirements_audit: $f did not resolve, so it was not audited" >&2
		worst=2
		continue
	fi
	bash "$here/lock_audit.sh" "$d" >"$d/stdout" 2>"$d/stderr"
	rc=$?
	cat "$d/stderr" >&2
	if [ -s "$d/stdout" ]; then
		# Files that include one another repeat the same findings: say so once.
		same=""
		for seen in "${shown[@]}"; do
			if cmp -s "$d/stdout" "$t/$seen/stdout"; then same=$seen; fi
		done
		if [ -n "$same" ]; then echo "## $f: the same findings as $same"; else
			echo "## $f"
			cat "$d/stdout"
			shown+=("$f")
		fi
	fi
	case "$rc" in
	0)
		audited=$(sed -n 's/^Found no known vulnerabilities.* in \([0-9][0-9]*\) packages\{0,1\}$/\1/p' "$d/stderr" | head -1)
		if [ -z "$audited" ]; then
			echo "requirements_audit: $f: uv audit exited 0 without its 'Found no known vulnerabilities ... in N packages' line" >&2
			rc=2
		elif [ "$audited" -eq 0 ] && grep -qvE '^[[:space:]]*(#|$)' "$f"; then
			echo "requirements_audit: $f names requirements and 0 packages were audited" >&2
			rc=2
		fi
		;;
	1) ;;
	*) rc=2 ;;
	esac
	if [ "$rc" -gt "$worst" ]; then worst=$rc; fi
done
exit "$worst"
