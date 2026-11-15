# Almanac: project brief

Almanac is a local proxy for Claude Code. `almanac` starts a server on
127.0.0.1 and launches Claude Code pointed at it. The proxy forwards every
request to Anthropic with Claude Code's own login, and changes only the request
body, in five ways:

1. **Startup layer.** Skill descriptions are shortened and rarely used tool
   schemas are deferred. Each loads in full when a message needs it.
2. **Project memory.** Conversations are indexed into a local SQLite tree,
   grouped by project. The project is worked out from the files a conversation
   touches, not only from the folder it started in. Summaries start out written
   by rule; a small model then rewrites them in the background through the same
   upstream and login, using a little of the user's allowance. With the optional
   `semantic` extra, a local model also gives each exchange a vector, so search
   finds exchanges by meaning.
3. **Compaction.** Near the context limit the history is compressed once and
   the compressed prefix is kept byte for byte, so the prompt cache keeps
   hitting. Its outline is built from the stored summaries.
4. **Model routing** (off by default). Each prompt is sent to a small, medium or
   large model with a matching effort, and sub-agents are routed from a
   `Difficulty:` line. A model switch rewrites the history into the new model's
   cache, so a move down on the main thread is priced first. The choice is
   frozen per turn and a refused model falls back to the original request.
5. **Cheatsheets** (`cheatsheet.mode`, on by default). Each conversation thread
   gets short working rules at the end of its system prompt, chosen by the
   model the request is sent with after routing. The text is read once per run
   and placed so that no cache breakpoint is added or moved.

## Rules that hold everywhere

- **Fully local.** The only network peer is the upstream the user's Claude Code
  already talks to. No other host receives conversation content. Never add a
  hosted service or telemetry. The one other host is Hugging Face, contacted only
  by `almanac semantic install` to download a model, with no conversation content.
  Summary calls go to that same upstream, and `memory.summary_model` set to `""`
  turns them off.
  - `almanac cloud send` sends stored conversation text to Claude Code's own
    cloud option, only when the person runs it.
  - With `cloud.auto` on (off by default), the proxy pushes a snapshot of a
    running task, packet included, to the user's own `origin` remote on the
    `almanac/handoff` branch. Off must mean no git command runs at all.
- **Never read, store or log credentials.** Auth headers are forwarded as
  received and never written anywhere. A background summary call holds them in
  memory for the length of the job only.
- **Fail open.** If a transform raises, or the upstream rejects a transformed
  body, the original request goes out unchanged.
- **Deterministic rewrites.** The same request must always be rewritten to the
  same bytes, or the prompt cache breaks. Decisions that could drift are frozen
  per conversation in the store.
- **Standard library only** for the core. Optional extras may add dependencies.

## Layout

- `src/getalmanac/` is the package (`almanac` is the command).
- `tests/` uses `unittest` and a fake upstream. No test calls a real API.
- Run tests with `python -m unittest discover -s tests`.

## Commit rules

- Author as the owner only. No assistant attribution, co-author or
  "generated with" lines in commits or pull requests.
- Conventional Commits: `type(scope): description`, imperative mood, subject
  at most 50 characters with no trailing period, body wrapped at 72 explaining
  why. Atomic commits.
- One branch per phase, then a pull request, then merge.
- Do not name other tools this project drew ideas from in commits, comments or
  docs.
- Almanac is the owner's personal tool in a private repository. It is installed
  from the checkout (`pip install -e .`) and is never published to PyPI.
