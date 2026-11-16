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
| `crew` | github.com/affaan-m/ECC (`agents/`) | d2ef2680 | MIT | manifest written here; descriptions shortened, models set, skill sections added, references to its own commands removed |
| `api-anything` | github.com/goodnight000/api-anything | d40abd44 | MIT | skill only; manifest written here: MCP server pinned to 0.4.0 and set to local-only, so nothing goes to its shared catalog |
| `bla` | github.com/qybaihe/bla | 883c5bac | MIT | skill only; manifest written here: MCP server pinned to 3.4.1 |

In every folder, `disable-model-invocation` was removed from the skills that had
it, so the model can start any of them without a slash command. In `crew`, 62
agents carry a "Skills to use" section.

To take a newer version of one, replace its folder from the source at a new
commit, scan it with `almanac skill scan`, and update this table.

The two plugins with an MCP server start it with `npx` at a fixed version. That
code comes from npm when first run and is not in this repository, so the scan
covered their skill text only.
