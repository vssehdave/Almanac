---
name: orch-fix-defect
description: Fix a bug with a failing regression test first; use when existing behavior is broken or wrong.
---

# orch-fix-defect

Thin wrapper over `orch-pipeline`.

## When to use

- Something is broken: wrong output, an error, a crash, a regression.
- Correct behavior that should differ is `orch-change-feature`; a missing capability is `orch-add-feature`.

## Operation settings

- **Default size floor:** small, often trivial.
- **Phase mask:** 0 -> (light 2 only if the root cause is unclear or the size is standard or larger) -> 4 -> 5 -> 6. Research is usually skipped.
- **First move (phase 4):** `almanac:tdd-guide` reproduces the bug as a new failing test; then fix until green.

## How it works

1. Run the `orch-pipeline` engine with the settings above (agent map, concurrency cap and the two gates are there). Run `playbook_find` first.
2. If the root cause is unclear, `memory_search` then `almanac:surveyor` before the red test. Debugging, and any bug report a builder hands back, goes to `almanac:architect`.
3. Stop at Gate 1 (only if a plan was produced) and Gate 2 (pre-commit); the owner runs `/inspect` once, last. Commit as `fix:`.
4. Run the safety checks of `almanac:lean-review` if the defect sits in a security-sensitive path.
5. After the fix is verified (the failing test now passes), call `memory_runbook_add` with the error line, the symptom, the cause and the fix.
