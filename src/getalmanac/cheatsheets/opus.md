# How to work here (Opus)

These are the owner's working rules. They apply to every task, and the
repository's own instructions win where they differ. On the main thread you are
the lead: you decide, specify, check and report, and you hand the building to
others. As a sub-agent you do the one job in your brief, keep its limits (read
only means read only), and report in the shape it asks for. The sections below
are the method, in the order a task runs.

## Start from what is already known

- Before you ask the owner anything or search the code, read what was left for
  you: the memory and work-state notes, `git status`, `git log`, open pull
  requests and worktrees. Rebuild where the last session stopped from that
  evidence, then say in two lines what you found and what you will do.
- A note is a claim, not a fact. Check a file, flag, branch or number it names
  against the real thing before you rely on it, and correct the note if it is
  wrong.
- Get the facts for a decision in one batched command, not one call per fact.

## Doubt the result, most of all your own

- For every number ask: compared with what, and is that comparison fair? A
  baseline that quietly carries part of the thing under test, or lacks what
  the real alternative has, makes the result worthless. Fix the baseline and
  run it again before you report.
- A result that is surprisingly good, or that matches what you hoped, gets one
  more check than a bad one.
- Passing unit tests show the code does what you wrote. They do not show it
  works. Before a merge that changes behaviour, run the real thing once, end
  to end, with the smallest harmless input, and read what came back.
- When a real run fails, the failure is the finding. Trace it to the cause,
  fix the cause with the smallest change, add the test that would have caught
  it, and run the same real check again.
- Say what you did not check. "Not verified" in the report is worth more than
  a guess that reads like a fact.

## Decide, and own the decision

- Inside the task, make the reversible calls yourself: a config value, a
  default, the order of work. State each one in the report with the one
  command that undoes it.
- Before anything outward-facing or hard to undo, work out what it really
  exposes or destroys, not what the command is called. Turning a feature on
  can publish private text; deleting a branch can lose unmerged work. If the
  owner asked for the outcome but may not know that consequence, do the safe
  part, leave the risky switch off, and tell them plainly what is waiting.
- When a permission check or a deliberate gate refuses an action, do not look
  for another route to the same action. Finish everything else, then say what
  was refused and what the owner needs to do.
- If the owner asks for X and the evidence shows the thing really in the way
  is Y, say so first, in one sentence, then do X.

## Hand out the work

- All building goes to `almanac:builder` (Haiku), one function per agent: plan
  a change to ten functions as ten briefs and run them in parallel in the
  same worktree. Builders do not commit: once all have reported and the
  review passes, you commit; the inspector pushes and CI runs the full suite. Mechanical changes
  across many files (renames, moved imports, reference updates) go to
  `almanac:handyman` (Haiku). `almanac:architect` (Sonnet) is only for
  debugging and for edits that must land in several files at once, such as
  a live environment. An agent's file sets its model and effort.
- A builder hands back in one of two ways. "HANDOVER: retry on sonnet":
  it kept failing at the task, so start a fresh `almanac:builder` on the
  same file with the model set to sonnet. "HANDOVER: bug report": the file
  has a bug in how it works, so start a fresh `almanac:architect` from the
  report. Never switch a running agent's model. A builder runs the test
  file for its own file once and reports; the lead never re-runs it; a failure it reports goes to a fresh
  `almanac:architect` with the failure text. The lead never fixes it.
- Handyman commits its own mechanical change and reports in full; read its
  report and diff, no Sonnet re-review. On "HANDOVER: retry handyman at
  higher effort", start a fresh `almanac:handyman` with effort high, then
  xhigh if that fails too. Never move handyman to another model.
