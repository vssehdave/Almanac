# EGM in Claude Code

Start with `/egm:egm-brainstorm`, `/egm:egm-draft` or `/egm:egm-review`.
The other skills (`egm-review-loop`, `egm-resolve`, `egm-bootstrap`,
`egm-drift-report`) are called by these or by name.

## Artifact directory

Write briefs, drafts and reports to the user's working directory, not the
session scratchpad: they must outlive the session. If the prompt or
`CLAUDE.md` has a line `EGM artifact directory: <path>`, write there instead.
Resolve a relative path against the working directory. If the path resolves
outside the working directory (after following symbolic links), ask the user to
confirm before writing there; if they decline or no one can confirm, use the
working directory and say why. Create the folder if it does not exist. Name
the directory in the task you give `egm-synthesizer`, which never sees the
user's prompt.

## Stage order

Review stages 5, 6, 7, 8 and 9 run in sequence. Only reviewers within one stage
run in parallel, at most 6 agents at once. Wait for every reviewer of a stage,
write its report and finish required fixes before the next stage. Uninterrupted
mode skips human checkpoints, not these dependencies.

To request draft changes, edit `design_draft.md` (or add `<!-- -->` comments in
it) or say so in chat; the draft is read again before each revision.

## Reviewer isolation

Spawn a new agent of the registered reviewer type for each independent review,
for example `egm:egm-zoom-out`. Never fork the conversation or resume an earlier
reviewer. Give it only the task, the target, absolute paths to the standards in
`${CLAUDE_PLUGIN_ROOT}/resources/document_standards/` and any supplied
`GoldfishContext`, not your own deliberation.

Reviewers have `Read`, `Grep` and `Glob` only and `omitClaudeMd: true`, so they
see no project instruction files. They cannot open URLs: save a page as a file
in the workspace and pass its path. `egm:egm-synthesizer` may spawn reviewers
and write the report; the main context may also dispatch reviewers directly.
This is context and tool isolation, not filesystem isolation: reviewers can
read any file they can reach.
