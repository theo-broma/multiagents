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

## The invariant the review must guard: providers are plugins

**Stated by the user, 2026-09-16, as a point of particular vigilance.**

> A provider is a plugin. All of a provider's logic lives in its config and in
> its own script. None of it is hardcoded in the main program.

This is not a preference about tidiness. It is what makes a fourth provider an
afternoon's work instead of a refactor, and every hardcoded name is a place where
adding one silently does nothing.

### Where the seam is

Both halves are tracked in the repo, so an agent in a worktree can read them:

- `src/multiagents/defaults/providers.yaml` — `bin`, `auth`, `spawn`,
  `usage_mode`, `models_cmd`, `models_parse`, `home_links`, `stream` per provider.
- `src/multiagents/defaults/providers/{claude,agy,opencode}.sh` — the actions.

The main program reaches them through `scripts.run_action()` and
`scripts.exec_action()`, by provider **name**, never by branching on which name it
is. `budget.read_provider()` asks the script FIRST and only then falls back;
`budget.read_all()` is driven by the loaded providers map, and its docstring
records that it used to be a hardcoded three-name table in which a newly added
provider could never appear at all. That fix is the invariant in action.

### The check, which is mechanical

```
grep -rnE '"(claude|opencode|agy)"|'"'"'(claude|opencode|agy)'"'"'' src/
```

**Any hit not in the baseline below is a finding.** Do not report this as a
matter of judgement or style; it is a grep with a known answer.

### The baseline — 5 known sites, already argued

The invariant largely holds: ten occurrences in 16,860 lines. The review's job is
**not to re-litigate these**, and a finding that merely restates one without new
evidence should not be filed. The job is to judge whether each argument still
holds, and to catch anything new.

| site | what | status |
|---|---|---|
| `budget.py:592` | `_BUILTIN = {"claude": ..., "opencode": ..., "agy": ...}` | **argued**: a fallback used only when a script has no `budget` action. Claude's quota is an undocumented cache with several bucket shapes and an overage block; the comment argues parsing it defensively in shell would be worse code in two places. **The weakest point of the invariant — look here first.** |
| `budget.py:663` | `builtin() if builtin is read_claude else builtin(spent)` | **not argued**: a special case keyed on the identity of one provider's function, because its signature differs. A real smell, and the mechanism by which the fallback table leaks into the dispatcher. |
| `budget.py:509` | `~/.local/share/opencode/auth.json` hardcoded | **questionable**: a credential path in the main program, while `providers.yaml` already carries `home_links` for exactly this. Possible duplicate source of truth — verify before filing. |
| `docker.py:774` | `AUTH_PROVIDER = "claude"` | **argued**: the auth proxy speaks one upstream's protocol and swaps an Anthropic bearer. The comment records that taking whichever vault came first out of a dict already caused a real fault — agy's vault mounted into a proxy talking to Anthropic. The honest reading is that the proxy is structurally single-provider; say so as a design finding if you think it should not be. |
| `cli.py:2502` / `auth.py:142` | `default="agy"`; `"run 'agy' to log in"` in a generic marker list | **unargued and minor**: a UX default and one provider-specific phrase. Low severity, but they are how the invariant erodes. |

### The finding worth making

**No test asserts this invariant.** Two tests cover the fallback *behaviour*
(`test_unimplemented_budget_falls_back_to_a_builtin`,
`test_read_all_hands_the_provider_name_to_executor_for`); none asserts that no new
provider name appears in `src/`. So the invariant is held by discipline, not
enforced — and this project already knows that distinction, because the roster's
$60 tier note says in as many words that a test pins the roster while the list
itself is maintained by hand.

The highest-value recommendation this review can make about providers is
therefore a **lint test that pins the baseline above**: any provider literal in
`src/` outside an allowlist fails the suite. That is implement-team work and does
not get built in this phase — file it as a finding with that shape.

### Who looks

The `auditor`, on whichever context covers `budget.py`, `providers.py`,
`auth.py`, `scripts.py` and `executor/docker.py` — this is exactly its remit:
what is wrong that runs perfectly well. Ranking-wise this should raise that
context's priority; a plugin seam that has quietly stopped being one costs the
whole extensibility claim, and nothing fails while it happens.

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

## The second invariant: agy carries Gemini only

**Stated by the user, 2026-09-16.**

> `agy` is for Gemini models. Its resold Claude and GPT models are not to be
> pinned.

