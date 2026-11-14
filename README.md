# Almanac

A local proxy for Claude Code. It makes each session start with less context,
gives every project a memory, and keeps long conversations inside the context
window. Nothing leaves your machine except the requests Claude Code was already
sending to Anthropic.

```
Claude Code  ──▶  almanac (127.0.0.1)  ──▶  api.anthropic.com
                     │
                     └── ~/.almanac/   SQLite memory, settings, a sizes-only log
```

- **No account, no server, no telemetry.** One Python package with no dependencies.
- **Your login is untouched.** Claude Code keeps its own sign-in. Almanac forwards
  the headers it receives and never reads, stores or logs a credential.
- **It fails open.** If anything in Almanac goes wrong, your request is sent
  exactly as Claude Code wrote it.

## Install

Python 3.10 or newer and Claude Code are the only requirements.

```bash
# with pipx (recommended)
pipx install git+https://github.com/vssehdave/Almanac

# or with uv
uv tool install git+https://github.com/vssehdave/Almanac

# or with pip
pip install git+https://github.com/vssehdave/Almanac
```

A wheel is attached to every [release](https://github.com/vssehdave/Almanac/releases)
if you would rather install a file. Then check the setup:

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
Everything else is relayed byte for byte.

### 1. A smaller session start

Every session begins by sending every skill description and every tool schema,
whether or not the conversation will use them.

- **Skills.** Each description is cut to a short stub. A skill keeps its full
  description when your first message matches it, when you list it in
  `skills.always_full`, or when two of the project's last ten conversations used
  it. If a later message matches a collapsed skill, its full description is
  placed next to that message. Every skill stays listed and can be invoked by
  name at any time.
- **Tools.** Schemas of rarely used tools (`tools.cold`) are deferred. Their
  names are added to the list of deferred tools Claude Code already keeps, and
  the model loads one through its normal tool search when it needs it. A tool
  your message names, a tool the conversation already called, or one used in two
  recent conversations stays loaded.

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
three times. A rate-limit or login error pauses summary calls for an hour. Set
`memory.summary_model` to `""` to turn this off. `almanac status` shows how many
exchanges have a model-written summary and what today's calls used.

Obvious secrets (API keys, tokens, private keys, `password=` values) are removed
before text is stored, including from model-written summaries.

### 3. Compaction that keeps the cache

When a conversation passes `compaction.trigger_tokens` (150,000 by default) and
you send a new message, everything before that message is replaced by:

- the session-start context, unchanged,
- an outline of every earlier exchange, and
- the most recent exchanges in full.

That replacement is stored and sent unchanged on every later request, so
Anthropic's prompt cache keeps hitting. It is rebuilt only after the
conversation has grown a good deal again, and thrown away if you rewind or edit
the covered messages.

The outline of earlier exchanges is built from the stored summaries: the
model-written ones when they exist, the rule-written ones otherwise. It is
plainer than what `/compact` produces, so each exchange's transcript is also
kept in the local memory and Claude can read any of them back by number. Compaction
happens only when you send a message. During a long tool loop Claude Code's own
compaction still applies. If your model has a larger window, raise the trigger:

```bash
almanac config set compaction.trigger_tokens 600000
```

## Why the cache keeps working

A proxy that rewrites requests can easily make every turn a cache miss. Almanac
avoids that with one rule: **the same request is always rewritten to the same
bytes.** Each decision (which skills stay full, which tools are deferred, what
memory was recalled, what the compacted prefix is) is made once per
conversation and stored. In the author's live runs the request after a rewrite
read 24,248 of 28,165 input tokens from cache.

One honest cost: Anthropic shares a cache of Claude Code's stock tool list
across all users, and a changed tool list does not hit it. The first session
after an idle hour writes its own copy. Sessions within the hour read it back.

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
| `almanac status` | Settings, files and recent activity |
| `almanac doctor` | Check that everything needed is in place |
| `almanac memory search WORDS` | Search stored conversations (`--project NAME`, `--all`) |
| `almanac memory show ID` | Read one stored exchange or topic |
| `almanac memory recent` | The latest conversations, by project |
| `almanac memory forget` | Delete conversations (`--project NAME`, `--conversation ID`, `--all`) |
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
| `tools.enabled` | `true` | Defer cold tool schemas |
| `tools.cold` | six tools | Tools deferred unless called for |
| `tools.hot` | `[]` | Tools never deferred |
| `memory.enabled` | `true` | Store finished exchanges |
| `memory.recall` | `true` | Add related memory to a new conversation |
| `memory.tools` | `true` | Give Claude Code the memory tools |
| `memory.keep_days` | `365` | Delete conversations untouched this long (`0` keeps all) |
| `memory.summary_model` | `claude-haiku-5-5` | Model that writes exchange summaries in the background (`""` turns it off) |
| `memory.summary_daily_calls` | `300` | Most summary calls per day |
| `compaction.enabled` | `true` | Compact long conversations |
| `compaction.trigger_tokens` | `150000` | Size that starts a compaction |
| `compaction.target_tokens` | `40000` | Most the summary may take |
| `remote_control` | `false` | Run sessions in HTTPS-proxy mode |
| `port` | `8787` | Where `almanac serve` listens |
| `upstream` | `https://api.anthropic.com` | Where requests are sent |
| `upstream_proxy` | `""` | Corporate `http://` proxy to reach the upstream through. Empty uses `HTTPS_PROXY` |
| `upstream_ca` | `""` | Extra certificate file to trust for the upstream |

For one run, `ALMANAC_DISABLE=skills,tools,memory,compaction` (or `all`) turns
layers off, and `ALMANAC_HOME` moves the files.

## What is stored, and where

Everything is under `~/.almanac/` (or `ALMANAC_HOME`):

| File | Holds |
|---|---|
| `almanac.db` | The memory tree, exchange transcripts, per-conversation decisions |
| `config.json` | Settings you changed |
| `logs/requests.jsonl` | One line per request: sizes, token counts, timing. No message text, no credentials |
| `mcp.json` | The entry that tells Claude Code how to start the memory tools |

The memory holds conversation text in plain SQLite on your disk. Delete any of
it with `almanac memory forget`, or all of it by removing the folder.

## If something goes wrong

- A layer that raises an error is skipped and the original request is sent.
- If Anthropic answers 400 to a rewritten request, Almanac resends it without
  compaction, and if that is refused too, exactly as Claude Code wrote it. That
  conversation is then left alone, and the reason is in the log.
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

While it runs, `almanac` shares it. Other clients can use it by setting
`ANTHROPIC_BASE_URL=http://127.0.0.1:8787` and
`_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL=1`.

## Limits

- Matching and memory search are keyword-based. There is no semantic search yet.
- Summaries are written by a small model in the background, so a new exchange
  has its rule-written text until the call finishes, and a failed call leaves it.
  Summary calls use a little of your allowance.
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
wheel, smoke-tests it and attaches it to a GitHub release. Publishing to PyPI is
a separate, manually started workflow (`publish`), and needs a trusted publisher
set up for the `getalmanac` project on PyPI first.

## Licence

MIT.
