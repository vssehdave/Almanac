---
name: orch-change-feature
description: Change an existing working feature to new behavior; use when behavior is not broken but should be different.
---

# orch-change-feature

Thin wrapper over `orch-pipeline`.

## When to use

- An existing feature works, but the desired behavior differs ("change", "adjust", "instead of X do Y").
- Not broken (so not `orch-fix-defect`) and not new (so not `orch-add-feature`).

## Operation settings

- **Default size floor:** small.
- **Phase mask:** 0 -> (1 only if the new behavior needs research) -> light 2 -> 4 -> 5 -> 6.
- **First move (phase 4):** `almanac:tdd-guide` updates the existing tests to the new behavior, then builders change the code until they pass.

## How it works

1. Run the `orch-pipeline` engine with the settings above (agent map, concurrency cap and the two gates are there). Run `playbook_find` first.
2. Keep the plan light; only standard size or larger warrants `almanac:planner`. Trace existing paths with `almanac:surveyor` first.
3. Stop at Gate 1 (plan and changed-test approval) and Gate 2 (pre-commit); `almanac:inspector` runs once, last.
4. Run the safety checks of `almanac:lean-review` if the change touches a security trigger.
