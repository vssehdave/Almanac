# How to work here (Haiku)

These are the owner's working rules. The repository's own instructions win
where they differ.

- Do exactly the task given. Do not widen it.
- Look before you answer: read the file, run the command. Never guess a path,
  a name or a result. If you could not find or verify something, say that.
- Send independent lookups together in one message, and fold a chain of shell
  steps into one command.
- Quote what you found exactly, with the file and line. Keep the answer short.
- If the task needs a design decision, touches many files, or fails twice, stop
  and say it needs a larger model. Do not push on with a guess.
- Never get a check to pass by weakening the check.
- Do not commit, push, delete or publish unless the task says to. When it does:
  one commit per finished unit of work, a change together with its tests, never
  on the main branch, and nothing staged that does not belong.

- For a file's history (when, why, which commit or branch) or what depends on
  it, call code_history / code_related before running git log, git blame or
  grepping for importers. Still read the file itself before editing it.
