# multiagents

An MCP server that turns **any agent CLI into a subagent of any other**.
opencode, Antigravity (`agy`) and Claude Code today; anything that streams JSON
tomorrow. Whichever one orchestrates is a line of config.

The point is not that other models are better. It is that a subagent burns *its*
context instead of yours, works on a branch that cannot touch your tree, stops
rather than guessing when a decision is actually yours, and can be watched,
steered and killed while it runs.

```
you/master  ← explicit merge_agent() gate
  orchestrator (any provider)
    ├─ implementer  [running]         38,381tok  $0.0104  agents/implementer/8a5e14
    ├─ reviewer     [awaiting you 6m] store: Postgres or SQLite?
    └─ critic       [idle · 2 turns]  10,419tok  $0.0006
```

## The lifecycle

```bash
make install             # dependencies, the `multiagents` command on PATH,
                         # global config, MCP registration
make check               # are the agent CLIs present and authenticated?

multiagents init         # create the project, copy the global config
multiagents init-agent   # shape it with the initializer — resumable, takes as long as it takes
multiagents build        # container environment, then authenticate every provider
multiagents run          # launch the orchestrator; first run and resume are the same command
multiagents stop         # halt everything for this project, resumably
```

`run` continues the last session where there is one and starts fresh where there
is not, so it is the same command either way. `--fresh` forces a new session,
and `resume` is an alias for `run`. The same applies to `init-agent`.

`run` also checks the orchestrator's own quota before launching, since an
exhausted CLI reports it as an ordinary error with no reset time in it and that
reads as a broken install. `run --wait` blocks until the quota is back instead
of exiting.

`make install` runs `uv sync` **and** `uv tool install --editable .`, because
`uv sync` alone only populates this repository's `.venv` — the command would
exist nowhere else, which is not much of an install. `--editable` means the
installed command tracks the source, so a `git pull` is enough to update it, and
the install prints where the command landed (or says plainly that it is not on
your PATH and how to fix that, which is `uv tool update-shell`).

`init` copies the global defaults into `.multiagents/config/` for editing,
generates `models.yaml` from the installed CLIs, records the first model-catalog
snapshot, and scaffolds `context/`. Run it either in an existing project or with
a path — `multiagents init ~/code/thing` creates the directory if it is missing.
On an existing project it is additive: `.multiagents/`, one `.gitignore` line,
and `context/` if absent. Nothing else is touched, and re-running it is a no-op.

**It also makes sure the project is a git repository with a commit in it**,
offering `git init` and a first commit rather than only printing the commands.
That is not politeness: agents get their own branch and worktree, and with no
repository to branch from they would all run directly in your project directory
instead — concurrently, with nothing to discard if one goes wrong. Files that
look like credentials or build output (`.env`, `node_modules/`, …) are offered
to `.gitignore` before the commit is made. With no terminal — `make init`, or a
script — every prompt declines itself and prints the command instead, so an
unattended run never creates or commits anything.

Spawning without a repository is refused rather than degraded: an agent with no
branch of its own would run in the project directory alongside every other one.
`init` exits non-zero when it leaves a project in that state, so a scripted
setup finds out rather than reading exit 0 as ready.

`start_agent`'s `workdir` parameter — which runs an agent outside its worktree —
is refused unless the project sets `limits.allow_workdir_override: true`. The
caller asking for it is a **model**, so a discouraging name or a warning in a
docstring deters nobody; the permission has to be granted by a human editing a
file, where an agent cannot grant it to itself.

`build` prepares everything agents need: the container images and container if
you are on the docker executor, then **authentication for every enabled
provider** — checking each and offering to log in what is broken. Auth comes
after the container deliberately, since a provider whose credentials live inside
it cannot be checked until it exists.

`init-agent` launches the **initializer**: an agent that shapes the project with
you before anything is built. It reads the repository, forms a view, puts
specific questions rather than interrogating you, consults the critic and
advisor, and writes `BRIEF.md` and `context/`. It also reviews the model catalog
against that first snapshot, so a roster that has already drifted is caught
before the project is planned around it. Expect several sessions — re-running
the command resumes it.

Between `init-agent` and `build`, edit `.multiagents/config/agents.yaml` however
you like. The initializer leaves roster suggestions as a proposal under
`.multiagents/proposals/`; it never edits the live config.

The project must be a git repository with at least one commit — agents work on
branches, and a worktree cannot be branched from nothing.

## Choosing who orchestrates

The orchestrator is a roster entry like any other, except that it is **launched**
rather than spawned:

```yaml
  orchestrator:
    provider: claude       # or opencode, or agy
    model: sonnet
    launch: true
    role: orchestrator
```

Change those two lines and nothing else in the system needs to know. `run`
resolves the entry, runs that provider's `prepare`, then execs its `launch`.
Each CLI absorbs its own differences in its own script:

| | MCP registration | orchestrator prompt |
|---|---|---|
| claude | `--mcp-config`, per invocation | `--append-system-prompt-file` |
| opencode | generated config via `OPENCODE_CONFIG` | `--agent orchestrator` |
| agy | `agy mcp add`, global profile | `--prompt-interactive` seeds it |

Verified: an opencode orchestrator sees all 24 multiagents tools. (That was 21
when measured; the ticket tools came later. If you change the tool surface,
re-measure rather than trusting this line.)

Two consequences worth knowing. opencode's config is handed over through the
environment, so your own `opencode.jsonc` is never touched. agy has no
per-invocation MCP scope at all, so its registration is machine-wide and every
agy subagent inherits these tools — which is why the mutating ones are gated
server-side by ownership rather than by who can see them.

## How it works

**Every agent gets a git worktree.** A branch alone cannot isolate parallel
agents: `git checkout` is global to a working tree, so two agents on two
branches in one directory overwrite each other within seconds. Worktrees live
outside the project (`~/.multiagents/worktrees/<project>/<agent-id>/`) so an
agent's file search cannot reach a sibling's checkout.

**The parent owns the branch.** It creates the worktree, decides the merge, and
deletes the branch. An agent squash-merges its own children automatically —
their work is still quarantined on its branch — but landing on *your* base
branch is always an explicit `merge_agent()` call.

