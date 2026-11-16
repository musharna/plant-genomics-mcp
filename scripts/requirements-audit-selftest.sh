#!/usr/bin/env bash
# requirements_audit.sh can fail: a vulnerable pin in a requirements file is
# found, for a Python this runner is not and through an `-r` include; the same
# files without it pass; and a file that cannot be audited exits 2, never 0.
# Real uv against real OSV, as in lock-audit-selftest.sh: a canned report would
# encode the belief this checks.
#
#   bash scripts/requirements-audit-selftest.sh [audit-script]   (default: requirements_audit.sh)
#
# The planted pin is pillow 12.3.0, marked python_version < '3.11'. Its
# advisories are published and stay published.
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
audit=$(realpath "${1:-$here/requirements_audit.sh}")
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
project() { # dir: a project that declares its floor and no dependency, as taxon3d does
	mkdir -p "$1"
	printf '[project]\nname = "requirements-audit-fixture"\nversion = "0"\nrequires-python = ">=3.10"\n' >"$1/pyproject.toml"
}
audit_in() { # dir [env...]: run the audit there; stdout/stderr to files, echo the exit status
	local d=$1
	shift
	(cd "$d" && env "$@" bash "$audit" . >"$d/stdout" 2>"$d/stderr")
	echo $?
}

# The planted fork must be one this runner's Python does NOT select, or a
# per-interpreter audit would find it too and case 1 would prove less.
py=$(uv python find 2>/dev/null)
want "the runner's Python is not in the planted fork (< 3.11)" ok \
	"$("$py" -c 'import sys; print("ok" if sys.version_info >= (3, 11) else "%d.%d" % sys.version_info[:2])' 2>&1)"

# 1. a pin only Python 3.10 selects, in requirements.txt; a second file reaches
#    it only through `-r`; a third names nothing vulnerable
project "$t/planted"
printf '# runtime\niniconfig==2.1.0\npillow==12.3.0 ; python_version < "3.11"\n' >"$t/planted/requirements.txt"
printf -- '-r requirements.txt\npackaging==25.0\n' >"$t/planted/requirements-dev.txt"
printf 'packaging==25.0\n' >"$t/planted/requirements-extra.txt"
want "a vulnerable pin in a requirements file: exit 1" 1 "$(audit_in "$t/planted")"
want "  ... named under the file that pins it" 1 "$(grep -A6 '^## requirements.txt$' "$t/planted/stdout" | grep -cE '^pillow 12\.3\.0 has [0-9]+ known vulnerabilit')"
want "  ... and found through the -r include, said once" 1 "$(grep -c '^## requirements-dev.txt: the same findings as requirements.txt$' "$t/planted/stdout")"
want "  ... the findings themselves printed once" 1 "$(grep -cE '^pillow 12\.3\.0 has [0-9]+ known vulnerabilit' "$t/planted/stdout")"
want "  ... the file without it is not listed" 0 "$(grep -c '^## requirements-extra.txt' "$t/planted/stdout")"
want "  ... with uv's summary line on stderr for each of the two" 2 "$(grep -cE '^Found [0-9]+ known vulnerabilit' "$t/planted/stderr")"

# 2. positive control: the same three files without the pin are clean, and were seen
project "$t/clean"
printf '# runtime\niniconfig==2.1.0\n' >"$t/clean/requirements.txt"
cp "$t/planted/requirements-dev.txt" "$t/planted/requirements-extra.txt" "$t/clean/"
want "the same files without it: exit 0" 0 "$(audit_in "$t/clean")"
want "  ... every file audited at least one package" 3 "$(grep -cE '^Found no known vulnerabilities.* in [1-9][0-9]* packages?$' "$t/clean/stderr")"
want "  ... and nothing on stdout" 0 "$(wc -c <"$t/clean/stdout" | tr -d ' ')"

# 3. no pyproject.toml at all: the floor comes from the runner's Python, the pin is still found
mkdir -p "$t/bare"
printf 'pillow==12.3.0\n' >"$t/bare/requirements.txt"
want "requirements files and no pyproject: exit 1" 1 "$(audit_in "$t/bare")"
want "  ... saying the floor is the runner's Python" 1 "$(grep -c "declares no requires-python: auditing for this runner's Python" "$t/bare/stderr")"

