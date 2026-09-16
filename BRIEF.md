# BRIEF — multiagents reviews itself

**Phase: review.** This project builds no features in this phase. The product is
findings, a characterization suite, and a report. Both the findings and the
suite are committed and merged.

`team: review` is already set in `.multiagents/config/project.yaml`.

---

## What this project is

`multiagents` is an orchestration tool. It runs a tree of LLM agents against a
codebase: a root orchestrator delegates to specialist subagents, each of which
works in its own git worktree on its own branch, and the parent merges what it
accepts. Agents run either as local subprocesses or — as here — inside one
Docker container per project, on an internal network with no route out except
through a filtering proxy.

16,860 lines of Python across 33 modules under `src/multiagents/`. The suite is
547 tests in a single file, `tests/test_core.py` (10,945 lines).

The thing that makes this review unusual is the only thing an agent needs to
understand before it starts:

> **The codebase under review is multiagents itself.** The agent reading it is
> running inside it. Its worktree is a checkout of the very source that spawned
> it, merged its branch, and metered its tokens.

Everything below that is surprising follows from that sentence.

---

## The two channels, and which one to use

This is the decision most likely to be got wrong, because the review pipeline's
own wording assumes it can never happen.

The pipeline says every defect in the reviewed code becomes an `F<n>` finding,
and says `bug-reporter` is for defects in **multiagents itself** — "never for
the code you are reviewing." Here those are the same code, so that sentence
cannot be followed as written. The rule for this project replaces it:

- **A defect found by READING the code is an `F<n>` finding.** It goes in
  `context/review/<context>.md`, into the ledger via `record_findings`, into the
  report, and comes back as work for the implement team. This is the default and
  it covers the overwhelming majority of what the review produces.

- **A defect OBSERVED in multiagents' runtime behaviour during this review run
  is a `TICKET`**, written by `bug-reporter`. A tool returning a shape its own
  description does not describe; a `merge_agent` reporting success having merged
  nothing; a status contradicting the events; an agent failing in a way the
  orchestrator cannot explain.

The cut is **read it** versus **it happened to me**. It is unambiguous in
practice: a tool failing under you is a visceral event, not a judgement call.

### Why the ticket channel is worth more here than usual

`bug-reporter` is normally told, truthfully, that it cannot read the multiagents
source: it works in a worktree of the *user's* project, and under docker the tool
is not mounted in the container at all. Commit `95beb2a` stopped sending it after
code it could not reach, because both blocking tickets this project had received
ended with a paragraph apologising for the restriction instead of a paragraph
about the bug.

Commit `a5b7a47` inverted that for this case. The runner detects a multiagents
checkout by the shape of the tree — so a fork or a rename still counts, and a
directory that merely shares the name does not — and tells the reporter that it
**can** read the source, asking it for `path:line` and for **which test should
have caught the defect**.

So a ticket here carries something no static reader can produce: live runtime
evidence *and* a source citation. That is the combination worth having, and it is
why this channel stays open rather than being folded into `F<n>`.

**The publishing rules do not relax.** A ticket is still written to be published
unread. A path under `src/` is the tool's own layout and is fine. A path under a
home directory is still the user's business and is not.

---

## Scope

### Branch

**The review runs on `refactor/split-consume` at HEAD.** That branch is 40
commits ahead of `main` and nothing is behind it; it is the de-facto trunk.
`base_branch` in `project.yaml` is empty, so agents branch from HEAD at spawn,
which is already correct — no configuration change is needed.

Considered and rejected: merging the branch to `main` first. Forty commits of
unreviewed refactor is precisely the surface the review exists to look at, and
merging it before review would land it unexamined to make the review tidier.

### Contexts

The cartographer's ceiling is seven bounded contexts. **The first pass covers the
top two or three, not seven.** The reason is budget and it is stated below.

**Rank by blast radius, not by line count.** This is an explicit instruction to
the cartographer and it overrides the instinct to start with the biggest file.
`cli.py` is 2,524 lines of mostly low-risk argument-parsing glue. The danger is
concentrated where being wrong costs a credential, a lost branch, or a quota:

- `executor/docker.py` + `authproxy.py` — the sandbox and the egress boundary.
  This is where multiagents' entire security claim lives: the allowlist proxy,
  the per-agent HOME, the refused docker socket, credential isolation. Agents
  inside it hold real credentials.
- `runner.py` + `driver.py` — agent lifecycle, stream interpretation, the prompt
  and environment block handed to each agent.
- `budget.py` + `providers.py` — quota reading, failover, the circuit breaker.
- `tree.py` — concurrent state. Where being wrong costs work already done.

That is a suggestion to rank against, not the answer. The cartographer measures
and decides; the map then goes to the user.

---

## Constraints that are real

### Budget is the binding constraint on this review

Measured 2026-09-16:

