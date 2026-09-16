# Skills

A skill is a brief fragment that grants a **capability** rather than an
identity. `team/` and `library/` say what an agent *is*; a skill says what it
may additionally *do*.

Mechanically there is nothing new here, and that is deliberate. A skill is a
markdown file resolved by the same three-layer lookup as every other brief —
shipped, then global, then project, with project winning — and it is attached
the same way, by naming it in an agent's `instructions` list:

```yaml
orchestrator:
  instructions:
    - team/_orchestrator.md        # who it is
    - skills/provisioning.md       # what it may also do
```

or in a team's orchestrator composition in `project.yaml`, which is the same
list by another route.

Both, for the orchestrator. A team's `orchestrator:` list **replaces** the
roster entry's rather than adding to it — that replacement is most of what a
team is — so a skill named only in `agents.yaml` is silently absent the moment
a team is active, which is the normal case.

## Why there is no `skills:` key

Because `instructions:` already is one. A separate key would need its own
resolution, its own layering, its own failure mode when a name does not
resolve, and would buy a word. The folder is the useful part: it makes the
capabilities discoverable (`multiagents skills`) and keeps them out of the
roster, where a reader is looking for agents.

## Why skills are composed, never fetched

`obra/superpowers` loads skills on demand when a trigger phrase matches. That
is right for one long-lived agent that can go and get more when it notices it
needs it. It is wrong here: our briefs ARE the agent's identity, handed to a
fresh process that gets one shot and cannot ask a follow-up question. An agent
that discovers halfway through that it had a capability all along has already
made the decision that needed it.

So a skill is present from the first token or it is not present at all.

## The cost, which is real

Every skill lengthens a brief, and a long brief is paid for on every single
launch. `multiagents doctor` prints the composed word count of each agent, so
this is measurable rather than a matter of opinion; watch it. Two of the
shipped briefs doubled in a month before anyone was counting.

A skill that is only useful to one agent belongs in that agent's own brief. A
skill earns its place here by being attachable to several, or by being
attachable to *different* ones as a project changes.

## Writing one

Follow the house style, with one distinction that matters:

- **Heuristics get reasons.** Judgement needs the why, or an agent cannot tell
  when the rule does not apply.
- **Invariants get imperatives.** A reason attached to an absolute rule is the
  premise for a loophole — an agent that understands *why* concludes that its
  case is the exception. State those flat, and say plainly that they are
  absolute.