# 3b. the floor is the project's however its pyproject writes it. A floor that is
#     not read becomes the runner's Python, and the Python 3.10 pin is then
#     resolved away: exit 0 on one package.
mkdir -p "$t/quoted"
printf "[tool.other]\nrequires-python = '>=3.13'\n\n[project]\nname = 'requirements-audit-fixture'\nversion = '0'\nrequires-python = '>=3.10'\n" >"$t/quoted/pyproject.toml"
cp "$t/planted/requirements.txt" "$t/quoted/"
want "a single-quoted requires-python, below another table's: exit 1" 1 "$(audit_in "$t/quoted")"
want "  ... audited for the project's floor" 1 "$(grep -c '^requirements_audit: requirements.txt (requires-python >=3.10)$' "$t/quoted/stderr")"
want "  ... the Python 3.10 pin named" 1 "$(grep -cE '^pillow 12\.3\.0 has [0-9]+ known vulnerabilit' "$t/quoted/stdout")"
want "  ... and the same floor read from the double-quoted project" 1 "$(grep -c '^requirements_audit: requirements.txt (requires-python >=3.10)$' "$t/planted/stderr")"
# A pyproject that cannot be read is not a project without a floor.
mkdir -p "$t/broken"
printf '[project\nrequires-python = ">=3.10"\n' >"$t/broken/pyproject.toml"
cp "$t/planted/requirements.txt" "$t/broken/"
want "a pyproject.toml that does not parse: exit 2" 2 "$(audit_in "$t/broken")"
want "  ... saying so, and auditing nothing" "1 0" "$(grep -c 'pyproject.toml did not parse' "$t/broken/stderr") $(grep -c '^Found ' "$t/broken/stderr")"

# 4. nothing to audit is clean, and says which kind of nothing
project "$t/none"
want "no requirements file: exit 0" 0 "$(audit_in "$t/none")"
want "  ... saying so" 1 "$(grep -c 'no requirements\*.txt here' "$t/none/stderr")"
project "$t/comments"
printf '# nothing yet\n\n' >"$t/comments/requirements.txt"
want "a requirements file of comments: exit 0" 0 "$(audit_in "$t/comments")"

# 5. a file that was not audited is exit 2, never clean
project "$t/unseen"
printf 'iniconfig==2.1.0 ; python_version < "3.0"\n' >"$t/unseen/requirements.txt"
want "a file that names a requirement and audits 0 packages: exit 2" 2 "$(audit_in "$t/unseen")"
want "  ... saying so" 1 "$(grep -c 'names requirements and 0 packages were audited' "$t/unseen/stderr")"
project "$t/unresolvable"
printf 'iniconfig==99.99.99\n' >"$t/unresolvable/requirements.txt"
printf 'packaging==25.0\n' >"$t/unresolvable/requirements-extra.txt"
want "a file that does not resolve: exit 2" 2 "$(audit_in "$t/unresolvable")"
want "  ... and the file after it is still audited" 1 "$(grep -cE '^Found no known vulnerabilities.* in 1 package' "$t/unresolvable/stderr")"
want "OSV and PyPI unreachable: exit 2, not clean" 2 "$(audit_in "$t/planted" UV_OFFLINE=1 UV_CACHE_DIR="$t/empty-cache")"
# Resolved, then OSV unreachable: the lock exists and the audit of it did not run.
want "warm a cache with the clean files: exit 0" 0 "$(audit_in "$t/clean" UV_CACHE_DIR="$t/warm-cache")"
want "resolved from that cache with OSV unreachable: exit 2" 2 "$(audit_in "$t/clean" UV_OFFLINE=1 UV_CACHE_DIR="$t/warm-cache")"
want "  ... every file resolved, so it was the audit that did not run" 0 "$(grep -c 'did not resolve' "$t/clean/stderr")"
# A uv audit that exits 0 without its summary line was not understood.
mkdir -p "$t/quiet/scripts" && cp "$audit" "$t/quiet/scripts/requirements_audit.sh"
printf '#!/usr/bin/env bash\nexit 0\n' >"$t/quiet/scripts/lock_audit.sh"
project "$t/quiet/p"
printf 'iniconfig==2.1.0\n' >"$t/quiet/p/requirements.txt"
want "an audit that exits 0 and says nothing: exit 2" 2 "$(
	cd "$t/quiet/p" && bash "$t/quiet/scripts/requirements_audit.sh" . >stdout 2>stderr
	echo $?
)"
want "  ... naming the missing line" 1 "$(grep -c "without its 'Found no known vulnerabilities ... in N packages' line" "$t/quiet/p/stderr")"

# 6. a vulnerable file beside one that was not audited: exit 2 wins, the finding is still printed
cp "$t/planted/requirements.txt" "$t/unresolvable/requirements-zz.txt"
want "not audited beside vulnerable: exit 2" 2 "$(audit_in "$t/unresolvable")"
want "  ... the finding is still on stdout" 1 "$(grep -cE '^pillow 12\.3\.0 has [0-9]+ known vulnerabilit' "$t/unresolvable/stdout")"

if [ "$f" -ne 0 ]; then
	for d in planted clean bare quoted broken unseen unresolvable; do
		echo "--- $d stderr:"
		cat "$t/$d/stderr"
	done
fi
echo "$((n - f))/$n passed"
[ "$f" -eq 0 ]
