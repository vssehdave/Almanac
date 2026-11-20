# Bundled skills and agents

The one plugin kept in this package, `almanac`, is loaded into every session with
`--plugin-dir` (turn it off, with your own bundle, by `almanac config set bundle.enabled false`).
Third-party skills are not shipped: you adopt the ones you choose with
`almanac skill add --bundle`, and they are kept in `<almanac home>/bundle/`, each
with a row in that folder's own `SOURCES.md`.

| Folder | From | Commit | Licence | Changed |
|---|---|---|---|---|
| `almanac` | written here | n/a | MIT | the `adopt` skill and the manifest |
