# multiagents

An MCP server that lets Claude Code delegate work to **other agent CLIs** —
[opencode](https://opencode.ai) and Antigravity (`agy`) today, anything that
streams JSON tomorrow — as supervised, git-isolated subagents.

The point is not that other models are better. It is that a subagent burns *its*
context instead of yours, works on a branch that cannot touch your tree, and can
be watched, steered and killed while it runs.

```
you/master  ← explicit merge_agent() gate
  orchestrator (claude sonnet)
    ├─ implementer  [running]        38,381tok  $0.0104  agents/implementer/8a5e14
    ├─ reviewer     [stuck]          doom_loop: edit called 3x with identical arguments
    └─ critic       [idle · 2 turns] 10,419tok  $0.0006
```

## Setup

```bash
uv sync
uv run multiagents init      # in the project you want agents to work on
uv run multiagents doctor    # CLIs, agents, auth, budget, git
```

`init` copies the global defaults into `.multiagents/config/` for editing,
generates `models.yaml` from the installed CLIs, records a model-catalog
baseline, and writes an MCP registration. Add the orchestrator alias it prints:

```bash
alias mao='claude --model sonnet \
  --mcp-config ~/.config/multiagents/mcp.json \
  --append-system-prompt-file ~/.config/multiagents/orchestrator.md'
```

`orchestrator.md` carries the session protocol: check the catalog at start,
consult the critic before consequential decisions, own every branch. Run
`multiagents mcp-config` to print the exact line.

The project must be a git repository with at least one commit — agents work on
branches, and a worktree cannot be branched from nothing.

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

Two agents are not task runners at all. `critic` and `gemini` are standing
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

### Adding a provider's auth

One script per provider, implementing a two-action contract, so nothing above it
needs to know which CLI it is:

```
<provider>.sh check     non-interactive, fast
                        exit 0  = authenticated
                        exit 10 = NOT authenticated
                        exit *  = unknown
                        stdout  = one line of status

<provider>.sh login     may be interactive and take the terminal
                        print what the user must do BEFORE doing it
```

Scripts live in `config/auth/`, resolved project-first then global then shipped,
and receive their situation through the environment (`MULTIAGENTS_EXECUTOR`,
`MULTIAGENTS_CONTAINER`, `MULTIAGENTS_PRIVATE_BACKING`, …). See
`auth/README.md`. `check` should not cost money — prefer inspecting stored
credentials over probing the API.

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

It runs automatically on `init` and `resume`, and the orchestrator is instructed
to call `check_model_catalog` at the start of each session. Cosmetic churn
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
| `auth/*.sh` | per-provider authentication scripts |
| `models.yaml` | **generated** — `multiagents refresh-models` |

`models_include` in `providers.yaml` decides which model namespaces get
recorded. It ships restricted to `opencode/*` (free zen tier) and
`opencode-go/*` (the subscription); `deepinfra/*` is excluded deliberately,
because those bill against a separate API key rather than the subscription and
listing them would invite agents onto an account you did not intend to spend
from.

### Adding a CLI

Add a block to `providers.yaml`. No Python, provided the CLI streams
line-delimited JSON. Rules are ordered and first-match-wins; unmatched lines
become `raw` events rather than being dropped, so:

```bash
multiagents probe mycli --model some-model
```

tells you exactly which lines still need a rule. Then add an `auth/mycli.sh`
implementing the contract above, and it is fully integrated.

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

`known: false` means spend is tracked but capacity is not. Never read it as
"plenty left". The purpose is *routing*: when your own five-hour bucket is
tight, delegating to an unrationed provider is the highest-value move available.

## Tests

```bash
uv run --with pytest pytest tests/ -q
```

46 tests covering the parts live runs do not reliably exercise: doom-loop
detection, credential redaction, config merge semantics, corrupt-tree recovery,
catalog drift assessment, the docker executor's mount and network construction,
and the auth contract. Provider event fixtures are real shapes captured from
opencode and agy, not invented.
