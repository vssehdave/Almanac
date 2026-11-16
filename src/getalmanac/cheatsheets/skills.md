## Skills and agents

The skills and agents below are part of how you work here, not extras. Use
them on your own judgment, the moment the work calls for one. The owner never
has to type a slash command or name a skill: read the request, decide which
apply, and load them before you start. Say in one line which you used.

- Starting a project, or a feature that spans several files or has open design
  questions: plan before building. `egm:egm-brainstorm` to explore the idea,
  `egm:egm-draft` to write the design document, `egm:egm-review` to have it
  read with fresh eyes (`egm:egm-review-loop` for a large design), and
  `egm:egm-resolve` to settle what the review found. Then build from the
  reviewed document. Skip all of this for a small change.
- A codebase with no written design: `egm:egm-bootstrap` writes its folder
  READMEs. Code that may have moved away from its design:
  `egm:egm-drift-report`.
- Building or changing an interface: load the `interfaces:` skill for the part
  in hand (`better-layout`, `better-typography`, `better-colors`, `better-ui`,
  `better-accessibility`, `better-writing`; `build-design` to build from a
  Figma file or image) and finish with `interfaces:interface-review`. A
  component with many states: `interfaces:state-machine`, then
  `interfaces:break` to stress it. Choosing between designs:
  `interfaces:variant`. "How was this site or effect built":
  `interfaces:explain-interface`. Use `astryx:astryx` as well when the project
  uses that design system.
- Any words a person will read in a product (labels, errors, empty states):
  `interfaces:better-writing`.
- A security question, a review of risky code, or an audit:
  `security-audit:security-audit`.
- Data or an action from a website that has no API, or a scraper: the
  `api-anything:api-anything` skill and its tools.
- A release that must resist automated reverse engineering:
  `bla:blind-llm-agents`.
- Two skills change a lot without supervision: `untangle:untangle-codebase`
  (a clean-up of a whole codebase) and `interfaces:afk` (unattended
  improvements on a branch). Start these only when the owner has asked for
  that outcome in words, and on a clean branch.
- Hand a self-contained piece of work to a `crew:` agent when one fits it, for
  example `crew:planner`, `crew:code-reviewer`, `crew:build-error-resolver`, a
  language reviewer or `crew:security-reviewer`. Start its prompt with a
  `Difficulty:` line. Do small things yourself: no agent for a job of a few
  tool calls, and never more than two agents at once.
- If a skill or agent named here is not in your list, carry on without it.
