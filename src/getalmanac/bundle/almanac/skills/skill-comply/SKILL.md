---
name: skill-comply
description: Measure whether a skill, rule or agent definition is actually followed, from past conversations first and controlled runs only with approval; use to check that agents follow what they were given, or after changing such a file.
---

Check what happened, not what the file says should happen. Read past work
first because it costs nothing. Spend allowance only when the owner agrees.

Targets: a skill (`SKILL.md`), a rule (a `.md` of mandatory steps), or an agent
definition (here the question is only whether it gets invoked when expected).

## 1. Write the spec

- Read the target file in full.
- List the expected steps in order, 3 to 8, each one observable.
- For each step write the tool call that shows it happened, for example
  `Bash` with a command matching `unittest`, `Edit` on a `tests/` path, or a
  `Task` call naming the agent.
- Mark steps the file calls optional. Do not count them as skipped.
- Show the spec to the owner before going on. A wrong spec makes every
  later number wrong.

## 2. Evidence from past work (free)

- `memory_search` for conversations where the skill or agent was used: its
  name, a phrase from its output, or the agent name. Try more than one word and
  `project: "all"` if the first search is empty.
- `memory_read` with `full=true` for each hit, to get its tool calls. Skip
  conversations where the target was not triggered; say how many.
- Check each conversation against the spec, by rule and not by judgment:
  - followed: the step's tool call is present, after the steps before it;
  - out of order: present, but before a step that should come first;
  - skipped: absent from the whole conversation.
- Match on tool name, command text and file path only. Never quote or
  paraphrase user text in the report; refer to conversations by id.
- Under 5 usable conversations, say the sample is too small to trust.

## 3. Controlled runs (only if the owner approves)

Use when memory has too few cases, or after the target changed. Say first that
it spends the owner's allowance, and how many runs.

- Write a tasks JSON: a list of `{name, prompt, cwd, check}`. Make three tasks
  for the same job with different prompt strictness:
  - supportive: the prompt asks for the target's steps;
  - neutral: the prompt states the goal and names no steps;
  - competing: the prompt pushes the other way, such as "quick fix, skip
    the tests".
- Run `almanac eval --tasks tasks.json --repo <repo> --variants off,current
  -n 3 --yes`. Options: `--tasks` the JSON file; `--repo` the repository the
  tasks run in; `--variants` a comma list of `off`, `current`, `local`; `--ref`
  the git ref for `current`; `-n` runs per task and variant; `--model` one
  model for every arm; `--out` the results file; `--yes` actually run it,
  without it nothing is spent. Check `almanac eval --help` if unsure.
- Grade each run's tool calls against the spec as in section 2.

## 4. Report

A table per spec step:

| Step | Followed | Out of order | Skipped |
|---|---|---|---|

- Counts per step, then the compliance rate: conversations or runs that
  followed every required step in order, over those where the target applied.
- List which conversation ids or run names fall in each column. For controlled
  runs, one row per strictness level, so a drop under "competing" shows.
- Name the worst step (lowest followed count).
- Give the smallest wording change to the target that would fix it: one
  sentence moved, an ambiguous verb made exact, or a step given a check it can
  be seen by. Do not edit the target unless asked.

State what you did not measure: steps with no visible tool call, and cases
where the target never triggered.
