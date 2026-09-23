# Agents with spawn rights can reach the server (tooling defect 6)

**Status:** contract, 2026-09-23.

**Ids.** Prefix `SM-`. Never renumber; retire with `SM-Rn — withdrawn: <why>`.

**Invariant carried over from phase 0:** providers are plugins. No provider
name may appear in `src/multiagents/*.py` or `executor/*.py` as a result of
this work. How each provider is given an MCP server lives in
`providers.yaml` and the provider scripts.

## The defect

The orchestrator protocol says `implementer` and `implementer-deep` consult
`dev-advisor` directly, and `agents.yaml` gives them spawn rights. Both run on
claude, and they have no `consult` tool (ag-829577, ag-d67496; the launch is
confirmed in `runs/ag-cbf6c0/command.json`). The cause, found by ag-4faa57 and
checked by the advisor (ag-25c350, turn 10):

- claude is spawned with `--strict-mcp-config` and no `--mcp-config`
  (`providers.yaml` ~255), so it loads no MCP server at all;
- opencode's `prepare` deliberately gives the server only to the launched
  orchestrator ("no subagent inherits the server");
- only agy subagents see it, through agy's global MCP profile.

Nothing in the history or docs says this was intended. It is a side effect of
keeping the user's own CLI configuration untouched.

Facts the implementation must respect (advisor, turn 10):
- The server identifies its caller by `MULTIAGENTS_AGENT_ID`
  (`runner.self_id`, `server._may_act_on`), which the executor already sets
  for the agent's process.
- Under the docker executor the agent's CLI, and therefore any MCP server it
  starts, runs **inside the container**. The project root is mounted at the
  same path. `uv` is **not** in the container.

## Behaviours

**SM-R1 — an agent with spawn rights has the server.** A spawned agent whose
definition grants spawn rights (`can_spawn: true`) is launched with the
multiagents MCP server available, on every shipped provider, under both the
local and the docker executor. It can call `consult`, `start_agent` and the
other tools its rights allow.
*Verified by:* for each shipped provider, the argv/env/config produced for a
spawn of an agent with `can_spawn: true` names the multiagents server (unit
test on the composed launch). **Plus a live check, which is required:** an
`implementer` run on claude under the docker executor calls
`consult("dev-advisor", …)` and gets a reply. The orchestrator runs it after
the merge. The developer makes it possible and says how to run it.

**SM-R2 — an agent without spawn rights still has none.** A spawned agent
without spawn rights is launched exactly as today: for claude,
`--strict-mcp-config` and no server.
*Verified by:* the same composed-launch test for `can_spawn: false`.

**SM-R3 — the caller is the agent, and the gates hold.** Calls from a
child's server are attributed to that child (`MULTIAGENTS_AGENT_ID`). The
existing server-side gates (ownership, `max_children`, `max_depth`,
concurrency) apply to it unchanged. A child cannot act on nodes it does not
own.
*Verified by:* a server-level test with `MULTIAGENTS_AGENT_ID` set to a
child: `consult` to its conversational child is allowed; merging or stopping
a node it does not own is refused.

**SM-R4 — the user's configuration is never touched.** No file in the user's
home configuration of any CLI is created or modified by a subagent spawn. Any
MCP configuration a spawn needs is written under the run's own state
directory, or passed on the command line.
*Verified by:* the composed-launch test asserts that every config path it
references is under the run's directory.

**SM-R5 — a server that cannot start does not break the agent.** If the MCP
server fails to start inside the agent's environment, the agent still runs
its task without it. The run records an event saying the server was
unavailable, so the orchestrator sees why a `consult` did not happen.
*Verified by:* a launch where the server command is deliberately
unresolvable: the run completes and the event is present. If the provider
CLI refuses to start at all with a broken server, say so and propose an
alternative rather than inventing one.

## Out of scope, recorded

- Whether agy's global registration works inside the container, given there
  is no `uv` there. If the developer finds it does not, report it; fixing it
  the same way is welcome but not required.
- Which agents should have spawn rights. That is `agents.yaml`, and it does
  not change here.
