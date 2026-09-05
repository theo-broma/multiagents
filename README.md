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
uv sync
multiagents init         # create the project, copy the global config
multiagents init-agent   # shape it with the initializer — resumable, takes as long as it takes
multiagents build        # container environment, then authenticate every provider
multiagents run          # launch the orchestrator; first run and resume are the same command
```

`run` continues the last session where there is one and starts fresh where there
is not, so it is the same command either way. `--fresh` forces a new session,
and `resume` is an alias for `run`. The same applies to `init-agent`.

`init` copies the global defaults into `.multiagents/config/` for editing,
generates `models.yaml` from the installed CLIs, records the first model-catalog
snapshot, and scaffolds `context/`.

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

Verified: an opencode orchestrator sees all 21 multiagents tools.

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

Task agents are started with `start_agent` and collected when they finish.
`researcher` and `reviewer` read; `implementer` and `tester` write on their own
branches.

Two agents are not task runners at all. `critic` and `advisor` are standing
advisors, reached with `consult()` — which blocks for a reply and **keeps its
context between calls**, so the orchestrator holds an actual conversation rather
than firing off amnesiac one-shot questions:

```
consult("critic", "The catalog says glm-5.3-flash input price rose 7.5x.
                   My researcher pins it. I intend to leave agents.yaml
                   alone — is that reasonable?")
```

They advise; they decide nothing and gate nothing. The orchestrator is
accountable for the outcome, and "the critic said so" is not a reason. Their
instructions push against both failure modes — rubber-stamping and
obstructing — and they are told to say "this doesn't need review" when consulted
about trivia. Both run on deliberately non-Claude models: feedback from the same
family as the orchestrator tends to agree with it.

Conversational agents sit in an `idle` state between turns — not active (so they
do not count against the concurrency limit), not terminal (so their session
stays resumable and their worktree survives).

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

## Where agents run

`executor.kind` selects the backend, and an agent may pin its own with
`executor:` in `agents.yaml`. `multiagents doctor` marks pinned agents with `*`.

### local

Git isolation via worktrees, credential separation via a per-agent `HOME` with
only that provider's state linked in, and a deny-by-default environment. What it
does **not** give you is process isolation: an agent running with
skip-permissions can reach anything your user account can.

### docker

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

## Security

- **No secrets in the environment.** Children start from a clean base and get
  only what `env_passthrough` names — empty by default, since every CLI
  authenticates with credentials it already stores. `SSH_AUTH_SOCK` is blocked,
  which matters: it is the one credential that is not a readable file, so
  withholding it genuinely prevents an agent authenticating or pushing as you.
- **Per-agent `HOME`** with only that provider's state linked in, so an opencode
  agent cannot read Claude's or agy's stored tokens.
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

## Budget

Three providers sit at three tiers of knowability, and the adapter reports that
honestly rather than inventing a number:

- **claude** — real subscription state from `~/.claude.json`: percent used per
  bucket, reset times, overage credits. It is a cache, so staleness is reported.
- **agy** — has a full quota subsystem internally but exposes none of it. Spend
  only; exhaustion is detected reactively from a failed run.
Budget is read through each provider's own `budget` action, so a newly added
provider gets an entry with no Python change.

- **opencode** — a Go subscription is *detectable* (`auth.json`), but the CLI
  exposes no headroom surface even with one active. It does report **real dollar
  cost per step** in its event stream, accumulated per agent and matching the
  web console's usage page — so cost is known exactly, capacity is not. What was
  checked and ruled out for headroom is recorded in `budget.probe_opencode`.

The web console's usage page (`opencode.ai/workspace/<id>/usage`) is behind
OpenAuth and cannot be fetched with the stored API key, which is an inference
credential for `zen/go/v1`. It is not needed: the same numbers arrive in the
stream, and its Session column is the tail of the `sessionID` already recorded
on each tree node, so rows match back to individual agents.

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
uv run --with pytest pytest tests/ -q
```

96 tests covering the parts live runs do not reliably exercise: doom-loop
detection, credential redaction, config merge semantics, corrupt-tree recovery,
catalog drift assessment, the docker executor's mount and network construction,
the provider script contract, the orchestrator-not-spawnable guards, the
ownership gate on mutating tools, and `awaiting_user` transitions and their
non-interaction with the watchdogs.

Provider event fixtures are real shapes captured from the CLIs, not invented —
`tests/fixtures/claude-stream.jsonl` is an actual run that used a tool, and a
golden test asserts every line of it classifies with nothing falling through to
`raw`.
