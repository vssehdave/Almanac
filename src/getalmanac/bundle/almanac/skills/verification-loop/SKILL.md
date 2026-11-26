---
name: verification-loop
description: Six-phase check of a finished change (tests, doctor, secrets grep, diff review, one real run, report); use after a feature or refactor, before a PR, or when the user asks if the work is really done.
---

Check the work before calling it done. Run the phases in order. Stop at the
first failure, fix the cause, then start again from phase 1. Never silence or
weaken a test to get a pass.

## 1. Tests

- Almanac: `python -m unittest discover -s tests`.
- Another repo: use its own test command (look in the README, CLAUDE.md or CI
  config). Do not guess one.
- Keep the output. A failing test is reported with its message, not summarised.

## 2. Doctor

- Run `almanac doctor` and read every line it prints.
- A warning is not a pass: report it, with the line, and say if the change
  caused it.

## 3. Secrets and bypasses

Search the diff (`git diff` plus `git diff --staged`), not the whole tree:

- Credentials and tokens: `sk-`, `Bearer `, `api_key`, `secret`, `password`,
  `Authorization`, private key headers, `.env` files.
- Bypasses: `--no-verify`, `--no-gpg-sign`, disabled or skipped tests.
- Any hit is reported with file and line, even if it looks harmless.

## 4. Review the diff

- Run `almanac:lean-review` on the change.
- Note what it found, and what you fixed or left on purpose.

## 5. Run the real thing once

- Run the changed code path for real, with the smallest harmless input: one
  command, one request, one file. Not a mock, not a unit test.
- Pick input that cannot spend money, delete data or reach a host the project
  does not already use.
- If it cannot be run here, say so. Do not count it as passed.

## 6. Report

Put the result first, then three plain lists:

- **Passed**: each phase that ran clean, with the command.
- **Failed**: each failure with its exact output.
- **Not checked**: each phase skipped or only partly run, and why.

A phase that did not run is "not checked", never "passed". Do not write
"verified" for something only read, or for a result that CI alone showed.