| provider | used | resets | measurable? |
|---|---|---|---|
| opencode | **85% of the monthly window** | Oct 5 — 19 days out | yes |
| claude | 57% | in 5 days; extra-usage credits already exhausted | yes |
| agy (Gemini pool) | unknown — its budget script times out | unknown | **no** |

opencode is where four review roles are pinned, including `characterizer`, which
is the volume role: several run in parallel per context, at 1800s each. Roughly
15% of a monthly window is the budget for this entire review.

Two consequences, both binding:

1. **Set `budget_tag` (`ctx-<name>`) and `budget_tokens` on every spawn for a
   context**, as the pipeline requires, and set them low enough that the brake
   fires early rather than as the window empties. Check `budget_tag_status`
   before each stage rather than discovering the ceiling when `start_agent`
   refuses.
2. **Cap parallelism.** Two characterizers per context, not four. When a budget
   runs out, that is the mechanism working: record what the context did not get
   and move on. Do not carry on under a different tag.

The configured fallback chain is `opencode → agy → defer`. Note what that means
honestly: failing over moves a high-volume load onto the **one pool nobody can
measure**. That is a degradation, not a safety net. Prefer letting the per-context
brake fire over relying on failover.

### The suite already exists, and that changes phase 3 and phase 4

547 tests, green inside the agent container at HEAD: **545 passed, 3 skipped**.

- **Phase 3 (harness) is nearly free here.** There is little to build. If the
  harness reports it has nothing substantial to add for a context, that is the
  correct result — merge it and move on, do not manufacture work for it.
- **Phase 4 (characterizers) shifts from pinning to gap-hunting.** With a green
  547-test suite, the valuable job is not recording what the code does from
  scratch; it is reading the existing tests for the assigned context and pinning
  the **untested edge cases**. The flagged half of their output — behaviours they
  had to pin that look wrong, tests that passed where they expected failure —
  remains the most valuable thing they produce.
- **The value of this review concentrates in phase 5**, the auditor and the
  adversary, and in mutation testing against a suite that already claims to
  cover this code.

### Characterizers must each write their own file

`tests/` holds one test module of 10,945 lines. Characterizers are add-only by
configuration, and the pipeline reads that as "so they cannot collide" — which is
**false when every one of them appends to the same file**. They would conflict at
the same end-of-file hunk on merge.

**Each characterizer writes to its own new file, `tests/test_char_<context>.py`.**
Pytest discovers `conftest.py` fixtures anywhere in the tree, so the existing
fixtures and helpers remain available with no import gymnastics.

### Running the suite

```
uv run --frozen pytest
```

`uv` is mounted from the host at `~/.local/bin/uv`, and `~/.cache/uv` is mounted
so dependencies resolve from `uv.lock` without hitting the network. Do **not**
use `.venv` — it is built against the host's Python 3.13 and the container image
carries 3.14.

Three tests skip inside the container because they shell out to `docker`, which
is deliberately absent there. **Three skips are the expected result, not a
defect, and not something to chase.**

`.multiagents/` is gitignored, so an agent in a worktree does not see the live
config. The agent briefs that ship with the tool are tracked, under
`src/multiagents/defaults/agents/`.

---

## Decisions already taken, and why

Recorded so nobody re-derives them.

- **Review runs on `refactor/split-consume`, not `main`.** See Scope.
- **Two channels, split by read-it vs it-happened-to-me.** See above.
- **Two to three contexts in the first pass, not seven.** Budget.
- **Rank by blast radius, not LOC.** A large parser is not a large risk.
- **One new test file per characterizer.** Merge mechanics.
- **`security-advisor` is deliberately NOT on the roster.** It gives design-time
  advice phrased as candidate requirements, and this team builds nothing there is
  a design for. A roster nobody will use costs attention at every decision.
- **The catalog was checked** (`check_model_catalog`, severity `none`). Every pin
  is live and every `opencode-go/*` pin is on the sanctioned $60 monthly tier.
  No repin is forced; the ones proposed are judgements, in
  `.multiagents/proposals/agents.yaml`.

---

## Where things are

- `context/README.md` — index of this directory.
- `context/review/MAP.md` — written by the cartographer in phase 1.
- `context/review/<context>.md` — findings, one file per context.
- `context/review/REPORT.md` — written by the reporter, last.
- `docs/open-questions.md` — what this project believes, is waiting to find out,
  or decided against, each entry with its evidence and how to check it. **Read
  the entries for your context before filing a finding against it**; several
  beliefs in there were wrong the first time and the corrections are recorded.
- `docs/rewrite-plan.md`, `docs/superpowers-review.md` — prior design decisions.
- `README.md` — 153 KB. It is the reference manual, not an introduction. Send
  `researcher` at it rather than reading it.

## This project does not specify before it builds

No `context/specs/`. Requirements here are expressed as tests and as the entries
in `docs/open-questions.md`. Nothing in this phase changes that.
