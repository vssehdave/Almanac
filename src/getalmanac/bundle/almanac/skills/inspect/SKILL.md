---
name: inspect
description: The last review before a merge, run by the main model; the owner types /inspect [mode] [target]. It reviews the diff and on a pass pushes, opens the PR, waits on CI and merges.
disable-model-invocation: true
argument-hint: "[ship|review|two-pass|ci|issue] [branch|pr|issue]"
---

You are now the inspector. You do this review yourself, on the main thread;
do not hand it to a sub-agent. Arguments: `$ARGUMENTS`.

## Modes

The first argument is the mode; with none, use `ship` on the current branch.

- `ship` (default): review the diff, then follow the ship flow below.
- `review`: review only. Report the verdict; push nothing.
- `two-pass`: `ship`, but review twice against the same checklist. The second
  pass tries to refute the first pass's PASS. Both must pass.
- `ci <pr>`: read the PR's failing checks only and report the root cause.
- `issue <n>`: review the issue (`gh issue view <n>`): is it reproducible,
  scoped, with acceptance criteria? Report each gap.

## Review checklist

The diff is `git diff main...<branch>`. Read it in parts, never a whole large file.

1. **Changed code.** Map the changed functions, classes and modules, and find
   their tests. List new code paths with no test.
2. **Behavioral coverage.** Each feature has a test; edge cases and error paths
   are covered; important integrations are covered.
3. **Test quality.** Meaningful assertions over no-throw checks; no flaky
   patterns; isolated tests with clear names.
4. **Commits.** Each commit follows the repo's rules: Conventional Commits type
   and scope, the repo's subject length, a body that says why, no attribution
   lines. Give a rewritten message for each that fails.
5. **Security.** Secrets or credentials logged or stored, injection (shell, SQL,
   path), unsafe deserialization, auth bypass, new network peers. Rank each by
   whether it is reachable from outside the machine; local-only noise is low.
6. **Production check.** What breaks on a real run: config defaults,
   migrations, fail-open paths, error handling. Local evidence only.

Rate each gap critical, important or nice-to-have. Any critical gap is a FAIL.
Load `almanac:lean-review` to judge scope, `almanac:python-testing` for tests,
`almanac:ai-regression-testing` for bug-fix tests.

While inspecting you do not edit code; fixes go to builders (below).

## Ship flow

1. Review. On FAIL, push nothing; go to the fix loop.
2. On PASS: `git push -u origin <branch>`, then `gh pr create --fill --base main`.
   Never force-push.
3. `gh pr checks <pr> --watch`. On a red check read
   `gh run view <id> --log-failed | tail -40`, name the root cause and the
   smallest fix as file and line. That is a FAIL; go to the fix loop.
4. Green: `gh pr merge <pr> --merge` (a merge commit keeps the branch commits;
   leave the branch). Report the merge commit.

## Fix loop

On FAIL, turn the findings into a numbered list of fixes, each with the fix you
chose. Send that list to `almanac:planner` for one-function builder briefs,
then send one `almanac:builder` per function, all in one message. When they
report, check their diffs and test results, commit, and run this checklist
again on the new diff (in the same mode). At most two fix rounds; if it still
fails, send the remaining findings to `almanac:architect` and tell the owner.

## Report

1. Verdict (PASS or FAIL) and, after a ship, the PR and merge commit.
2. Critical gaps.
3. Other findings and the fixes made.
4. What was not checked.
