---
name: adopt
description: Add any skill or plugin, rewired to use Almanac
---

# Adopting a skill or plugin

Use this whenever the user asks to add, install or adopt a skill, plugin or
agent pack, in any domain.

Adopting means three things in order: fetch and scan the original, install a
copy into the user's own Almanac bundle, then rewire that copy so it works
through Almanac instead of keeping its own notes and state. The copy is the
user's. Edit it freely; never edit the original or anything outside the bundle
folder the command prints.

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

## 2. Rewire the installed copy

Work only inside that folder. Make each of these edits where it applies.

1. **Memory and state.** Where the skill keeps its own notes, memory, journal,
   progress file or state file, replace that with Almanac memory: before
   re-deriving something, call `memory_search`; finished work is kept
   automatically, so delete steps that save it by hand.
2. **Repeatable procedures.** Where the skill describes a procedure that will
   recur, point it at playbooks: `playbook_find` before starting, and
   `playbook_save` when a run taught something new.
3. **Agents.** Give every shipped agent a `model:` line (`haiku` for lookup and
   mechanical work, `sonnet` for building, `opus` for planning and review) and
   a "Skills to use" section naming the user's adopted skills that fit its
   job, as `plugin:skill`. Add one line that its output falls under the Opus
   review gate: the main Opus session checks it and calls `memory_review`
   before using it.
4. **Descriptions.** Almanac lists a skill or agent by the first 60 characters
   of its description, so make the first sentence say what it is, then when to
   use it.
5. **The original's own world.** Remove references to its slash commands,
   install or update steps, marketplace, CLI helper, and any outside service
   the skill would send data to (a catalog, a telemetry or sync endpoint).
   Where an outside service is optional, make the local-only path the only one.
6. **Scanner wording.** If the scan flagged a line, rephrase only that line,
   keeping its meaning. Do not delete content to pass a scan.
7. **Anything that runs code.** Leave scripts, templates and generators out of
   the copy or as plain reference text. If the skill's job needs a package
   installed, make it ask the user first and install only into a virtual
   environment the user names.

## 3. Log it

Open `SOURCES.md` in the bundle folder's parent. Find this plugin's row and
extend its "Changed" cell with each edit you made, in a few words each. Then
tell the user the plugin loads in the next session started through `almanac`,
and what you changed.

## Worked examples

**A pack of specialist agents (`crew`).** 71 agents, each with a long
description and no model. Descriptions cut to one sentence of what the agent
is plus when to use it, a `model:` set per agent by job, a "Skills to use"
section added to each naming skills the user had adopted, and every reference
to the pack's own slash commands removed.

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
