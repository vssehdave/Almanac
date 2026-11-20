## Skills and agents

The skills and agents in your list are part of how you work here, not extras. Use
them on your own judgment, the moment the work calls for one. The owner never
has to type a slash command or name a skill: read the request, decide which
apply, and load them before you start. Say in one line which you used.

- Your own adopted skills and agents are listed with the others: use one when
  its description fits the request.
- To add a skill or plugin the owner names, use the `almanac:adopt` skill.
- Hand a self-contained piece of work to an agent when one fits it. Start its
  prompt with a `Difficulty:` line. Do small things yourself: no agent for a
  job of a few tool calls, and never more than two agents at once.
- A skill that changes a lot without supervision (a clean-up of a whole
  codebase, unattended improvements on a branch): start it only when the owner
  has asked for that outcome in words, and on a clean branch.
- When a note says an Almanac playbook matches the request, follow it, and
  where it has an approved script run that with `playbook_run` and do not redo
  the steps by hand. At the end of a multi-step task that will come up again,
  record it with the `almanac:playbook` skill.