The reason is measurement, and it is the same reason the budget section gives
for distrusting failover. agy resells `claude-sonnet-4-6` and
`claude-opus-4-6-thinking` from a pool that `budget_status` reports with
`counted: false` — separate from the Gemini pool and readable by nothing. A pin
there trades a provider whose spend can be seen for one whose cannot.

**The live project roster now complies.** Three fallbacks were repinned:
`implementer` and `harness` and `characterizer` all carried
`agy: claude-sonnet-4-6`.

### The finding to file

**The shipped defaults do not comply**, and they are what every new project
starts from — so `multiagents init` reintroduces the violation on each one:

| site | pin |
|---|---|
| `src/multiagents/defaults/agents.yaml:225` | `agy: claude-sonnet-4-6` |
| `src/multiagents/defaults/agents.yaml:245` | `agy: claude-opus-4-6-thinking` |
| `src/multiagents/defaults/agents.yaml:421` | `agy: claude-sonnet-4-6` |
| `src/multiagents/defaults/agents.yaml:447` | `agy: claude-sonnet-4-6` |
| `src/multiagents/defaults/agents/library/README.md:86,146` | both, in paste-ready blocks |

These were **not** changed during initialisation: they are production source, and
this phase builds nothing. File them as a finding, with the same shape as the
provider-plugin one — the fix is a lint assertion that no `agy:` pin names a
non-Gemini model, so the rule is enforced rather than remembered.

Note the second-order effect before judging severity: the library README blocks
are *copied by the initializer into proposals*, so one stale block propagates
into projects that never read the defaults file.

## The roster's own guard rail does not cover this project

`test_a_checking_pair_never_collapses_onto_one_model` reads `_shipped_agents()` —
the defaults. **The live `.multiagents/config/agents.yaml` is asserted by
nothing**, and the pair list it checks contains only implement-team pairs
(`tester`/`adversary`/`reviewer` against the three implementer tiers). No review
team pair is in it.

That is how this roster arrived at a real collision that nothing caught: the
cartographer and the characterizer were both pinned to `claude/opus`, primary,
so they collided always rather than only during an outage. Found by hand,
2026-09-16, and fixed by moving the characterizer to sonnet.

**Another finding to file**, and the most valuable of the three, because it is
the one that would have caught the other two: extend the property to the live
roster and to the review team's pairs.

### One known exception, recorded so it stays deliberate

`harness` and `characterizer` are both `claude/sonnet`, and both fall to
`opencode-go/kimi-k2.7-code`. They collide in every direction, and it is
accepted rather than fixed:

- They are a funnel, not a checking pair. The characterizer never judges the
  harness; it consumes the API the harness reports.
- The dangerous case is already surfaced to a human. When the harness finds a
  context cannot be exercised without changing production code, the pipeline
  stops that context and the harness files it as a `critical` architecture
  finding — so its judgement is read, not silently applied.
- **In this project specifically it bounds almost nothing.** The suite is
  already green at 547 tests, so phase 3 has little to build. See below.

Revisit it on a project where the harness has real work to do.

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
- **agy carries Gemini only.** The user's constraint, 2026-09-16. The live
  roster complies; the shipped defaults do not, and that is a finding. Its own
  section above.
- **The characterizer is `claude/sonnet`, not opus.** See the revised budget
  decision below.
- **Providers are plugins, and the review guards it.** The user's constraint,
  stated 2026-09-16. It has its own section above, with a grep, a baseline of 5
  argued sites, and the finding worth making.
- **`security-advisor` is deliberately NOT on the roster.** It gives design-time
  advice phrased as candidate requirements, and this team builds nothing there is
  a design for. A roster nobody will use costs attention at every decision.
- **The characterizer runs on `claude/sonnet`, capped at two per context.**
  Revised 2026-09-16, replacing an earlier decision to keep it on opencode. The
  roster had moved it to `claude/opus`, which was rejected on two counts: it is
  the volume role on the same subscription as the orchestrator's own opus (57%
  used, resets in 5 days, extra-usage credits already spent), so parallel runs
  could starve the one context that cannot be replaced; and it collided with the
  cartographer, also opus. Sonnet keeps the capacity gain at a fraction of the
  cost and breaks the collision.
  **The starvation risk is reduced, not removed** — it is still the
  orchestrator's subscription. Cap parallelism at two, set `budget_tokens` per
  context, and if the brake fires mid-context, record that context as
  half-covered and move on. It is not a reason to retry elsewhere.
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
