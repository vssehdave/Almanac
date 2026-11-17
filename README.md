# Almanac

A local proxy for Claude Code. It makes each session start with less context,
gives every project a memory, and keeps long conversations inside the context
window. Nothing leaves your machine except the requests Claude Code was already
sending to Anthropic. (The exceptions are things you choose to do: download a
model, see [Search by meaning](#search-by-meaning), scan or fetch a skill, see
[Installing skills safely](#installing-skills-safely), hand a conversation to a
cloud session, see [Cloud hand-off](#cloud-hand-off), and turn on the
[automatic hand-off](#automatic-hand-off), which pushes a branch to your git
remote.)

```
Claude Code  ──▶  almanac (127.0.0.1)  ──▶  api.anthropic.com
                     │
                     └── ~/.almanac/   SQLite memory, settings, a sizes-only log
```

- **No account, no server, no telemetry.** One Python package with no required dependencies.
- **Your login is untouched.** Claude Code keeps its own sign-in. Almanac forwards
  the headers it receives and never reads, stores or logs a credential.
- **It fails open.** If anything in Almanac goes wrong, your request is sent
  exactly as Claude Code wrote it.

## Install

Python 3.10 or newer and Claude Code are the only requirements.

```bash
# the repository is private: clone it with your own GitHub login, then
git clone https://github.com/vssehdave/Almanac && cd Almanac
pip install -e .
```

The install is editable, so `git pull` in that folder is the update. A wheel is
also attached to every [release](https://github.com/vssehdave/Almanac/releases).
Then check the setup:

```bash
almanac doctor
```

## Use

Type `almanac` where you would type `claude`. Every argument is passed through.

```bash
almanac                       # an interactive session
almanac --resume              # any Claude Code argument works
almanac -p "summarise this repo"
```

After one message, see where the context went and what was kept out:

```bash
almanac report
```

```
Latest session start: 2026-11-14 02:18, claude-haiku-5-5

  24,250 tokens sent (about 36,668 without Almanac, 12,418 kept out)

  part                                           tokens   without
  skill listing                                   4,224    10,136
  system prompt                                   3,847
  tool: PowerShell                                    0     3,531
  ...
```

Those figures are from a real session on the author's machine, which has about
65 skills installed. The total is exact (it comes from the API's reply). The
per-part split is that total divided by size, so treat it as an estimate. Your
numbers depend on how many skills and tools you have.

## What it changes

Almanac only ever changes the JSON body of Claude Code's own model requests.
Everything else is relayed as received, with two exceptions. On model requests
`Accept-Encoding` is set to `identity`, so Almanac can read the reply. And
hop-by-hop headers (`Connection`, `Keep-Alive`, `Transfer-Encoding`, `TE`,
`Upgrade`, `Proxy-Authorization`, `Proxy-Connection`) are dropped, while `Host`
and `Content-Length` are set again for the upstream.

### 1. A smaller session start

Every session begins by sending every skill description and every tool schema,
whether or not the conversation will use them.

- **Skills.** Each description is cut to a short stub. A skill keeps its full
  description in the list when you put it in `skills.always_full` or when two of
  the project's last ten conversations used it; that second set is read once per
  project per day, so every new conversation of the project gets the same list.
  When your first message matches a collapsed skill, its full description is
  placed next to that message, and so it is for a later message that matches.
  Every skill stays listed and can be invoked by name at any time.
- **Agents.** The list of agent types gets the same treatment: each description
  is cut to `startup.agent_chars` characters (60 by default, `0` turns this off),
  and the `(Tools: ...)` part of a cut line is dropped. An agent whose
  description matches your message gets its full line next to that message, as
  skills do. Every
  agent stays listed and can be used by name.
- **System prefix** (off by default, `startup.system_prefix`). Claude Code sends
  its session notes (MCP server instructions, advisor notes, the deferred tool
  names, the agent types and the skill list) in a message that comes after what
  you typed, so a new conversation can never read them from the prompt cache.
  With this on, those sections are cut out of that message and placed as one
  block at the end of the system prompt, ahead of your text, and the cache
  breakpoint on the old last system block moves with them. The text is moved
  as it is, in a fixed order, and only on the main thread. The skill and agent
  lists move too, because they are the same in every conversation of a project.
  The cut depends only on the request itself, so every request of a conversation is
  rewritten the same way. When those sections change (a new MCP server or
  skill, say), the next conversation writes them to the cache once.
- **Tools.** Schemas of rarely used tools (`tools.cold`) are deferred. Their
  names are added to the list of deferred tools Claude Code already keeps, and
  the model loads one through its normal tool search when it needs it. A tool
  the conversation already called stays loaded, and so does one used in two
  recent conversations of the project. That second set is read once per project
  per day, so every new conversation that day sends the same tool list and
  reads it from the cache. A cold tool your first message names is not kept
  loaded: it is announced by name and the model loads it through ToolSearch.

Matching is keyword-based (BM25) and runs locally. It does not understand
synonyms: "make slides" finds a skill that says "slide deck", but a request
phrased with none of a skill's words will not expand it. The stub is still
there for the model to read.

### 2. Memory per project

Finished exchanges are stored in a local SQLite file as a tree:

```
project  ─▶  conversation  ─▶  topic  ─▶  exchange
```

An exchange is what you asked, the final answer, the tools used and the files
touched. Exchanges about the same subject are grouped under a topic.

**A project is a git repository, not a folder.** It is identified by its remote
URL, so two clones are one project and a worktree belongs to its main checkout.
A conversation is assigned from evidence, in this order:

1. the files its tool calls touch,
2. a path or a known project's name in your message ("use the billing repo"),
3. the folder the session started in.

So work on one repository done from another's folder is filed where it belongs.

When a new conversation starts, the earlier exchanges that match its first
message are added to that message, along with the titles of the project's most
recent conversations. Claude Code also gets three read-only tools to look
further: `memory_search`, `memory_read` and `memory_projects`.

Each exchange is first stored with a title and text written by rule. In the
background, a small model (`memory.summary_model`, Haiku by default) then
replaces the title and adds a one to three sentence summary of what was done,
what was decided or found, and which files were touched. The call goes to the
same upstream as your session, with the login your session already uses. It
costs a little of your allowance: a few hundred tokens per exchange, at most
`memory.summary_daily_calls` calls a day. Nothing goes to any other host. If a
call fails, the rule-written text stays, and each exchange is tried at most
three times, and each failed call leaves one line in the request log (status and
reason, no text). A 401, 403, 429 or 529 reply pauses summary calls for an hour. A
call only carries text from the conversation whose login it uses. Set
`memory.summary_model` to `""` to turn this off. `almanac status` shows how many
exchanges have a model-written summary and what today's calls used.

Obvious secrets (API keys, tokens, private keys, `password=` values, bearer
credentials, passwords in URLs) are removed before text is stored, including
from model-written summaries. This is best effort: see [Security](#security).

Text handed back to the model (the note for a new conversation, the compaction
summary) is marked as a quoted record, and lines of tool output are indented so
they cannot pose as a `User:` turn. That lowers the risk of an injected
instruction in stored text. It does not remove it.

#### Search by meaning

By default, memory search matches words. Ask for "the bill is off by a penny"
and it will not find an exchange about "invoice totals a cent short".

An optional extra fixes that. It adds a small embedding model that runs on your
machine (all-MiniLM-L6-v2, about 90 MB, CPU only):

```bash
pip install "getalmanac[semantic] @ git+https://github.com/vssehdave/Almanac"
almanac semantic install
```

`almanac semantic install` downloads the model from huggingface.co. That is the
only time Almanac contacts a host other than the upstream, and no conversation
text is sent. The model is never downloaded without that command.

Once the packages and the model are there, the background worker gives each
exchange a vector after it is stored, and again after its summary is written.
It also catches up on older exchanges a few at a time, and `almanac semantic
index` does all of them at once. Search (`almanac memory search`, the
`memory_search` tool and the note added to a new conversation) then merges the
keyword matches with the nearest exchanges by meaning. Exchanges that are not
close enough are left out.

- The vectors live in the same SQLite file, tagged with the model that made
  them. If the model ever changes, old vectors are ignored and made again.
- The note added to a new conversation is still chosen once and kept byte for
  byte, so a vector made later never changes it. Working out the query vector
  for it takes roughly 20 milliseconds once the model is loaded (not measured).
- Loading the model takes roughly a second (not measured). `almanac serve` and
  `almanac` load it in the background as they start.
- If the packages or the model are missing, or loading fails, search quietly
  stays keyword-based. `almanac semantic off` does the same on purpose, and
  `almanac semantic on` undoes it. `almanac semantic status` shows what is in
  use and how many exchanges have a vector or are waiting.
- The model reads English best and takes about the first 256 tokens of each
  exchange.

### 3. Compaction that keeps the cache

When a conversation passes `compaction.trigger_tokens` (150,000 by default) and
you send a new message, everything before that message is replaced by:

- the session-start context, unchanged,
- an outline of the earlier exchanges, by status, and
- the most recent exchanges in full.

That replacement is stored and sent unchanged on every later request, so
Anthropic's prompt cache keeps hitting. It is rebuilt only after the
conversation has grown a good deal again, and thrown away if you rewind or edit
the covered messages.

Each stored exchange has a status: `open` (the newest one, or one that ended on
a question), `done`, `decision` (you stated a rule or a choice: "from now on",
"always", "never", "instead of" and similar), `superseded` (your next message said
it went wrong) or `noise` (only "ok" or "go ahead", or a command echo). Plain
rules set it when the exchange is stored and again for the last exchanges
whenever a new one arrives; the model that writes the summary may refine it at no
extra call, but never turns a rule-made `decision` or `superseded` into something
else. `almanac status` and `almanac memory recent` print the counts.

The outline is in sections, within the same size budget: standing decisions
first (oldest first, the last to be dropped), then open work with the longer
summary, then done exchanges, newest first, until the room runs out, with a line
saying how many more are not listed and that `memory_search` finds them.
Superseded and noise exchanges are not listed, only counted. Each exchange's
transcript is also kept in the local memory and Claude can read any of them back
by number. The summaries are plainer than what `/compact` produces. The prefix
is built when you send a message, from the statuses at that moment; a status that
changes later affects only the next one.

**Inside one long turn.** A turn can run hundreds of tool calls with no message
from you, so there is nothing to rebuild. When a request in the middle of a turn
reaches `compaction.window_share` (0.85) of the model's window, the old tool
results of that turn are replaced by a one-line stub: all but the newest
`compaction.keep_results` (12), leaving out results that are errors or under 2,000
characters. The stub keeps the result's block and its id, so every call still has
its answer, and says how long the result was. Tool calls, thinking, and
everything before your last message are never touched. The stubbed set is stored
and only grows during the turn, so every later request stubs the same results
the same way and the cache keeps hitting; if one pass is not enough the next one
stubs more, down to the newest 4. **A stubbed result cannot be restored:** if
Claude needs it, it has to run the tool again. The set is cleared when the
prefix is rebuilt, and `window_share` set to 0 turns this off. If the upstream
refuses a body with stubs, the original goes out and the conversation is left
alone from then on, as with a refused compaction. Stubs take back the room of the
results only: a turn whose own tool calls and replies fill the window still will.

Almanac now keeps the context, so it also sets `DISABLE_AUTO_COMPACT=1` for
Claude Code when it starts it (`compaction.own_the_context`, on by default, and
only while `compaction.enabled` is on). Claude Code's own compaction is then
off; to have it back, run `almanac config set compaction.own_the_context false`.
If your model has a larger window, raise the trigger:

```bash
almanac config set compaction.trigger_tokens 600000
```

### 4. Choosing the model for you

You should not have to pick a model by hand. With routing on, Almanac picks one
for each prompt and sets a fitting effort level:

| Prompt | Model (setting) | Effort |
|---|---|---|
| Simple, basic: a lookup, a summary, a rename, a one-step command | `small`, Haiku | low |
| Coding: write, change, fix or test code | `medium`, Sonnet | medium |
| Too complex, or it failed: design, an audit, a hard bug, work across many files, "that is still broken" | `large`, Opus | as Claude Code sends it, never lowered |

Effort is set only on requests where Claude Code already sends one.

It is off until you turn it on:

```bash
almanac config set routing.mode all        # or: subagents
```

- `off` changes nothing. Not one byte of any request.
- `subagents` routes only the sub-agents Claude Code starts. Your own model stays as you set it.
- `all` routes the main conversation too.

The rules are a short table in `routing.py`. A turn is judged from its length,
its wording and the files it names, read from what you typed (pasted logs, stack
traces and code blocks are not taken as a sign of a hard job or a failure). A turn with no sign of being easy or hard,
such as a "why" question, a short follow-up or text that is not English, keeps
the model you were on. A turn that says the last answer was wrong goes to
`large`. Fast-mode requests (`speed`) are never routed. Three tool errors in a row, or one call failing three times,
raise the next turn one tier, for three turns. With `routing.classifier` set to
`"model"`, the turns the rules are unsure about are settled by one small-model
call. It adds a short wait (each network step gives up after 2 seconds) to that turn and uses a little of
your allowance. It shares the daily cap on small-model calls with the summaries
(`memory.summary_daily_calls`) and is not turned off by `memory.summary_model`.
If the call fails, the rules decide.

**Sub-agents.** Almanac adds one fixed paragraph to Claude Code's `Agent` (or `Task`) tool.
It asks the model to start each sub-agent prompt with `Difficulty: simple`,
`standard` or `hard`. Almanac reads that line and picks the sub-agent's model.
A sub-agent keeps its model for its whole life. Without that line, a task that
asks for no coding and no hard job keeps the model Claude Code chose for it,
whatever the agent's type.

**Switching models costs money.** Anthropic's prompt cache belongs to one
model. If a conversation moves to another model, the whole history is written
into that model's cache again, at the cache-write price, and the old cache goes
cold. Almanac's memory does not change that. So on the main thread:

- A move **up** is made whenever the turn needs it. Getting it right beats cost.
- A move **down** is made only if it is clearly cheaper (`routing.margin`, 20%).
  Almanac counts the history's tokens, the price of writing them into the new
  cache against reading them from the warm one, and the work the turn is
  expected to take. One quick lookup while 150,000 tokens sit warm in Sonnet's
  cache stays on Sonnet.
- The prices in `routing.prices` are relative numbers for comparing models.
  The defaults are list-price ratios as of 2026-11-03 for Haiku, Sonnet and
  Opus 5.5 (the Haiku cache-read ratio is assumed, as it is not listed, and
  Haiku costs 5 times as much above 100,000 tokens of history). Edit them if
  your prices differ.
- Almanac assumes a cache used last turn is still warm. After a long pause it
  is not, so Almanac is a little too careful about moving down.
- Compaction (above) keeps the history small, which keeps a move cheap.

Each turn's choice is stored and reused for every request of that turn's tool
loop, so the model does not change in the middle of a loop, except after a
refused request, when the rest of the turn uses your own model. Moving between
models mid-conversation was tested live in every direction, with thinking blocks
and tool calls in the history, and the API accepted every case. Thinking blocks
written by Opus 5.5 can only be read by some other models, so after a move off
Opus they are dropped silently (and not billed) and the earlier reasoning is
lost. When a tier cannot be used (refused, or the request does not fit its
window), the turn stays on the previous model with that model's own effort, so
the cache is kept.

**Your own choice wins.** If a request names a model other than the three in
`routing.models`, Almanac leaves it alone, and `almanac route status` names
that model. If it is the model you normally use, routing does nothing until it
is in the table: `almanac config set routing.models.large <model id>`, and give
`routing.windows` its context window. To keep a project on one tier:

```bash
almanac route pin large      # until `almanac route pin off`
almanac route status         # mode, project, pin, and the last 10 decisions
```

A pin belongs to a project. Both commands print the project key they used. The
proxy works out a conversation's project from the files it touches, so if that
differs from the folder you pinned in, the pin does not apply.

**If the upstream refuses a routed request** (an unknown or unavailable model,
an unsupported field), Almanac sends the request again without the model
change. If that one is accepted, it marks the tier unavailable for an hour and
shows it in `almanac route status`; if it fails too, or is only overloaded,
nothing is marked. The rest of that turn goes out as Claude Code sent it, which
can change the effort on the same model and so rewrites that turn's cache. The
request is never dropped.

**Audit it.** `almanac route status` lists each decision with its reason, the
estimated cost of staying and of switching, and whether it was applied.
`almanac status` also shows usage for the last 5 and 24 hours (requests, input,
cache write, cache read and output tokens, and a weighted total at list-price
ratios, not the plan's real accounting) and a cache line: the share of input
read from cache, the cold returns (a request that came back after a pause
longer than the shortest cache life it asked for) with the cache writes they
cost, and the cache lives Claude Code asked for. `almanac report --json`
carries the same under `meter`. The log records `cache` and `idle` per request.

`almanac report` also says, for the last 24 hours, how many new conversations
started in a project that had an earlier one, where their prompts first differed
from the previous one's (the tool, system block or message block, top three), and
how many were simply cold (over an hour apart, or another login). The log records
this as `prefix` on each conversation's first request: `diverge_at`, `index`,
`tokens_before`, `tokens_after`, `since_project` and `same_account`. Only hashes
of the blocks and a 12-character hash of the login are kept, and nothing is changed
in the request.

`almanac report` and `almanac status` add requests per tier, moves made and
declined, raises after failures, and tokens per model, read from the replies'
own `usage`. The request log also records the model that really answered.

### 5. Working rules for each model

Every conversation thread gets a short set of working rules added to the end
of its system prompt. The text depends on the model the request is sent with,
after routing. The main thread and its forks get the model's full sheet: Opus
gets `opus.md` and then `common.md`, Sonnet gets `sonnet.md` and then
`common.md`, Haiku gets `haiku.md` alone (a fork shares its parent's cached
prefix, so it gets the same text). A sub-agent does one job and never hands
over, merges or briefs anyone, so it gets only `agent.md` (about 200 words),
plus the skills rules unless the model is Haiku. Any other model gets nothing. Requests without tools (titles, quota checks) and
Almanac's own background calls are left alone.

The text is read once per run and added in the same place every time, so the
system prompt keeps the same bytes and the cache keeps hitting. No cache
breakpoint is added or moved. If a routed request is refused and sent again on
the original model, the retry carries that model's rules.

```bash
almanac cheatsheet show haiku    # the exact text added, and its size in tokens
almanac cheatsheet show agent    # what a sub-agent gets instead
almanac cheatsheet path          # where your own copies go
almanac config set cheatsheet.mode off
```

To change the text, put a file with the same name (`opus.md`, `sonnet.md`,
`haiku.md`, `common.md` or `agent.md`) in the folder `almanac cheatsheet path` prints. It
replaces the packaged one from the next run.

## Bundled skills and agents

Almanac carries its own set of skills and sub-agents and loads them into every
session it starts, with Claude Code's `--plugin-dir`. Nothing is copied into
the Claude config folder, so they are present under any account and only in
sessions started through `almanac`. They are fixed copies kept in this
repository (`src/getalmanac/bundle/`), so there is nothing to install or update.

| Plugin | What it adds |
|---|---|
| `egm` | Plan before building: brainstorm, draft a design document, review it with fresh reviewers |
| `interfaces` | How to build and finish an interface: typography, colour, layout, accessibility, review |
| `astryx` | Working with the Astryx design system |
| `untangle` | A phased clean-up of a whole codebase |
| `security-audit` | A multi-phase security audit with validated findings |
| `crew` | 71 specialist sub-agents (planning, review per language, build fixes, audits); 62 of them are pointed at the skills above |
| `api-anything` | Calling a website through its own data requests, for scrapers and sites with no API. Runs locally only: nothing goes to its shared catalog |
| `bla` | Hardening release artifacts against automated reverse engineering |
| `almanac` | Almanac's own skill: how to write a [playbook](#playbooks) |

The [working rules](#5-working-rules-for-each-model) tell each model when to reach for
them, so no slash command is needed: every bundled skill may be started by
the model itself. Fable gets only that part of the rules (`fable.md` and
`skills.md`), since its own working method is the one the others are taught.
Where each plugin came from, at which commit, and what was changed is in
`src/getalmanac/bundle/SOURCES.md`. `almanac config set bundle.enabled false`
turns the whole bundle off.

`api-anything` and `bla` each start a small server through `npx` at a fixed
version (Node 22 or newer). That code comes from npm on first use.

## Playbooks

A playbook is a recorded way of doing a task you repeat: when it applies, the
ordered list of decisions, which skills and agents were used, and, where the
job is mechanical, a script that does all of it with no model in the loop. The
point is that the next run does not work the approach out again, and where a
script exists the job is one tool call.

Playbooks live in `~/.almanac/playbooks/<name>/` (`almanac playbook path`
prints it): `playbook.json` and, optionally, `run.py`. A name is lowercase
letters, digits and hyphens. A playbook has `triggers`, short lowercase
phrases; a trigger hits when every word of it is in your request. A playbook
can be tied to one project or apply everywhere.

**How a match shows up.** For the newest request you typed in the main
conversation (not sub-agents), Almanac scores the playbooks with plain code
and no model call. The one with the most trigger words hit wins; ties go to the
one used more, then by name. A request shorter than 12 characters, or a plain
"yes" or "continue", never matches. A match adds one short note to that
message, at most 1,500 characters: the playbook's name, when it applies, its
steps, its skills and agents, and how to run its script. The note is stored the
first time it is made, so every later request of that conversation, and any
replay, carries the same text even if you edit or delete the playbook; the
prompt cache keeps hitting. Each match counts as one use.

**Drafts and approval.** Claude writes playbooks, and a script it writes could
do anything, so a script is a draft until you approve it. `almanac playbook
approve NAME` prints the first 40 lines of `run.py` and its hash, then records
the hash. If `run.py` changes in any way afterwards, the playbook goes back to
draft. A draft script is never run by Almanac; the note tells Claude the path so
it can run it with Bash. What Claude or the miner writes is a draft, with or without a script: a draft
is not offered in matches, in the system prompt's list or by `playbook_find`
until you run `almanac playbook approve NAME` (a playbook with no script is
approved with no script text to read; `list` shows it as `draft`, and the
launch line names new drafts). A playbook's text cannot contain a
`system-reminder` tag: it is refused on save and a file that has one does not
load.

**Automatic playbooks.** The note only asks Claude to call `playbook_run`, and
Claude may not. `almanac playbook approve NAME --auto` approves the script and
also lets Almanac run it itself. From then on, when the playbook is the best
match for a request you typed and some trigger of two or more words has every
word in it (a one-word trigger never starts a script), Almanac runs `run.py`
before the request goes to Claude: in the folder of that conversation, with the
same Python and no shell as `playbook_run`, and the whole process tree stopped
after 60 seconds. Its output goes to temporary files and only the first 3,000
characters are kept. This happens on every request that matches, one run per
typed message at its place in the conversation (the later requests of the same
message reuse the stored result; typing the same words again after a rewind
runs it again). The store is not locked while the script runs. The note says
the script has already run, gives its exit code and its output as data, not
instructions (3,000 characters in all, with any `system-reminder` tags
removed), and tells Claude to answer from that, or, if the exit code is not 0
or it timed out, to say so and carry on by hand with the steps.

An automatic script runs in the conversation's folder with no model and no
prompt in between, so a script that runs that project's own tests or build runs
that repository's code. Read the script before you approve it with `--auto`.

Only you can set this, and the approval is kept apart from the playbook:
`--auto` writes the script's hash into `~/.almanac/auto.json` (outside the
playbooks folder), and a playbook runs by itself only when its `playbook.json`
says `auto` and `auto.json` holds the hash of the script as it is now. A
playbook Claude saves or updates is never automatic (a save also removes its
`auto.json` entry), `approve NAME` without `--auto` and `remove` delete the
entry, and a changed script goes back to draft and stops running. `--auto` is
refused for a playbook with `args` (a script that needs inputs cannot be run
from the words of a request) and for one with no trigger of two or more words.
`almanac playbook list` and `show` say `auto` only while it would really run
now, and say the script changed when it was edited after approval. The log line
of that request has `playbook.ran` and `playbook.exit` (or `playbook.timeout`).
Nothing runs when `playbooks.auto_run` is `false`, when the conversation's
folder is missing, or for sub-agents.

**A reminder when the offer is skipped.** If a playbook with an approved script
matched but is not automatic, and Claude has made two tool calls in that
message without calling `playbook_run` (a `ToolSearch` call does not count),
Almanac adds one more short note after the tool results of the newest message,
telling Claude to call `playbook_run` for it now unless the request really
differs. It is added once per message, stored, and put back in the same place
on every later request, so the prompt cache keeps hitting. If the history was
compacted or rewound so that the place is gone, it is left out.

**The three tools** (served by `almanac mcp`, with the memory tools):

- `playbook_find {query}`: the top 5 playbooks for some words.
- `playbook_save {playbook, script}`: write or update a playbook. The bundled
  `playbook` skill tells Claude when and how.
- `playbook_run {name, args}`: run an approved script with the Python from
  Almanac's own environment, in isolated mode, without a shell, in the session's
  folder, for at most 120 seconds. It returns the exit code, stdout and stderr
  (20,000 characters each at most).

**Commands.** `almanac playbook list`, `show NAME`, `approve NAME [--auto]`,
`remove NAME`, `path`.

**Settings.** `playbooks.enabled` (default `true`) turns the whole feature off;
`playbooks.inject` (default `true`) set to `false` stops the notes but leaves
the tools.

**Finding playbooks by itself.** Almanac can also notice a task you repeat in
your past sessions and write it up, with no effort from you.

- `almanac playbook mine [--days 30] [--min 3]` uses no model. It reads Claude
  Code's own session files (`<config folder>/projects/<project>/<session>.jsonl`)
  in `~/.claude`, in `$CLAUDE_CONFIG_DIR` if set, and in every folder listed in
  `playbooks.transcript_dirs` (for a second account, for example
  `almanac config set playbooks.transcript_dirs '["~/.claude-work"]'`). It only
  reads: nothing in those folders is changed. It skips sub-agent lines, files
  older than `--days`, and folders of Almanac's and other tools' own background
  sessions (names containing `claude-mem-observer`, `AppData-Local-Temp` or
  `-Temp-`). For each request you typed that was followed by at least 3 tool
  calls, it keeps the text (secrets removed, cut to 600 characters), the
  session, the project, the names of the tools used (for Bash only the first
  two words of the command) and the tokens spent. It never keeps a tool result,
  Claude's replies or a command's full text. Requests with similar wording are
  grouped; a group with at least `--min` requests from at least 2 sessions, that
  no playbook's triggers already match, is a candidate. The top 20 by tokens
  are saved in `~/.almanac/playbooks/_candidates.json` and printed as a table
  with the count, sessions, tokens and a first example.
  **Ways of working.** Often you do the same job in different words ("ship
  it", "open the PR", "wrap this up"). So the miner also groups requests by
  how the work was done, whatever the words. Each tool call falls into a
  *phase*: branch, edit, test, commit, push, pr, ci, merge, release, sync,
  install, delegate, skill, web, browser or schedule. A command chained with
  `&&`, `;` or `|` counts as one step per command. Text in quotes and the
  body of a here-document are ignored, and a step keeps only the program and
  one or two plain words after it (`gh pr merge`, `python -m build`), never a
  path or an argument. Read-only filters (`head`, `tail`, `cut`, `cat`, `wc`,
  `sort`, `uniq`, `grep`, `ls`, `tr`, `tee`, `xargs`, `sed -n`) are left out of
  the steps. Requests whose sets of phases overlap by 75% or
  more, with at least 3 phases in common, from at least 2 sessions, that no
  playbook's triggers already match, make a candidate marked `way of working`, shown in the table as the phases in the
  order they usually occur (for example `branch > edit > commit > push > pr`).
  Reading, searching and one-off commands have no phase and do not count. A
  group that is mostly one worded group again is not listed twice, and one
  whose phases mostly match a way of working already compiled, or the `phases`
  a stored playbook of the miner records, is not compiled again. A candidate
  of this kind is compiled like any other, but the playbook is written for the
  way of working, not for one request.
- `almanac playbook compile [--top 1] [--dry-run]` takes the best candidate not
  yet compiled and runs one headless Claude Code session (Sonnet, in a fresh
  temporary folder, allowed only to read and write there and to run Python) to
  write it as `playbook.json` and, if every step is mechanical, `run.py` with a
  self-test. The candidate is given to it as quoted data, not as instructions.
  Almanac, not the model, then reads the two files, checks them like any
  playbook, saves them as a draft with source `miner`, and deletes the folder.
  A candidate that does not produce a valid playbook is not tried again for 7
  days. `--dry-run` prints the prompt and the command and runs nothing.
- **In the background.** Each time `almanac` starts Claude Code, a detached
  process mines your sessions if the candidates are more than a day old and
  then compiles within the daily limit. It never delays the start and prints
  nothing into the session (its log is `~/.almanac/logs/playbook-miner.log`).
  The cost is at most `playbooks.compile_per_day` Sonnet sessions a day on your
  Claude Code login (one by default; `0` turns compiling off). When a draft made
  this way waits for your approval, the next start shows one line with its name.
  A script it wrote runs only after `almanac playbook approve NAME`, like any
  other. While a session stays open, the same check repeats every 6 hours, with
  no output.
- **Playbooks on hand.** A way of working cannot be found from the words of a
  request such as "yes, do it". So when `playbooks.enabled` and
  `playbooks.inject` are on, the cheatsheet ends with a list of the playbooks
  you have that apply everywhere: one line each, by name, 12 at most. It goes
  to the main thread and its forks, not to other sub-agents or to Haiku. Claude is told to call
  `playbook_find` with a playbook's name, which returns its full steps, and to
  run one marked `(script)` with `playbook_run`. The list is read once per run,
  so it does not change the system prompt in the middle of a run.

Settings for this: `playbooks.background` (default `true`),
`playbooks.compile_per_day` (default `1`) and `playbooks.transcript_dirs`
(default `[]`).

## Installing skills safely

Skills and plugins from other people run with your permissions. Almanac puts a
scan in front of them, using NVIDIA's SkillSpector, which you install once:

```bash
uv tool install git+https://github.com/NVIDIA/skillspector.git
```

Almanac runs it as a separate program, always with `--no-llm`, so no file
contents are sent to a model. SkillSpector still sends the names and versions of
dependencies it finds to api.osv.dev to look up known vulnerabilities. Without
SkillSpector nothing is scanned, and Almanac says so; it never treats a skill as
fine because it could not check it.

**Scan something**

```bash
almanac skill scan ~/.claude/skills/some-skill
almanac skill scan https://github.com/someone/skills
```

It prints the score, the verdict and each issue with its file and line. The
exit code is 0 for SAFE (score 0 to 20), 1 for CAUTION (21 to 50), 2 for
DO_NOT_INSTALL (51 to 100) and 3 when the scanner is missing or failed. The
verdict is a pattern check: a security skill that explains attacks can score
high without doing anything harmful, so read the issues.

**Install a skill**

```bash
almanac skill add https://github.com/someone/skills --skill their-skill --ref <full commit SHA>
```

The source is fetched into `~/.almanac/quarantine/` with git hooks, links,
submodules and LFS turned off, and nothing in it is run. Each folder with a
`SKILL.md` that would be installed is scanned. Only if every one is SAFE are
they copied (without links or `.git`) to `~/.claude/skills/<name>`, or under
`CLAUDE_CONFIG_DIR` when that is set. CAUTION is refused unless you add
`--accept-caution`. DO_NOT_INSTALL or a failed scan is always refused; there is
no flag to override it. An existing skill is kept unless you add `--replace`.
Each install is recorded in `~/.almanac/skills.json` with its source, commit,
content hash, score and verdict. Skill names with `..`, a drive or an absolute
path are refused.

A repo that is a Claude Code plugin with hooks or MCP servers is not installed.
Almanac scans it, lists the hooks and MCP commands it declares, and prints the
`claude plugin` commands for you to run yourself.

**At every start**

`almanac` hashes each skill folder and each installed plugin. Anything flagged
by an earlier scan, and still unchanged, is shown in a warning block before
Claude Code starts, with the command for the full report. It is not blocked or
removed; that is your call. Anything new or changed is scanned by a background
process, at most 5 per start, because one scan takes from about 15 seconds to
several minutes. Its results show at the next start. On a machine with many
skills the first starts work through them 5 at a time; `almanac skill audit`
scans all of them now and prints a table (`--again` rescans everything).
Hashing stops after 10 seconds, even part way through a folder, and what is
left waits for the next start.

Once you have read an item's findings, `almanac skill accept NAME` stops its
launch warning. It prints what you are accepting and needs a stored scan of the
item as it is now. The acceptance holds for those exact files only: any change
brings the scan and the warning back. `almanac skill unaccept NAME` undoes it.
Accepting changes nothing in `skill add`, where DO_NOT_INSTALL still has no
override. Set
`skills.scan_on_launch` to `false` to turn this off.

**The pack**

`pack.json` (shipped with Almanac) lists third-party skills and plugins, each pinned to a
full commit SHA. `almanac pack list` shows them. `almanac pack install [NAME]`
installs the skill entries through the same scan as `skill add`, at the pinned
commit. Plugin entries are only scanned and shown with their manual commands,
and program entries (the scanner, a secret scanner) are only listed. `--file
PATH` reads another pack file.

## Why the cache keeps working

A proxy that rewrites requests can easily make every turn a cache miss. Almanac
avoids that with one rule: **the same request is always rewritten to the same
bytes.** Each decision (which skills stay full, which tools are deferred, what
memory was recalled, what the compacted prefix is) is made once per
conversation and stored. In the author's live runs the request after a rewrite
read 24,248 of 28,165 input tokens from cache.

One honest cost: Anthropic shares a cache of Claude Code's stock tool list
across all users, and a changed tool list does not hit it. The first session
after an idle period writes its own copy, and later sessions read it back. The
length of that period (thought to be about an hour) has not been measured here.

## Remote Control

Claude Code only offers Remote Control when its base URL is Anthropic's own.
Because `almanac` normally works by changing that base URL, `/remote-control`
is not offered in a normal `almanac` session.

HTTPS-proxy mode keeps it. The base URL is left alone and Almanac is set as
Claude Code's HTTPS proxy instead:

```bash
almanac config set remote_control true      # every session
ALMANAC_HTTPS=1 almanac                     # or just this one
```

To read the model requests in this mode Almanac has to end the TLS connection
itself. It does that as narrowly as it can:

- A certificate authority is made for each launch, limited by a name
  constraint to `api.anthropic.com`. It signs one certificate and its key is
  deleted, so it can never sign another.
- Only Claude Code trusts it, through `NODE_EXTRA_CA_CERTS` in its own
  environment. Nothing is added to your system or user trust store.
- Connections to every other host pass through as opaque tunnels.
- The proxy address carries a random token, so other programs on the machine
  cannot use it.

It needs the `openssl` program (Git for Windows, macOS and Linux ship it) and
steps aside with a one-line notice if you use a proxy it cannot go through
(see below). A corporate `http://` proxy is fine: Almanac keeps using it.

**Status:** tested as far as a model request going through the mode and
`/remote-control` appearing in the command menu. A Remote Control session
driven from a phone has not been tested yet, which is why the mode is off by
default.

## Cloud hand-off

A Claude Code cloud session runs on Anthropic's machines, so it keeps going
while this machine is offline. It uses the same allowance as your local
sessions.

A cloud session starts from a prompt and the repository, and nothing else. It
does not get the local conversation or this memory. So Almanac writes the
prompt: a hand-off packet, built from what it stored about the conversation and
from `git`. It holds the last message you typed, your earlier requests, one
line per exchange of what was done, the transcript of the latest work, and the
state of the repository.

```bash
almanac cloud packet          # read the packet first; nothing leaves the machine
almanac cloud send            # show it again, ask, then start a cloud session
```

Both use the latest conversation of the project the current folder belongs to.
`--conversation ID` picks another one.

`send` prints the packet and what the cloud will and will not see, and asks
before it runs `claude --cloud <packet>` in the current folder. `--yes` skips
the question; without a terminal to ask in, `send` refuses unless you give it.

The cloud session clones the GitHub remote at your current branch, not this
folder. So push first. `send` names the branch and its upstream and lists what
the cloud will not see: commits that are not pushed, changes that are not
committed, and untracked files.

To get the work back:

- `claude --teleport` pulls a cloud session back to this machine.
- When GitHub is connected at claude.ai, the session pushes its work to a
  `claude/` branch.

This proxy and its memory do not travel, apart from the packet. Claude Code
decides what else a cloud session gets from this machine (settings, hooks, MCP
servers); Almanac does not know.

- The packet is capped at 24,000 characters, because it travels as one
  command-line argument. Past that, the oldest lines of "what was done" are
  left out and the packet says how many. The task is never cut short of 6,000
  characters. On Windows the packet is made shorter until the quoted command
  fits the 32,767-character limit.
- Common secret patterns are taken out of it, the same ones as from stored
  text. That is best effort, not complete, and the packet also holds file
  paths and the remote's address. Tool output is not sent at all: each result
  is one line naming the tool and its size, while Claude's own text and the
  tool calls stay. Read it with `almanac cloud packet` first.
- On Windows, `send` will not run Claude Code through a `claude.cmd` or `.bat`
  launcher (an npm install), because `cmd.exe` would cut the packet at its
  first line. Point `ALMANAC_CLAUDE` at `claude.exe`.
- When `claude --cloud` fails, `send` says so: it needs a Claude Code with
  `--cloud` and a login with a claude.ai account.
- An exchange is stored when Claude Code finishes its turn. If you stop it in
  the middle of a turn, the packet's task is the message before that one. Read
  the packet first.

**Status:** the packet and the command are tested with the call to Claude Code
replaced. A cloud session started from a packet has not been tried yet.

## Automatic hand-off

Off by default, and opt-in per repository: it needs `cloud.auto` on **and**
`almanac cloud on` run inside the repository. A repository you have not turned
on gets no branch, no heartbeat and no git command from Almanac. When it is on,
a task that stops because this machine went quiet (offline, asleep, shut down)
is picked up by a scheduled cloud task.

Nothing can start a cloud session from a machine with no network, so the
trigger has to wait in the cloud already. It works like this:

- While a task runs, Almanac pushes a snapshot of it to the branch
  `almanac/handoff` on `origin`, once every heartbeat (10 minutes by default).
  The snapshot is your current commit plus your uncommitted changes to tracked
  files, plus the hand-off packet as `ALMANAC_HANDOFF.md` and a small
  `almanac-handoff.json` (state, time, base branch and commit). Untracked files
  never go in. A changed tracked file whose name looks like a credential
  (`.env`, `*.pem`, `id_rsa`, ...), that holds a common secret pattern, that is
  not text or that is over 1 MB stays at its committed version, and the packet
  lists it. It is built with a separate index, so your index, HEAD, branches
  and `git status` stay exactly as they were. Only that one branch is pushed:
  no tags, whatever `push.followTags` says.
- "Running" is what the proxy sees: a model request came in within the last
  heartbeat, or the last reply asked for a tool. When a reply ends the turn,
  Almanac pushes once with state `idle` and stops until the next request.
- A scheduled task you create in the cloud runs every hour. If the hand-off
  branch says `working` and is older than three heartbeats, it takes over: it
  makes a `claude/almanac-takeover-<commit>` branch, carries on the task,
  pushes and opens a draft pull request against your branch. Otherwise it
  stops at once.
- If the upstream itself keeps failing while this machine can still reach
  GitHub (an API outage or overload, not a dead network), waiting 30 minutes
  would be wasted. After `cloud.stall_after` failed attempts in a row (5 by
  default; `0` turns this off) Almanac pushes the snapshot at once with state
  `stalled`, and the next hourly run takes it over without the age check.
  Only a connection error, a timeout or a status of 500 or more counts, 529
  included. A 429 is your plan's usage limit, which a cloud session draws on
  too, and 400, 401 and 403 are not outages, so none of them count. Any reply
  below 400 resets the count and pushes `working` again at once. One `stalled`
  push per stall; if it fails, the normal heartbeat rules go on.
  **Known limit:** if the local session recovers after a takeover has started,
  both carry on: the cloud one on its own `claude/almanac-takeover-...`
  branch. Almanac cannot stop it, so check `almanac cloud status`.

All of it runs off the request path. No git, no `origin`, a refused push or
no network costs one line in the log; requests go out as before.

```bash
almanac cloud watchdog        # the setup steps and the prompt for the scheduled task
almanac config set cloud.auto on
almanac cloud on              # in each repository that should take part (`almanac cloud off` stops it)
almanac cloud status          # on or off here, the last heartbeat, any takeover branch on origin
```

Setup, once: connect GitHub (the Claude GitHub App at claude.ai/code, or
`/web-setup` in Claude Code; on a Team plan an Owner must enable the GitHub
connector), turn `cloud.auto` on and restart Almanac, run `almanac cloud on` in
the repository, then in that repository run `/schedule` and create an hourly task with the prompt `almanac cloud
watchdog` prints.

When a takeover happened, `almanac cloud status` names the branch. Pull the
work back with `claude --teleport` or `git fetch origin <branch>`.

**Status:** tried once for real on 2026-11-17 with a test task and the state
`stalled`. The scheduled task cloned the repository, read the hand-off, made
the takeover branch and did the task. Its push was refused: the Claude GitHub
App had no access to the repository. A public repository is enough to clone
but not to push, so the app (or `/web-setup`) is needed either way. A takeover
that ends in a pushed branch and a draft pull request has not been seen yet.

Know this before you turn it on:

- **What is sent where.** To the `origin` remote of each repository you turned
  on, branch `almanac/handoff`, force-pushed (that
  branch only): your uncommitted edits to tracked files, every local commit
  not pushed yet (they are the snapshot's history), and the packet. The packet
  is your conversation's task and recent transcript, including tool output
  such as file contents and command output from outside the repository, and
  absolute paths. Common secret patterns are taken out of it; that is best
  effort, not complete. Read one first with `almanac cloud packet`.
- **Only repositories you turned on.** `almanac cloud on` lists the repository
  by its project (its remote's address), so every clone of it on this machine
  takes part. `almanac cloud off` stops it at the next heartbeat check, without
  a restart; a branch already on origin stays until you delete it. On a public
  repository the branch is public.
- Your pre-push hooks run on every push. The commit says `[skip ci]`, which
  stops GitHub Actions, but other services that build every branch may still
  build it.
- **Anyone who can push to `origin` can write that branch.** A collaborator
  could plant a `working` hand-off, and the scheduled task would carry it out
  with your GitHub access. Turn this on only for repositories only you push to.
- `almanac memory forget` does not touch the branch; delete it with
  `git push origin --delete almanac/handoff`.
- **Cost.** There is no separate charge for the cloud machine, but each
  scheduled run uses a little of your plan, even the runs that find nothing to
  do. On a team with metered usage credits switched on, runs past the plan
  limit can be billed.
- **Delay.** A takeover can come up to one schedule interval plus three
  heartbeats after the machine went quiet (about 1.5 hours with the defaults).
  The first snapshot of a task is pushed one heartbeat after it starts, so a
  task that stops sooner leaves nothing to take over.
- **It cannot tell why the heartbeat stopped.** A laptop put to sleep in the
  middle of a task looks the same as a dropped network, so the cloud may carry
  on work you meant to come back to. Stopping a tool with Esc and then closing
  the laptop looks like a task still running.
- **One hand-off branch per repository.** Two machines, worktrees or
  sessions working in one repository overwrite each other's snapshot, and an
  `idle` from one can hide another that is still working. Work on a detached
  HEAD is never taken over.
- The setting is read when Almanac starts, so restart it after a change.

**Status: the cloud side is untested.** The snapshot, the heartbeat, the idle
push and what lands on the branch are tested against a local bare repository.
Nothing has been pushed to a real GitHub repository, and the scheduled cloud
task and its takeover have never been run.

## Behind a corporate proxy

If your network sends outbound traffic through an HTTP proxy, Almanac goes
through it too. It looks for the proxy in this order:

1. The `upstream_proxy` setting.
2. `HTTPS_PROXY`, then `ALL_PROXY`, from the environment you start `almanac` in.

`NO_PROXY` is honoured, so a host listed there is reached directly. Almanac
does not use the proxy for Claude Code's own traffic to Almanac. It adds
`localhost` and `127.0.0.1` to `NO_PROXY` for Claude Code.

```bash
almanac config set upstream_proxy http://proxy.example.com:3128
```

- Only `http://` proxies (the usual CONNECT kind) work. `socks5://` and
  `https://` proxies are reported by `almanac doctor` as not supported.
- A login in the URL (`http://user:pass@host:3128`) is sent as basic proxy
  authentication. It is kept in memory only. Almanac never writes it to its
  store or logs, and prints the proxy as `host:port`. Setting it in the
  environment keeps it out of `config.json` as well.
- Basic authentication only. NTLM and Kerberos proxies are not supported.
- If the proxy re-signs HTTPS traffic with a company certificate, Almanac must
  trust that certificate. It trusts the file named by `upstream_ca`,
  `SSL_CERT_FILE` or `NODE_EXTRA_CA_CERTS` (the one Claude Code reads) as well
  as the system's. Verification is never turned off.
- `almanac doctor` shows which proxy is used and where it came from, whether
  an extra certificate file loaded, and whether a connection through the proxy
  works.

## Commands

| Command | What it does |
|---|---|
| `almanac [claude arguments]` | Run Claude Code through the proxy |
| `almanac claude [arguments]` | The same, for arguments that clash with a command below |
| `almanac report [--json]` | Where the latest session's context went, and what was kept out |
| `almanac status` | Settings, files and recent activity, usage for 5 and 24 hours, and cache health |
| `almanac doctor` | Check that everything needed is in place |
| `almanac memory search WORDS` | Search stored conversations (`--project NAME`, `--all`) |
| `almanac memory show ID` | Read one stored exchange or topic |
| `almanac memory recent` | The latest conversations, by project |
| `almanac memory forget` | Delete conversations and their saved choices (`--project NAME`, `--conversation ID`, `--all`). Keeps any changed in the last hour unless you add `--force` |
| `almanac cloud packet` | Print the hand-off packet for a cloud session (`--conversation ID`). Sends nothing |
| `almanac cloud send` | Show that packet, ask, then start a Claude Code cloud session from it (`--conversation ID`, `--yes`) |
| `almanac cloud watchdog` | The setup steps and the prompt for the scheduled task of the [automatic hand-off](#automatic-hand-off) |
| `almanac cloud status` | Whether the automatic hand-off is on, its last heartbeat, and any takeover branch on origin |
| `almanac semantic install` | Download the model for search by meaning (needs `getalmanac[semantic]`) |
| `almanac semantic status` | Whether the packages and model are there, and how many exchanges have a vector |
| `almanac semantic index` | Make vectors for every stored exchange that is waiting |
| `almanac semantic on \| off` | Use the model when it is there, or stay keyword-based |
| `almanac route [status]` | Routing mode, pin, unavailable tiers and the last 10 decisions |
| `almanac route pin small\|medium\|large\|off` | Keep this project on one tier, or lift the pin |
| `almanac cheatsheet show [opus\|sonnet\|haiku\|fable\|agent]` | The working rules added for that model (`agent`: what a sub-agent gets), and their size |
| `almanac cheatsheet path` | Where your own copies of the rules go |
| `almanac skill scan PATH\|URL` | Scan a skill or plugin (exit 0 SAFE, 1 CAUTION, 2 DO_NOT_INSTALL, 3 not scanned) |
| `almanac skill add URL\|PATH` | Fetch into quarantine, scan, install the skills only if SAFE (`--skill`, `--ref`, `--accept-caution`, `--replace`) |
| `almanac skill accept NAME` | Stop the launch warning for an item at its current contents (`skill unaccept NAME` undoes it) |
| `almanac skill audit` | Scan installed skills and plugins not scanned yet, then a table (`--again` for all) |
| `almanac pack list` | The pinned third-party skills and plugins in `pack.json` |
| `almanac pack install [NAME]` | Install the pack's skills through the scan; plugins and programs are only shown |
| `almanac playbook list` | Playbooks: name, status, uses, title. Also `show NAME`, `approve NAME [--auto]`, `remove NAME`, `path`, `mine [--days N] [--min N]`, `compile [--top N] [--dry-run]` |
| `almanac projects` | Projects seen. `projects merge FROM INTO`, `projects rename NAME NEW` |
| `almanac config [set KEY VALUE \| path]` | Show or change settings |
| `almanac serve [--port N]` | Run only the proxy, for clients you start yourself |
| `almanac mcp` | The memory tools over stdio (Claude Code starts this itself) |

## Settings

`almanac config` prints them all. `almanac config set KEY VALUE` changes one.

| Key | Default | Meaning |
|---|---|---|
| `skills.enabled` | `true` | Collapse skill descriptions |
| `skills.stub_chars` | `90` | Characters kept for a collapsed skill |
| `skills.always_full` | `[]` | Skills never collapsed |
| `startup.agent_chars` | `60` | Characters kept for a listed agent type (`0` leaves the agent list alone) |
| `startup.system_prefix` | `false` | Move the stable session sections to the end of the system prompt so new conversations read them from cache |
| `skills.scan_on_launch` | `true` | Scan new or changed skills and plugins in the background at each start, and warn about flagged ones |
| `tools.enabled` | `true` | Defer cold tool schemas |
| `tools.cold` | six tools | Tools deferred unless called for |
| `tools.hot` | `[]` | Tools never deferred |
| `memory.enabled` | `true` | Store finished exchanges |
| `memory.recall` | `true` | Add related memory to a new conversation |
| `memory.tools` | `true` | Give Claude Code the memory tools |
| `memory.keep_days` | `365` | Delete conversations untouched this long (`0` keeps all) |
| `memory.summary_model` | `claude-haiku-5-5` | Model that writes exchange summaries in the background (`""` turns it off) |
| `memory.summary_daily_calls` | `300` | Most summary calls per day |
| `memory.semantic` | `true` | Search by meaning when the packages and the model are installed |
| `compaction.enabled` | `true` | Compact long conversations |
| `compaction.trigger_tokens` | `150000` | Size that starts a compaction |
| `compaction.target_tokens` | `40000` | Most the summary may take |
| `compaction.window_share` | `0.85` | Inside one long turn, old tool results are stubbed once a request reaches this share of the model's window (`0` is off) |
| `compaction.keep_results` | `12` | Tool results of the current turn that are never stubbed (the newest) |
| `compaction.own_the_context` | `true` | Also switch off Claude Code's own auto-compaction (`DISABLE_AUTO_COMPACT=1`) while compaction is on |
| `routing.mode` | `"off"` | `"off"`, `"subagents"` or `"all"`: which requests get a chosen model |
| `routing.models` | Haiku, Sonnet, Opus 5.5 | The model for `small`, `medium` and `large` |
| `routing.ceiling` | `"large"` | The highest tier routing may pick (a pin ignores it) |
| `routing.classifier` | `"rules"` | `"model"` lets a small model settle turns the rules are unsure about |
| `routing.effort` | low, medium, (kept) | Effort per tier, set only where Claude Code already sends one; `""` keeps its value |
| `routing.margin` | `0.2` | How much cheaper a move down must be than staying |
| `routing.escalate_after` | `3` | Errors in a row, or one call failing this often, that raise the next turn |
| `routing.escalate_turns` | `3` | Turns a raise lasts |
| `routing.prices`, `routing.work` | see `config.py` | Relative prices and expected work per turn, for pricing a switch |
| `routing.windows` | 1,000,000 each | Context window per model id (unknown ids count as 200,000) |
| `playbooks.enabled` | `true` | Match requests against playbooks and give Claude the playbook tools |
| `playbooks.inject` | `true` | Add the note about the matching playbook to a request |
| `playbooks.auto_run` | `true` | Run the approved script of a playbook approved with `--auto` before a matching request goes to Claude (`false` never runs one) |
| `playbooks.transcript_dirs` | `[]` | More Claude Code config folders to mine for repeated tasks (`~/.claude` and `$CLAUDE_CONFIG_DIR` are always read), for example `["~/.claude-work"]` |
| `playbooks.background` | `true` | Mine and compile in the background at each start |
| `playbooks.compile_per_day` | `1` | Most Sonnet sessions a day that write a playbook from a repeated task (`0` is off) |
| `cheatsheet.mode` | `"on"` | `"off"` stops adding the working rules for each model |
| `offline.hold_minutes` | `480` | Minutes a model request waits for the network to return (`0` fails at once) |
| `cloud.auto` | `false` | Keep a running task's snapshot on `origin`'s `almanac/handoff` branch (`on`/`off`) |
| `cloud.heartbeat_minutes` | `10` | How often that snapshot is pushed while a task runs |
| `cloud.stall_after` | `5` | Failed upstream attempts in a row (connection error or status 500+) before the snapshot is pushed at once as `stalled`; `0` is off |
| `remote_control` | `false` | Run sessions in HTTPS-proxy mode |
| `port` | `8787` | Where `almanac serve` listens |
| `upstream` | `https://api.anthropic.com` | Where requests are sent |
| `upstream_proxy` | `""` | Corporate `http://` proxy to reach the upstream through. Empty uses `HTTPS_PROXY` |
| `upstream_ca` | `""` | Extra certificate file to trust for the upstream |

For one run, `ALMANAC_DISABLE=skills,agents,tools,memory,compaction` turns those
layers off (`all` turns off every change to a request, the cheatsheet and
playbooks included), and `ALMANAC_HOME` moves the files.

## What is stored, and where

Everything is under `~/.almanac/` (or `ALMANAC_HOME`):

| File | Holds |
|---|---|
| `almanac.db` | The memory tree, exchange transcripts, per-conversation decisions (deleted after 45 days of not being used) |
| `serve.json` | While `almanac serve` runs: its port, its secret and the key that proves it, so `almanac` can find it. Removed when `serve` is stopped with Ctrl+C. A `serve` that is killed leaves it, and the next `serve` overwrites it |
| `config.json` | Settings you changed |
| `logs/requests.jsonl` | One line per request: sizes, token counts, timing. No message text, no credentials |
| `mcp.json` | The entry that tells Claude Code how to start the memory tools |
| `playbooks/` | One folder per playbook: `playbook.json` and, optionally, `run.py` |
| `skills.json` | Skills installed by `almanac skill add`: source, commit, content hash, score, verdict, date |
| `skill-scans.json` | The last scan of each installed skill and plugin, by content hash |
| `quarantine/` | Fetched sources while they are scanned; removed when the command ends |
| `logs/skill-scan.log` | Output of the background scans started at launch |

The memory holds conversation text in plain SQLite on your disk. Delete any of
it with `almanac memory forget`, or all of it by removing the folder. Forget
also removes the saved choices for those conversations (compaction prefixes,
recall notes, which skills stayed full), and the recall notes of every other
closed conversation, because a note can quote the ones you deleted. It leaves a conversation alone if it
changed in the last hour, because that one may still be open; close it and run
forget again, or add `--force`. Saved per-conversation choices are cleaned up
after 45 days without being used, and a conversation that keeps running keeps
its choices.

With `cloud.auto` on, a copy of the latest packet and your uncommitted work
also sits on GitHub, on the `almanac/handoff` branch. Forget does not reach it;
`git push origin --delete almanac/handoff` deletes it.

## If something goes wrong

- A layer that raises an error, a store that cannot be read or written, or a
  request body Almanac cannot parse: the original request is sent as it came.
- If Anthropic answers 400 to a rewritten request, Almanac resends it without
  compaction, and if that is refused too, exactly as Claude Code wrote it. That
  conversation is then left alone, and the reason is in the log. A 400 that says
  the prompt is too long is not counted against compaction, so it can run again.
- Requests that do not come from Claude Code itself (a script you run from its
  shell that inherits the base URL) are relayed untouched.
- To rule Almanac out, run plain `claude`.

## Always on

`almanac` starts its own proxy and stops it when Claude Code exits. If you start
background sessions that outlive the launcher, or use a client `almanac` cannot
launch, run one long-lived proxy instead:

```bash
almanac serve
```

`almanac serve` prints an address that ends in a secret, for example
`http://127.0.0.1:8787/AbC...`. The proxy answers only requests that carry
that secret as the first part of the path. While it runs, `almanac` finds it
and shares it by itself. Any other client needs the printed address:

```
ANTHROPIC_BASE_URL=<the address serve printed, secret included>
_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL=1
```

A new secret is made each time `serve` starts. After a restart every session
that was using the old `serve` gets a 403 until it is started again: close
those Claude Code sessions and run `almanac` again, and give a client you
started by hand the new address. The secret is also in `serve.json` in
the Almanac folder (`almanac config path` shows where), which only you can read
on Linux and macOS. A request without the secret gets a 403 and a line in
`logs/requests.jsonl` with `"kind": "refused"`.

## Security

Almanac is built for one person on their own machine. What it does and does not
protect:

**Protected**

- Only this machine can reach it: it listens on 127.0.0.1 and refuses any other
  `Host`.
- Web pages cannot use it. Any request with an `Origin` header, and every
  `OPTIONS` request, is refused before anything is forwarded.
- A program needs the secret in the address (see [Always on](#always-on)).
  `almanac` makes a new one for each proxy it starts. The check is a constant
  time comparison.
- `almanac` shares a running `almanac serve` only if that server answers a
  random challenge with a value computed from a key in `serve.json`. That key
  is separate from the secret in the address and is never sent in any request,
  so something that just answers on port 8787, or that has seen the address, is
  not trusted.
- On Linux and macOS the Almanac folder is created readable by you only (mode
  0700) and the database and logs are mode 0600.
- Your login is forwarded and never read, stored or logged.
- A network path (`\\host\share` or `//host/share`) that a tool call names is
  never opened to work out a project.

**Not protected**

- Another program running as you. It can read the secret (it is in the
  environment of Claude Code and in `serve.json`), the database and the logs.
  Almanac is not a wall against your own account.
- Other accounts on a shared machine, on Windows. The file modes above are not
  set there. The profile folder's own permissions are what keep others out, and
  that has not been tested with a second account.
- A `serve` that stops while a session is using it. Its port is then free, and
  another local program could take it and receive that session's requests,
  login included. Do not run `serve` on a machine shared with people you do not
  trust.
- What gets stored. Secrets are removed by pattern before text is saved: private
  keys, common API key formats, `NAME=value` where the name ends in `secret`,
  `token`, `password` or `api key`, bearer and basic credentials, and passwords
  in URLs. A secret in some other shape is stored as typed, in plain SQLite. The
  stored text is also sent to the summary model with your login. Claude Code
  keeps its own full transcripts in `~/.claude` regardless.
- The session-start text in a compaction. The stored compaction keeps the
  session-start text exactly as sent, so a secret in it is stored.
- Memory crosses accounts. Memory is kept per project, not per login. If you use
  a work and a personal account, give each its own `ALMANAC_HOME`. A client
  that holds the secret can also read any project's memory by naming it in its
  first message.
- Settings. A login written into `upstream_proxy` is saved in `config.json` in
  plain text. Set `HTTPS_PROXY` in the environment instead.
- The semantic model is a pinned download over HTTPS. It is not checked against
  a hash.
- Deleted text. `almanac memory forget` overwrites deleted rows and empties the
  database's write-ahead file, but it cannot reach backups or the free space of some disks.
  A recall note already added to a conversation that is still open keeps its
  snippets until that conversation is forgotten. `forget --project` deletes the
  conversations filed under that project: an exchange about it inside a
  conversation filed under another project stays until that one is forgotten.
- Injected instructions. Stored text is marked as a quoted record, but a model
  can still be fooled by it.

## Two accounts

If you use two Claude logins (for example `~/.claude` and `~/.claude-work`),
Almanac can carry on with the other one when the first runs out of usage. It is
off until you set `accounts.fallback` to a list of Claude Code config folders:

    almanac config set accounts.fallback '["~/.claude-work", "~/.claude"]'

When Anthropic answers a model request with a usage-limit error, Almanac sends
the same request again with the other login's token, once. If that works, Claude
Code never sees the error, and later requests go straight out on the other login
until the first login's usage comes back, then the first is tried again (and if
it is still refused, the new reset time replaces the old one). Almanac works out
that time from the reply: the `anthropic-ratelimit-unified-reset` header, else
`retry-after`, else a phrase like "resets 2:30am" in the message (read as local
time, or in the zone given in brackets), else `accounts.cooldown_minutes` (30)
from now. The time is kept between 1 minute and 7 days away.
This happens only when the upstream is the Anthropic API itself
(`https://api.anthropic.com`); with any other upstream the limit is passed
through and no other login's token is read.
A plain rate limit (slow down) is passed through as it is. If the other login
cannot take the request either, Claude Code gets the original limit message.
The first switch prints one line on the proxy's stderr with the reset time,
`almanac status` lists the folders and when the first login is tried again, and
the request log records `account` with the folder name (never a token).

Things to know:

- Almanac only uses a login that is already valid. It never refreshes a token
  and never writes a credentials file. If the other login's token has expired,
  start Claude Code once with that account to renew it.
- The prompt cache is per account, so the first request after a switch is a full
  cache write.
- A switch means the other account's plan is used.
- Only Claude Code's own model requests are switched. Its side requests that
  Almanac does not touch still use the login Claude Code started with.
- Tested against a fake upstream and fake credential folders, not a real limit.

## When the network drops

If the upstream cannot be reached, Almanac holds a model request and sends it
when the network returns, instead of failing it. `offline.hold_minutes` (default
480, so 8 hours) sets how long it waits; `0` turns this off and a request fails
at once with a 502, as before. While a streaming request waits, Almanac sends
pings so the connection stays open. A request that is not streamed answers with
an error after at most 9 minutes, because Claude Code stops waiting for a reply
after 10. If the wait runs out, or the upstream then answers with an error, the
stream is cut with no invented message, so Claude Code retries and shows the
real result. A client that gives up while its request waits is dropped and its
request is never sent later. `almanac report` counts the requests that waited.
Tested against a fake upstream that goes down and comes back, not a real
outage, and not with a live Claude Code retry.

## Limits

- Skill and tool matching is keyword-based. Memory search works by meaning only
  after you install the optional `semantic` extra and its model. Its search reads
  every vector of the project, which is fine up to about 20,000 exchanges.
- Summaries are written by a small model in the background, so a new exchange
  has its rule-written text until the call finishes, and a failed call leaves it.
  Summary calls use a little of your allowance.
- Routing is judged from the prompt's wording and a few request facts, so it
  will sometimes choose wrong. Pin a tier, or set `routing.mode` back to `off`,
  when it does. Its prices are guesses for comparing models. The only fields it
  rewrites are `model` and, when Claude Code already sends one, `output_config.effort`.
- Corporate proxies must be plain `http://` ones with no login or a basic login. A company certificate
  that re-signs HTTPS is trusted only if you point to its file. That setup is
  tested with a fake proxy, not a real company network.
- Tested on Windows with a Claude subscription login. The test suite also runs
  on macOS and Linux, but live sessions there have not been tried.
- Claude Code's request format is not a published contract. Almanac looks for
  specific markers in it and leaves a request alone when they are missing, so a
  Claude Code update can reduce what it saves until Almanac catches up.

## Development

```bash
git clone https://github.com/vssehdave/Almanac && cd Almanac
pip install -e .
python -m unittest discover -s tests
```

The tests use a fake upstream. None of them calls a real API.

### Releasing

Pushing a tag `vX.Y.Z` that matches the version in `pyproject.toml` builds the
wheel, smoke-tests it and attaches it to a GitHub release. Almanac is a
personal tool in a private repository and is not published to PyPI.

## Licence

MIT.
