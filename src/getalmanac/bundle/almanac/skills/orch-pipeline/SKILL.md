---
name: orch-pipeline
description: Gated research, plan, TDD, review, commit pipeline behind the orch-* skills; use when running or tuning an orch-* operation.
---

# Orchestrator Pipeline (shared engine)

The `orch-*` skills are thin wrappers. They do not do the work: they classify the
request, choose which phases of this pipeline run, and hand each phase to an
Almanac agent. This file is that pipeline. Start from an operation skill
(`orch-add-feature`, `orch-fix-defect`, ...) rather than from here.

Before starting, run `playbook_find` for the task; when a run taught something
new, record it with `playbook_save`. Use `memory_search` before re-deriving
earlier findings.

## The operation family

| Skill | Operation | Trigger | First move |
|-------|-----------|---------|------------|
| `orch-add-feature` | feature | capability does not exist yet | research + plan a new slice |
| `orch-change-feature` | tweak | works, but desired behavior differs | amend existing behavior and its tests |
| `orch-fix-defect` | fix | broken; behavior is wrong | reproduce as a failing test, then fix |
| `orch-refine-code` | refactor | behavior stays, structure improves | restructure while keeping tests green |
| `orch-build-mvp` | mvp | bootstrap from a design or spec document | read the document, cut vertical slices |

## Step 0: classify size

Ceremony scales to blast radius. Score the request on three signals, take the
highest tier any signal reaches, and state it in one line so the owner can
override:

| Tier | Files touched | New dependency / contract | Design ambiguity | Phases that run |
|------|---------------|---------------------------|------------------|-----------------|
| trivial | 1, a few lines | none | none | 4 -> 5 -> 6 |
| small | 1 file / 1 function | none | clear once you read the code | (1 light) -> 4 -> 5 -> 6 |
| standard | 2-5 files | maybe a new internal module | one real choice | 1 -> 2 -> 4 -> 5 -> 6 |
| large | many / cross-cutting | new external dependency, public API, or a spec document | several open questions | 1 -> 2 -> (3) -> 4 -> 5 -> 6 |

Phase 0 (intake) always runs. Anything touching a security trigger (below) or a
public API is at least standard, whatever the file count.

## The phases

Each phase hands off; none is done inline by the lead.

- **0. Intake.** Restate the request. For `orch-build-mvp`, read the spec and
  extract scope, locked decisions and a feature list.
- **1. Research.** Run `memory_search` first, then send `almanac:surveyor` to
  trace the existing code. Use the `search-first` skill when a new dependency
  or utility is in play.
- **2. Plan.** Send `almanac:planner`. Its output is a checklist of builder
  briefs, one file per item (`path: the change; test: command`), in build
  order. -> GATE 1.
- **3. Scaffold.** `orch-build-mvp` only: build the first end-to-end slice the
  same way as phase 4.
- **4. Implement (TDD).** Write the failing test first with `almanac:tdd-guide`,
  then build to green: one `almanac:builder` per file (HANDOVER rules as in
  `agents/builder.md`: "HANDOVER: retry on sonnet" or "HANDOVER: bug report").
  Mechanical multi-file changes go to `almanac:handyman`. Debugging, a bug
  report a builder handed back, or edits that must land in several files at
  once go to `almanac:architect`.
- **5. Review.** `almanac:lean-review` on the diff. If the diff
  touches a security trigger, weigh its safety checks first.
- **6. Commit and gate.** Conventional commits, one per logical chunk. -> GATE 2.
  Then send `almanac:inspector` once, last, on the committed change. It pushes
  and opens the PR.

## Concurrency cap

Any number of Haiku agents (builder, handyman, surveyor), at most two Sonnet
(architect, tdd-guide, planner), one Opus (inspector). Never more than 6
sub-agents at once.

## The two gates

The family is gated, not autonomous:

1. **GATE 1, after Plan.** Show the checklist; write no implementation until the
   owner approves it.
2. **GATE 2, before Commit.** Show the diff summary and the proposed messages;
   do not commit until the owner confirms.

Everything between the gates flows without stopping. Output of the Opus agent
falls under the Opus review gate (`memory_review` before use).

## Security trigger

Run the safety checks of `almanac:lean-review` when the diff touches authentication or
authorization, user-input handling, database queries, file-system paths,
external API calls, cryptography, or secrets.

## Verification

- the size tier was stated and matched the work
- both gates were honored
- the inspector ran once, last, on the committed change
- new or changed behavior has tests that ran
- commits are conventional and scoped to one logical change