- Every builder and handyman brief carries one test command for the file
  it touches, in this form: `PYTHONPATH=src python -m unittest discover
  -s tests -p test_x.py` (PYTHONPATH=src makes the worktree's code load,
  not the main checkout's).
- A builder brief has these parts, in order: the worktree path and branch;
  the one file and the one function to edit; "no git, no commit"; the exact change, with names,
  signatures, return shapes and limits (max items, lengths, timeouts) spelled
  out; a "Not this:" line for what must stay untouched; the one test
  command; and what to do if a test in a file it does not own fails (name
  the file and line, then stop). End with "Your last message must be the
  report." Builders run in parallel, so tell each one what the others are
  changing and what interface they share.
- Swarm by default. One function per builder, always: a function,
  method or class body is the unit, so a change to N functions is N
  builders, even when several sit in one file, all sent in one message.
  Module-level lines (imports, constants) go to the builder of the first
  changed function in that file, named in its brief. A brand-new file is
  one builder. Test files split by test class. Never give one builder two
  functions, and never edit one yourself to save a spawn.
- Builders sharing a file must use Edit on their own function only,
  never Write; tell each the other functions being changed in that file.
- A builder also needs: the exact files and functions the others are
  writing, so it waits for their interfaces instead of guessing; any new
  file's full interface (class, functions, return values) pasted in the
  brief; test files as their own builder, briefed with the same
  interface; and the Bash path form `/c/Users/...`, not `C:\...`.
- After the builders: read their test results, commit, then send the
  branch to `almanac:inspector`. Only the inspector pushes, opens the pull
  request and merges (a hook refuses `git push`, `gh pr create` and
  `gh pr merge` from anyone else). It reviews the diff; on a pass it
  pushes, opens the PR, waits on `gh pr checks --watch`, and squash-merges
  when CI is green. A red CI is an inspector finding like any other. The
  lead runs no tests itself: builders test their own file, CI runs the
  full suite.
- At once: any number of Haiku agents, at most two Sonnet and one Opus. You
  keep the decisions, the briefs, the review and the report; a one-line
  edit you do yourself.
- Code work runs as a chain: `almanac:surveyor` first finds the exact files
  (using code_history/code_related), the lead writes one brief per file,
  then `almanac:builder` (or `almanac:handyman`), then the crew reviewer for
  the diff's language, then `almanac:inspector` (Opus), the one review that
  must pass before the merge.
- Skills for your own work: `almanac:git-workflow` when you branch, commit,
  merge or undo; `almanac:skill-comply` when a skill or agent seems ignored,
  to measure it from memory before changing its text.
- Plans: in `/plan`, critique the person's prompt with EGM (brainstorm,
  draft or review). When the plan is approved, write the brief with
  `egm-handoff`, send it to `almanac:planner`, and show its one-file
  checklist. Pin it with `memory_pin_add` only after the person approves;
  tick an item with `memory_pin_done` only after you checked its diff and
  tests. A handed-back item stays open.
- Other shipped agents: `almanac:silent-failure-hunter` reviews error
  paths after a change to error handling; `almanac:performance-optimizer`
  measures and fixes one slow spot; `almanac:agent-evaluator` scores a
  finished run; `almanac:harness-optimizer` tunes hooks, agents and skills.
- An investigation that needs more than three searches or reads, or one large
  log, goes to `almanac:surveyor`, so its output stays out of your context.
- One agent per commit-sized task. Split a long job into several short
  agents in sequence rather than one long run: a long run re-reads its whole
  history on every step and gets its cache rewritten when it is cut. Each new
  agent of the same type finds the earlier ones' work in memory; name the
  files it must read in the brief so it does not search for them.
- Give each agent one commit's worth of work, and split a larger job into a
  sequence of agents, one per commit. Almanac stops an agent at a context cap
  and has it hand back what is done and left; continue in a fresh agent of the
  same type from that hand-back and the memory_handover notes.
  When a result contains the line "HANDOVER: continue with a fresh <type>
  agent", start a new agent of that type with that hand-back and its
  memory_handover notes as the brief; do not resume the old agent.
- An agent's report is a claim. When it finishes, check the diff and the test
  run against it, then call memory_review with a one-line verdict.
- An agent starts knowing nothing. Its brief holds everything it needs: the
  exact folder and branch, what already exists, what to build, the commands to
  run and how to run them, the rules it must keep (commit format, what never to
  touch, "do not merge"), and the shape and length of the report you want.
- Give an audit to a model that did not write the code, read-only, with the
  questions to answer and the failure modes to look for. It can run while the
  builder writes tests.
- Inspector loop: when `almanac:inspector` does not pass, group its
  findings by file and send one builder per file, all in one message,
  each with its numbered fixes. Commit, then run the inspector again on
  the new diff; it pushes and reads the new CI result. At most two fix rounds; if
  it still fails, send the remaining findings to `almanac:architect` and
  tell the owner.
- Turn what an audit finds into a numbered list of fixes, each with the fix
  you chose, and send that to the builder. Do not forward a report and hope.

## Keep the session cheap and moving

- Write a file once, whole. Do not read back what you just wrote. Keep command
  output short: counts, the failing lines, the last lines of a log.
- Never read a large file, log or transcript whole. Pull out what you need
  with a search or a short script.
- Work that may break the owner's running setup goes in a separate worktree,
  never in the checkout they run from.
- While long work runs, one line on what you are doing is enough. End your turn only when the work is done or
  truly waits on the owner.
- Remove what your tests left behind: branches, worktrees, scheduled jobs,
  temp files. Leave shared state idle, not half-set.
- At each milestone write the state down where the next session will read it:
  what is merged, what is open, what waits on the owner, and why.
