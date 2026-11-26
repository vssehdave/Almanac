---
name: adopt
description: Add any skill or plugin, rewired to use Almanac
---

# Adopting a skill or plugin

Use this whenever the user asks to add, install or adopt a skill, plugin or
agent pack, in any domain.

Adopting means integrating: fetch and scan the original, install a copy into
the user's own Almanac bundle, then make that copy part of one coherent skills
layer that works through Almanac. The copy is the user's. Edit it freely; never
edit the original or anything outside the bundle folder the command prints.

Never run anything you fetched: no install script, no setup command, no hook,
no generated tool. You only read and edit text.

## 1. Fetch and scan

Run `almanac skill add --bundle SOURCE` (an https git URL or a local folder).
Add `--ref SHA` to pin a commit and `--skill NAME` to take one skill out of a
larger repository. Read what it prints.

- **DO_NOT_INSTALL or a failed scan:** stop. Tell the user what was flagged. It
  cannot be overridden.
- **CAUTION:** show the user the issues and ask. Only on a clear yes, run it
  again with `--accept-caution`.
- **SAFE:** it installed. The last lines give the bundle folder path. It also
  lists any hooks (not installed: tell the user and let them decide) and MCP
  servers (kept only when pinned to an exact version; any left out are named).

## 2. Integrate (mandatory)

The install prints an integration report, and the plugin is "integration
pending" until it is clean. Run `almanac skill integrate NAME` to see it again.
Work only inside the bundle folder. Its three kinds of line:

1. **overlap:** read both files. Decide keep, merge or delete: fold the better
   parts into the skill or agent that already existed, then delete the
   duplicate. Never keep two skills for one job.
2. **outside reference / layout:** inline the content a link or
   `${CLAUDE_PLUGIN_ROOT}` path needs, or drop the reference; move agents to
   `agents/NAME.md` and skills to `skills/NAME/SKILL.md`.
3. **Rule conflicts** the report cannot see: where the skill's rules contradict
   an existing skill or how Almanac behaves (memory, playbooks, cheatsheets,
   model routing, the 6-agent cap), the existing rule wins; edit the copy.

Then make each remaining item fit Almanac:

- **Memory and state.** Replace its own notes, journal or state file with
  `memory_search` before re-deriving; finished work is kept automatically.
- **Repeatable procedures.** Point them at `playbook_find` before starting and
  `playbook_save` when a run taught something new.
- **Agents.** Give each a `model:` line (`haiku` for lookup and mechanical
  work, `sonnet` for building, `opus` for planning and review), a "Skills to
  use" section naming adopted skills as `plugin:skill`, and one line that its
  output falls under the Opus review gate (`memory_review` before use).
- **Descriptions.** Almanac lists a skill by the first 60 characters of its
  description: say what it is, then when to use it.
- **The original's own world.** Remove its slash commands, install or update
  steps, marketplace, CLI helper and any outside service it would send data
  to; keep only the local path.
- **Scanner wording.** Rephrase only a flagged line, keeping its meaning.
- **Code.** Leave scripts and generators out or as plain text. If the job
  needs a package, ask first and install only into a virtual environment the
  user names.

Rerun `almanac skill integrate NAME` until it exits 0. It exits 1 while
anything is open, and `almanac doctor` lists plugins still pending.

## 3. Log it

Open `SOURCES.md` in the bundle folder's parent. Find this plugin's row and
extend its "Changed" cell with each edit and each merge or deletion you made, in a few words each. Then
tell the user the plugin loads in the next session started through `almanac`,
and what you changed.

## Updating an adopted skill

Run `almanac skill update`. It changes nothing; it lists adopted skills with a
newer upstream commit. For each newer one: first note the "Changed" cell it
prints, since re-adopting overwrites the copy. Then run the printed command
(that is step 1 above, and the scan rules apply). Redo the edits from the old
Changed cell in step 2, then rerun `almanac skill integrate NAME`, and write the row's Changed cell again in step 3.

## Worked examples

**A pack of specialist agents (`crew`).** 71 agents, each with a long
description and no model. Descriptions cut to one sentence of what the agent
is plus when to use it, a `model:` set per agent by job, a "Skills to use"
section added to each naming skills the user had adopted, and every reference
to the pack's own slash commands removed.
The 17 per-language reviewers and 10 per-language build resolvers were later merged into `code-reviewer`
and `build-error-resolver`: each keeps its old body as `references/<agent>/<language>.md`, and the agent reads the
one that matches the code.

**A skill with an MCP server (`api-anything`).** The server was pinned to an
exact version (`0.4.0`) and its environment set to local-only, because its
default shared what it learned with an outside catalog. The skill text was
changed to say operations are learned and replayed locally only.

**A multi-command plugin (`cli-anything`).** Its five commands became five
skills the model may start. The clone step and URL examples became local paths
only. `pip install -e .` became "ask first, and only into a virtual
environment the user names". Its REPL helper was replaced by a small plain
text description, and two sentences the scanner flagged were reworded with the
same meaning.
