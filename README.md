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
    ├─ implementer  [running]  8,353tok  agents/implementer/b7b751
    └─ reviewer     [stuck]    doom_loop: edit called 3x with identical arguments
```

## Setup

```bash
uv sync
uv run multiagents init            # in the project you want agents to work on
uv run multiagents doctor          # checks CLIs, models, quota, git
```

`init` copies the global defaults into `.multiagents/config/` for editing,
generates `models.yaml` from the installed CLIs, and writes an MCP registration.
Add the orchestrator alias it prints:

```bash
alias mao='claude --model sonnet \
  --mcp-config ~/.config/multiagents/mcp.json \
  --append-system-prompt-file ~/.config/multiagents/orchestrator.md'
```

`orchestrator.md` carries the session protocol: check the catalog at start,
consult the critic before consequential decisions, own every branch. Run
`multiagents mcp-config` to print the exact line.

The project needs to be a git repository with at least one commit — agents work
on branches, and a worktree cannot be branched from nothing.

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
multiagents watch     # tail every state transition, live
multiagents tree      # snapshot
```

## The critic

One agent in the roster is not a task runner. `critic` is a standing advisor,
talked to with `consult()` — which blocks for a reply and **keeps its context
between calls**, so the orchestrator holds an actual conversation rather than
firing off amnesiac one-shot questions.

```
consult("critic", "The catalog says glm-5.3-flash input price rose 7.5x.
                   My researcher pins it. I intend to leave agents.yaml
                   alone — is that reasonable?")
```

It advises; it decides nothing and gates nothing. The orchestrator is
accountable for the outcome, and "the critic said so" is not a reason. Its
instructions push against both failure modes — rubber-stamping and
obstructing — and it is told to say "this doesn't need review" when consulted
about trivia.

It runs on a deliberately non-Claude model: feedback from the same family as
the orchestrator tends to agree with it.

Conversational agents sit in an `idle` state between turns — not active (so
they do not count against the concurrency limit), not terminal (so their
session stays resumable and their worktree survives).

## Model catalog drift

`agents.yaml` pins specific model ids, and the ground underneath them moves. A
model can be withdrawn, repriced, or lose `tool_call` — the last of which makes
it unusable as an agent and fails runs in a confusing way.

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

It runs automatically on `init` and `resume`, and the orchestrator is
instructed to call `check_model_catalog` at the start of each session. Cosmetic
churn (descriptions, release notes) is ignored; only `cost`, `limit`,
`tool_call`, `reasoning`, `structured_output` and `modalities` are watched.

Nothing edits `agents.yaml` automatically. The tool reports, the orchestrator
consults the critic about what it intends to do, and then the orchestrator
decides.

## Configuration

Three layers, each overriding the last: shipped defaults → `~/.config/multiagents/`
→ `<project>/.multiagents/config/`. Maps deep-merge, so a project can retune one
agent's model without restating the roster; lists and scalars replace.

| file | what |
|---|---|
| `project.yaml` | executor, git policy, security, limits, budget |
| `providers.yaml` | how to drive each CLI — **the extension point** |
| `agents.yaml` | the roster: name → provider, model, instructions, permissions |
| `agents/*.md` | per-agent instructions, prepended to every prompt |
| `models.yaml` | **generated** — `multiagents refresh-models` |

`models_include` in `providers.yaml` decides which model namespaces get
recorded. It ships restricted to `opencode/*` (free zen tier) and
`opencode-go/*` (the subscription); `deepinfra/*` is excluded deliberately,
because those bill against a separate API key rather than the subscription and
listing them would invite agents onto an account you did not intend to spend
from. Add the pattern back if you want them.

### Adding a CLI

Add a block to `providers.yaml`. No Python, provided the CLI streams
line-delimited JSON. Rules are ordered and first-match-wins; unmatched lines
become `raw` events rather than being dropped, so:

```bash
multiagents probe mycli --model some-model
```

tells you exactly which lines still need a rule.

## Security

- **No secrets in the environment.** Children start from a clean base and get
  only what `env_passthrough` names. `SSH_AUTH_SOCK` is blocked, which matters:
  it is the one credential that is not a readable file, so withholding it
  genuinely prevents an agent authenticating or pushing as you.
- **Per-agent `HOME`** with only that provider's state linked in, so an opencode
  agent cannot read Claude's or agy's stored tokens.
- **Redaction is structural** — every byte written to disk or returned through a
  tool passes through `scrub()`, which masks secret-shaped strings, secret-named
  keys, and any literal registered as sensitive. It lives in the writer, so no
  call site can forget it.
- **Nothing is published.** With `git.remote` empty, agent work never leaves the
  machine. Pushing is always an explicit call, never a side effect of finishing.

### What is *not* protected