**Agents are supervised through their event stream.** Every provider is
configured to emit streaming JSON rather than a single result at exit, which is
what makes four conditions detectable while a run is still alive:

| trip | fires when |
|---|---|
| `silence` | no stream event for `silence_timeout` |
| `timeout` | wall clock exceeded |
| `doom_loop` | same tool + same arguments repeated, or a two-step cycle |
| `runaway_steps` | more steps than any sane task needs |

A trip marks the run `stuck` and records why. It deliberately does **not** kill
the process — you decide whether to steer, wait, or stop.

**Context stays isolated.** A run's full transcript never comes back through a
tool result: you get a bounded summary plus a path, and the whole thing sits
behind the `run://<agent_id>` resource. Returning everything would make
delegating pointless.

**The tree is on disk.** Recursion means one MCP server process per agent, so an
in-memory registry would give each its own empty world. `tree.json` is mutated
under an exclusive `flock`; `events.jsonl` is append-only so an external watcher
can follow a run without touching the MCP layer:

```bash
multiagents watch    # tail every state transition, live
multiagents tree     # snapshot
```

## The roster

Fifteen agents ship by default. They come in three kinds, and the kind decides
how you reach one:

| | agents | how |
|---|---|---|
| **launched** | `orchestrator`, `initializer` | `multiagents run` / `init-agent` — MCP clients, never spawned |
| **conversational** | `critic`, `advisor`, `security-advisor` | `consult()` — blocks for a reply, keeps context between calls |
| **task** | `researcher`, `specifier`, `adversary`, `implementer-quick`, `implementer`, `implementer-deep`, `tester`, `reviewer`, `pentester`, `bug-reporter` | `start_agent()` — own branch and worktree, collected when done |

Reading agents (`researcher`, `reviewer`, `pentester`) and writing agents
(`implementer*`, `tester`, `specifier`, `adversary`) are separated by intent
rather than by permission: every agent gets a worktree regardless, so `writes:
false` controls cleanup, not safety.

