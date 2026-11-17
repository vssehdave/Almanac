Edited for Almanac from CLI-Anything at 34f5195: the skill_generator.py and template steps are replaced by composing the skill file by hand; the REPL skin is replaced by the local REPL helper; cli-hub marked not available here.

# SKILL.md Generation (Phase 6.5)

Generate a SKILL.md file that makes the CLI discoverable and usable by AI agents
through the skill-creator methodology. This file serves as a self-contained skill
definition that can be loaded by Claude Code or other AI assistants.

## Purpose

SKILL.md files follow a standard format that enables AI agents to:
- Discover the CLI's capabilities
- Understand command structure and usage
- Generate correct command invocations
- Handle output programmatically

## SKILL.md Structure

### 1. YAML Frontmatter — Triggering metadata for skill discovery:

```yaml
---
name: "cli-anything-<software>"
description: "Brief description of what the CLI does"
---
```

### 2. Markdown Body — Usage instructions including:

- Installation prerequisites
- Basic command syntax
- Command groups and their functions
- Usage examples
- Agent-specific guidance (JSON output, error handling)

## Writing Process

Compose the skill file (`SKILL.md`) by hand, after Phase 6 (Test Documentation), from what the harness
actually contains:

1. Read the software name and version from `setup.py`.
2. Read the command groups and options from the CLI file (Click decorators) and
   from `cli-anything-<software> --help`.
3. Read usage and system package requirements from `README.md`.
4. Fill in the frontmatter and body structure above, with realistic examples.

## Output Location

SKILL.md is written canonically at the repo root, with a packaged compatibility
copy for installed harnesses:

```
skills/
└── cli-anything-<software>/
    └── SKILL.md

<software>/
└── agent-harness/
    └── cli_anything/
        └── <software>/
            └── skills/
                └── SKILL.md
```

## Integration with CLI Build

The SKILL.md generation should be run after Phase 6 (Test Documentation) completes
successfully, ensuring the CLI is fully documented and tested before creating the
skill definition.

## Key Principles

- SKILL.md must be self-contained (no external dependencies for understanding)
- Include agent-specific guidance for programmatic usage
- Document `--json` flag usage for machine-readable output
- List all command groups with brief descriptions
- Provide realistic examples that demonstrate common workflows

## Preview-Capable Harnesses

If the harness supports previews, the generated or edited `SKILL.md` should
include a dedicated preview section that covers:

- the producer command surface: `cli-anything-<software> preview ...`
- the consumer command surface: `cli-hub previews ...` (not available here)
- whether `preview diff`, `preview live ...`, or poll mode exist
- how agents should use `--json` results and artifact paths
- at least one publish example and one inspect/watch example

For the full preview methodology, see
[`preview-methodology.md`](preview-methodology.md).

## Skill Path in CLI Banner

The local REPL helper's banner displays the absolute path of the packaged
`skills/SKILL.md` copy. AI agents can read the file at the displayed path:

```python
# In the REPL initialization (e.g., shotcut_cli.py)
from cli_anything.<software>.utils.repl_helper import print_banner

print_banner("<software>", version="1.0.0")  # Displays the packaged SKILL.md path
```

## Package Data

Ensure `setup.py` includes the skill file as package data so it is installed with pip:

```python
package_data={
    "cli_anything.<software>": ["skills/*.md"],
},
```
