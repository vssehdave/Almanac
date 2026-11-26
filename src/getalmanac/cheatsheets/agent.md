# How to work here (sub-agent)

These are the owner's working rules for a sub-agent. The repository's own
instructions and your brief win where they differ.

- Do the one job in your brief and keep its limits. Read-only means read-only.
  "Do not merge" means do not merge. If the job needs more than the brief
  allows, say so instead of widening it.
- Read a file before you edit it. Never guess a path, a name or a result.
- Run the check before you say done, and say what you did not verify.
- Never get a test to pass by silencing it, weakening it or catching an error
  and ignoring it. Fix the cause, or report that you could not.
- After two failed attempts at the same thing, stop and report what you tried
  and what you saw. Do not push on with a guess.
- Do not commit, push, delete or publish unless the brief says to. Never add
  attribution, co-author or session lines to a commit unless the brief asks.
- Report in the shape and length the brief asks for. Put the result first, in
  plain words, then anything the caller must know or decide.

- For a file's history (when, why, which commit or branch) or what depends on
  it, call code_history / code_related before running git log, git blame or
  grepping for importers. Still read the file itself before editing it.
