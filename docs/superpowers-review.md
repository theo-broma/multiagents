# What to take from `obra/superpowers`

A read of [obra/superpowers](https://github.com/obra/superpowers) against this
project, with Gemini 3.1 Pro as the second opinion. Written 2026-09-15 against
commit depth-1 of `main`.

Four things worth taking, two worth rejecting, and one place where the
comparison says something uncomfortable about our own test suite.

---

## The frame that decides what transfers

Superpowers is **a methodology library**; multiagents is **a runtime**. That is
not a slogan, it is the thing that decides which of its ideas are useful here.

Superpowers ships fourteen skills — markdown directories with a `SKILL.md`,
loaded on demand into a single agent, triggered by a frontmatter description
written as a trigger phrase (`description: Use when encountering any bug, test
failure, or unexpected behavior, before proposing fixes`). It is portable across
about fourteen agent CLIs. Its job is to make *one* agent behave like a
disciplined engineer.

A large share of its content exists because a single agent has no other way to
enforce sequencing. `using-git-worktrees` is a skill there; here it is what the
runner does to every agent whether it likes it or not. `dispatching-parallel-
agents` is a skill there; here it is `start_agent` plus `max_concurrent`.
`requesting-code-review` is a skill there; here it is a roster entry with its
own model, on a different family from the code's author, with a machine-read
verdict line.

So the test for every idea below is: **does this solve a problem we have not
already solved structurally?** Most of superpowers does not. Four things do.

---

## Take: rulings, not stalls

The strongest single idea in the repository, from
`skills/subagent-driven-development/SKILL.md`:

> A running plan does not wait on a human. Conflicts, ambiguities, plan
> defects, a cap you would have asked to exceed — decide them. Record every
> decision in the ledger as `Ruling: <what you decided> — <why> — <what it
> costs if wrong>`, and keep going. A wrong ruling costs rework your human
> partner can see and undo; a session parked on a question costs their whole
> day and buys nothing.
>
> Four things stop you, and only these: an irreversible or destructive
> operation; a security-sensitive action; a side effect outside this worktree
> that norms say you ask about first (a merge, a push to a shared branch, a
> publish); and a plan so broken that every path forward is a guess.

**Why this matters here.** We have `NEED_DECISION`, which stops an agent dead,
keeps its branch and session, and waits for a human. We have a great deal of
prose telling agents *when to use it* and almost nothing telling them when
**not** to. The orchestrator's brief says "leave only genuinely user-level
choices" — one line, against several pages of encouragement to stop and ask.

The asymmetry is the bug. Stopping is safe-looking and individually cheap, so
it is the default a model drifts toward, and a session that parks on every
ambiguity is one the user has to babysit — which is the opposite of the thing
this system is for.

Superpowers supplies both halves: a closed list of four things that stop you,
and an auditable record of everything you decided instead. The record is what
makes the autonomy acceptable: a wrong ruling is visible and revertible.

**What to build.** A `Rulings` section in the orchestrator's core brief with
that closed list, and a ruling ledger alongside the findings ledger — same
shape, same reasoning about immutability. `record_ruling(what, why, cost_if_wrong)`,
appended, never edited, surfaced at the end of a run so the user reads a list
of trade-offs rather than discovering them.

This also pairs with something we already have and under-use: `NEED_INFO` is
non-blocking and `NEED_DECISION` is blocking, and nothing today tells an agent
that choosing the second when the first would do costs the user their day.

---

## Take: authority framing for invariants

This is the one where I was wrong and Gemini said so plainly:

> When you give an LLM a reason for an invariant, you are not educating it; you
> are giving it the premise for a loophole. The LLM will inevitably conclude,
> "I am just fixing a typo, so the regression risk is zero. Therefore, the rule
> doesn't apply."

Our house style is to give the reason in the prose so an agent can tell when a
rule does not apply. For **heuristics** that is right and I would not change it
— how to rank findings, when to route to a coder tier, how to write a report.
Those are judgement, and judgement needs the why.

For **invariants** it is an attack surface. Checking our own briefs against
this:

- `adversary.md`: *"No live systems … There is no route out of your container
  anyway; an attempt is wasted time."* The rule is absolute; the reason given
  is prudential and evaporates the moment an agent believes a route exists.
- `implementer.md`: *"This is the single rule that makes the arrangement worth
  anything. A suite the implementer can edit is not a contract."* A model can
  agree with every word and still conclude that its docstring fix does not
  weaken anything.

The distinction to adopt: **heuristics get reasons, invariants get imperatives.**
The short list of genuine invariants is roughly — never touch a protected path,
never target anything outside this worktree, never use or print a discovered
secret, never edit a findings file, never merge or push your own branch.

Note where this matters most. The protected-paths rule has a mechanical
backstop: the merge gate reverts violations whatever the brief says. The
no-live-targets and no-credential-use bounds have **no mechanical backstop at
all** — the prose is the entire control. Those are precisely the ones currently
written in our reasonable, reasons-first voice.

---

## Take: a cheap behavioural test for briefs

The uncomfortable one. From `writing-skills/testing-skills-with-subagents.md`:

> Testing skills is just TDD applied to process documentation. **If you didn't
> watch an agent fail without the skill, you don't know if the skill prevents
> the right failures.**

We have 519 tests. Essentially every brief assertion is of the form *this string
appears in this file*. Not one asserts that an agent given the brief behaves
differently from one that was not. Our briefs are unvalidated prose with a
checksum — the tests prove we did not delete a sentence, not that the sentence
works.

I assumed closing this meant running real agents twice per rule, which would
cost money and wall clock against a suite that currently runs in 110 seconds.
Gemini's version is much cheaper and I think correct:

> You do not need to run a full 10-step agent loop. Pass the brief and a
> synthetic adversarial prompt to the LLM. Do not let it execute tools. Assert
> strictly on its *first response* or *first tool call intent*. Use a fast,
> cheap model for the test runner. You aren't testing if it can solve a complex
> coding problem; you are testing if the *prompt boundaries hold under
> pressure*.

Superpowers supplies the scenario format too — combined pressures, forced
choice, rationalizations recorded verbatim and then countered in the next
revision:

> It's 6pm, dinner at 6:30pm. Code review tomorrow at 9am. You just realized
> you didn't write tests. A) Delete code, start over with TDD tomorrow
> B) Commit now, write tests tomorrow C) Write tests now (30 min delay)

