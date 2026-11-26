# Bundled skills and agents

The one plugin kept in this package, `almanac` (its skills and the owner's agents), is loaded into every session with
`--plugin-dir` (turn it off, with your own bundle, by `almanac config set bundle.enabled false`).
Apart from `find-skills`, third-party skills are not shipped: you adopt the ones you choose with
`almanac skill add --bundle`, and they are kept in `<almanac home>/bundle/`, each
with a row in that folder's own `SOURCES.md`.

| Folder | From | Commit | Licence | Changed |
|---|---|---|---|---|
| `almanac` | written here | n/a | MIT | the `adopt` skill and the manifest |
| `almanac/skills/find-skills` | github.com/vercel-labs/skills (`skills/find-skills`) | 57217cf8 | MIT (its `LICENSE` kept beside it) | description leads with what it does; installs go through `adopt` instead of `npx skills add`; search kept; `npx skills init` replaced by playbooks |
| `almanac/agents/surveyor.md`, `almanac/agents/inspector.md` | github.com/affaan-m/ECC (`agents/code-explorer.md`, `agents/pr-test-analyzer.md`, via the owner's `crew` bundle) | d2ef2680 | MIT | renamed; surveyor kept on Haiku, inspector to Opus; inspector's skill pointed at `almanac:ai-regression-testing` |
| `almanac/skills/ai-regression-testing` | github.com/affaan-m/ECC (via the owner's `crew-skills` bundle) | 4adeee67 | MIT | none |
| `almanac/agents/builder.md`, `almanac/agents/architect.md` | written here | n/a | MIT | the owner's own agents |
| `almanac/skills/error-handling`, `almanac/skills/python-testing` | github.com/affaan-m/ECC (via the owner's `crew-extra` and `crew-skills` bundles) | d2ef2680 | MIT | copied unchanged so the shipped agents have their skills |
| `egm` | github.com/google/elephant-goldfish-model (the Elephant-Goldfish Model plugin) | n/a (copied from a local bundle copy, version 0.1.1) | Apache-2.0 (its `LICENSE` kept beside it) | reviewer personas written into `agents/` (the adapters pointed at persona files that did not exist) and the manifest cut to name, version and licence; other-host files, modes and install, marketplace and permission notes dropped; `docs/claude.md` shortened; skill paths use `${CLAUDE_PLUGIN_ROOT}`; sub-agent dispatch capped at 6 at once |
