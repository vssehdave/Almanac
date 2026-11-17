# Changelog

## Unreleased

- **A nudge to record a playbook.** When a typed request follows a turn of `playbooks.nudge_after` (default 25, `0` is off) or more tool calls that saved and matched no playbook, the note in the new request says so and points at the `almanac:playbook` skill. It is kept per turn, so the prompt cache holds, and never goes to sub-agents.

- **Which model did which task.** `almanac report` lists the sub-agent threads of the latest conversation with their type, model, requests, tokens and the first 80 characters of their task, and warns when more than four ran at once or an Opus sub-agent was given a build job. The request log now records `agent_type` on sub-agent requests and a redacted `task` (80 characters) on the first one of a thread.

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
