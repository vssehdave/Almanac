---
name: test
description: "Use to run a CLI-Anything harness's tests and record the results in its TEST.md."
---

Edited for Almanac from CLI-Anything at 34f5195: the clone step, the URL argument and URL example removed (local paths only); the fixed harness location replaced; added the copies-only rule; relative links point to the shared docs folder.

# cli-anything:test Command

Run tests for a CLI harness and update TEST.md with results.

## CRITICAL: Read HARNESS.md First

**Before running tests, read `../../docs/HARNESS.md`.** It defines the test standards, expected structure, and what constitutes a passing test suite.

## Usage

```bash
/cli-anything:test <software-path>
```

## Arguments

- `<software-path>` - **Required.** A **local path** to the software source code (e.g., `/home/user/gimp`, `./blender`). If the owner gives a URL, ask them to clone it first and give you the local path.

  The software name is derived from the directory name. The agent locates the CLI harness in the harness folder next to the source, or asks the owner.

## What This Command Does

1. **Locates the CLI** - Finds the CLI harness based on the software path
2. **Runs pytest** - Executes tests with `-v -s --tb=short`
3. **Captures output** - Saves full test results
4. **Verifies subprocess backend** - Confirms `[_resolve_cli] Using installed command:` appears in output
5. **Updates TEST.md** - Appends results to the Test Results section
6. **Reports status** - Shows pass/fail summary

## Test Output Format

The command appends to TEST.md:

```markdown
## Test Results

Last run: 2024-03-05 14:30:00

```
[full pytest -v --tb=no output]
```

**Summary**: 103 passed in 3.05s
```

## Example

```bash
# Run all tests for GIMP CLI
/cli-anything:test /home/user/gimp

```

## Success Criteria

- Run the real software only on copies of files inside the harness's temporary folder
- All tests pass (100% pass rate)
- TEST.md is updated with full results
- No test failures or errors
- `[_resolve_cli]` output confirms installed command path

## Failure Handling

If tests fail:
1. Shows which tests failed
2. Does NOT update TEST.md (keeps previous passing results)
3. Suggests fixes based on error messages
4. Offers to re-run after fixes
