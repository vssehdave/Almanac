## Lean changes

Make the smallest change that fully solves the problem. Small is about how much
you add, never about how much of the job you finish.

- Scope first. Read the task and the code it touches, then list every place the
  change has to reach (callers, tests, fixtures, config, exports, docs) and what
  it could break for someone else: data lost or exposed, callers that stop
  working. That list is the job. Nothing outside it is.
- Pick the first of these that fully works:
  1. Do not build it. Leave out any feature, option or flexibility nobody asked
     for, and say in one line what you left out. A vague request gets the
     smallest version that does its core job.
  2. Use what the codebase already has, the way the code around it uses it.
  3. Use the standard library or the platform, unless the project has its own.
  4. Use a dependency that is already installed. Never add one for a few lines.
  5. Write the least code that works.
- Add no abstraction, wrapper, conversion, setting or hook for a later that has
  not come. Keep the layers, interfaces and conventions the code already has.
  Taking code out beats putting code in.
- The shortest diff wins only after everything the change must reach is
  covered. Prefer the plain two lines to a clever one-liner that has to be
  decoded.
- Fixing a bug: search for every caller of the code you are changing, then fix
  the cause once, in the shared place.
- Code you move or merge keeps its validation and its error handling. When two
  options are about equally small, take the one that holds on edge cases.
- Never trim validation at a trust boundary, error handling that protects data,
  security, accessibility, or anything the person asked for.
- A comment says only a why that the code cannot show, in one line. A shortcut
  with a known limit is marked `shortcut: <the limit>; <when to replace it>`.
- New logic that is not trivial (a branch, a loop, a parser, money, security, a
  whole new script) comes with one small test. A trivial change needs none.
- When a reply changes code, end it with a line on what you chose to leave out
  and any risk the person has to know about.
