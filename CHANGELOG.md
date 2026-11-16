# Changelog

## Unreleased

- **Working rules for each model.** Each conversation thread now gets a short
  set of working rules at the end of its system prompt, picked by the model
  the request goes out on: Opus and Sonnet get their own text plus a common
  part, Haiku gets a shorter text, other models get nothing. It is on by
  default; `almanac config set cheatsheet.mode off` turns it off. Files with
  the same names under `ALMANAC_HOME/cheatsheets/` replace the packaged ones.
  New commands: `almanac cheatsheet show` and `almanac cheatsheet path`. The
  text is the same on every request, so the prompt cache keeps hitting.
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
