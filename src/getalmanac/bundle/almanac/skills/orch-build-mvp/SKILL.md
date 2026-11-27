---
name: orch-build-mvp
description: Bootstrap a working MVP from a spec document; use when a design or spec must become a running MVP in thin slices.
---

# orch-build-mvp

Thin wrapper over `orch-pipeline`.

## When to use

- The owner has a design or spec document (SDD, PRD) and wants a working vertical slice built from it. The argument is the document path.

## Operation settings

- **Default size floor:** large; the full pipeline including scaffold.
- **Phase mask:** 0 (read the spec) -> 1 -> 2 (heavy) -> 3 (scaffold) -> 4 -> 5 -> 6.
- **First move (phase 0 -> 2):** read the document; extract scope, locked decisions and the feature list; order it into thin vertical slices (one end-to-end path first, not all models then all views). Phase 3 builds that first slice.

## How it works

1. Run the `orch-pipeline` engine with the settings above (agent map, concurrency cap and the two gates are there). Run `playbook_find` first.
2. Plan with `almanac:planner`: one checklist of one-file builder briefs per slice.
3. Build each slice test-first: `almanac:tdd-guide` for the tests, `almanac:builder` per file. After each slice, run the document's acceptance checks with the slice's test command; a failure goes back to a builder, or to `almanac:architect` if the cause is unclear. Repeat until the slice passes or stops improving, then report to the owner.
4. Stop at Gate 1 (slice plan) and Gate 2 (pre-commit). Commit the scaffold and each slice as separate `feat:` commits; `almanac:inspector` runs once, last, on the final committed change.
5. Run the safety checks of `almanac:lean-review` for any slice touching a security trigger.
