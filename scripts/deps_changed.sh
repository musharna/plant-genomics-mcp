#!/usr/bin/env bash
# Did this PR or push change what gets installed? Exit 0 = yes (audit it now),
# exit 1 = no. From ~/repo-template.
#
# pip-audit answers two different questions, and until 2026-09-19 one PR step
# asked both: "did THIS change bring in a vulnerable package?" (a property of the
# diff; blocks the PR) and "has a CVE been published against what is already on
# the default branch?" (a property of the calendar; it turned unrelated PRs red:
# anyio 4.13.0 held two syncs on 2026-09-18). The second question now belongs to
# the nightly audit job, which opens an issue instead of blocking work.
#
#   EVENT   pull_request | push | workflow_dispatch | schedule
#   BASE    PR base sha                 (pull_request)
#   BEFORE  sha before the push         (push)
set -uo pipefail
pat='(^|/)(uv\.lock|pyproject\.toml|requirements[^/]*\.txt|setup\.py|setup\.cfg)$'
case "${EVENT:-}" in
pull_request) range="${BASE:?}...HEAD" ;;
push)
	# A new branch or a force-push has no usable `before`: audit, do not guess.
	if [ -z "${BEFORE:-}" ] || ! git cat-file -e "${BEFORE}^{commit}" 2>/dev/null; then
		echo "no comparable base: auditing"
		exit 0
	fi
	range="${BEFORE}..HEAD"
	;;
*)
	echo "event '${EVENT:-}': auditing unconditionally"
	exit 0
	;;
esac
changed=$(git diff --name-only "$range" | grep -E "$pat" || true)
if [ -n "$changed" ]; then
	echo "dependency files changed in $range:"
	echo "$changed" | sed 's/^/  /'
	exit 0
fi
echo "no dependency file changed in $range"
exit 1
