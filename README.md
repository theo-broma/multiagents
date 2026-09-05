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
alias mao='claude --model sonnet --mcp-config ~/.config/multiagents/mcp.json'
```

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

## Roadmap: the docker executor

`executor.kind: docker` is stubbed with its constraints documented in
`executor/docker.py`. It is one config line away once implemented, because
everything above the executor is identical either way. The constraints, all
established by inspecting this machine:

- **Never mount the docker socket** — Docker here is rootful and the user is in
  the `docker` group, so socket access is host root.
- **Mount paths must match the host exactly** — a linked worktree's `.git` file
  stores an absolute path to the main repository, and vice versa.
- **A container protects the host from the agent, not your tokens from the
  agent.** The control that helps is egress allowlisting through a CONNECT
  proxy, so a token an agent can read is one it cannot post anywhere.
- **Mount the CLIs, don't bake them in** — they self-update on the host.

## Budget

Three providers sit at three tiers of knowability, and the adapter reports that
honestly rather than inventing a number:

- **claude** — real subscription state from `~/.claude.json`: percent used per
  bucket, reset times, overage credits. It is a cache, so staleness is reported.
- **agy** — has a full quota subsystem internally but exposes none of it. Spend
  only; exhaustion is detected reactively from a failed run.
- **opencode** — a Go subscription is *detectable* (`auth.json`), but the CLI
  exposes no quota surface even with one active: no subcommand, no new tables,
  and `opencode stats` reports `$0.00` because subscription models are not
  billed per token. Spend-only. What was checked and ruled out is recorded in
  `budget.probe_opencode`.

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
