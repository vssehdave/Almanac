---
name: prompt-optimizer
description: Rewrite a draft prompt with intent, scope and missing context filled in; use when the owner asks to improve a prompt, not to run the task.
---

# Prompt Optimizer

Analyze a draft prompt, critique it, match it to the agents and skills that are
installed, and output a complete optimized prompt the owner can paste and run.

## When to Use

- The owner says "optimize this prompt", "improve my prompt" or "rewrite this prompt"
- The owner pastes a draft prompt and asks for feedback or enhancement
- The owner says "I don't know how to prompt for this"

### Do Not Use When

- The owner wants the task done directly (just execute it)
- The request is to optimize code or performance (refactoring, not prompting)
- The owner asks for a skill inventory

## How It Works

**Advisory only. Do not execute the owner's task.** Write no code, create no
files, run no commands. The only output is an analysis plus an optimized prompt.
If the owner says "just do it", say this skill only produces prompts and ask
for a normal task request.

Run these phases in order and present the result in the Output Format below.

### Phase 0: Project detection

1. Run `memory_search` for earlier prompts and decisions on this topic.
2. Read the project's `CLAUDE.md` if present, for conventions.
3. Detect the tech stack from project files (`package.json`, `go.mod`,
   `pyproject.toml`, `Cargo.toml`, `pom.xml`, `*.csproj`, ...). If nothing is
   found, say so; do not guess.

### Phase 1: Intent detection

Classify the task: new feature, bug fix, refactor, research, testing, review,
documentation, infrastructure or design.

### Phase 2: Scope assessment

Use codebase size when a project was detected; otherwise estimate from the
prompt and mark it uncertain.

| Scope | Heuristic | Route |
|-------|-----------|-------|
| TRIVIAL | one file, under 50 lines | direct edit |
| LOW | one component or module | one builder brief |
| MEDIUM | several components, one domain | `almanac:planner`, then builders |
| HIGH | cross-domain, 5+ files | `orch-add-feature` (or the matching orch skill) |
| EPIC | multi-session, multi-PR | `orch-build-mvp` or a written multi-session plan |

### Phase 3: Component matching

Map intent and scope to what is installed:

| Intent | Agents | Skills |
|--------|--------|--------|
| New feature | `almanac:planner`, `almanac:tdd-guide`, `almanac:builder` | `orch-add-feature`, `verification-loop` |
| Bug fix | `almanac:tdd-guide`, `almanac:architect` | `orch-fix-defect` |
| Refactor | `almanac:handyman`, `almanac:builder` | `orch-refine-code` |
| Research | `almanac:surveyor` | `search-first`, `iterative-retrieval` |
| Testing | `almanac:tdd-guide` | `python-testing` |
| Review | `almanac:lean-review`, `almanac:inspector` | its safety checks |

Add language skills by stack (for example `python-testing` for Python) and
`almanac:lean-review` on the diff.

### Phase 4: Missing context detection

Mark each item as auto-detected or needed from the owner: tech stack, target
scope (files or modules), acceptance criteria, error handling, security
requirements, testing expectations, performance limits, UI needs (if frontend),
database changes (if data layer), existing patterns to follow, and what NOT to
do. If 3 or more critical items are missing, ask up to 3 questions before
writing the optimized prompt, then fold the answers in.

### Phase 5: Workflow and model

Place the task in the lifecycle: Research, Plan, Implement (TDD), Review,
Verify, Commit. For MEDIUM and above, plan first. Recommend a model by scope:
Haiku or Sonnet for TRIVIAL to MEDIUM, Opus for planning on HIGH and EPIC. For
HIGH and EPIC, split into sequential prompts: research and plan; one phase per
prompt, each ending with `verification-loop`; a final integration check.

## Output Format

Answer in the owner's language, in this structure.

1. **Prompt diagnosis.** Strengths; a table of issues (issue, impact, fix);
   questions needing an answer (state what was auto-detected instead of asking).
2. **Recommended components.** A table of type (agent, skill, model), name and
   purpose.
3. **Optimized prompt, full version.** One fenced code block, self-contained:
   task with context, tech stack, agents and skills at the right stages,
   acceptance criteria, verification steps, scope boundaries.
4. **Optimized prompt, quick version.** A compact form by intent, for example:
   "Send `almanac:planner` for [feature]. `almanac:tdd-guide` for tests, then
   builders. `verification-loop`."
5. **Enhancement rationale.** A table of what was added and why.

Footer: "Not what you need? Tell me what to adjust, or make a normal task request
if you want execution instead of prompt optimization."

## Example

Draft: "add login to the app". Diagnosis: no stack, no acceptance criteria, no
scope boundary, and login touches a security trigger. The rewrite names the
detected stack, lists acceptance criteria (valid login, invalid login, lockout),
routes through `orch-add-feature`, calls for the safety checks of `almanac:lean-review`, and says
what is out of scope (password reset, social login).

## Related

- `almanac:planner` and `orch-pipeline` for the work the prompt will start.
- `playbook_find` for a recorded way of doing the task; `playbook_save` to keep one.
