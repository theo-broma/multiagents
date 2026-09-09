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

multiagents init         # create the project, copy the global config, choose the executor
multiagents build        # container images if this project uses docker, then authenticate
multiagents init-agent   # shape it with the initializer — resumable, takes as long as it takes
multiagents run          # launch the orchestrator; first run and resume are the same command
multiagents stop         # halt everything for this project, resumably
```

`build` comes before `init-agent`, not after: the initializer is told to
consult the critic and the advisor, and a consult spawns an agent, so on a
docker project it needs the images too. Both commands check for them and refuse
with the fix named rather than failing partway through a conversation.

The lifecycle is a **loop, not a line**. `init-agent` shapes a phase, `run`
executes it, and when the brief's work is done the orchestrator hands back
rather than inventing more: it executes the brief, it does not decide what the
project is. `init-agent` then shapes the next phase, and it knows to expect
that — coming back to a project with merged work and existing specs is a
different job from starting one, so it reads what was actually built rather
than what was planned, and extends the brief instead of rewriting it.

Between phases, an ordinary next task needs none of that: tell the orchestrator.
`init-agent` is for when the *project* changes, not when the task does. The test
is whether an agent that has never spoken to you would need it in `BRIEF.md` to
work correctly.

`run` continues the last session where there is one and starts fresh where there
is not, so it is the same command either way. `--fresh` forces a new session,
and `resume` is an alias for `run`. The same applies to `init-agent`.

`run` also checks two things before launching, because both otherwise fail
after you have already briefed the orchestrator:

- **the orchestrator's own quota** — an exhausted CLI reports it as an ordinary
  error with no reset time in it, which reads as a broken install.
  `run --wait` blocks until the quota is back instead of exiting.
- **the container images**, on a docker project. The orchestrator runs on the
  host, so nothing about docker is exercised until it delegates; a missing
  image would otherwise surface as a failed spawn several minutes in. Note that
  the *container* does not need starting — the first agent brings up the
  network, the proxy and the workspace in about a second — but images are built
  by `multiagents build` and never lazily.

`run --no-launch` reports both without refusing, because inspecting a broken
project is what it is for.

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
multiagents monitor  # all of it, in a browser or (--tui) in this terminal
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

### One session per launched role

`claude --continue` resumes *the most recent conversation in the directory*, and
both launched roles run in the project root — so `init-agent` started after
`run` reopened the **orchestrator's** conversation. Reported from a real
session.

Each role now owns a session id, generated once and kept in
`.multiagents/launch/<role>.session`. The launcher resumes it by id when a
transcript for that id exists, creates it under that id when one does not, and
rotates to a new id for `--fresh` — reusing one that already names a transcript
would collide with the session it points at.

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

```
multiagents tickets                         # the queue
multiagents tickets show <id>               # the issue exactly as it would post
multiagents tickets submit <id>             # prints it, asks, then gh issue create
multiagents tickets resolve <id> --note …   # you fixed it
multiagents tickets resolve <id> --declined # it was not a bug
multiagents tickets discard <id>
```

`resolve` closes the loop the others open. Marking a ticket fixed used to exist
only as an MCP tool — reachable by the orchestrator and not by the person who
did the fixing — so a ticket you reported and then fixed stayed `reported` for
ever unless somebody edited `tree.json` by hand.

It says the thing that is easy to forget in each direction, too: resolving one
that was filed upstream reminds you the issue is still open for everyone else,
and resolving one that was never filed points out the defect is still there for
anyone who has not fixed it locally.

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

### The provider script contract

A provider is a block in `providers.yaml` plus **one script**. No Python.

```
<provider>.sh check     exit 0 authenticated / 10 not / * unknown; one line of status
<provider>.sh login     may take the terminal; prints what to do BEFORE doing it
<provider>.sh budget    prints one JSON object of quota headroom; exit 64 = not implemented
<provider>.sh usage     prints the lines the monitor shows for this provider,
                        given the parsed budget in MULTIAGENTS_BUDGET; 64 = generic view