**What to build.** An opt-in eval suite, separate from `tests/`, not run in CI
by default — `multiagents eval-briefs` or a `pytest -m eval` marker. One
scenario per invariant, on `haiku` or `gemini-3.8-flash`, no tools, asserting on
the first response. Start with the five invariants above, because those are the
rules where a loophole actually costs something.

The first run is the interesting one: it tells us which of our carefully argued
paragraphs a model talks itself past.

---

## Take (smaller): announcements as commitment

Gemini's catch, and the one thing in their persuasion material that is
mechanically useful rather than psychological:

> If an orchestrator or a characterizer must emit a structured `INTENT: <goal>`
> line before executing a batch of commands, that text anchors the LLM's
> attention in its own context window. It prevents the agent from forgetting
> its goal and wandering off to refactor an unrelated file mid-task.

Our agents already close with `## Result`. An opening `INTENT:` line would
bookend that at negligible cost, and there is a use for it beyond the
psychology: **the supervisor could read it.** We already detect doom loops and
runaway steps mechanically; a declared intent gives a watchdog something to
compare current activity against, which is the one wandering failure mode we
cannot currently see.

Worth trying on the long-running agents first — the implementer tiers and the
characterizer — rather than everywhere.

---

## Reject: Graphviz decision diagrams

Their skills embed `digraph` blocks for "when to use this". It renders nicely
on GitHub and it is the wrong shape for us. Our routing rules are prose *with
the reasoning attached* — the coder-tier rule is not "is it complex? → deep",
it is a paragraph explaining why routing by importance is the tempting and
wrong criterion. A decision diagram is that table with the reasoning stripped
out, which is exactly what makes a rule get misapplied at the edges.

