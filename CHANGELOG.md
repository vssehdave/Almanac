# Changelog

## Unreleased

- **The daily summary budget no longer loses counts.** The routing classifier, live
  and idle summaries and the imported-summary job each read the day's count, added to
  it and wrote it back, so calls that finished out of order (or on the importer's own
  connection) overwrote each other. Reserving a call and adding its tokens are now
  single atomic updates in the database.

- **A cold return is compacted.** A request that comes back after its cache has
  expired rewrites the whole prefix whatever Almanac sends, so with
  `compaction.on_cold` (on by default) it is compacted from twice
  `compaction.target_tokens` instead of the trigger size: by the stored prefix on
  the main thread at a human turn, by the stubs inside a long turn and in a
  sub-agent. The request log records `cold` (estimated tokens before and after).

- **`almanac status` shows raw usage, not a weighted total.** The weighted figure used
  API list-price ratios, which are not a plan's accounting, so it is gone from `status`
  and from `almanac report --json`. Each period (5 hours, 24 hours) shows input, cache
  write, cache read and output tokens, and for cold returns: how many, the cache tokens
  they wrote, that as a share of the period's cache writes plus input, how many
  `compaction.on_cold` compacted, and the estimated tokens that saved.
- **Auto-import and its summary pass use the login Almanac runs with.** They read
  `$CLAUDE_CONFIG_DIR` when it is set (for example a work account), else `~/.claude`,
  instead of always `~/.claude`. So `almanac status` no longer reports "no valid login"
  for someone who runs only the second account. A scratch or eval `ALMANAC_HOME` still
  never imports.

- **Playbooks are their own package.** The playbook code (folder format, approval and
  signing, matching, route offers, includes and `RESULT`/`RESUME`, dedupe, the miner and
  the compile step) moved to Playbook (`getplaybook`, a dependency now), which also works
  without Almanac as a command and a Claude Code plugin. Almanac keeps the proxy side:
  the offer layer (`playbook_layer.py`), the prompt library's candidates for the miner,
  and `almanac playbook ...`, which now runs Playbook's command on `~/.almanac/playbooks`
  with the same `playbooks.*` settings, so existing playbooks, approvals and automatic
  entries keep working. The `playbook_*` tools are served by Playbook's MCP server (added
  to the MCP config next to the memory server) and the bundled `playbook` skill is
  Playbook's plugin. Claude Code now also gets `PLAYBOOK_HOME` and `PLAYBOOK_CONFIG`, and
  `ALMANAC_PROXY` makes the plugin's hooks stay silent. New setting `playbooks.skip_folders`.

- **Playbooks are offered from what a thread is doing, can include each other, and
  report one clear result.** Typed words rarely say what the work is ("continue"),
  so on each request of the main thread and of sub-agents Almanac now compares the
  route so far (the phases of the tool calls), the assistant's own narration and,
  for a route that ships, whether the repo has commits to push, and offers an
  approved playbook as one short note at the end of the newest message when two of
  the three agree. The decision is stored per message (a retry sends the same
  bytes) and `requests.jsonl` records `playbook_offer`, `signals` and
  `playbook_taken`. New playbook fields: `route`, `agent_type` (a playbook of a
  sub-agent type is offered only to that type, and `""` only to the main thread)
  and `includes` (playbooks run first, in order, stopping at the first failure;
  `playbook_run` takes `from` to go on from a step, and a failure ends with a
  `RESUME:` line). `playbook_run` now leads with `RESULT: pass` or
  `RESULT: fail (exit N)` and shows only the failing part (failing test names
  and tracebacks for unittest and pytest). The miner reads sub-agent transcripts,
  finds test-loop, poll and inspect chains and offers them as small playbooks the
  bigger ones include. A playbook that would repeat a stored route adds its triggers
  to it instead, and `almanac playbook dedupe` merges the ones already there.
  Behaviour change: the reply of `playbook_run` has a new shape (no `exit code`
  line, no separate stderr section).

- **Skills are adopted, not shipped.** The package no longer carries third-party
  skills or agents, and `pack.json` is no longer shipped (`almanac pack` reads
  `<almanac home>/pack.json`). Sessions load the package's own `almanac` plugin plus
  every plugin folder in `<almanac home>/bundle/`, one `--plugin-dir` each.
  `almanac skill add --bundle SOURCE [--ref SHA] [--skill NAME] [--name FOLDER]`
  fetches through the same quarantine and scan as `skill add` (DO_NOT_INSTALL
  refused; CAUTION needs `--accept-caution` or a yes at the terminal; nothing
  fetched is run), then installs skills and agents as plain text into that folder
  with a manifest, the licence file, the `disable-model-invocation` flag removed,
  hooks listed but never installed, MCP servers kept only when pinned to an exact
  version, and a row in `bundle/SOURCES.md`. A new `adopt` skill in the package
  plugin then rewires the copy to Almanac memory and playbooks. The launch check
  now also watches the bundle folders. `almanac skill import-bundle PATH` carries
  an older bundle over once. Behaviour changes: the skills cheatsheet no longer
  names the old bundled skills (an install that wants that text keeps it in its
  own `cheatsheet path` copy), and `skill add` asks at a terminal before
  refusing a CAUTION verdict.

