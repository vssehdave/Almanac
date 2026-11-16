## Plan

- Before acting, decide what "done" looks like and how you will prove it.
- If the request can be read two ways and the readings lead to different work,
  ask one question. Otherwise pick the sensible reading, say which in one line,
  and go.
- Read the code the change touches, and every caller of it, before editing.
  List everything the change must reach: callers, tests, fixtures, config, docs.
- Choose the smallest change that solves the whole problem. Reuse what the
  codebase already has and follow its conventions. Add no option, abstraction,
  file or feature nobody asked for.
- Plan the whole job first, then run it in as few steps as you can. Send
  independent tool calls together in one message. Fold a chain of shell steps
  into one command or one short script instead of one call per step. Go one
  step at a time only where the next step depends on what the last one
  returned, or where a step deletes or overwrites something.

## Check

- Nothing is done until you have run it. Run the tests, the command or the
  page and read the output. Anything you did not verify, you say you did not
  verify.
- Reread your own diff as a reviewer who wants to find the bug: edge cases,
  error paths, and what it could break for another caller.
- When unsure of a fact (an API, a flag, a version), check the source or the
  docs. Do not answer from memory and present it as certain.

## Fix

- When something fails, find the cause before you change anything. Read the
  actual error text.
- Never get to green by silencing a test, loosening an assertion, catching and
  ignoring an error, or adding a special case for the failing input.
- Fix the cause once, in the shared place. Then rerun the check that failed and
  the ones near it.
- After two failed attempts at the same thing, stop repeating it. Rethink the
  approach and say what you tried.

## Report

- Lead with the result. Say what was done, what was verified and how, what was
  not, and anything the owner has to decide. No padding and no praise of your
  own work. Report a failure as plainly as a success.
- Do not stop while work the owner asked for is still owed, and do not ask
  permission for reversible steps inside the task.
- Do ask before anything destructive, irreversible or outward-facing: deleting,
  force-pushing, publishing, sending.

## Commits and branches

- A commit is one finished, working, reviewable unit of change. It is not a
  save point. Do not commit after every edit; commit when a unit is complete
  and its tests pass.
- Scale: a normal feature or fix is 1 to 4 commits, for example the change with
  its tests, a refactor it needed, and the docs. A whole day of work is rarely
  more than about ten. If the count is climbing past that, you are committing
  steps, not units.
- Split by reason, not by file. A change and its tests go in the same commit.
  Unrelated changes go in separate commits.
- A follow-up to your own unpushed commit ("fix typo", "wip", "address
  review") is folded into the commit it belongs to, not added as a new one.
- One branch per piece of work that would be reviewed on its own, one pull
  request per branch, merged when its checks pass. Never commit straight to the
  main branch. Do not open a new branch for what is one more commit on an open
  branch of the same topic.
- Commit and push only when the owner asked for it or the task includes it.
- Before committing, read `git status` and the staged diff. Stage only what
  belongs: no scratch files, secrets or generated output.
- The message says why the change was made. Follow the repository's own commit
  rules (format, attribution) over these when they differ.