Gemini agreed without prompting.

## Reject: on-demand skill loading

Superpowers loads instruction fragments when a trigger matches. Tempting,
because our briefs are long:

| brief | words |
|---|---|
| composed orchestrator (implement) | 4,382 |
| `_orchestrator.md` core | 2,810 |
| `implementer.md` | 2,216 |
| `_initializer.md` | 2,121 |
| median agent brief | ~950 |

Their target is under 500 words for a non-core skill. We are well over it
everywhere.

But the architectures are not comparable. Their skills are *advisory* for one
long-lived agent that can fetch more when it needs it. Our briefs **are the
agent's identity**, handed to a fresh process that gets one shot and cannot ask
a follow-up question. A partially loaded identity is an agent that does not know
what it is — and we have already seen the failure mode this session, when a
composed brief with a missing half needed an explicit guard telling the
orchestrator to stop rather than run on half a brief.

What *is* worth stealing from that section is the discipline, not the
mechanism: they measure word counts and treat every token in a frequently
loaded file as a cost. We have never once measured a brief. Two of ours doubled
in length this month without anyone noticing.

---

## Where they are better than us, and we should be honest about it

**They test their prose. We do not.** Covered above; it is the real finding of
this review.

**They have a debugging discipline and we have none.**
`skills/systematic-debugging/SKILL.md` opens with:

> **The Iron Law:** NO FIXES WITHOUT ROOT CAUSE INVESTIGATION FIRST. If you
> haven't completed Phase 1, you cannot propose fixes.

Nothing in our roster says this. Our implementer brief says "read the failure
before changing anything", which is a sentence, not a discipline. This maps
directly onto the **maintenance/triage team** sketched earlier — reproduce,
failing test, minimal fix, regression — and their four-phase structure is a
better starting point than inventing one.

**Their descriptions are triggers; ours are labels.** Compare:

- theirs: `Use when encountering any bug, test failure, or unexpected
  behavior, before proposing fixes`
- ours: `Writes code on its own branch. The workhorse, and the default.`

Theirs tells a model *when this applies*. Ours tells a reader *what this is*.
Since `list_agents` descriptions are the orchestrator's main routing signal,
rewriting them as triggers is a cheap improvement to the decision we most want
made well. This is small and I would do it alongside the invariant pass.

---

## One idea I would not take as given

Gemini wanted to cut `verification-before-completion` as prose and replace it
with a mechanical gate — no merge unless the test command exits zero. The
instinct matches how we did protected paths, and in general it is right.

It does not survive contact with our pipelines. **The tester's branch is red by
design** — that is the whole of phase 3 — and a blanket green-to-merge gate
would make the implement pipeline unable to merge its own contract. The review
team has the same problem in reverse. So it would have to be per-agent
(`requires_green: true` on the implementer tiers only) and would need a project-
level verify command, which a tool that runs over arbitrary repositories does
not have today.

Worth doing, bigger than it sounds, and not a one-liner. Recorded here rather
than built.

---

## Suggested order

1. **Invariant pass** over the briefs — imperatives for the five absolute
   rules, reasons everywhere else. Cheap, and the no-live-targets and
   no-credential bounds have no other control.
2. **Rulings** — the closed stop-list in the orchestrator's core brief, plus a
   ruling ledger. The largest behavioural change on this list.
3. **Brief evals** — opt-in, cheap model, no tools, one scenario per invariant.
   Do this third so it can measure whether (1) actually worked.
4. **Trigger-shaped descriptions**, alongside (1).
5. `INTENT:` lines on the long-running agents, if (3) shows wandering.
6. Debugging discipline, when the maintenance team gets built.