<provider>.sh prepare   idempotently register the MCP server for this CLI
<provider>.sh launch    exec this CLI interactively as an orchestrator
```

Captured actions (`check`, `budget`, `usage`) are run and read; handed-over actions
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
| `providers/*.sh` | one script per provider: check, login, budget, usage, prepare, launch |
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

## The monitor

`multiagents status` answers one question in one line. When what you want is
*what is this project doing*, there is a monitor:

```
multiagents monitor          # a local page, opened in your browser
multiagents monitor --tui    # the same thing drawn in this terminal
```

Both front ends are thin. Everything they show comes from one `snapshot()` and
everything they do goes through one `actions.perform()`, so they cannot drift
into disagreeing about what is true, or into one of them quietly growing a
capability the other lacks — there is a test that fails if they do.

**The poll never moves the page under you.** It compares a signature of what is
worth showing — counts, statuses, token totals, alert texts — and when nothing
in it changed, nothing is rebuilt at all. When something did, scroll positions
are read off before the rebuild and put back after, the page's own included,
because detaching a node resets its `scrollTop`. Without that, reading the
activity log or a long transcript meant being snapped back to the top every two
seconds. The config form is never redrawn by the poll, since redrawing a form
under a cursor eats what is being typed.

The poll also has to stay cheap, so it reads files and nothing
else: git and the auth scripts are separate endpoints, asked for on demand, and
each provider's `usage` script output is cached against the budget that produced
it — a subprocess per provider per tick is an idle monitor with a fan. Agent
transcripts and the event log are read **backwards from the end**; this project
has seen an 11 MB stream, and the button that opens one is the button you press
when something has already gone wrong.

**Live** is the page you leave open. Running agents with their tokens, cost,
steps, elapsed time and token rate; each provider's usage; anything an agent is
blocked on, answerable in place; and an alert banner at the top for the things
that silently cost you a morning — a limited or stalled orchestrator with its
reset time, a provider with no headroom, a tripped circuit breaker, an agent
marked running whose process is gone.

**Config** lists every setting in the merged configuration with the right
control for its type: a dropdown for an agent's provider and for the models that
provider actually serves, a toggle for a switch, a number field for a limit. The
schema is *derived* rather than maintained beside the config — types from the
values already there, choices from the config itself, and **the help text under
each setting is that setting's own comment**. Edits go to the project layer and
are written surgically: the line changes, every comment in the file stays. These
files are mostly comments and the comments are the documentation; a round-trip
through a YAML dumper would leave a config that still worked and taught nobody
anything.

Surgery on YAML by hand earns three specific defences, each of which was a real
bug first: a block scalar's body is skipped entirely, because `description: >-`
is followed by prose and prose contains lines like *"Use it when: …"* that look
exactly like keys; an inline comment is found by scanning for a `#` outside
quotes rather than by pattern, because the pattern that protected `key: "#fff"`
also deleted the comment on `key: "value"  # note`; and the indent step is
measured from the file rather than assumed to be two, since a new key at the
wrong depth is a different key. Structural edits are refused outright and a file
that would not parse is never saved.

**History** is the tree, parents holding their children, collapsible, with the
transcript in a side column when you click an agent — prompt, stream, summary,
result — plus the branch view: which agent branches exist, which are merged,
which still hold unmerged work, and how much.

**Costs** rolls the same usage up three ways, because *what did last night
cost*, *which agent is expensive* and *which model is expensive* are three
different questions.

### Each provider shows its own usage

A quota's shape differs per provider and there is no honest common denominator:
claude has two rolling windows and a credit pool, opencode serves three windows,
agy exposes nothing at all. Flattening those into one bar would invent precision
for two of the three.

So `usage` is an optional action in the [script contract](#the-provider-script-contract):
it receives the already-parsed budget in `MULTIAGENTS_BUDGET` — it formats, it
never re-fetches — and prints whatever its numbers deserve. Exit 64 falls back to
a generic rendering.

```
claude     ████████░░  78% of the tightest window
           resets 2026-09-09 16:50
           credits 86.03 of 85.00 — spent
           nothing carries a session past a full window

opencode   rolling  ░░░░░░░░░░   0%  2026-09-09 19:57
           weekly   ████████░░  86%  2026-09-14 00:00
           monthly  ██████░░░░  65%  2026-10-05 13:11
```

Claude's panel names the credit pool because that is the story: when it is
spent, a full window stops work dead and the CLI announces it as *"you've hit
your monthly spend limit"*. Somebody reading the panel at that moment should not
have to already know that.

### Full control, and what guards it

The monitor can stop an agent, steer it, merge or discard its branch, push it,
answer a question, move a ticket along, lift a pause, and start the orchestrator
headless. It cannot *spawn* an agent: that is the orchestrator's job through
MCP, with its depth, concurrency and budget checks, and a button that bypassed
all of it would be a second, worse scheduler.

Three things guard the API, and it takes all three:

- **It binds 127.0.0.1 only.** Nothing on the network can reach a page that
  stops agents and rewrites config.
- **Every call carries a token**, minted per run and embedded in the page it
  serves. Localhost is reachable by *other programs on this machine*, a hostile
  browser tab included.
- **The `Host` header must be a loopback name**, checked on every route
  including `/`. This is the one that is easy to miss: a site can point
  `local.evil.com` at 127.0.0.1, at which point the *browser* believes it is
  same-origin and sends the request with no preflight, the connection arrives
  on loopback looking ordinary, and `GET /` hands back the page with the token
  in it. DNS rebinding defeats bind-plus-token on its own; an advisor caught
  this and was right.

Destructive actions declare themselves in `actions.DESTRUCTIVE` and both front
ends confirm them in the same words. `signal_process` will only signal a pid the
tree says is ours.

## Watching the orchestrator

`run` execs into the provider's CLI, so the orchestrator *is* that process and
there is nobody inside it to report on itself. `run` therefore starts a small
supervisor beside it, detached, which exits on its own when the pid it watches
disappears.

```
$ multiagents status
working        producing output 6s ago
               observed 0s ago
               transcript quiet for 6.1s
               0 agent(s) running
               claude headroom 85%, resets 2026-09-07T13:59:59
```

It samples four things and never reads a word of the conversation: whether the
process exists, whether the session log has grown, how many agents are running,
and what the provider says about headroom.

### And acting on it

`run` no longer execs. It spawns the CLI as a child that owns the terminal and
stays alive as its parent — which buys exactly one thing, and it is the thing a
detached watcher could never have: **the exit code**. From outside you only
learn that a pid went away, and `/exit` and a dropped connection look identical.

| exit | reading |
|---|---|
| `0` | the session ended normally |
| `SIGINT` / 130 | interrupted from the keyboard |
| `SIGTERM` / 143 | `multiagents stop`, or something else asked it to end |
| `SIGHUP` / 129 | **the terminal was lost** — a closed window, a dropped connection |
| anything else | **it crashed** |

The first three end quietly. **A lost terminal** — the case this exists for —
makes `run` wait and **start the orchestrator again, interactively**, with an
opening message asking it to take stock:

> Your previous session ended unexpectedly. This is a restart, not a new task,
> and not a decision point. Take stock first: agents left interrupted in the
> tree, open questions and tickets, and any branch holding a WIP commit made by
> the recovery — that work may be mid-edit and is not a finished result. Say
> briefly what you found, then carry straight on with the work. Do not propose a
> plan and wait for it to be approved, and do not ask whether to proceed; nobody
> may be reading. Stop for the user only where you would have stopped in any
> other session — a choice that is genuinely theirs to make.

The last three sentences are load-bearing. A restart that comes back, proposes
a plan and waits has turned one interruption into two, and the restart may well
have happened because nobody was there.

Interactive is the point. `claude [options] [prompt]` opens a session *with* a
first user turn rather than waiting for one to be typed, so the restart starts
working — but on a terminal you can see, and stop. That is what makes retrying
an unknown state reasonable: a crashed orchestrator resumed where nobody is
watching is the case the advisor rightly called an automated rampage; the same
resume in front of you is a session you can Ctrl-C.

Five attempts a minute apart by default (`limits.restart_attempts`,
`limits.restart_delay_seconds`), waiting out a quota reset if one is in the way,
and stopping the moment an attempt ends deliberately.

**A crash is not retried**, and that is a deliberate reversal of the obvious
behaviour. By the time an error reaches the process boundary the CLI has already
exhausted whatever internal retry it has, so whatever produced it is still there
and a restart reads it again — five times, spending tokens to arrive back where
it started.

The tempting defence, "only retry if it survived a while first", does not hold
either: a context-length overrun or an OOM parsing a huge payload takes minutes
to arrive and then repeats exactly. `limits.restart_on_crash` turns retrying on
for anyone who wants it, and even then a failure inside
`restart_min_runtime_seconds` stops immediately as the same fault being read
again.

**With no terminal left**, retrying interactively has nowhere to run, and it
falls through to the headless loop — subject to the check that somebody had
actually given the session work to do.

For the parent to be there to decide any of this it has to outlive the terminal,
so it takes a do-nothing handler for **SIGHUP** as well. The child still gets
the default disposition, because a handler is reset on exec while `SIG_IGN`
would be inherited.

Before any session starts, `run` reconciles what a previous one left. Agents are
spawned in their own session so that stopping one also stops the shells and test
runners beneath it — which means they **outlive a server that crashed**. Those
orphans are reaped (through the container for a docker agent, since killing the
`docker exec` client would leave the agent inside running), unless another
session is live, in which case they are left to their owner.

Their work is committed at that point, not during teardown: `gitops` shells out
with a two-minute timeout and a git call on a closing event loop can hang the
shutdown it is part of. The commit is labelled for what it is —

```
WIP: implementer interrupted before it finished (ag-1)

Committed by multiagents so the work is not lost, NOT by the agent. The tree
may be mid-edit and syntactically broken through no fault of the agent — treat
it as a checkpoint to inspect, never as a finished result.
```

— because an orchestrator that picks the branch up later must not run tests
against half-written files and spend tokens debugging syntax errors the
termination caused.

Four details make a spawned child behave like the exec it replaces, and the
third is the one that bit:

* stdio is inherited and **no new session is created**, so the child stays in
  the terminal's foreground process group. A new session would leave it unable
  to read stdin at all — the first read raises `SIGTTIN` and it stops.
* the terminal mode is saved and restored, since a child dying in raw mode would
  otherwise leave an unusable shell; with `exec` the shell cleaned that up.
* SIGINT and SIGQUIT get a **do-nothing handler here, not `SIG_IGN`**.
  `SIG_IGN` is inherited across exec and a handler is not — so ignoring them in
  the parent made the child ignore them too, and Ctrl-C stopped reaching the
  orchestrator entirely. Found by running it; reading the code would not have
  shown it.
* nothing in this process reads stdin, or it would steal the child's keys.

| | verdict |
|---|---|
| the CLI's own limit message, unanswered | `limited` — its provider stopped it |
| gone, no headroom | `out_of_quota` — it ran out |
| gone, headroom fine | `stopped` — it exited or crashed |
| alive, log moving | `working` |
| alive, silent, agents running | `waiting` — probably on them |
| alive, silent, nothing running | `idle` — probably on you |
| alive, silent, no headroom | `stalled` |

**The quota reading is what makes the middle two distinguishable**, and they need
opposite responses: one waits for a reset, the other is a bug.

### The stop that never reaches the exit code

Every path above keys on the process **ending**. A usage limit does not end it.
The CLI prints the limit into its own chat log and stays at the prompt, alive
and idle, so `run` waits on a `wait()` that will never return, `status` says
*"idle — probably waiting for you"*, and a session that a provider stopped dead
at 11am is still sitting there at 5pm having done nothing.

That is a real morning lost, and it was worse because the quota reader had gone
blind at the same time: the vendor removed `cachedUsageUtilization` from
`~/.claude.json`, which took `stalled` off the table too and left `idle` as the
only thing status could say. Both halves came from one session that sat at a
limit for three hours and eighteen minutes while `status` reported it as waiting
for a human who was not being asked for anything.

**That blindness is fixed at the source** — see [asking the account
directly](#asking-the-account-rather-than-a-cache-of-the-answer) — but the
detection below stays, because a reader that depends on one undocumented surface
staying put has already been wrong once.

So the parent now **polls while the child runs** (`STALL_POLL_SECONDS`, 60s)
instead of blocking on it, and asks one question: is the CLI's own limit message
the last thing said. If it is, twice in a row, the session is ended so it can be
restarted — with a printed minute of grace, because anything typed clears the
detection and hands the session back to the person sitting in front of it.

Then it waits and starts the session again — and the interesting part is what it
refuses to conclude from the message.

**The wording does not identify the limit.** Measured on 2026-09-09 against
`/api/oauth/usage`: claude prints *"You've hit your monthly spend limit"* when
the **five-hour window** fills while the extra-usage credit pool — the thing
that would otherwise have carried the session past it — happens to be spent. It
names the pool, not the wall, and then contradicts itself in the same sentence:
*"your session limit resets 1pm"*. Waiting does fix it. A marker may still
declare `resets: false` for a
string one day known to mean a dead account, and the run then stops with exit 3
rather than waiting, since waiting cannot put money in an account. **Nothing
ships marked that way, and a test enforces it**, because the two mistakes are
not equal: a wall wrongly assumed costs a whole afternoon of doing nothing,
while a window wrongly assumed costs one relaunch per wait.

So the tree is paused for that provider and the run waits — **until the reset
timestamp the provider gives**, capped at six hours, which is the payoff for
asking the account instead of parsing the sentence. With no timestamp to be had
it falls back to `limits.limit_wait_seconds` (15 min, ×2 ×3 ×4 across
consecutive limits). Then the session starts again with the resume prompt.
**A wait is not a restart attempt** and does not spend one:
the window is five hours, and `restart_attempts` backing off from a minute would
give up halfway through it having proved only that the limit was still there.
`limits.limit_max_waits` (12) bounds it instead, reaching past ten hours.

For a `resets: false` limit the pause is held for
`limits.spend_limit_pause_hours` so the reason stays visible to anything reading
the tree — not as a claim about when it clears. Launching the role by hand lifts
it immediately: only a person can fix a dead account, so a person starting it
again *is* the event it was waiting for. Only a person, though — under a script
(`stdin` is not a tty) the pause stands, or a deliberate stop would become a
retry loop on a timer.

### Asking the account rather than a cache of the answer

`~/.claude.json`'s `cachedUsageUtilization` held percent-used per window, reset
timestamps and the overage pool — everything routing needs. Then a vendor update
removed it, and nothing here noticed: `known=False` is a legitimate state for a
provider that cannot report headroom, so the system went quiet rather than
wrong, which is worse.

That key is a **cache of one HTTP response**: `GET /api/oauth/usage`, the
request Claude Code makes to fill it, authenticated with the OAuth token in
`~/.claude/.credentials.json`. So the reader now prefers the cache while it is
fresh — free, and no request against somebody's rate limit — and asks the
account directly when it is stale or gone. One parser serves both, because they
are the same payload.

```
$ multiagents doctor
budget
  claude       53.0% used, resets 2026-09-09T16:49:59+00:00
```

Two things fall out of the payload that prose could never have given us:
`five_hour.resets_at`, which turns a blind backoff into one wait of the right
length, and `extra_usage.spend_limit_reached`, which is the honest boolean for
the condition the CLI describes in a misleading sentence.

**The account being risked is the user's, not this project's.** An advisor's
objection, which I take: the endpoint is undocumented, a bot-detector cannot see
an OAuth token's good intentions, and the blast radius of being judged
unwelcome is somebody's account rather than a degraded reading. So:

- `limits.ask_provider_for_usage` turns it off, and the shipped config explains
  the trade rather than burying it. Off, readings fall back to the CLI's cache
  and go quiet when it does.
- **One fetch per machine, not per process.** Every agent runs its own MCP
  server, so an in-process cache would put N processes across the same
  staleness second and into the same millisecond. A shared file plus a
  non-blocking `flock`: whoever takes the lock fetches, everyone else keeps the
  stale copy rather than queueing. On a **cold start** there is no stale copy
  and every process arrives in the same second, so a reader that loses the lock
  waits up to three seconds for the winner's answer instead of reporting a
  reading nobody had to be without.
- **Jittered**, so restarts and machines never settle into one exact heartbeat.
  A perfectly periodic request is a signature.
- **A refusal stops the asking.** 401 and 403 hold off for six hours: they need
  a human, and asking again on a timer until one appears is the behaviour that
  would deserve being blocked. 429 gets five minutes rather than that company —
  it far more often means *too many at once* than *you are out*, and answering
  a sixty-second concurrency limit with an hour of silence turns somebody
  else's transient into our own outage. A `Retry-After` header overrides all of
  it: the server knows, we are guessing.
- **An honest `User-Agent`.** Copying the CLI's own would make the request
  indistinguishable from it, which is impersonation to evade a check — a
  different thing from reading your own usage, and not a thing this does.

The token is read at the moment of use, never held, never passed to a child, and
registered as a redaction literal (see `scrub()` above) so that if it escapes by
a route nobody thought of it is masked before that output reaches disk. An HTTP
error keeps only `error.type` and `error.message`, scrubbed and capped — "HTTP
403" alone would hide *account suspended* and *unsupported region*, while the
rest of the body can echo back what was sent. An expired token is not sent at
all: refreshing it is the CLI's job, and since `known=False` means *unknown*
rather than *empty*, an unreadable quota never stops work — it just says
plainly that `claude` needs running once.

Undocumented, therefore fenced: every failure degrades to `known=False` with a
note.

### Why not read the transcript's contents

Because there is nothing structured in it to read. Searching a real 11 MB
transcript for a rate-limit event returned eighteen apparent hits, and every one
was the session's own prose *about* quotas. A supervisor matching on content
would have fired on the conversation that designed it.

That is not hypothetical here: an earlier classifier in this project scanned
agent output for the same words, and an advisor writing "quota" cooled a
provider down for fifteen minutes and lost the conversation. Structure only.

**One exception, and it is narrow.** `providers.yaml` may list `limit_markers`
under a provider's `transcript:` block — literal strings the CLI's *error
handler* prints, not sentences a model composes. Reading those is not
classifying prose; it is reading a stack trace that was delivered to the wrong
address, because the CLI chose the chat log as its error channel instead of
stderr or an exit code. Three guards keep it from becoming the thing it is not:
the string is matched only in the **last assistant message**, so a session that
recovered is history and an agent quoting it mid-conversation is not the CLI
speaking; anything a **person typed after it** cancels the match, tool results
excluded by shape rather than by content; and a provider that declares no
markers is never read for one at all.

`providers.yaml` says where a CLI keeps its session log — only claude declares
one, since opencode uses a sqlite database and agy an opaque directory. A
provider without one still gets process liveness, agent activity and quota, and
says which it is rather than reporting a fault.

## Surviving a crash or a power cut

What is on disk when the power goes:

| | survives | why |
|---|---|---|
| committed agent work | yes | git branches, and git fsyncs its own objects |
| uncommitted worktree edits | usually | ordinary files, subject to the filesystem |
| `events.jsonl`, `runs/*` | yes | append-only; a partial last line is skipped |
| `tree.json` | yes, minus at most the last write | see below |

`tree.json` is written to a temp file and `os.replace`d, so no reader ever sees
half of one. But **atomic is not durable**: without a flush, the rename can land
while the file's contents are still in the page cache, and what survives is a
zero-length tree. So the temp file is fsynced before the rename and the
directory after it.

fsync narrows that window and cannot close it, so the previous copy is kept as
`tree.json.bak`. A corrupt read falls back to it, **heals the file** by writing
the recovery back — otherwise every later read re-recovers and the project stays
one bad read from the empty case — and keeps the damaged original as
`tree.json.corrupt-<timestamp>`, because the first thing anyone wants is to see
what was in it.

The cost of recovery is exactly one write: the backup is a generation behind, so
the newest question, ticket or deferred task may be gone. Everything older is
intact, including the session ids without which nothing resumes.

When both copies are unreadable it says so, loudly, naming what was lost.
Emptying the tree silently was the old behaviour and it is the worst one — every
session, question and queued task disappears while the next command reports a
clean project as though the work had never happened.

Session ids are also emitted to `events.jsonl` when first seen. They are the one
field reconstructible from nowhere else, so the append-only log carries them
even if the tree does not.

## When the orchestrator itself runs out

Two different moments, and only one of them was handled.

**Before launching**, `run` reads the orchestrator provider's headroom and
refuses with the reset time rather than letting the CLI fail with an opaque
error. `run --wait` blocks until the quota is back.

**Mid-session** nothing in multiagents notices, and this is structural rather
than an oversight: the orchestrator *is* the process — `run` execs into it — so
there is no supervisor left to watch it. What follows depends on the CLI. If it
merely refuses further turns, the session sits idle and the agents already
running carry on, since they are separate processes on their own providers. If
it exits, the MCP server it hosts exits with it, and every in-flight agent is
cancelled and recorded as `interrupted: the server exited while this agent was
running`.

That last case used to lose work. The commit an agent's run makes sits after
the re-raise in the cancellation handler, so an agent killed with the server
left its edits uncommitted in a worktree nobody opens again. It now commits on
that path too, synchronously, because the event loop may already be shutting
down and there is nothing left to await with.

So an orchestrator that runs out mid-session costs you the session and not the
work: branches carry what each agent had done, and `multiagents run` resumes.

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

### What makes a headless turn start at all

Interactively, the orchestrator's brief arrives as a **system prompt**
(`--append-system-prompt-file`), so the CLI opens and waits: it has its
instructions but no task. A headless turn passes the nudge as `-p`, which is the
**user message** you would otherwise type. That is the whole difference, and it
is why the same session that sits idle in a terminal starts working immediately
without one.

Which creates a case worth refusing. If a session dropped *before* anyone typed
anything, "continue where you left off" has nowhere to continue from, and the
nudge would have it invent work from `BRIEF.md` — unattended, with agents
holding bypass permissions. So the handover checks the transcript for a human
turn first, and stops if there was none.

A `user` record is not enough to go on: in a real session 992 of them were tool
results against 102 typed messages. A typed message is the one whose content is
a plain string rather than a list of `tool_result` blocks — shape again, never
words.

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

## Credentials cached in a running process

Re-authenticating writes a new token to a file the container reads through a
symlink, so it ought to be enough. It is not, for a CLI that keeps the
credential in a **running process**.

A real session found this the hard way. The token was revoked, the user
re-authenticated successfully, the host worked — and agents kept failing with
`401 OAuth access token has been revoked`. Every obvious explanation was
eliminated in turn: the credential file was a live symlink, byte-identical and
unexpired; the copied `.claude.json` matched down to `machineID`; the egress
allowlist was not blocking anything. Cross-testing isolated it:

```
container + host's HOME   -> 401 revoked
host      + agent's HOME  -> works
```

`claude` runs a background daemon. The container had been up for hours, started
before the re-login, and its daemon was serving the revoked token from memory
while the file on disk was correct throughout. Restarting the container fixed
it. `auth status` could never have caught this — it reads the same correct file.

The bug-reporter filed this itself, as `bug-cee638`, and found a second thing
with it: `steer_agent` returned `{"steered": true, "status": "running"}` against
a process that had already exited. `_launch` returns when a process has
*started*, and an unauthenticated provider answers in well under a second — so
the caller waited for progress that could not come. A steer now waits briefly
and reports what actually happened.

`auth_status` reports the two things separately rather than blending them:

```
stored_login    true      the credential really is on disk
last_run        failed    and the evidence says it does not work
recent_failures 2         401 revoked
```

`last_run` is `success`, `failed` or **`untested`** rather than a boolean: a
provider that has simply not run yet is not a broken one, and a false there
would send the orchestrator off to debug a healthy system.

Three things now close it:

- **`auth login` refreshes the container.** Idle, it restarts without asking,
  which costs a few seconds. Busy, it asks first — a restart kills every agent
  in there, including ones on providers that are perfectly fine.
- **`doctor` compares timestamps.** A container that started before a
  provider's credentials were last written is flagged, because that is exactly
  the shape of this failure and costs nothing to check:

```
  !! claude: credentials changed 17 min after the container started — a CLI
     that caches them in a running process is still serving the old ones.
```

The general principle, worth remembering when adding a provider: **a credential
change invalidates any process that may already have read it.** A long-lived
shared container is the thing that makes that observable; per-agent ephemeral
containers would not have the problem at all, which is a fair argument against
the current design and not one this handles.

## What a failure rate does and does not tell you

A day of real use looked like a 46% failure rate — 19 of 41 agents. Broken down
it was almost entirely one thing:

```
13   the claude token revocation — one incident, counted thirteen times
 2   deliberate: one stopped by the orchestrator, one by a test
 4   genuinely unexplained
```

**Four of 41, about 10%.** The headline number was a single credential outage
wearing nineteen hats, which is worth remembering before optimising any figure
of this shape.

The four were then legible enough to fix a real gap: three ended with agy
emitting `{"kind": "result", "status": "ERROR"}` — a structured verdict that
`_classify` read to decide "failed" and the code recording the outcome then
discarded, leaving a node marked failed with an empty reason. The provider's own
verdict is now the reason, and a result carrying a non-success status keeps its
payload for the same reason an unparsed line does.

Two hypotheses were tested against the data and **refuted**, which is worth
recording since both were plausible:

- *Long runs exhaust context and die.* The opposite: eight runs over twenty
  minutes, zero failures, including one of 6506 seconds and one that spent 5.5M
  tokens successfully. The failure rate was highest at the **short** end, where
  the 401s died in seconds.
- *A 300-second gateway timeout.* The three agy failures cluster at 306, 309 and
  342 seconds, which is suggestive — but the proxy's idle timeout is 600s and
  those runs were making tool calls throughout.

Twenty-one data points cannot settle this, and slicing them further is a way of
appearing busy. The useful move was making the next failures say what happened.

### The number that is missing

Every figure above counts runs that **failed**. A run that succeeds, merges, and
turns out to be wrong costs far more — everything built on it — and appears
nowhere.

That is not derivable after the fact: branch and timing cannot tell "this
reviewer examined that work" from "this ran next", and the guess breaks the
moment two checks overlap or a branch is reused. So the orchestrator declares
it, with `start_agent(..., verifies=<agent_id>)`, and `multiagents usage
--checks` reports the graph:

```
work                        checked by                     outcome
ag-8a5e14 implementer       ag-bd31b1 reviewer             done
                            ag-4f2a91 tester               failed
```

Whether a review *found* something is in its prose, and deciding that from here
would be the same mistake as classifying a failure from an agent's own words. So
the verifier says it, on one line, in a form a machine can read:

```
VERDICT(approved): nothing here needs changing
VERDICT(rejected, 3): three defects, the first blocking
```

That is not circular. The whole arrangement already trusts a reviewer's
judgement over an implementer's code; this only asks it to state that judgement
where it can be counted. `reviewer`, `tester` and `pentester` are told to end
with one, and the report becomes a rate over the work that was actually judged —
counting unjudged runs as approved would flatter it:

```
2 run(s) checked, 3 check(s); 1 needed more than one.
1 of 2 judged run(s) were rejected (50% rework) — work that finished and had
to be redone anyway.
```

### One free retry

A run that dies with nothing to say, before it has done any work, is a transient
glitch far more often than a real fault — so it is retried once rather than
making the orchestrator reason about infrastructure.

Bounded by cost, which is where this departs from the advice that produced it: a
run that died at 996 seconds had spent 5.5M tokens, and silently spending that
again is not absorbing a glitch. Past `retry_silent_failure_under_seconds` it is
reported and handed back.

The first version kept "have I retried?" on the in-process `Run`, and `_launch`
builds a fresh one — so the flag reset on every retry and one free retry became
an unbounded loop, which only the provider circuit breaker stopped. The count
lives on the node now, where it survives the relaunch it guards.

## Diagnosability

A day of real use produced 41 agents and 19 failures, and three of the four
things that made those failures hard to read were ours.

**An unparsed line was discarded.** A stream line matching no rule becomes a
`raw` event so that nothing is silently lost — but the record written to
`stream.jsonl` omitted the payload, so what landed on disk was
`{"kind": "raw", "text": ""}`. One 996-second failure had exactly one raw event
three seconds before it died, and it was empty: the line that would have
explained it was parsed, found not to match, and thrown away. `multiagents
probe` exists to find lines falling through to `raw` and could not show them
either. The payload is now kept, bounded and scrubbed.

**A run that says nothing now reports its mechanics.** Four runs failed after
minutes of work with an empty result and no reason, leaving the orchestrator
unable to steer, retry sensibly or report upward. They now record what is
knowable:

```
[no output] the run ended with exit 1 after 996s and 125 step(s), having said
nothing. Last activity: bash({'command': 'ls -la /tmp/opencode/pub-cache/…'})
```

Facts, and deliberately not a story about why. Inventing intent from a failed
run is the mistake that once cooled a provider down over the word "quota".

**And a single failure no longer makes a provider suspect.** The fix that made
`auth_status` report evidence alongside a stored credential warned on
`failures >= 1`, so any ordinary stumble — a watchdog trip, a timeout, a bad
task — had the orchestrator announcing that a provider was in trouble. A field
that under-reported was replaced by one that over-reported, in the same day. It
now warns at the circuit breaker's threshold, where the evidence is a pattern
rather than an event.

## When a provider is simply broken

A live session lost its claude OAuth token to a server-side revocation. Thirteen
agents then failed one after another, each producing exactly this and nothing
else, until the user noticed:

```
Failed to authenticate. API Error: 401 OAuth access token has been revoked.
```

Two things let that run on. `claude auth status --json` reports `loggedIn: true`
from **local state** — it never asks the server, so `doctor` cheerfully said the
provider was authenticated throughout. And the failure carried no structured
signal to key on: the CLI's own result event said `status: success` while the
only output was the error, and the exit code was the sole honest bit in it.

The obvious fix is to match that text. This project has already been burned by
exactly that: a classifier reading agent output cooled a provider down for
fifteen minutes because an advisor used the word "quota" in a sentence, and
classifiers have read only exit status and stderr ever since. Narrowing the rule
to failed runs is not enough either — an agent debugging a test whose output
contains an auth error will quote it, and then fail for some other reason.

So nothing reads the text. **A provider whose last three runs all failed is
broken whatever the reason**, and that is knowable from exit statuses alone. The
breaker trips, sets a cooldown, and `choose_provider` routes around it or defers
— the machinery that already exists for a constrained provider. `doctor` marks
it:

```
  !! claude    stopped after 3 consecutive failures: failed: API Error: 401 …
```

A single success resets the count, because two failures and a success is a bad
afternoon rather than a broken provider. `limits.provider_failure_threshold`
and `limits.provider_down_cooldown_seconds` tune it.

The same session also produced `claude --model opencode-go/kimi-k2.7-code` and
`claude --model deep` — model overrides from another provider's namespace,
rejected by the CLI after a spawn had already been paid for. A `model` override
is now checked against what that provider actually serves before anything is
started, unless `models.yaml` is empty, where refusing would be worse than the
mistake it prevents.

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

### The reserve, and what it is for

Before every spawn, `choose_provider` compares the agent's provider against
`budget.reserve_headroom` (15%) and diverts to the `fallback_chain` if it is
below. That reserve exists for one narrow reason — an orchestrator with nothing
left cannot read the results of the agents it started, and a stalled
orchestrator stops the tree rather than one agent.

Applied to *every* provider, it did something else entirely. opencode reports
the worst of its three windows, so at **86% of a weekly window** — with the
five-hour window it actually runs against sitting empty — headroom read 0.14,
fell under the reserve, and every implementer silently ran on the fallback
model for the rest of the week. Worse, it diverted work *away* from the
provider whose quota we can measure and *toward* one that reports nothing,
because an unmeasurable provider is `usable` by definition.

So it is two switches:

| | default | |
|---|---|---|
| `budget.reserve` | `false` | apply the reserve to every provider |
| `budget.reserve_orchestrator` | `true` | apply it to the provider the orchestrator runs on — including when that provider is somebody's *fallback*, or diverted work would land on exactly the slice being kept |

Off, a worker uses what you are paying for until it genuinely runs out.
`known: false` is still never treated as empty: the reserve cannot be applied
to a number nobody has.

### Failing over past a provider the agent cannot use

The chain is a list of providers, but an agent can only move to one it names a
model for: a model id belongs to its provider's namespace, so `agy --model
opencode-go/glm-5.3-flash` is not a fallback, it is a failure with extra steps.

That check used to happen *after* the choice. The chooser returned the first
usable entry in the chain, the caller looked for a model for it, found none —
and **reverted to the provider it had just ruled out**. Measured, in a real
session: claude's token was revoked, the breaker cooled it down, the chain was
`[opencode, agy]`, and `flutter-tester` named a model for agy only. Every spawn
picked opencode, failed the model check, and ran on cooling-down claude anyway.
Five runs into an authentication wall with a working fallback one place further
down the list.

So the chooser is now told which providers the agent can use and offers no
other. When none of them can take the work, the answer is to **defer** — the
task waits and the tree pauses — rather than to run into the wall we just
identified. The reason names what to add: *"no model named for opencode, agy —
add one under `models:` to allow failover there."*

The breaker had a matching fault. It latched: once `tripped` was set it never
tripped again, so when its cooldown lapsed every subsequent failure was free and
a dead provider was retried all evening. Past the threshold, what suppresses a
trip is now an **active cooldown** — one trial at a time, and a failure in that
trial cools it down again.

**And the routing now says so.** The decision computed a reason and threw it
away, so an implementer on the wrong model could only be explained by reading
`choose_provider`. The node records `routed_from` and `routed_why`, a `routed`
event is emitted, the monitor's agent card shows *"↳ meant for opencode — …"*,
and a provider sitting below the reserve raises a warning that says work is
being sent elsewhere.

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

274 tests covering the parts live runs do not reliably exercise: doom-loop
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
