---
name: better-writing
description: Writes and reviews docs, READMEs, code comments, commit bodies and reports so they are plain, direct and easy to act on; use when drafting or editing prose that a reader will rely on.
---

# Writing

Write for a reader who has one pass and a task to finish. This covers docs, READMEs, code comments, commit bodies and reports.

## Lead with the point

Put the answer or the change in the first sentence. Background comes after, and only when the reader needs it to act. A report opens with its verdict, a commit body with what changed, a README with what the thing does.

## Short, plain sentences

- One idea per sentence. Split a sentence that holds two actions.
- Pick the word a tired reader gets first time. "Use" over "utilize", "start" over "commence".
- Delete every word that does no work. "In order to" becomes "to", "a number of" becomes "several", and "it is important to note that" goes.
- No idioms, jokes or slang. They do not translate, and they cost the reader time.
- Cut "please", "simply", "just", "easily" and "successfully". They add nothing.

## Concrete over vague

Name the thing: the file, the function, the number, the command. "The retry loop waits 5 seconds" beats "the wait is adjustable". Replace "fast", "robust" and "clean" with what they mean: the measured time, the failure it survives, the lines it removed.

## Address the reader directly

Write "you" in instructions, never "the user". Write steps in the imperative: "Run the tests", not "The tests should be run". Use "we" only for the team's own decisions, and do not switch between "I" and "we" in one document.

## One name per thing

Pick one name for each object and use it everywhere. "Project" and "workspace" for the same thing is a fault. Use the names the code already uses. A different word is fine only when it names something different.

## Headings and links

- Headings use sentence case, and every heading of one level uses the same case.
- Link text names the destination: "See the install guide", never "click here" or "here".
- Two links that both read "learn more" are indistinguishable. Add the subject to each: "Learn more about exports".

## Docs and READMEs

- Open with what it is and what it does, then how to use it, then the rest.
- Show a working example before the options. Give the exact command and the output it prints.
- Make each step one action. Number the steps when order matters.
- State what the reader needs before starting: the version, the tool, the file it reads.

## Code comments

A comment says why the code is the way it is, or what the reader cannot see from it. It does not restate the code. Keep it to one line where possible, and skip "this function" and "here we".

## Commit bodies

- Subject: short and imperative, such as "Add retry to upload".
- Body: what changed and why, in plain sentences, wrapped near 72 columns.
- Leave out the file list; the diff already shows it. Leave out tool or session names unless the reader needs them.

## Errors and failures

Say what failed, then what to do: "Could not read config.json. Check the path and run again." Avoid "Oops", "something went wrong" and exclamation marks. Do not blame the reader or the tool.

## Reports

- Open with the result in one sentence.
- Then the evidence: the command you ran and the lines it printed.
- Mark anything you did not check as "Not verified", with the reason.
- Use a table only when the reader compares rows, such as findings by severity.

## Before you finish

Scan the draft for these and fix each one:

| Pattern | Fix |
| --- | --- |
| "click here", "here", a bare "learn more" | Name the destination |
| "Oops", "something went wrong", "!" in an error | Say what failed and the next step |
| "please", "simply", "just", "successfully" | Cut it |
| "the user", "one should" | Write "you" or the imperative |
| "in order to", "it is important to note" | Cut to the point |
| Two names for one object | Pick one |
| "fast", "robust", "clean" with no measure | State the measure |
