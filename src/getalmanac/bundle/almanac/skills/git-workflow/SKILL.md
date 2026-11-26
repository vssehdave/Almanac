---
name: git-workflow
description: Git rules for branches, worktrees, commits, pull requests, conflicts and undoing a change in this repository. Use when creating a branch or worktree, writing a commit, opening or merging a pull request, resolving a conflict, or undoing a change.
---

Git rules for Almanac. The repository's own instructions (CLAUDE.md) win
where they differ.

## Branches and worktrees

- One branch per change, in its own worktree. Never commit on `main`.
- Name the branch by type: `feat/...`, `fix/...`, `docs/...`, `chore/...`.
- Builders never touch git. The lead reviews the diff, runs the tests and
  makes the commit.

## Commits

- Format: `type(scope): subject`. Types: feat, fix, docs, test, refactor,
  perf, chore, ci.
- Subject: imperative mood ("add", not "added"), at most 50 characters, no
  period at the end.
- Body: wrapped at 72 characters. Say why the change is needed; the diff
  already shows what changed.
- Atomic: one commit does one thing. A test goes in the same commit as the
  code it tests.
- Author as the owner only. No co-author lines, no "generated with" lines,
  no assistant attribution.
- Before every commit run `git status` and `git diff --staged`. Stage the
  named files, not `git add .`, so no stray file (a secret, a local note)
  gets in. Review a large diff with `almanac:lean-review` first.

## Pull requests

- Open the pull request from the branch once its tests pass. The title
  follows the commit subject format.
- The description says what changed, why, and which test command was run.
- Merge only after review and passing checks, and only when the owner asks.
  Merge one phase branch at a time.

## Conflicts

- Read both sides first. Each side has an intent: the change on the branch
  and the change already on the base.
- Write the result so both intents hold. Do not take one side whole unless
  the other side is obsolete.
- Run the tests after resolving. If both intents cannot hold, ask the owner
  which one wins.

## Undoing a change

- On a shared branch (pushed, or with an open pull request), undo with
  `git revert <commit>`. It adds a commit and keeps the history.
- On a local branch that nobody else has, `git commit --amend` or
  `git reset --soft HEAD~1` can fix the last commit.
- Needs the owner's explicit approval first, and say what it discards:
  `git reset --hard`, any force push (`--force`, `--force-with-lease`),
  `git branch -D`, and `--no-verify`.
- Never force-push `main` and never rewrite its history.

## Cleanup after a merge

- Remove the worktree with `git worktree remove <path>`, then run
  `git worktree prune` if a path was deleted by hand.
- Delete the merged branch with `git branch -d`, which refuses unmerged
  work. Check `git branch --merged main` first.
- Check `git worktree list` at the end of a session, so no worktree is left
  behind.
