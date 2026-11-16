# Bundled skills and agents

Each folder here is a Claude Code plugin that `almanac` loads into every session
with `--plugin-dir` (turn it off with `almanac config set bundle.enabled false`).
They are fixed copies, kept in this repository so nothing has to be installed or
updated. Each was scanned and its findings read before it was added. Tests,
icons and marketplace files were left out; nothing else was changed unless noted.

| Folder | From | Commit | Licence | Changed |
|---|---|---|---|---|
| `egm` | github.com/google/elephant-goldfish-model | 72d06f52 | Apache-2.0 | icon line removed from the manifest; brainstorm, draft and review may be started by the model |
| `interfaces` | github.com/jakubkrehel/skills | a72290d1 | MIT | interface-review may be started by the model |
| `untangle` | github.com/caldoai/skills | f3cf4777 | MIT | manifest written here |
| `astryx` | github.com/meta/astryx (`plugins/astryx`) | 312604ad | MIT | nothing |
| `security-audit` | github.com/cloudflare/security-audit-skill | c1c8a8c1 | MIT | manifest written here |

To take a newer version of one, replace its folder from the source at a new
commit, scan it with `almanac skill scan`, and update this table.
