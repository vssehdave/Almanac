---
name: egm-handoff
description: Write the plan brief when a plan is approved on leaving plan mode: what the plan-mode chat decided, rejected and left open, for almanac:planner to turn into a file-by-file checklist. Use right after ExitPlanMode is approved.
---

# Plan Brief

Write this brief once, right after the person approves leaving plan mode. It
records what the plan-mode chat taught. It is not a progress report, so do not
say what is done or what is left.

## Where

Write it to the path the hook gave you. If no path was given, ask the person in
chat for one and end your turn.

## Sections

Use these headings, in this order. Each section is short bullets with facts,
not narration.

- **Goal**: what the person wants, in their words.
- **Decided**: each choice, with a one-line reason.
- **Rejected**: options raised and turned down, so they are not brought back.
- **Constraints**: rules, limits, and things never to touch.
- **Open**: still unsettled. The planner must ask, not guess.
- **Known files**: paths the chat already named, each with why it matters.
- **Inputs**: paths of `concepts_brief.md` or a design doc written by
  egm-brainstorm or egm-draft, if any.

## Rules

- Take content only from the chat and the EGM critiques. Invent nothing.
- Mark anything unsure as Open.
- Do not write a file-by-file plan. That is the planner's job.
- Keep the whole brief under one screen.
- Write in plain, direct English.

## After writing

1. Send the path, with the Inputs paths, to `almanac:planner`.
2. Show the person the checklist the planner returns.
3. Pin the brief with `memory_pin_add` only after they approve it.
4. Tick items with `memory_pin_done` only after the lead has checked each
   builder's diff and tests.
