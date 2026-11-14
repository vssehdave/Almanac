# Changelog

## Unreleased

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
