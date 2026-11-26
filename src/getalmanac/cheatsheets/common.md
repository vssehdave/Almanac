## Plan

- Before acting, decide what "done" looks like and how you will prove it.
- If the request can be read two ways and the readings lead to different work,
  ask one question. Otherwise pick the sensible reading, say which in one line,
  and go.
- The rules for scoping and sizing a change are under "Lean changes".
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

## Code history

- For a file's history (when, why, which commit or branch) or what depends on
  it, call code_history / code_related before running git log, git blame or
  grepping for importers. Still read the file itself before editing it.

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
  own work. Report a failure as plainly as a success. A comparison goes in a
  small table.
- Do not stop while work the owner asked for is still owed, and do not ask
  permission for reversible steps inside the task.
- Do ask before anything destructive, irreversible or outward-facing: deleting,
  force-pushing, publishing, sending.

## Commits and branches

- Follow the repository's own commit and branch rules. Where it has none: one
  finished, tested unit per commit, on its own branch, never on main.
