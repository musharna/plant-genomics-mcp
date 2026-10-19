# The review bar

Read by the guardrails `review` job, from the BASE branch: a PR cannot rewrite
the bar it is reviewed under. Template-owned: change it in `repo-template` and
resync. A finding the reviewer keeps repeating belongs here, or better in a hook.

## Look for

Most code here is machine-written, so hold it to a HIGHER bar than
hand-written code. Check specifically for:

1. Tests that cannot fail: assertions that pass on the broken code, negative
   checks with no positive control, fixtures that encode the belief under
   test, wall-clock budgets used as correctness checks.
2. Guards whose predicate cannot observe what it must discriminate.
3. Silent no-ops: bare `str.replace()`, unasserted return values, swallowed
   exceptions, fallbacks that hide failure.
4. Correctness: off-by-one, unit/scale errors, wrong-name-right-value, regex
   backtracking, resource leaks, concurrency.
5. Security: injection, path traversal, secrets, unsafe deserialisation.

Report only findings you can point to a `path:line` for. Ignore style.

## Tag every finding

- **Important**: a bug this PR introduces or exposes. Fix before merge.
- **Nit**: real but minor. At most three per review.
- **Pre-existing**: a bug in code this PR did not change. Report it, but it
  is not the PR's fault and not a reason to hold the merge.

When a finding is one instance of a pattern, name the class and list every
instance in the PR, so one fix covers them all.

## Format

The first line after the marker is the count: `2 Important, 1 Nit, 0
Pre-existing.` If nothing meets the bar, that line is `Nothing meets the bar.`
and the review ends there. Findings follow, most severe first.

The `review-gate` job reads that line: any Important fails it and holds the
merge. So from round 2 on, the count includes every Important still open from
an earlier round, and `Nothing meets the bar.` means none is.

## Rounds

The job tells you the round: one round is one review comment posted on this PR.

- **Round 1**: review the whole PR.
- **Round 2 and later**: review only the commits since your last review comment
  (`gh pr view <n> --comments`) and post a short delta. Never re-post a finding
  from an earlier round; an unfixed one gets one line, `still open: <path:line>`.
- **Round 4 and later**: the review is not converging. Open with one short
  paragraph asking the author to step back and reconsider the approach, then
  report Important findings only.
