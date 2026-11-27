---
name: orch-refine-code
description: Restructure code without changing behavior; use when structure should improve but behavior must stay the same.
---

# orch-refine-code

Thin wrapper over `orch-pipeline`.

## When to use

- Same behavior, better structure: extract modules, remove duplication, remove dead code, rename for clarity.
- If behavior is meant to change at all, use `orch-change-feature` or `orch-fix-defect`.

## Operation settings

- **Default size floor:** standard.
- **Phase mask:** 0 -> 2 (plan the restructure) -> 4 (keep green) -> 5 -> 6. No new behavior tests; the existing suite is the safety net.
- **First move (phase 4):** confirm the relevant tests exist and are green before touching code; if coverage is thin, add characterization tests first (`almanac:tdd-guide`). Then restructure in small steps, re-running tests after each.

## How it works

1. Run the `orch-pipeline` engine with the settings above (agent map, concurrency cap and the two gates are there). Run `playbook_find` first.
2. Mechanical moves and renames across files go to `almanac:handyman`; extractions to `almanac:builder` as two briefs (new function, then its caller).
3. Stop at Gate 1 (restructure plan) and Gate 2 (pre-commit); `almanac:inspector` runs once, last.
4. Commit as `refactor:`; the diff must be behavior-neutral.