- **The hand-off branch is sealed, and the watchdog stops costing usage when idle.**
  `cloud/handoff` now holds one file, `almanac-handoff.sealed`: the packet, the
  record and your uncommitted changes (a binary diff against the parent commit),
  encrypted and authenticated with the hand-off key (HMAC-SHA256 stream, standard
  library only). Nothing readable is on the branch. The watchdog prompt opens it
  with one Python check (tag first, then decrypt, signed time, parent commit, diff
  applies), then branches from the parent and applies the diff. `ALMANAC_HANDOFF.md`
  and `almanac-handoff.json` are gone. **An older branch from before this holds
  readable files: delete it yourself** (`git push origin --delete cloud/handoff`);
  Almanac deletes nothing. Unpushed local commits are still ordinary commits in the
  branch's history. New settings: `cloud.allow_public` (default off: no push when
  `gh` says the repository is public), and `cloud.routine` (the scheduled task's id).
  With a routine set, Almanac always enables it only while a hand-off is live and
  its login has usage left, and disables it otherwise, so it never runs on a
  depleted subscription. `almanac cloud routine check` reads the routine and prints
  its status, enabled flag and next run; the endpoint it uses is unverified. Update
  the watchdog prompt of an existing scheduled task from `almanac cloud watchdog`.

- **One record per finished exchange.** The prompt library row is now written for
  every exchange that made a tool call (sub-agent exchanges too) and also holds the
  distinct tool names and the call count. `library.min_tools` became a read-time
  filter: searches, lists, merges and miner candidates show only rows with that many
  calls, never a sub-agent's. What a project keeps using is read from these rows
  (`library.recently_used`), so the separate `used` table is gone: an older database
  is migrated on first open (one row per lane, shown nowhere, with its tools and
  skills) and the table dropped. Agent types now count too: one that two of the
  project's last ten conversations called keeps its full line in the agent listing.
  Behaviour changes: usage is counted per finished exchange, not per request; with
  memory off nothing is recorded, so only `always_full` pins; forgetting a
  conversation forgets its usage.

- **Sub-agents recall the project, by agent type, with a review status.** A new sub-agent
  now gets the project recall note (earlier conversations and earlier sub-agents
  alike, on its own prompt) next to its pointer to the parent, frozen with it so
  the text stays the same on every request. Sub-agent conversations store their
  agent type, and recall lists work of the same type first. They also store a
  review status: `pending` (shown as "(unreviewed)" in recall and search) until an
  Opus model calls the new `memory_review` tool; the proxy, which sees the model,
  files the verdict, so a call from any other model changes nothing. New convo
  columns `agent_type`, `review` and `review_note` are added to older databases.

- **A clean profile for `almanac` sessions, and `almanac sandbox`.** `almanac` now
  starts Claude Code with your settings minus `profile.off_plugins` (claude-mem,
  Logbook, Pocketbook), the hooks matching `profile.off_hooks` (`getlogbook`) and the
  user skills in `profile.off_skills` (`logbook`): `--setting-sources project,local`
  plus a generated `--settings` file, with your skills, agents, commands and
  `CLAUDE.md` put back through `--add-dir` (as the profile folder's own `CLAUDE.md`, so sub-agents get it). A new
  proxy layer strips the same plugins' skills, agents, MCP tools, server instructions
  and hook text from any request as a second line. `almanac sandbox` runs plain
  Claude Code in `~/.claude-sandbox` with only the plugins you name.
- **Names-only skill listing.** `skills.stub_chars` 0 now lists `- name` with no
  description and no trailing separator (it used to keep a short description
  whole). The default is 60 characters, cut at a word.
- **Lean changes are native.** The main thread and every sub-agent, on any
  model, get Almanac's own minimal-change rules as the first part of the
  cheatsheet text (`startup.lean_rules`, `lean_level`, `lean_rules_text`), the same
  bytes on every request. The overlapping bullets were removed from `common.md`.
  `profile.off_hook_text` (empty by default; add the banner's own name) cuts the mode banner hooks of a plugin that stays
  installed, and a plugin added to `profile.off_plugins` now also drops its
  user-settings hooks by name.

- **Memory: complete without a command, finer records, a fuller recall note.**
  While the proxy runs, a background thread imports new or grown Claude Code
  transcripts from `~/.claude/projects` (never any other config folder) about
  30 seconds after start and then once an hour: sessions at most
  `memory.auto_import_days` (14) old, not written to for an hour, for projects
  the store already holds. It then runs the summary pass (personal login,
  within `memory.summary_daily_calls`) and makes a vector for every exchange and
  fact that lacks one, with the local model (no API tokens; it says so when no
  model is installed). `memory.auto_import` (default on) turns it off. It also stays off when `ALMANAC_HOME` points anywhere but `~/.almanac`, unless that home's `config.json` sets `memory.auto_import` to `true`, so a scratch copy of the store never imports real transcripts. The
  summary call now also returns up to 5 short facts per exchange (about 170
  more output tokens), with hidden thinking off and a 1,500-token cap so no
  reply is cut off; each is a searchable row (words and meaning) linked to
  its exchange, shown as the hit's snippet. `almanac memory summarize --facts`
  adds facts to exchanges summarized before. The recall note keeps its
  `memory.recall_chars` budget and now lists the standing decisions and open
  work of the project's recent conversations first, then related hits.
  `almanac status` shows the last import round.

- **Memory: sub-agents know their parent conversation, and their work is
  indexed.** A sub-agent's first message gets one frozen line naming the
  conversation it belongs to and the call `memory_search {"conversation": id}`
  (same bytes on every request of the sub-agent, so the cache holds; forks get
  nothing). When a sub-agent ends, its exchange is stored through the usual
  path (cleaning, redaction, status rules) as a conversation linked to its
  parent; it never shows as open work and is not listed as a conversation.
  `memory_search` gains a validated `conversation` filter, and `query` is
  optional with it (the conversation's exchanges are listed).
  A sub-agent that ends by calling `SubagentHandback` is stored too, with its
  report as the answer. The parent is found from the main thread's lane, saved
  before its reply is stored, so a sub-agent started on the first turn still
  finds it, and one started after `/clear` is filed under the new conversation.
  Sub-agent prompts are never listed or recalled as the person's own words;
  search hits from them say "sub-agent of conversation N".

- **Routing: a failure ladder, an agent model floor, effort-aware pricing.** A
  thread whose tool calls keep failing climbs one rung of its starting model's
  ladder (`routing.ladders`): Haiku medium, Sonnet low, Sonnet medium; Sonnet
  medium, Opus low, Opus high; Opus low, Opus high. This holds for the main thread
  and, new, for a sub-agent judged on its own history; forks stay on their
  parent's model and `routing.ceiling` caps every ladder. In `subagents` mode the
  main thread's model is untouched but its effort now follows its model's
  ladder. A sub-agent is never routed below the model it starts on, whether
  its definition asked for it or it inherited it (a pin still wins);
  `routing.route_inherited` (default false) lets an inherited model be routed down.
  Saying an earlier attempt failed takes the next rung of the ladder, not the
  top tier. In `subagents` mode effort is written only on a raise and never
  lowers the one Claude Code sent. A malformed `routing.ladders`, `effort` or
  `effort_cost` in `config.json` falls back to the default. **Default `routing.effort` is now medium, medium, low**
  (was low, medium, kept), so Opus requests that carry an effort are sent at low.
  `routing.effort_cost` scales the reply cost, as an estimate, when a stay is
  compared with a move.

- **`almanac memory import` stores past Claude Code conversations.** It reads
  the transcripts in `~/.claude/projects` and stores them through the same code
  the proxy uses (cleaning, redaction, status tags, search). The project is
  chosen for each exchange from the files its tool calls touch, `cd` and
  `git -C` folders and worktrees (a deleted worktree is matched to its repository
  by name), and a session that changes project becomes one conversation per
  project. Stored dates are the transcript's, and an exchange older than 14 days
  is never `open` (this also holds when a model summary later sets a status).
  Importing again stores nothing new; `--dry-run` prints per-project counts, status
  counts and pending summaries without writing. No model calls are made: imported
  exchanges keep rule-written summaries, because the summary worker only serves the
  live conversation and the command never reads a login.

- **Security fix: follow-ups to the hand-off, typed-text and redaction fixes.**
  The takeover branch is now named by the first 12 characters of the hand-off
  signature, not the commit SHA, so an old signed snapshot pushed again (a new
  commit, the same signature) is seen as already taken over; **re-create the
  scheduled task from `almanac cloud watchdog`**. The check removes the two
  hand-off files from its temporary index with `update-index --force-remove`,
  so a checkout that tracks its own `ALMANAC_HANDOFF.md` no longer makes it
  fail. A custom or plugin slash command's body (the one text block after a
  command block, unless it is the command's output) no longer counts as typed.
  Redaction: a bare value counts as code only when the whole value is a call
  or subscript (`get_key()`, `keys[name]`), so `JWT_SECRET=s3cr3t(xyz)abc123`
  is removed again; a `*_KEY` value of 16+ characters with no digit that is
  not an identifier, and dashed licence keys, are removed; `Pwd=` keeps only
  `$(`, `${`, `$NAME`, `./`, `../`, `~/`, `/` and drive paths as code; and a
  `|` or `;` inside a quoted `curl` argument no longer hides `-u user:pass`.
  Limit of the replay fix: an older heartbeat that was never taken over still
  passes, because its signed time is only checked against the clock, not
  against earlier takeovers.

- **Prompt library entries become playbook candidates.** `almanac playbook
  mine` now also lists a current library entry that was used at least twice
  and whose route is two or more commands and nothing else (`Bash:` steps; a
  skill, an edit, a read, an agent or any other tool means it needs judgment). Entries
  with the same route make one candidate, and one is left out when the
  transcript mining already found the same steps, a playbook's triggers
  already match it, or it was already compiled. They go through the same
  compile step as before and stay drafts until you approve them.
- **Security fix: smaller hardening items.** Git's error text (it can echo a
  remote URL with its password) and library errors are redacted before they are
  raised or logged; the compaction summary is cleaned (tags and secrets) before
  it is stored; and a playbook file that is a symbolic link is no longer written
  through.
- **Security fix: the cloud watchdog checks a signature.** It used to act on
  any commit on `cloud/handoff`, so anyone who could push there could give the
  cloud task its instructions. `almanac-handoff.json` now carries an HMAC-SHA256
  over its fields and the packet text, made with a secret from the OS keyring
  (if the `keyring` module is installed) or the file `cloud.key` in the Almanac
  home. `almanac cloud watchdog` prints the line to put in the cloud task's
  environment as `ALMANAC_HANDOFF_KEY`, and the watchdog prompt now verifies
  the signature first and stops if it does not match: **set the variable and
  update the scheduled task**. The packet also carries only rule-written
  titles and answers, no model summaries, which are built from tool output.
  The signature also covers the snapshot's tree hash (without the two hand-off
  files) and its time: the watchdog recomputes the tree, measures the age from
  the signed time and rejects a time more than 5 minutes ahead. An unset or
  empty key fails the check, and nothing is ever signed with an empty key.
- **Security fix: more secrets are removed.** PGP private key blocks, PuTTY
  `.ppk` keys, `SecretAccessKey` values and `ASIA` keys, ODBC `Pwd=`, any
  `UPPER_CASE_KEY=` style assignment, quoted multi-word passwords, URL
  passwords with an empty user or a `/` or `@` in them, Google `ya29.` and
  `1//` tokens, Slack `xapp-`, Telegram bot tokens, Azure `sig=` and
  `curl -u user:password`. Code is left alone: `MY_KEY = get_key()`,
  `MAX_TOKEN = 4096`, `pwd=$(pwd)`, `curl -u $USER:$TOKEN` and a `grep -u`
  after a pipe keep their text, and a bare `UPPER_CASE_KEY=` value is taken
  out only when it is 12 or more characters with a digit and a letter (or is
  quoted). Telegram tokens must start `AA` after the colon.
- **Security fix: a skill's text is not what you typed.** After a typed slash
  command Claude Code inserts the skill's own text ("Base directory for this
  skill: ..."). Typed-text detection counted it as your words, so a skill file
  saying "from now on always ..." became a `decision`, a "Standing decision" in
  the handover, an automatic playbook trigger and the cloud packet's task. That
  block no longer counts; a message typed after a command and its output does.
- **Security fix: recall no longer prints tool output.** The note at the top of
  a new conversation, and the compaction outline's related list, took hits from
  the transcript search, so text from a tool result or a web page could appear
  as a plain line. They now use only exchanges matched by their question,
  answer or summary; `memory_search` still searches tool output and marks it as
  a quoted record.
- **Security fix: cleaning stored text takes linear time.** The pattern that
  strips `<system-reminder` tags backtracked quadratically (a `<` followed by
  20,000 spaces took 10 seconds), and so did the private-key pattern on a pile
  of unclosed `BEGIN` lines. Both ran on every tool result, in the background
  worker and inline during compaction under the store lock.
- **Automatic playbook approvals are signed.** `approve --auto` now signs its
  `auto.json` entry with a random key kept in `auto.key`, so copying an
  approved script's hash into `auto.json` no longer makes it run by itself.
  Entries written by older versions are not signed: run
  `almanac playbook approve NAME --auto` again for each automatic playbook.

- **`almanac library forget` names what it deleted.** It prints the ids.
- **Manage and look up the prompt library.** `almanac library list`,
  `show ID` (the entry, its fixes and the entries it replaced, with what
  changed) and `forget ID` (deletes the entry and the older ones it replaced).
  A new `library_find {query}` tool, added after the existing ones, returns up
  to 3 current entries shaped like the note and ending with the quoted-record
  sentence. `almanac report` gains one line: entries written and notes shown
  in the last 24 hours, counted from a record the writer now logs per entry.

- **The prompt library is shown to the model.** A typed request that matches
  a current entry (at least `library.threshold`, 0.6, of the entry's request
  words in the typed text, and 3 shared words, 5 for a long request; fenced
  code and logs ignored) gets a short note in the
  playbook's slot: the date, how often it was used, the route last time, the
  fixes found then and the files, worded as a record to check, with the id for
  `memory_read`. Main thread only, never with a playbook note, decided once per
  turn so the cache holds, and `library.inject` turns it off. Current entries
  are searched through a new full-text table that is filled in for an existing
  store the first time it is opened.

- **Cloud branches are named `cloud/`.** The snapshot branch is now
  `cloud/handoff` and a takeover pushes `cloud/almanac-takeover-<commit>`. The
  old name `almanac/handoff` read as if every project's snapshot went to the
  Almanac repository; each snapshot always went to its own project's repo.
  The watchdog prompt changed with it: update the scheduled task.
- **Memory no longer drops an exchange over an odd tool result.** A tool
  result whose content holds a non-text value made indexing fail; such
  values are now left out of the stored text.
- **Prompt library: how each solved prompt was worked.** After an exchange of
  the main conversation ends with at least `library.min_tools` (5) tool calls,
  code writes an entry: the request, the route, files, the fixes (a failed
  step and what recovered it) and the outcome. No model call, redacted, and
  deleted with its conversation. Not shown to the model yet.
- **Entries on the same task replace each other.** A new entry with nearly the
  same request and route becomes the current one; the older is kept, linked
  as superseded, and the new one lists the steps added, dropped and new fixes.
- **Saved tool output has no size limit.** Results are kept whole however
  long, and the start-up prune no longer drops the oldest past 100 MB: it all
  stays on this machine. Conversations still follow `memory.keep_days`.
- **Bundled `cli-anything`, edited.** Five skills (`cli-anything`, `list`,
  `refine`, `test`, `validate`) plus its methodology and guides, to build a
  command-line tool for a program with no API. Local paths only, no clone,
  no installs without asking, no generator scripts or MCP guide. Edits are
  listed in `bundle/SOURCES.md`.
- **A long turn is searchable while it runs.** A turn with hundreds of tool
  calls used to be stored only when it ended. Now, after every 30,000
  characters of new work in a turn, an open record of the turn so far is stored
  and found by `memory_search` (marked as still running), and the finished
  exchange replaces it. The write happens in the background worker after the
  reply, never on the request, and no request body changes.

- **Search covers tool output.** `memory_search` and the note for a new
  conversation now also match the transcript of each exchange, tool output
  included (up to 200,000 characters of an exchange, 20,000 of one result), so
  an id, an error or a path that only appeared in a command's output can be
  found, and the hit's snippet is the matching line. An exchange the title and
  text search already finds ranks as before; exchanges only the transcript
  matches are added after it by rank fusion. A store from an earlier version is
  filled in the first time it is opened.

- **Tool output kept whole, and stubs you can undo.** Before a long turn's old
  tool results are replaced by a stub, and when an exchange is stored, the full
  result is saved in the local memory (compressed, secrets removed). The stub
  now says that `memory_read {"result": "<tool call id>"}` returns it, and a
  result cut short in a transcript says the same. `memory_read` takes `result`
  with `start` and `end` and returns at most 400 lines or 40,000 characters at a
  time, as a quoted record. `almanac memory forget` deletes the saved results,
  and the start-up prune drops the oldest once they pass 100 MB. A turn that is
  already running when this is installed keeps its old stub wording, so its
  cache is not broken. With `memory.enabled` off the stub is unchanged.
  A result on one very long line is paged in 40,000-character chunks, nothing
  is saved with `memory.enabled` off, `memory_search` replies carry the
  quoted-record note, and `Cookie:` and `Set-Cookie:` values are removed before
  anything is stored.
- **A fork of a conversation sent unchanged is sent unchanged too.** When the
  upstream refused a conversation's rewritten form, its forks were still
  rewritten, so their tool list differed from the parent's.
- **A fork sends its parent's tool list.** A fork shares its parent's cached
  prefix, but made its own deferral decision and could keep a tool loaded that
  the parent had deferred, so its tools differed and missed the cache. Startup
  decisions of a fork now come from its parent's conversation. The prefix report
  also hashes an `x-api-key` login, and its cold limit is one named constant.

- **Guidance for the memory tools.** The memory server now sends constant
  instructions with its handshake (summaries drop exact values, read the
  exchange first, no hit is not proof, stored text is a record). New option
  `memory.always_load` (off) lists the tools in every session start.
- **Related exchanges in the compaction outline.** The outline now lists up to
  five earlier exchanges of the conversation that match the message that
  triggered the compaction, between open work and the done list. They are
  found once, stored with the prefix and replayed unchanged; with no match the
  outline is as before. Search ties are now ordered by id.
- **A summary for each conversation, and how it ended.** A conversation with no
  request for 10 minutes (or still waiting when Almanac exits) is summarized in
  the background in one small-model call over its exchange summaries and
  statuses. It respects `memory.summary_model`, the daily call cap and the
  pause after a 401, 403, 429 or 529 reply. With `memory.summary_model` set to
  `""`, the summary is written by rule from the exchange titles and no call is
  made. The last thing asked and the start of the last reply are stored with
  it. A new conversation's note now shows each recent conversation's summary and
  how it ended, and `memory_projects` does the same. The note of a running
  conversation does not change. A new tool, `memory_list`, lists conversations
  with `since`, `until` and `project` filters.
- **A handover and a view of what you typed, with no model.** `memory_handover`
  gives a conversation's summary, how it ended, the decisions you set, the open
  work and your last messages. `memory_user_messages` lists what you typed,
  filtered by conversation, time and words. Both cite exchange numbers and show
  stored text as a quoted record.
- **Stable tool and listing bytes across conversations.** New conversations of
  one project now send the same tool list and the same skill and agent
  listings, so they read both from the prompt cache. Which cold tools stay
  loaded and which skills and agents stay full in the listing are decided from
  the project's recent use once per project per day. A cold tool named in the
  first message is no longer kept loaded (ToolSearch loads it), and the skills
  and agents that message matches are shown in full next to it instead of in
  the listing. The system prefix now moves the skill and agent listings
  whenever they are present.
- **A nudge to record a playbook.** When a typed request follows a turn of `playbooks.nudge_after` (default 25, `0` is off) or more tool calls that saved and matched no playbook, the note in the new request says so and points at the `almanac:playbook` skill. It is kept per turn, so the prompt cache holds, and never goes to sub-agents.

- **Which model did which task.** `almanac report` lists the sub-agent threads of the latest conversation with their type, model, requests, tokens and the first 80 characters of their task, and warns when more than four ran at once or an Opus sub-agent was given a build job. The request log now records `agent_type` on sub-agent requests and a redacted `task` (80 characters) on the first one of a thread.
- **The prefix report skips Claude Code's billing line.** It changes in every
  conversation, but a real measurement showed the cache still read past it,
  so naming it as the break hid the real one.
- **Where the cache prefix breaks.** On the first request of each new main-thread conversation,
  Almanac now compares the prompt's blocks (tools, system blocks, the first two
  messages) with the project's previous first request, by hash, and logs the first
  block that differs as `prefix` (`diverge_at`, `index`, `tokens_before`,
  `tokens_after`, `since_project`, `same_account`). `almanac report` sums it up for
  the last 24 hours. Observation only: the request is not changed, and only
  hashes and a short hash of the login are stored.

- **Security fixes.**
  - A playbook field that contains a `system-reminder` tag is refused, on save
    and on load, so a playbook cannot close the frame it is shown in.
  - A playbook Claude or the miner saves is a draft, with or without a script,
    and is not offered until you run `almanac playbook approve NAME`.
  - The headless playbook compile session no longer has Python. Almanac runs
    the script's `--self-test` itself, and the session gets its own
    `ALMANAC_HOME` inside its temporary folder.
  - The summary model can no longer mark an exchange as a standing decision.
  - The cloud packet leaves tool output out: one line per result, with the tool
    and its size.
  - The second login's token is sent only when the upstream is
    `api.anthropic.com`.
  - `almanac playbook approve --auto` and the README say an automatic script
    runs in the conversation's folder with no model and no prompt, so it can
    run that repository's own code.

- **A stable system prefix** (`startup.system_prefix`, off by default). The MCP
  instructions, advisor notes, deferred tool names, agent types and skill list
  move from the session message, which follows the typed text, to one block at
  the end of the system prompt, with the cache breakpoint. A new conversation
  can then read them from cache. The cut depends only on the request's bytes.
- **A tighter playbook miner.** The steps of a way of working no longer list
  read-only shell filters (`head`, `tail`, `cut`, `cat`, `wc`, `sort`,
  `uniq`, `grep`, `ls`, `tr`, `tee`, `xargs`, `sed -n`); the tools still do.
  Turns join a group only at 75% phase overlap (was 60%) and share at least
  3 phases with its seed, so one catch-all group no longer swallows the rest.
  A playbook now records the `phases` it was mined from (an optional list in
  `playbook.json`; older files load as before), and a way of working that a
  stored playbook of any source already covers is not mined or compiled
  again, so the same work no longer gets several drafts.
- **Short working rules for sub-agents.** A sub-agent thread used to get the
  model's whole cheatsheet, most of it the lead's method (handovers, briefing
  agents, merging, deadlines). It now gets a new `agent.md` (do the one job in
  the brief, check before saying done, stop after two failed attempts, report
  result first) plus `skills.md` unless the model is Haiku. The main thread and
  forks keep the full sheet unchanged, so a fork still shares its parent's
  cached prefix. For Opus a sub-agent's text drops from about 4,450 to about
  1,320 tokens. `almanac cheatsheet show agent` prints it, and an `agent.md` in
  `almanac cheatsheet path` replaces the packaged one.
- **A usage and cache meter.** Each request in the log now records `cache`
  (how many `cache_control` breakpoints the client sent and the `ttl` of each,
  "5m" when none is given) and `idle` (seconds since the previous request of
  the same conversation thread). `almanac status` prints usage for the last 5
  and 24 hours and a cache line: the share of input read from cache, the
  "cold returns" (requests that came back after a pause longer than the
  shortest ttl they asked for) with the cache writes they cost, and the ttl
  values seen. `almanac report --json` adds the same data under `meter`.
  Nothing about a request or reply changes.
- **A second login takes over when the first is out of usage.** With
  `accounts.fallback` set to a list of Claude Code config folders (off by
  default), a model request that comes back with a usage-limit 429 is sent
  again once with the next valid login's token, and later requests skip the
  spent login until its usage comes back (read from the reply's reset
  header, `retry-after` or "resets 2:30am" wording, else
  `accounts.cooldown_minutes`). A plain rate limit, an expired alternative
  or two spent logins return the original reply unchanged.
  Almanac never refreshes or writes a login. `almanac status` shows the
  folders and the log records which one was used.
- **The Opus rules cover working against a deadline.** Write the handover
  first and keep it current, stop starting things near the end, know which
  login the work is charged to, and audit anything that runs on its own.
- **A playbook script can run before Claude sees the request.** The note
  only asked Claude to call `playbook_run`, and Claude could ignore it.
  `almanac playbook approve NAME --auto` now also lets the proxy run the
  approved script itself when the playbook is the best match for a typed
  request and a trigger of two or more words matches: in the conversation's
  folder, output to temporary files, the whole process tree stopped after 60
  seconds, once per typed message, and without holding the store's lock. The
  note then says the script already ran and carries its output as data (3,000
  characters at most, reminder tags removed), with the steps to fall back on if
  it failed. Only the owner can set this: the approval is a hash in
  `~/.almanac/auto.json`, written by `approve --auto`, outside the playbooks
  folder; a playbook Claude saves is never automatic, a changed script goes
  back to draft, and a playbook with `args` or only one-word triggers cannot be
  automatic. `list` and `show` say auto only while it would really run. New setting `playbooks.auto_run` (default
  `true`) turns it off; the request log has `playbook.ran` and `playbook.exit`.
- **A skipped script is called back once.** When a playbook with an approved
  script matched but is not automatic, and Claude has made two tool calls in
  that message without calling `playbook_run`, one reminder is added after the
  newest tool results. It is stored with the message it went into and put back
  byte for byte on every later request, so the prompt cache keeps hitting.
- **Playbook mining finds ways of working, not only wording.** The miner
  grouped past requests only by similar words, so a job you asked for in
  different words each time was never found. It now also groups requests by
  the phases of work they went through (branch, edit, test, commit, push, pr
  and so on) and lists a group as `way of working: branch > edit > commit >
  push > pr`. Chained shell commands count one step each. The compile prompt
  gets the phases and steps. `almanac playbook mine` shows the new rows.
- **Claude is told which playbooks exist.** The cheatsheet now ends with a
  list of the playbooks on hand that apply everywhere (12 at most, by name),
  because a way of working cannot be matched from the words of a request. It
  goes to the main thread and forks, not to other sub-agents or Haiku.
  `playbook_find` with a playbook's exact name returns the whole playbook,
  every step uncut. It follows `playbooks.inject`.
- **The playbook check repeats while a session is open.** It ran only when
  `almanac` started Claude Code. A long session now checks again every 6
  hours, with no output.
- **The Opus working rules are the full method now.** `opus.md` was a short
  list of slips. It now says how the lead model works a task from start to
  finish: start from the stored state, doubt its own measurements, make the
  reversible decisions and say them, hand building to Sonnet and audits to a
  second Opus with complete briefs, keep the session cheap, clean up, and
  report the result first.
- **`ALMANAC_DISABLE=all` now turns everything off.** It left the cheatsheet
  and playbooks on, so a run meant to be plain Claude Code still had text
  added to it.
- **A status on every exchange, and a compaction outline by status.** Each
  stored exchange is tagged `open`, `done`, `decision`, `superseded` or `noise`
  by plain rules (the summary model may refine it, never downgrading a rule-made
  decision or superseded). The outline built at a compaction is now in sections:
  standing decisions first, open work, then done exchanges newest first until
  the room runs out. Superseded and noise are only counted. `almanac status` and
  `almanac memory recent` print the counts.
- **Relief inside one long turn.** In the middle of a turn, once a request
  reaches `compaction.window_share` (0.85) of the model's window, old tool
  results of that turn (all but the newest `compaction.keep_results`, 12) are
  replaced by a one-line stub that keeps the block and its id. The stubbed set
  only grows within a turn, so the cache keeps hitting; it is cleared at the
  next rebuild. A stubbed result cannot be restored: the tool has to be run
  again. `window_share` `0` is off.
- **The real window is learned from a size refusal.** The window table can be
  wrong for a conversation (one model id has a 200k and a 1M form). When the
  upstream answers "prompt is too long: N tokens > M maximum", M is kept for
  that conversation and the same request is relieved against it and sent
  again, so the turn goes on.
- **Claude Code's own auto-compaction is switched off** while Almanac compacts
  (`compaction.own_the_context`, on by default): `DISABLE_AUTO_COMPACT=1` is set
  for the child. Set it to `false` to have Claude Code's own back.
- **Playbooks from your past sessions.** `almanac playbook mine` finds tasks
  you repeat in Claude Code's session files with no model, and `almanac
  playbook compile` has one headless Sonnet session write the best one up as a
  draft playbook (source `miner`; a script waits for `approve`). At each start a
  detached process does this within `playbooks.compile_per_day` (default 1, `0`
  is off), and one line at launch names drafts waiting for approval. A new rule
  in the skills cheat sheet tells Claude to follow a matching playbook and run
  its approved script. Settings: `playbooks.transcript_dirs`,
  `playbooks.background`, `playbooks.compile_per_day`.

- **Shorter agent list.** The list of agent types Claude Code sends at session
  start (about 6,400 tokens with 88 agents) is cut to a 60-character stub per
  agent, without the `(Tools: ...)` suffix on cut lines
  (`startup.agent_chars`, `0` is off, `ALMANAC_DISABLE=agents`). The choice is
  frozen per conversation, and an agent whose description matches a message gets
  its full line back, the way skills do. Names are never changed.

- **Playbooks.** A playbook records how a repeated task is done: when it
  applies, an ordered decision list, the skills and agents used, and
  optionally a script that does the job with no model. Almanac matches each
  new request against them with plain code and adds a short note naming the
  one that applies, frozen per conversation so the cache keeps hitting. New
  tools `playbook_find`, `playbook_save` and `playbook_run`, and
  `almanac playbook list|show|approve|remove|path`. A script is a draft until
  you approve its exact text, and goes back to draft when it changes. A bundled
  `almanac` plugin carries the `playbook` skill that teaches Claude to write
  them. Settings: `playbooks.enabled`, `playbooks.inject`.

## 0.2.0 (2026-11-16)

- **Stalled hand-off.** With the automatic hand-off on, when the upstream
  fails 5 times in a row (`cloud.stall_after`, `0` is off) on a connection
  error, a timeout or a status of 500 or more, Almanac pushes the snapshot at
  once marked `stalled`, and the cloud task takes it over at its next run
  without waiting 30 minutes. A 429 or other 4xx does not count. A later
  success pushes `working` again. If the local session recovers after a
  takeover began, both carry on, the cloud one on its own
  `claude/almanac-takeover-...` branch.

- **Skills without slash commands.** Every bundled skill may now be started by
  the model, the rules for when to use each are in their own file
  (`skills.md`), and Fable and Mythos receive that file too. More crew agents
  point at the skills (62 of 71).
- CI runs on Windows only.

- **Bundled skills and agents.** Almanac now carries its own plugins and loads
  them into every session with `--plugin-dir`: design planning and review,
  interface building, the Astryx design system, codebase untangling, security
  audits, and a crew of 71 sub-agents pointed at those skills. The working
  rules for Opus and Sonnet say when to use them. `bundle.enabled` set to
  `false` turns it off.

- **Accepting a flagged skill.** `almanac skill accept NAME` stops the launch
  warning for a skill or plugin whose findings you have read, for its current
  files only; any change brings the warning back. `skill audit` shows it as
  accepted with its real verdict. `skill unaccept` undoes it. `skill add` is
  unchanged. The launch check now also stops hashing inside a large folder when
  its time is up, `almanac pack` finds its list from any folder, and removing a
  quarantine folder no longer warns on Python 3.12 and later.
- **Working rules for each model.** Each conversation thread now gets a short
  set of working rules at the end of its system prompt, picked by the model
  the request goes out on: Opus and Sonnet get their own text plus a common
  part, Haiku gets a shorter text, other models get nothing. It is on by
  default; `almanac config set cheatsheet.mode off` turns it off. Files with
  the same names under `ALMANAC_HOME/cheatsheets/` replace the packaged ones.
  New commands: `almanac cheatsheet show` and `almanac cheatsheet path`. The
  text is the same on every request, so the prompt cache keeps hitting.
- **Skill scanning.** Skills and plugins can be checked with NVIDIA's
  SkillSpector before they are installed (`almanac skill scan`, static checks
  only). `almanac skill add` fetches a source into a quarantine folder with
  nothing run, scans the skill folders, and installs them only on SAFE.
  CAUTION needs `--accept-caution`; DO_NOT_INSTALL or a failed scan cannot be
  overridden. Plugins with hooks or MCP servers are scanned and shown, not
  installed. At each start, Almanac warns about flagged skills and plugins and
  scans new or changed ones in the background, 5 per start
  (`skills.scan_on_launch`; `almanac skill audit` does all of them now). A
  pinned `pack.json` lists third-party skills and plugins, installed with
  `almanac pack install`. The scanner contacts api.osv.dev with dependency
  names and versions. Tested on Windows with SkillSpector 2.12.0.
- **Model routing.** Almanac can now choose the model for you. With
  `routing.mode` set to `subagents` or `all` (it is `off` by default and then
  changes nothing), each prompt goes to Haiku (simple work), Sonnet (coding) or
  Opus (design, audits, hard bugs, work that failed). Haiku and Sonnet get a
  matching effort level where Claude Code sends one; Opus keeps the effort
  Claude Code asked for. Fast-mode requests are never routed. Sub-agents are routed from a `Difficulty:` line that a short paragraph
  added to the `Agent` tool asks the model to write. A move to a cheaper model on
  the main thread is priced first, because a model switch writes the whole
  history into the new model's cache. Repeated tool errors raise the next turn a
  tier. A prompt with no sign of being easy or hard keeps your model. A
  refused routed request is sent again without the model change, and the tier
  is marked unavailable only if that one is accepted. New commands:
  `almanac route status` and `almanac route pin`. `almanac report` and
  `almanac status` show requests per tier, moves and tokens per model, and the
  request log records the model that answered.
- **Riding out a network drop.** When the upstream cannot be reached, a model
  request is held and sent when the network returns, for up to
  `offline.hold_minutes` (default 480; `0` turns it off and requests fail at
  once, as before). A streaming request gets pings while it waits. A request
  that is not streamed answers with an error after at most 9 minutes. If the
  wait runs out, or the upstream then answers with an error, the stream is
  cut, so Claude Code retries and sees the real result. A client that leaves
  while its request waits is dropped and never served later. `almanac report`
  shows how many requests waited. Tested with a fake upstream, not a real
  outage.
- **Automatic hand-off (off by default).** With `cloud.auto` on, a task in
  progress in a repository turned on with `almanac cloud on` (`off` stops it;
  no other repository is touched) is pushed as a snapshot to `origin`'s
  `almanac/handoff` branch every `cloud.heartbeat_minutes` (10): HEAD plus
  uncommitted edits to tracked files (never untracked files; a changed file
  that may hold a secret stays at its committed version), the hand-off
  packet and its state. Only that branch is pushed, never tags. When the turn
  ends it is marked idle. `almanac cloud watchdog` prints the setup and the
  prompt for an hourly scheduled cloud task that takes over a snapshot whose
  heartbeat stopped, on a `claude/almanac-takeover-` branch with a draft pull
  request. `almanac cloud status` shows the last heartbeat and any takeover.
  The snapshot leaves your index, HEAD and branches alone, runs off the
  request path, and a failure is one log line. It puts uncommitted work on
  GitHub; `git push origin --delete almanac/handoff` removes it. Each
  scheduled run uses a little plan usage even when there is nothing to do.
- **Cloud hand-off.** `almanac cloud packet` prints a hand-off packet for a
  stored conversation: the last message you typed, your earlier requests, one
  line per exchange of what was done, the latest transcript and the state of
  the repository, capped at 24,000 characters. Common secret patterns are
  taken out; that is best effort, so read it first. `almanac cloud send`
  shows the packet, asks, and starts a Claude Code cloud session from it with
  `claude --cloud`, so the work can go on while this machine is offline. The
  cloud clones the GitHub remote, so `send` lists the commits that are not
  pushed and the files that are not committed. On Windows it refuses a
  `.cmd` launcher and keeps the quoted command under the length limit.
- **More secret shapes are taken out of stored text:** `DB_PASS=`,
  `AccountKey=`, `PRIVATE_KEY=`, JSON-escaped `NAME=\"value\"`, `whsec_`
  and `hf_` keys, Slack webhooks, `Authorization:` values and private keys
  cut off before their END line.
- **Security: web pages cannot use the proxy.** A request with an `Origin`
  header, and every `OPTIONS` request, is now refused with a 403 before
  anything is forwarded. Refusals are logged (`"kind": "refused"`), without the
  secret or the query string. This changes no bytes sent upstream.
- **Security: the address now carries a secret.** The proxy answers only
  requests whose path starts with a random secret, made for each proxy and
  compared in constant time. `almanac` hands Claude Code the right address.
  `almanac serve` prints it, so a client you start by hand needs that address
  (see "Always on" in the README). A restarted `serve` makes a new secret, so
  sessions that were sharing it must be started again; the 403 says so. The
  secret is stripped before the request goes upstream, so upstream bytes do not
  change.
- **Security: `almanac` no longer trusts whatever answers on port 8787.** To
  share a running `almanac serve` it must answer a random challenge with a
  value computed from a key in `serve.json`. The key is separate from the
  address secret and is never sent in a request. The file is readable only by
  you and is removed when `serve` is stopped with Ctrl+C, unless another
  `serve` has written its own since. A listener that returns a JSON list or a
  malformed proof no longer crashes the launcher.
- **Security: fail open is complete.** A store error, a transform error, a text
  block that is not a string or a body too deep to parse no longer drops or
  hangs a request, including on the retry after a 400. The original body goes
  out.
- **Security: forget really forgets.** `almanac memory forget` and the
  `memory.keep_days` clean-up now also delete the conversation's saved choices
  (compaction prefix, recall note, project, skill and tool decisions) and its
  usage rows, overwrite deleted text, and empty the database's write-ahead file. Forget also
  deletes the recall notes of other closed conversations, which can quote the
  deleted text, and with `--project` the saved choices of subagents and of
  conversations that were never indexed. A conversation
  changed in the last hour is kept unless you add `--force`. The 45-day
  clean-up no longer deletes the saved choices of a conversation that is still
  being used.
- **Security: private files on Linux and macOS.** The Almanac folder is created
  with mode 0700 and the database and logs with mode 0600. Nothing changes on
  Windows.
- **Security: network paths are never opened.** A `//host/share` or
  `\\host\share` path in a tool call is ignored when working out a project, so
  a model cannot make Windows contact another host.
- **Security: summary calls carry only their own conversation.** A summary call
  now sends exchanges only from the conversation whose login it uses, and stops
  after the first failure instead of trying every exchange.
- **Security: wider secret removal.** Removed before storing: `NAME=value` for
  names ending in secret, token, password or api key (but not `max_tokens`),
  bearer and basic credentials, passwords in URLs, and Stripe, Google, npm,
  GitLab and SendGrid keys.
- **Security: stored text is marked as a record.** New recall notes and new
  compaction summaries say they are quoted records, and tool output lines are
  indented so they cannot pose as a `User:` turn. `</system-reminder >` and
  similar spellings are removed. Notes and prefixes already stored are not
  changed.
- **Security: smaller fixes.** A comparison of a non-ASCII proxy token no longer
  raises. A negative `Content-Length` or chunk size is refused. An idle client
  is dropped after 16 minutes. On Windows another program can no longer bind
  the port of a running proxy. `/almanac/health` shows only the scheme and host
  of the upstream.
- **Search by meaning.** The new optional extra `getalmanac[semantic]` and the
  command `almanac semantic install` add a small embedding model that runs on
  your machine (all-MiniLM-L6-v2, about 90 MB). With it, `almanac memory
  search`, the `memory_search` tool and the memory note for a new conversation
  find exchanges by meaning as well as by words. The download is the only time
  a host other than the upstream is contacted, and no conversation text is
  sent. Vectors are made in the background, again after a summary is written,
  and are tagged with the model that made them. Without the packages or the
  model, or with `memory.semantic` set to `false`, search is unchanged. New
  commands: `almanac semantic install | status | index | on | off`.
- **Model-written summaries.** A small model (`memory.summary_model`, Haiku by
  default) now writes each stored exchange's title and a short summary in the
  background. The call uses the same upstream and the login the session already
  carries, and that login is never stored. It uses a little of your allowance:
  at most `memory.summary_daily_calls` calls a day, paused for an hour on a
  rate-limit or login error. A failed call keeps the rule-written text.
  Compaction outlines and recall use the new text. Set `memory.summary_model`
  to `""` to turn it off. `almanac status` shows the counts.
- **Corporate proxy.** Almanac now reaches the upstream through an `http://`
  proxy: the new `upstream_proxy` setting, else `HTTPS_PROXY` or `ALL_PROXY`,
  with `NO_PROXY` honoured. A login in the URL is sent as basic proxy
  authentication and is never stored, logged or printed. Other connections in
  HTTPS-proxy mode use the proxy too, and that mode no longer turns itself off
  because a proxy is set.
- **Company certificates.** The new `upstream_ca` setting, `SSL_CERT_FILE` and
  `NODE_EXTRA_CA_CERTS` add trusted CA files for the upstream. Verification
  stays on.
- **Doctor.** `almanac doctor` shows the proxy and its source, the extra
  certificate files, and whether a connection through the proxy works.
- **Fix: compaction recovers after an in-place edit.** When an edit to
  already-summarized messages dropped the stored summary, the old token count
  was still trusted, so the full history was sent and refused with a 400, every
  turn. The count is dropped with the summary and the size is estimated again.
- **Fix: a "prompt is too long" 400 no longer turns compaction off.** Only
  other 400s to a compacted request set the per-conversation off switch. The
  request is still resent as before, and the next message re-estimates the size.
- **Fix: summary calls.** A reply that opens with a thinking block now stores
  its summary (`max_tokens` is 1000, was 300). A summary call that stores
  nothing writes one `"kind": "summary"` line to the request log with the HTTP
  status and a reason (`status`, `empty`, `parse` or `error`), never any text.
- **Fix: a tool's inner model call is not a conversation.** A request whose
  tools include none the client runs (such as the call web search makes) is no
  longer indexed, compacted or given memory, so it no longer shows up as a
  one-exchange conversation with no project.
- **Fix.** With a proxy in the environment, Claude Code sent its requests for
  Almanac's loopback address to that proxy. `localhost` and `127.0.0.1` are now
  added to its `NO_PROXY`.

## 0.1.0 (2026-11-14)

First release.

- **Proxy.** `almanac` runs Claude Code through a loopback proxy that forwards
  every request with Claude Code's own login. A failed rewrite falls back to the
  original request.
- **Startup layer.** Skill descriptions are collapsed to stubs and rarely used
  tool schemas are deferred, each loading in full on a match.
- **Project memory.** Finished exchanges are stored locally as a tree of
  project, conversation, topic and exchange. Projects are git repositories,
  picked from the files a conversation touches. New conversations get related
  memory, and Claude Code gets tools to search it.
- **Compaction.** Long histories are replaced once by a stored summary that is
  re-sent unchanged, so the prompt cache keeps hitting.
- **HTTPS-proxy mode.** Optional mode that keeps Claude Code's base URL, so
  Remote Control stays available. Off by default.
- **Commands.** `report`, `status`, `doctor`, `memory`, `projects`, `config`,
  `serve`, `mcp`.