`writes: false` and `permission: readonly` state intent; they are **not** a
permission boundary. Neither CLI can be made genuinely read-only from the
command line — with no permission flag `agy` auto-denies every tool and returns
an empty response, so a "read-only" agent configured that way cannot even read a
file. Read-only is enforced by git isolation instead: the agent works in its own
worktree and its branch is dropped if it turns out to be empty.

Likewise, the local executor gives you git isolation and credential separation,
but **not process isolation**. An agent running with skip-permissions can reach
anything your user account can. That is what the docker executor is for.

## The docker executor

`executor.kind: docker` runs agents inside one long-lived container per project.
Build it once, then switch:

```bash
multiagents docker build     # workspace + egress proxy images
multiagents docker up
multiagents docker status    # image, container, network, every mount
multiagents docker check     # proves the egress boundary
multiagents docker shell     # get in and look around
```

The local executor gives git isolation and credential separation but **not
process isolation** — an agent with skip-permissions can reach anything your
user account can. The container closes that, and adds `--cpus`/`--memory`/
`--pids-limit`, which are the only hard cap on runaway recursion.

**Verified, not assumed.** An agent running inside reports the container
hostname, uid 1000, writes files owned `1000:1000` on the host, and commits on
its own branch. `~/.claude.json` and `~/.ssh` are not visible in the container
at all — better isolation than local mode, where they are merely not linked.

### Egress

Agents sit on an `--internal` Docker network with **no route off the host** and
reach the world only through an allowlisting proxy that is also attached to the
bridge. This is a real boundary, not an environment-variable suggestion:

```
$ docker exec -e HTTPS_PROXY= <container> curl https://example.com/
curl: (6) Could not resolve host: example.com
$ docker exec <container> ip route show default
(nothing)
```

Unsetting the proxy variables does not help; there is nowhere to go. `docker
check` proves both directions — an allowlisted host reaches, a denied one does
not. Filter patterns are anchored, so `example.com` permits `api.example.com`
but not `evil-example.com`.

The socket is never mounted. Setting `mount_docker_socket: true` is *refused* by
preflight rather than honoured: with rootful Docker and a user in the `docker`
group, that is host root.

### What the allowlist must include

An isolated agent that cannot install a dependency is not safer, just useless.
The shipped allowlist covers three groups, and the trade differs for each:

| group | why | if you drop it |
|---|---|---|
| model endpoints | no agent runs without them | nothing works |
| package registries and docs | `pip install`, `npm install`, reading docs | agents thrash |
| source hosting | cloning, reading issues | no `git clone` |

Source hosting is the only group that could carry data *out*. Agents hold no git
credentials to push with — `GITHUB_TOKEN` and `GH_TOKEN` are in `env_block` and
`SSH_AUTH_SOCK` is never forwarded — but remove the group if that is not a trade
you want.

Verified inside the container: `pip install requests` succeeds,
`raw.githubusercontent.com` is reachable, and `evil-github.com` and
`github.com.attacker.net` are both blocked.

### Mixed execution

An agent can pin its own backend with `executor:` in `agents.yaml`, overriding
the project default:

```yaml
  reviewer:
    provider: agy
    executor: local
```

This exists because **agy does not currently work under docker**. Its on-disk
OAuth token (`~/.gemini/oauth_creds.json`) is expired, and whatever it actually
authenticates with does not survive into a container — it falls back to an
interactive login and times out. Tested with the host `HOME`, matching
`machine-id` and hostname, and the D-Bus session bus all mounted; none helped.
opencode containerises fine, so the shipped roster runs opencode agents in the
container and agy agents on the host. `multiagents doctor` marks pinned agents
with `*`.

If you get agy authenticating inside a container, remove the pin — nothing else
needs to change.

## Budget## Budget

Three providers sit at three tiers of knowability, and the adapter reports that
honestly rather than inventing a number:

- **claude** — real subscription state from `~/.claude.json`: percent used per
  bucket, reset times, overage credits. It is a cache, so staleness is reported.
- **agy** — has a full quota subsystem internally but exposes none of it. Spend
  only; exhaustion is detected reactively from a failed run.
- **opencode** — a Go subscription is *detectable* (`auth.json`), but the CLI
  exposes no headroom surface even with one active: no subcommand, no new
  tables. It does report **real dollar cost per step** in its event stream,
  which is accumulated per agent and matches the figures on the web console's
  usage page — so cost is known exactly, capacity is not. What was checked and
  ruled out for headroom is recorded in `budget.probe_opencode`.

The web console's usage page (`opencode.ai/workspace/<id>/usage`) is behind
OpenAuth and cannot be fetched with the stored API key, which is an inference
credential for `zen/go/v1`. It is not needed: the same numbers arrive in the
stream, and its Session column is the tail of the `sessionID` already recorded
on each tree node, so rows can be matched back to individual agents by hand.

Providers declare how their usage accumulates, because getting it wrong
corrupts every number above it:

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

Covers the parts live runs do not reliably exercise — doom-loop detection,
redaction, config merge semantics, corrupt-tree recovery — using real captured
event shapes from both CLIs.