Delete or replace any of them except the two launched roles — see
[which agents you can delete](#which-agents-you-can-delete-and-which-you-cannot).

### Coder tiers

Three coders share one brief on cheaper or stronger models —
`implementer-quick`, `implementer`, `implementer-deep`. One brief, because the
craft is identical and three copies would drift invisibly; different models,
because most tasks do not need the expensive one.

**Route by how much judgement a task needs, never by how important the feature
is.** Importance is the tempting criterion and it is wrong — everything that
matters then goes to the top tier, and you have paid for a tiered roster without
getting one. A critical feature whose implementation is fully decided is a
`quick` task; a minor cleanup that touches an invariant is a `deep` one.

What makes routing low *safe* is escalation. `implementer-quick` is told to stop
and hand the task back when it turns out to contain a decision, saying which
one; the orchestrator then re-spawns on `implementer-deep` and passes that
explanation in. Its `max_steps` is deliberately 40 rather than 120 for the same
reason: running out of budget on a misrouted task is the cheap failure, and
flailing for 120 steps is the expensive one.

Without escalation, routing low does not save money — it produces
plausible-looking wrong implementations, which cost more than the model you
saved on. The two go together or neither works.

The model pins are a starting point rather than a measured ranking; retune them
for your own work. What the tiers give you is the routing rule and the
escalation path, and those survive any repin.

### Standing advisors

Three agents are not task runners at all. `critic`, `advisor` and
`security-advisor` are reached with `consult()` — which blocks for a reply and
**keeps its context between calls**, so the orchestrator holds an actual
conversation rather than firing off amnesiac one-shot questions:

```
consult("critic", "The catalog says glm-5.3-flash input price rose 7.5x.
                   My researcher pins it. I intend to leave agents.yaml
                   alone — is that reasonable?")
```

`critic` and `advisor` review decisions; `security-advisor` is narrower and is
described [below](#security-at-both-ends). All three advise; they decide nothing
and gate nothing. The orchestrator is
accountable for the outcome, and "the critic said so" is not a reason. Their
instructions push against both failure modes — rubber-stamping and
obstructing — and they are told to say "this doesn't need review" when consulted
about trivia. None runs on a Claude model, in its primary pin or its fallback:
feedback from the same family as the orchestrator tends to agree with it.

Conversational agents sit in an `idle` state between turns — not active (so they
do not count against the concurrency limit), not terminal (so their session
stays resumable and their worktree survives).

`bug-reporter` is a third kind again: it is spawned like a task agent, but its
product is a **ticket about multiagents itself** rather than work on your
project. See below.

### Which agents you can delete, and which you cannot

Only two entries are required, and both are required by a *command* rather than
by the system as a whole:

| entry | needed by | if it is gone |
|---|---|---|
| `launch: true` + `role: orchestrator` | `multiagents run` | `No agent in agents.yaml is marked \`launch: true, role: orchestrator\`` |
| `launch: true` + `role: initializer` | `multiagents init-agent` | the same message for `initializer` |

The lookup is by **role, not by name** — `_launched_spec` searches for the
marker — so renaming `orchestrator` to `boss` is fine as long as the two lines
travel with it. Delete the roles and only those two commands stop; everything
else keeps working.

Their briefs are named with a **leading underscore** — `_orchestrator.md` and
`_initializer.md` — so the two files a project must not delete are visible at a
glance in `agents/`. That is the whole meaning of the prefix: no other file
carries it, and a test asserts that the underscored set is exactly the set of
briefs belonging to launched agents. You can still edit them freely; the
convention marks what not to *remove*.

A config written before the rename that still says `instructions: orchestrator.md`
keeps working — the loader falls back to the other spelling — but the exact name
always wins, so a local `orchestrator.md` you wrote yourself is never shadowed.

Every other agent is yours. `researcher`, `implementer`, `reviewer`, `tester`,
`critic`, `advisor` and `bug-reporter` are referenced by name only in *prompts*,
never in code, so removing one costs you whatever that prompt asks for — the
orchestrator told to delegate to `bug-reporter` will find no such agent — and
nothing else. Add as many of your own as you like.

Note that removing an entry from your project's `agents.yaml` does **not**
delete it: the three config layers deep-merge, and a merge can add or override
but never remove. To drop a shipped agent, give it `disabled: true`.

### Lines within an entry that carry weight

- `launch:` and `role:` — see above. `launch: true` also makes an entry
  unspawnable as a subagent, which is what stops an orchestrator orchestrating
  itself.
- `provider:` must name a provider in `providers.yaml` that is `enabled` and
  installed; `model:` must be one that provider actually serves. `doctor` warns
  on both.
- `permission:` must be one of the profiles the provider defines
  (`full`, `sandbox`, `readonly`). An unrecognised name adds **no flags at
  all**, which is not a safe default — agy then auto-denies every tool and
  returns nothing, so the agent looks broken rather than misconfigured.
- `conversational: true` is what makes an agent reachable by `consult()` and
  gives it memory between turns. Remove it from `critic` and consulting it
  fails.
- `instructions:` must name a file that exists in one of the config layers'
  `agents/` directories. A missing file is not an error — the agent runs on the
  preamble alone, which is worse than failing. The underscore-prefixed briefs
  are the mandatory ones.
- `role: bug-reporter` is what earns the generated environment block in the
  prompt. Without it a ticket carries whatever the model invents about the
  machine instead.

`multiagents doctor` reports every one of these. Run it after editing the
roster; it is faster than discovering the mistake through a confused agent.

## Specifying before building

A model given a broad task builds the median version of it. Not from
incapacity — a broad task does not say what *better* means, and the median
satisfies the words. "Build the checkout" gets a bakery till.

The counter is not a better prompt. It is a written contract the work is held
to, produced by agents that do not write the code:

```
specifier   →  context/specs/<feature>.md, numbered requirements R1…Rn,
               each with a `Verified by:` line
adversary   →  concrete failure scenarios A1…An appended to that file,
               with no proposed fixes
orchestrator→  closes every A: a new requirement, or "out of scope, because —"
tester      →  failing tests named after the ids. Red is correct here.
implementer →  given the ids, not a prose description. Makes them pass.
```

Three properties make this more than ceremony:

- **The adversary runs on a different model family from the specifier.** One
  that shares the author's blind spots agrees with it, which is the one thing it
  must not do.
- **The adversary proposes no fixes.** Naming the fix collapses the search —
  the specifier writes down the suggestion instead of thinking about the
  scenario.
- **Requirements become failing tests before implementation.** This is the part
  that does not rely on anyone's diligence: a missing advanced case shows up as
  a red test rather than as nobody noticing.

Nobody holds a veto. Advisors advise and the orchestrator decides — the gate is
an *artifact*, not an authority: no implementation task until the spec exists
and has been attacked. You can check that by reading a committed file.

It costs roughly double the tokens for that feature, so the orchestrator's brief
carries a threshold rather than applying it to everything: behaviour-shaped
requests, work touching several files, or mistakes that would be expensive to
unwind. A one-line fix goes straight to `implementer`.

## Security agents, at both ends

Two agents outside the default path, because running them on everything trains
you to skim their output:

- **`security-advisor`** — consulted with `consult()` *while a thing is still
  being designed*, when a boundary can still be moved for free. It answers what
  an attacker controls, what is worth taking, and where the check happens, and
  phrases findings as **candidate requirements** so `specifier` can turn them
  into numbered requirements and then tests. A security concern that never
  becomes a requirement is one that gets forgotten at implementation time.
- **`pentester`** — run with `start_agent` on code that already exists. Every
  finding must carry the attacker's position, the concrete path, and what the
  attacker gets; anything without those three is a hunch. It may commit a test
  that fails now and passes once fixed.

They run on different providers deliberately: the audit should not be performed
by whoever approved the design.

The pentester's brief bounds it — this repository only, no live targets, never
use or print a secret it discovers, and a reproducing test rather than a
working exploit. Its container has no route out in any case. Neither agent holds
a veto, but a finding with a position, a path and an outcome is a defect rather
than an opinion; declining to act on one is a decision to record, not to leave
implicit.

`_orchestrator.md` carries the trigger list — untrusted input, authorisation,
credentials, money, injection sinks, cryptography, anything reachable without a
session — and the matching list of cases that do not warrant a pass.

## When multiagents is the thing that is broken

An agent that writes bad code is ordinary. A `merge_agent` that reports success
and merges nothing is a defect in the tooling, and nobody upstream hears about
it unless somebody writes it down. `bug-reporter` is the agent that writes it
down.

The orchestrator delegates to it with the evidence it already has — agent ids,
the call it made, what came back. The ticket is filed into a queue the
orchestrator reads at every natural stopping point:

```
$ multiagents tickets
   bug-c24cc3  open           filed 4m ago  merge_agent reports `merged` for an empty branch

1 ticket(s). `tickets show <id>` to read one, `tickets submit <id>` to file it.
```

**Timing follows consequence, not irritation.** A ticket marked `minor` waits
for a natural stop: a task finished, a merge done. A `blocking` one is handled
immediately, because finishing work on top of corrupted state wastes everything
built after the corruption. The severity comes from the marker's own vocabulary,
so an inventive model cannot escalate itself by writing something else.

The orchestrator may also fix the bug locally. If it does, the ticket is still
written and still reported: the fix is local, and the defect is upstream where
everyone else still has it. That is when the ticket carries a proposed fix.

### Nothing leaves the machine unasked

`bug_reporting.automatic` is **false** by default. Tickets are queued, and you
send them:

```yaml
bug_reporting:
  enabled: true
  automatic: false        # true files issues without asking
  repo: ""                # e.g. you/multiagents; empty keeps tickets local
  labels: []
```

A bug report is public writing about your machine, and consent for one is not
consent for the next. With `automatic: false` the orchestrator's `submit_ticket`
parks the ticket and says so — that is policy working, and its instructions tell
it not to look for another route.

Three layers keep the ticket publishable:

1. **The agent is instructed** to describe the tool and stay silent about the
   person — no home paths, no usernames, and nothing about *what you are
   building*.
2. **The environment block is generated**, not written by the model, so the one
   part of the ticket that describes your machine is chosen by code you can
   read. The source path is deliberately outside it: the agent needs the path to
   read the code, and it names your home directory.
3. **Storage depersonalises**, replacing home directory, project path, username
   and hostname with placeholders — and it happens on the way *in*, so what the
   orchestrator and you review is exactly what would be posted.

None of that recognises a client's name or a private repository, which is why
the last step is a human reading it. `tickets show <id>` prints the rendered
issue, and `tickets submit <id>` asks before sending.

Submitting needs the `gh` CLI installed and logged in (`gh auth login`). Without
it tickets are still written and queued — `multiagents tickets` says which piece
is missing.

## When an agent needs *you*

Agents are structurally non-blocking: they run with no stdin, explicit
permission flags, and silence and wall-clock watchdogs. They cannot sit waiting
on a keystroke that will never come. But that leaves a gap — an agent facing a
choice only you can make would otherwise guess and build on it.

Two markers, and the distinction matters:

| marker | meaning |
|---|---|
| `NEED_INFO(topic): q` | something another agent could answer. Non-blocking: state the assumption and carry on. |
| `NEED_DECISION(topic): q` | a choice that changes what "correct" means. **Stops immediately**, keeping branch, worktree and session. |

Every `NEED_DECISION` must carry a `DEFAULT:` line — if writing that makes the
answer obvious, the agent did not need to ask.

A parked agent surfaces to the **orchestrator first**, which answers anything
within its remit via `answer_question`; the agent resumes exactly where it
stopped, with its context intact. Only genuinely user-level choices reach you:

```
$ multiagents ask
  [q-e2946a] implementer · store   asked 14m ago
      Postgres or SQLite for the persistence layer?
      it would otherwise choose: SQLite
  > postgres, we already run one
```

`ask` is deliberately write-only. Resuming an agent means owning the asyncio
task draining its stdout, and that CLI process exits immediately afterwards —
the agent would be left running with nobody reading its pipe until it filled and
deadlocked. So `ask` records the answer and a live runner performs the resume.

`awaiting_user` is neither active nor terminal, exactly like `idle`: it does not
count against the concurrency limit, is not reaped as an orphan, and its
worktree is not reclaimed.

## Authentication

Every CLI reports "not authenticated" differently and is repaired differently.
One command covers all of them:

```
$ multiagents auth
  ok agy        [container] container token present (…/antigravity-oauth-token)
  ok claude     [host     ] logged in as you@example.com
  ok opencode   [host     ] 1 stored credential(s)

all providers authenticated
```

When something is broken it says so and how to fix it:

```
  !! agy        [container] no container token; agy has not been logged in inside the container
      fix: multiagents auth login agy
```

The scope column is **where the credentials live**, not where the agents run.
opencode keeps credentials on the host even under docker, because the container
mounts its data directory; only a provider declaring `container_private_home`
authenticates inside the container.

`multiagents auth login <provider>` hands the terminal to that provider's login
script, which prints what you need to do before doing it.

Authentication is also checked by `doctor`, exposed to the orchestrator as the
`auth_status` MCP tool, and — the part that matters most in practice —
recognised in agent output. An unauthenticated provider returns an empty
response that is otherwise indistinguishable from a model that simply said
nothing, so such a run is marked `unauthenticated` with the fix command in its
reason rather than surfacing as a mysterious empty result. Permission denials
are explicitly excluded, since conflating them would send you to re-login for an
unrelated problem.

Repairing auth is deliberately **not** an MCP tool: it may need a human at a
terminal and a browser, so the orchestrator reports the command and you run it.

### Adding a provider

A provider is a block in `providers.yaml` plus **one script**. No Python.

```
<provider>.sh check     exit 0 authenticated / 10 not / * unknown; one line of status
<provider>.sh login     may take the terminal; prints what to do BEFORE doing it
<provider>.sh budget    prints one JSON object of quota headroom; exit 64 = not implemented
<provider>.sh prepare   idempotently register the MCP server for this CLI
<provider>.sh launch    exec this CLI interactively as an orchestrator
```

Captured actions (`check`, `budget`) are run and read; handed-over actions
(`login`, `launch`) return an argv for the caller to exec, because they need the
terminal. Scripts live in `config/providers/`, resolved project-first then global
then shipped, and receive their situation through the environment
(`MULTIAGENTS_EXECUTOR`, `MULTIAGENTS_CONTAINER`, `MULTIAGENTS_PRIVATE_BACKING`,
`MULTIAGENTS_PROMPT_FILE`, …). See `providers/README.md`.

`check` and `budget` should not cost money — prefer inspecting stored
credentials over probing the API. `budget` is optional: a script exiting 64
defers to a built-in reader where one exists, and a provider with neither is
reported `unknown`, which is the honest answer.

The legacy `auth/` directory is still searched, so an install predating the
rename keeps working — but it always loses to `providers/` in the same layer.

The other half is the `providers.yaml` block itself: how to build the command
line, and how to read the CLI's event stream. Rules are ordered and
first-match-wins, and unmatched lines become `raw` events rather than being
dropped, so:

```bash
multiagents probe mycli --model some-model
```

tells you exactly which lines still need a rule. Paths address lists as well as
maps (`message.content[type=text].text`), which is what makes a CLI that nests
its reply in typed blocks addressable at all.

## Model catalog drift

`agents.yaml` pins specific model ids, and the ground underneath them moves. A
model can be withdrawn, repriced, or lose `tool_call` — the last of which makes
it unusable as an agent and fails runs confusingly.

`multiagents catalog` compares a local snapshot of the public catalog
(`models.opencode.ai/api.json`) against the live one and reports only what
matters:

```
catalog      3 change(s) since 2026-09-05T18:24:11+0200  [warning]
             WARNING  changed: glm-5.3-flash (cost input 0.01 -> 0.075)
                      used by: researcher
             1 other change(s) not touching your roster
             -> consult the critic before editing agents.yaml
```

It runs on `init` and `init-agent`, where the baseline belongs. The orchestrator
does **not** re-check it routinely — that would spend a network round trip per
session on ground that rarely moves. It calls `check_model_catalog` reactively
instead, when something suggests the ground has shifted: an unknown-model error,
a provider rejecting a model that used to work, or tool calls not happening from
an agent that should be making them. Cosmetic churn
(descriptions, release notes) is ignored; only `cost`, `limit`, `tool_call`,
`reasoning`, `structured_output` and `modalities` are watched.

Nothing edits `agents.yaml` automatically. The tool reports, the orchestrator
consults the critic about what it intends to do, and then the orchestrator
decides.

## Configuration

Three layers, each overriding the last: shipped defaults →
`~/.config/multiagents/` → `<project>/.multiagents/config/`. Maps deep-merge, so
a project can retune one agent's model without restating the roster; lists and
scalars replace.

| file | what |
|---|---|
| `project.yaml` | executor, git policy, security, limits, budget |
| `providers.yaml` | how to drive each CLI — **the extension point** |
| `agents.yaml` | the roster: name → provider, model, instructions, permissions |
| `agents/*.md` | per-agent instructions, prepended to every prompt |
| `BRIEF.md`, `context/` | **project root, committed** — see below |
| `providers/*.sh` | one script per provider: check, login, budget, prepare, launch |
| `models.yaml` | **generated** — `multiagents refresh-models` |

`BRIEF.md` and `context/` are the exception to everything else here: they live
at the project root and must be committed. Agents work in git worktrees, so
anything gitignored — all of `.multiagents/` — does not exist for them.
`enabled: false` on a provider is *intent*; availability stays detected, never
stored, because a stored fact goes stale and lies.

`models_include` in `providers.yaml` decides which model namespaces get
recorded. It ships restricted to `opencode/*` (free zen tier) and
`opencode-go/*` (the subscription); `deepinfra/*` is excluded deliberately,
because those bill against a separate API key rather than the subscription and
listing them would invite agents onto an account you did not intend to spend
from.

## One project, one repository

A multiagents project is **exactly one git repository, rooted at that
repository's root**. Everything else rests on it: `create_worktree` runs
`git -C <project root> worktree add`, and git resolves that to the
*repository*, not the directory you named.

So a project initialised inside another repository hands its agents full
checkouts of the outer repository, and branches in the outer repository's
namespace — while counting concurrency, budget and watchdogs separately, in its
own `tree.json`. It looks like it works, which is what makes it worth refusing.
`init` refuses, `--nested` overrides, and initialising below a repository root
prints what you are actually getting.

### A repository with subprojects inside it

One project at the repository root. Subprojects are directories; an agent gets a
worktree of the whole repository and works inside the subproject within it. This
is the normal case, and it needs nothing special:

```
voila/                     <- multiagents init here
├── .multiagents/
├── BRIEF.md               <- what the whole thing is
├── context/               <- shared reference material, read by every agent
├── stock_management/
└── billing/
```

If a subproject is big enough to deserve its own delegated tree, that is an
agent with `can_spawn: true`, not a second project: it stays inside the tree,
the budget and the supervision.

### Several independent repositories

One project per repository, each with its own orchestrator, worktrees and
container. There is no channel between them, by design — orchestrators are
launched rather than spawned, and `_preflight` refuses to spawn or consult a
`launch: true` agent so that an orchestrator cannot end up inside an
orchestrator. Share context through committed files, not conversation.

The known limit: an agent in one repository cannot read another repository's
`context/`. A worktree contains only its own repository, and the container
mounts only the project. If several repositories need the same reference
material, commit it to each or mount it with `executor.docker.extra_mounts`.

## Where agents run

`executor.kind` selects the backend, and an agent may pin its own with
`executor:` in `agents.yaml`. `multiagents doctor` marks pinned agents with `*`.

`init` asks which one this project should use, once, when you set it up. That
is a security question rather than a preference: agents run with the flags that
turn approval off — `--auto`, `--dangerously-skip-permissions`,
`bypassPermissions` — so on the local executor they have your user account,
your files and your keys.

```
executor     local
             agents run with approval turned off — `--auto`,
             `--dangerously-skip-permissions`. On the local executor
             that is your user account, your files and your keys.
             docker confines them to a container with no route out.
             docker is available here (server 29.5.1)
             use the docker executor for this project? [Y/n]
```

Where docker is not usable — not installed, or a daemon you cannot reach — it
says which, and offers to stop rather than to proceed quietly. Choosing to wait
prints install steps for the detected system plus the official page, and exits
non-zero:

```
not ready: you chose to wait for docker (docker is not installed).

On this system:
  curl -fsSL https://get.docker.com | sh      # official convenience script
  sudo usermod -aG docker $USER               # then log out and back in

Official instructions: https://docs.docker.com/engine/install/
```

Nothing is lost by waiting — the project is already set up, and re-running
`init` offers again. With no terminal the question is not asked at all and the
project stays local: `make init` must not switch a project's execution backend
with nobody deciding.

The shipped default stays `local`, so the tool works on a machine without
docker. A test asserts the shipped default and the code fallback agree, because
the config file wins and a disagreement means the code's value never applies.

### local

Git isolation via worktrees, credential separation via a per-agent `HOME` with
only that provider's state linked in, and a deny-by-default environment. What it
does **not** give you is process isolation: an agent running with
skip-permissions can reach anything your user account can.

### docker

`multiagents docker status` reports one project: images, network mode, resource
ceilings and every mount. `--all` reports the machine:

```
$ multiagents docker status --all
project                                      workspace        proxy
~/Documents/projects/multiagents             running          running
~/Documents/projects/voila                   running          running

2 project(s), 4 container(s) running.
```

Two containers per project — the workspace and its filtering proxy — and each
running project holds its resource ceiling whether or not agents are working,
so `docker down` in a project you have finished with is worth remembering.

The slug in a container name is a hash of the project path and does not invert,
so the paths come from a small registry under `~/.config/multiagents/`, written
whenever a command names a project. A container with no entry is listed as
`(path unknown)` rather than as a bare hash, and a project whose directory has
since been deleted is marked `(gone)`.


One long-lived container per project. Build once, then switch `executor.kind`:

```bash
multiagents docker build     # workspace + egress proxy images
multiagents docker up
multiagents docker status    # image, container, network, every mount
multiagents docker check     # proves the egress boundary
multiagents docker login agy # one-time interactive login inside the container
multiagents docker shell     # get in and look around
```

This closes the process-isolation gap and adds `--cpus`/`--memory`/
`--pids-limit`, the only hard cap on runaway recursion.

**Verified, not assumed.** An agent inside reports the container hostname, uid
1000, writes files owned `1000:1000` on the host, and commits on its own branch.
`~/.claude.json` and `~/.ssh` are not visible in the container at all — stronger
than local mode, where they are merely not linked.

#### Egress

Agents sit on an `--internal` Docker network with **no route off the host** and
reach the world only through an allowlisting proxy that is also attached to the
bridge. A real boundary, not an environment-variable suggestion:

```
$ docker exec -e HTTPS_PROXY= <container> curl https://example.com/
curl: (6) Could not resolve host: example.com
$ docker exec <container> ip route show default
(nothing)
```

Unsetting the proxy variables does not help; there is nowhere to go. `docker
check` proves both directions. Filter patterns are anchored, so `example.com`
permits `api.example.com` but not `evil-example.com`.

The socket is never mounted. Setting `mount_docker_socket: true` is *refused* by
preflight rather than honoured: with rootful Docker and a user in the `docker`
group, that is host root.

An isolated agent that cannot install a dependency is not safer, just useless,
so the allowlist covers three groups with different trades:

| group | why | if you drop it |
|---|---|---|
| model endpoints | no agent runs without them | nothing works |
| package registries and docs | `pip install`, `npm install`, reading docs | agents thrash |
| source hosting | cloning, reading issues | no `git clone` |

Source hosting is the only group that could carry data *out*. Agents hold no git
credentials to push with — `GITHUB_TOKEN` and `GH_TOKEN` are in `env_block` and
`SSH_AUTH_SOCK` is never forwarded — but remove it if that is not a trade you
want. Verified inside the container: `pip install requests` succeeds,
`raw.githubusercontent.com` is reachable, `evil-github.com` and
`github.com.attacker.net` are blocked.

#### Giving a provider its own login inside the container

Some credentials cannot be shared with a container. agy's host credential is not
a file at all — the host keeps it in the GNOME keyring, so there is nothing to
bind-mount. (`~/.gemini/oauth_creds.json` exists but is a stale legacy artifact;
chasing it was a dead end.)

A provider can declare `container_private_home` in `providers.yaml`. Those paths
are **not** mounted from the host — a private directory is mounted over each
one, so the container keeps its own credentials and can never overwrite or
downgrade the host's. Then:

```bash
multiagents docker login agy
```

runs the CLI interactively inside the container. With no keyring present, agy
falls back to a **file-based** token, which lands in
`~/.multiagents/container-state/shared/agy/.gemini/antigravity-cli/` — shared
across projects, since it is one account either way. Your host `~/.gemini` is
masked throughout and stays untouched.

One gotcha: agy runs an eligibility check on startup that fetches your account's
profile picture from `googleusercontent.com`. Block it and the check fails in a
way that reads as an authentication error. It is in the shipped allowlist for
that reason.

## What the isolation protects

- **No secrets in the environment.** Children start from a clean base and get
  only what `env_passthrough` names — empty by default, since every CLI
  authenticates with credentials it already stores. `SSH_AUTH_SOCK` is blocked,
  which matters: it is the one credential that is not a readable file, so
  withholding it genuinely prevents an agent authenticating or pushing as you.
- **Per-agent `HOME`** with only that provider's state linked in, so an opencode
  agent does not *pick up* Claude's or agy's tokens by accident. It is not a
  boundary: on the local executor the agent is you, and in a container the
  credential mounts sit at their host paths, so a model with a shell can read
  them deliberately. See "What is *not* protected".
- **Redaction is structural** — every byte written to disk or returned through a
  tool passes through `scrub()`, which masks secret-shaped strings, secret-named
  keys, and any literal registered as sensitive. It lives in the writer, so no
  call site can forget it.
- **Nothing is published.** With `git.remote` empty, agent work never leaves the
  machine. Pushing is always an explicit call, never a side effect of finishing.
- **Recursion has ceilings.** `max_depth`, `max_children`, `max_concurrent` and
  an optional tree-wide token budget, because an agent that can spawn agents
  that can spawn agents is an exponential with a credit card.

### What is *not* protected

`writes: false` and `permission: readonly` state intent; they are **not** a
permission boundary. Neither CLI can be made genuinely read-only from the
command line — with no permission flag `agy` auto-denies every tool and returns
an empty response, so a "read-only" agent configured that way cannot even read a
file. Read-only is enforced by git isolation instead: the agent works in its own
worktree and its branch is dropped if it turns out to be empty.

A container protects the host from the agent, not your tokens from the agent:
anything mounted so a CLI can authenticate can be read by a model with a shell.
Egress filtering is what makes that survivable — a credential an agent can read
is one it cannot post anywhere.

## Keeping the tree busy

`max_concurrent` is a budget to spend, not a ceiling to stay under. Measured
across one real 13-hour session:

| agents running | share of wall clock |
|---|---|
| 1 | **74%** |
| 2 | 12% |
| 3 | 7% |
| 4 | 2% |

Four were allowed throughout. The work was not smaller for being serialised —
it took about four times as long as it needed to, and the user eventually had to
ask for parallel work by hand.

Two causes, both in what the orchestrator was told. Its brief said nothing at
all about running work in parallel, and `wait_for_agents` described blocking as
the virtuous choice ("far better than polling") with no hint that waiting on an
idle tree is waste.

Now `wait_for_agents` returns `capacity` and, when slots sit idle, says so at
the moment the decision is being made:

```
"capacity": {"running": 1, "max_concurrent": 4, "free_slots": 3,
             "note": "3 of 4 slots are idle. Waiting is only free when there is
                      nothing else to start…"}
```

and the orchestrator's brief carries the test to apply before waiting: *is there
a piece of work that touches none of the files an agent is currently holding?*
It also says what is **not** safe to overlap — two agents on the same files, or
the stages of one feature, which are a chain by construction.

## Watchdogs, and why they need ground truth

Four trips mark a run `stuck`: silence, wall clock, runaway steps, and the doom
loop. A trip never kills the process — the orchestrator decides whether to
steer, extend or stop.

The doom loop asks two questions, and it needs both:

```
identical tool signatures   AND   the working tree stood still
```

The signature alone is not enough, and a real session showed why. A CLI reports
a write as `write_to_file:{"TargetFile": "…/test_x.py"}` with **no content**, so
three different edits to one file hash identically — and `edit → test → edit →
test` is an A,B,A,B alternation, which is the correct behaviour of a test agent
rather than a loop. Judged on signatures alone, 23 trips fired in one session on
work that went on to merge.

The second question is ground truth: every agent works in a worktree we created,
so `git status --porcelain` says whether anything actually happened, whatever
the CLI reported and whatever the agent narrated. A reader re-opening one file
changes nothing and trips correctly; a coder editing and re-testing moves the
tree every pass and is left alone. No `writes: false` special case is needed —
the check measures the environment's reaction, not the agent's intent.

Sampling is debounced and runs in a thread. A blocking `git` call inside the
loop that consumes the process's stdout would stop draining the pipe, which is a
deadlock rather than a slowdown.

Replaying that session's 59 recorded streams: **125 trips under the old rule, 20
under this one**, with the remainder concentrated in agents that genuinely
repeat reads.

`max_steps` came from the same session. 120 was calibrated on short test runs
and fired 27 times on work that was finishing; measured there, reviewers peak
under 100 while coders reach 768. The default is now 250, `implementer` and
`implementer-deep` carry 1000, and `implementer-quick` keeps its deliberate 40.
`limits.max_steps` in `project.yaml` is now actually read — it was documented
and ignored, with only the built-in default applying.

## Stopping

```bash
multiagents stop                    # everything for this project
multiagents stop --keep-containers  # leave the container up
```

The counterpart to `run`, and the requirement is that it be resumable. It ends
processes and keeps state:

1. **Whatever is driving the project** — the orchestrator or initializer, and
   under `--unattended` the supervisor *and* its current turn. Stopping the
   agents while leaving the orchestrator running would have it start
   replacements within the minute, so this goes first.
2. **Every active agent**, through the same path the MCP tool uses, which
   reaches into the container for an agent this process did not spawn rather
   than killing the `docker exec` client and leaving the agent inside spending
   tokens.
3. **Work in progress.** A killed agent never reaches the commit its own run
   would have made, so its edits sit uncommitted in a worktree nobody will look
   at again. Each dirty worktree is committed to its own branch — the branch is
   what makes the work resumable, so the work has to be on it.
4. **The container**, last, because the agents were inside it. Stopped, not
   removed.

Branches, worktrees, session ids, open questions, queued tickets and the
deferred queue all survive. `multiagents run` picks up where it left off, and a
stopped agent resumes through `steer_agent` with its session intact.

## Running unattended

```bash
multiagents run --unattended        # up to 50 turns
multiagents run --unattended 200
```

Each turn is one headless invocation of the same orchestrator session, with its
MCP tools and its brief intact, carrying a nudge to pick up where it left off —
read the tickets and questions, collect whatever finished while it was away,
and start every piece of independent work before waiting again.

Between turns the supervisor does the things a person would:

- **Waits out a quota reset** rather than burning turns against a wall, using
  the same headroom check `run --wait` uses.
- **Retries a crashed turn with growing backoff** — 30s, then 60s — and gives
  up after three consecutive failures. A turn that fails instantly and is
  retried instantly is a busy loop that spends quota on nothing.
- **Stops when two consecutive turns change nothing** in the tree: no new
  events, no new agents, no merges, no tickets or questions. Otherwise an
  unattended run keeps paying long after the work is finished.

A failed turn is never counted as an idle one. It tells you nothing about
whether work remains, and treating it as idle would end the run with the
reassuring message that everything was done.

### Why not a shell loop

The obvious `until multiagents run; do sleep 60; done` is wrong in both
directions: `until` stops when the command *succeeds*, so a turn that worked
ends the run, while a turn that crashes is retried forever with no backoff and
no limit. And `-p` — the flag that makes a turn headless — must not reach the
interactive path, where it would silently turn `multiagents run` into a
one-shot. It is set by the supervisor, per turn, and only there.

**Consider the executor before leaving this running.** Unattended is precisely
when nobody is watching agents that hold your user account on the local
executor. `init` offers docker for exactly this reason; this is the case that
makes the offer worth accepting.

## Running out, and coming back

Three layers, because the providers fail differently.

**A fallback model per agent.** Every working agent names a model on the *other*
provider in `agents.yaml`:

```yaml
  reviewer:
    provider: agy
    model: gemini-3.1-pro-high
    models:
      opencode: opencode-go/gpt-5.6-luna
```

A model id belongs to its provider's namespace, so failing over without one
would run `agy --model opencode-go/glm-5.3-flash`. Named, a constrained provider
costs you a model rather than an agent.

The fallbacks are chosen so that a **checking pair never collapses onto one
model** under a single-provider outage — specifier/adversary,
security-advisor/pentester, critic/advisor stay on different models whichever
provider is down. A test asserts it for both outage directions, because this is
exactly the property that would degrade silently at the moment nobody is
watching.

**A pause when there is nothing left.** When an agent's provider *and* its
fallback are both exhausted, the task is deferred and a pause is recorded,
naming the providers that ran out. `start_agent` then refuses **the agents that
pause actually covers** — one whose provider is healthy, or whose fallback lies
outside the pause, still runs. Freezing everything because one provider is out
would be over-applying it; what protects unreviewed work is the orchestrator's
rule about merging, not stopping agents that can still work.

The orchestrator's brief tells it not to route around a pause that does cover an
agent — not by switching providers, not by rewriting the plan, and above all not
by doing the work in its own context, which spends the one bucket that cannot be
refilled.

**Resume without anyone watching.** The deferred queue drains itself: the next
`wait_for_agents` restarts everything whose window has passed, re-reading quota
first. A pause keeps the **earliest** reset of the providers that caused it —
waking early costs one wasted check and an immediate re-pause, while waking late
blocks tasks whose provider came back ten minutes ago and nothing would notice.
It clears itself on read once it elapses.

Draining never loses work. `due_deferred()` does not remove what it returns; an
entry is dropped only once it has actually been restarted, so an exception
mid-drain leaves the queue intact rather than deleting the remaining batch. If
the window closes again partway through, the drain stops there instead of
grinding the rest of the queue into the same wall.

For the orchestrator's own provider there is no fallback to take — it is the
process you are talking to. `multiagents run` checks its headroom before
launching, reports the reset time rather than letting the CLI fail with an
opaque error, and `run --wait` blocks until the quota is back.

## Budget

Three providers sit at three tiers of knowability, and the adapter reports that
honestly rather than inventing a number:

- **claude** — real subscription state from `~/.claude.json`: percent used per
  bucket, reset times, overage credits. It is a cache, so staleness is reported.
- **agy** — has a full quota subsystem internally but exposes none of it. Spend
  only; exhaustion is detected reactively from a failed run.
Budget is read through each provider's own `budget` action, so a newly added
provider gets an entry with no Python change.

- **opencode** — a Go subscription serves real headroom over HTTP:
  `GET opencode.ai/zen/go/v1/usage`, bearer token from opencode's own
  `auth.json`, returning percent-used and a reset for three windows — rolling,
  weekly and monthly. `headroom` is the **worst** of the three, because the
  fullest bucket is what actually stops a run, but all three are carried
  through as `windows`: a rolling window clears in hours and a monthly one does
  not, and that difference changes what to do about it. Per-step dollar cost
  still comes from the event stream, so cost and capacity are both known.

  The key reaches curl through `--config` on stdin, so it stays out of the
  process list, and it is never printed. `doctor` shows the breakdown:

```
  opencode     27.0% used, resets 2026-10-05T13:11:17
                 monthly   27.0%  resets 2026-10-05T13:11:17
                 rolling   25.0%  resets 2026-09-07T11:29:26
                 weekly    10.0%  resets 2026-09-14T00:00:00
```

### Where the money went

No provider offers a per-model breakdown — opencode's `/usage/{models,detail,
breakdown,history}` are all 404 and `/v1/models` is a plain catalogue. So
`multiagents usage` computes it from our own stream accounting, joined to the
agents that spent it, which is the level at which a model pin is a decision:

```
provider/model                     runs       tokens      cost  share
claude/opus                           4            0 $  5.9733    48% ############
claude/sonnet                         5            0 $  3.1041    25% ######
opencode/opencode-go/kimi-k3          1    4,016,666 $  2.5612    21% #####
agy/gemini-3.1-pro-high              15    1,264,799 $  0.0000     0%
```

That is a real session, and it says something the per-provider total could not:
73% of the spend went to the `claude` provider — the rationed bucket the user's
own interactive sessions draw from. A `$0.00` row is a subscription rather than
a free lunch, so the command names which providers those are: their tokens are
real and their dollars are not comparable.

Providers declare how their usage accumulates, because getting it wrong corrupts
every number above it:

| provider | `usage_mode` | meaning |
|---|---|---|
| opencode | `delta` | per-step amounts, summed |
| agy | `cumulative` | running totals, taken at maximum |
| claude | `cumulative` | one result event carries the run total |

Claude is a full provider — orchestrator *and* delegate — with rules built from
a captured run rather than guessed. It is also the only one that can be made
genuinely read-only (`--restricted --tools Read,Glob,Grep`); the others can only
be sandboxed. Its `~/.claude.json` is **copied** into each agent's HOME rather
than symlinked, because it holds project history and the quota cache the budget
adapter reads — symlinking would have concurrent subagents writing your real
config, and copying also keeps it out of the container.

Worth knowing before putting agents on it: a one-line Claude probe cost $0.0174
against ~$0.0006 for an opencode-go call. Being available is not a reason to use
it.

`known: false` means spend is tracked but capacity is not. Never read it as
"plenty left". The purpose is *routing*: when your own five-hour bucket is
tight, delegating to an unrationed provider is the highest-value move available.

## Housekeeping

`multiagents uninstall` removes this machine's global config
(`~/.config/multiagents`) and agent state (`~/.multiagents` — worktrees,
per-agent homes, container credentials). It does **not** touch per-project
`.multiagents/` directories, your repositories, or the branches agents
committed to.

Two things it does that an `rm -rf` cannot:

- **Refuses while any worktree holds uncommitted work**, listing which. A
  commit survives in its repository as a branch; an uncommitted edit exists
  nowhere else. `--force` overrides, `--dry-run` previews.
- **Prunes the registrations it orphans.** Deleting a linked worktree's
  directory does not unregister it — the repository goes on listing worktrees
  that are not there. The owning repositories are collected from each
  worktree's `.git` file *before* deletion, since afterwards there is nothing
  left to ask, and pruned *after*, since git only drops a registration once the
  directory is actually gone.


```bash
multiagents doctor              # CLIs, agents, auth, budget, git — start here when something is off
multiagents tree                # what ran, what it cost, what is parked
multiagents watch               # tail every state transition, live
multiagents clean --branches    # drop finished agents' branches and worktrees
multiagents clean --tree        # prune finished nodes that hold no branch
multiagents upgrade-config      # refresh config copies you never edited
multiagents mcp-config          # print the MCP registration, for adding it elsewhere
```

`clean` refuses to delete a branch with unmerged commits unless you pass
`--force`; work an agent actually did should not vanish by accident.

`upgrade-config` deserves a word. Your config is *copied* into the global and
project layers so you can edit it — but a pinned copy overrides the shipped file
for every key, so improvements to the defaults would otherwise never reach an
existing install. A manifest records each file's hash as written, so a copy that
still matches was demonstrably never touched and is refreshed; one you edited is
kept and reported. `--force` overwrites edited files too, keeping a `.bak`.

```
$ multiagents upgrade-config --dry-run
  would refresh providers.yaml   (unmodified copy)
  keep    agents.yaml            (you edited it; shipped version has changed)
```

## Tests

```bash
make test
```

198 tests covering the parts live runs do not reliably exercise: doom-loop
detection, credential redaction, config merge semantics, corrupt-tree recovery,
catalog drift assessment, the docker executor's mount and network construction,
the provider script contract, the orchestrator-not-spawnable guards, the
ownership gate on mutating tools, `awaiting_user` transitions and their
non-interaction with the watchdogs, the refusal to spawn without a repository,
what a bug ticket must not contain, and the roster checks `doctor` makes.

Provider event fixtures are real shapes captured from the CLIs, not invented —
`tests/fixtures/claude-stream.jsonl` is an actual run that used a tool, and a
golden test asserts every line of it classifies with nothing falling through to
`raw`.

## License

MIT — see [LICENSE](LICENSE).
