---
name: orch-add-feature
description: Build a brand-new feature through the gated pipeline; use when the capability does not exist yet.
---

# orch-add-feature

Thin wrapper over `orch-pipeline`.

## When to use

- The owner wants a capability that does not exist yet ("add", "build", "support ...").
- Not a correction (`orch-fix-defect`) and not a change to existing behavior (`orch-change-feature`).

## Operation settings

- **Default size floor:** standard; run research and plan unless clearly small.
- **Phase mask:** 0 -> 1 -> 2 -> 4 -> 5 -> 6 (skip 3, which is MVP-only).
- **First move (phase 4):** `almanac:tdd-guide` writes new failing tests for the new behavior, then builders implement to green.

## How it works

1. Run the `orch-pipeline` engine with the settings above (agent map, concurrency cap and the two gates are there). Run `playbook_find` first.
2. Classify size first; small features collapse toward 4 -> 5 -> 6.
3. Research is `memory_search`, then `almanac:surveyor`; plan is `almanac:planner`.
4. Stop at Gate 1 (plan approval) and Gate 2 (pre-commit); `almanac:inspector` runs once, last.
5. Load crew:security-review if the feature touches a security trigger.
