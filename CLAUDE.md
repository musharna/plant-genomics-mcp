# The bar for code in this repo

Most code here is machine-written. It is held to a HIGHER bar than hand-written
code, not a lower one. This file is the contract; CI and the pre-commit hooks
are its enforcement. Read it before writing anything.

## 1. Before code: outcome and constraints, in writing

State, in the PR description or the issue, before the first line of code:

- **Outcome**: the observable behaviour that will exist when this is done.
- **Acceptance check**: the command whose exit status decides done. If no such
  command exists yet, writing it is the first task.
- **Constraints**: what must not change (public API, file formats, data on
  disk, runtime budget), and what is out of scope.

Ambiguity is a stop sign. Ask; do not pick the most likely reading.

## 2. A test is worth only what it can fail on

Every test ships with three controls:

1. **Seen to fail**: run it against the broken state and confirm it fails FOR
   THE STATED REASON. A security test that asserts "some exception was raised"
   passes on vulnerable code too.
2. **Positive control**: a negative assertion (error raised, input rejected)
   lives in the same test as the assertion that the legitimate path succeeds.
3. **Real execution at every system boundary**: code that talks to a registry,
   a CLI, the network or another process's file layout gets at least one check
   that drives the real thing, not only a fixture.

A fixture encodes your belief; it cannot fail on that belief. A wall-clock
budget cannot tell a slow host from a broken test. A surviving mutant
(`nightly-guardrails.yml`) is the coverage report.

## 3. House rules (enforced; see `.pre-commit-config.yaml`)

Each rule is an incident, not a preference:

- `x.replace(a, b)` with the result discarded is a silent no-op.
- `pytest.raises(Exception)` cannot tell a wrapped error from a leaked one.
- `except ParseError` with defusedxml imported lets the attack path escape.
- `# nosec` names the test id AND a traced reason.
- No nested lazy quantifiers; no `nohup ... &`; no `text-transform: uppercase`
  over unit strings; no absolute developer-machine paths.

Never narrow, skip or `--no-verify` a hook to get a commit through. If a hook
is wrong, fix the hook in `repo-template` and resync.

## 4. When you miss, write the lesson, do not hand-fix in silence

A finding from review, the nightly fuzz or mutation job, or a human reading
the diff means the bar was missed. The fix is two commits, not one:

1. the code fix, with the test that now fails on the old code;
2. the lesson, appended to `LESSONS.md` (create it on first use): one line,
   dated, stating the class of miss and the mechanism that now catches it.
   If the class can be caught by a hook or a lint rule, add the hook to
   `repo-template` in the same PR. Prose that must hold every time is a hook.

## 5. Analysis code: the result is the product, and it can be wrong while every test is green

If this repo has `guardrails-analysis.yml`, the software bar above is necessary, not sufficient:

- **Write `SPEC.md` first** (estimand, exclusions, model, decision rule, what would falsify). It must be
  committed before the first `analysis/` commit. Changing it is a change of question: label `spec-change`.
- **Every analysis has a controls file** in `tests/controls/`: a planted-effect ladder (the smallest effect
  it can see, so a null is a bound), a shuffle null with the exchangeable unit written down, a positive
  control that passes on real data AND fails on shuffled data, a representability check, a
  `DataContract` at every hand-off (units, build, key uniqueness, row-count delta), and a corruption
  injection that must fail loud. `scripts/analysis_controls.py` and `scripts/analysis_controls.R` have all
  six (same names, same refusals); each library's selftest proves every control can fail. An R analysis
  gets an R controls file, not a Python shim around it.
- **Every number the README or paper reports is in `claims.yml`** with its generator and a typed
  tolerance. Never compare bytes; a consistently wrong figure byte-matches itself.
- **Do not fake a plot, a control, or "verified".** If a control cannot be built, say which and why in
  `AI-USAGE.md`. An exclusion count you did not plan is a finding, not a filter.
- **`scripts/reproduce.sh` from a fresh clone is the acceptance check** (or `make figures`). A stage a
  hosted runner cannot run is skipped there with a printed reason and its claims carry `hosted: false`;
  it still runs locally. If it needs data, `scripts/fetch-data.sh` with checksums.
- **A notebook is not a record unless it reproduces top-to-bottom from a fresh kernel.** The notebooks
  stage (`scripts/notebooks_check.py`) refuses committed execution counts that are not 1..n, outputs kept
  after a cell was cleared, committed error outputs, and any error on fresh execution. Numbers a notebook
  reports still go through `claims.yml`; the executed copy is never byte-compared to the committed one.

## 6. Reporting

Lead with defects, then what was verified and how (command + exit status),
then what was not verified. "Done" means the acceptance check from section 1
exited 0 and you have read its output.
