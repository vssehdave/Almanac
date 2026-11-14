# Changelog

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
