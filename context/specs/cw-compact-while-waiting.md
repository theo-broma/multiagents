# CW — compaction while agents run, and the return checklist

Status: contract, orchestrator, 2026-10-01. Source: user decision
2026-10-01 ("accepter la compaction pendant l'attente d'agent en précisant ce
qu'il y a à vérifier au retour"). Amends P0-R8c and P0-R8f
(`context/specs/phase0-context-and-team.md`). Ids `CW-R*`, never renumbered.

## Why

The driver compacts only at rest, and never while something it considers
"busy" exists. Today (advisor ag-d20e1e, turn 6):
- interactive R8f (`driver.py` ~521, `_busy`) is refused by any session-scoped
  **unseen root result** and any **deferred** entry — running agents do not
  block it;
- unattended R8c (`driver.py` ~1149) is refused by any **active** node and any
  deferred entry.
An orchestrator doing its job nearly always has an unseen result or an active
agent, so since the driver restart of 2026-09-29 no compaction fired while
the context went past 400k (researcher ag-bee342: rest gaps of 900 s to
10 000 s above 200k, no `compact_*` event, and no record of why). CW replaces
that protection with (a) the orchestrator recording in-flight state on disk
before it waits, (b) a safe stop that never interrupts a launch, and (c) a
return message saying exactly what to re-check.

Agents survive a stop of the orchestrator's CLI (SV,
`context/specs/agent-survival.md`): the server detaches them and the next one
attempts to adopt them. The orchestrator's backgrounded MCP calls (e.g.
`wait_for_agents`) are interrupted; its other background waits/wakes may or
may not survive.

## Behaviours

**CW-R1 — in-flight work no longer blocks.** In both paths, active nodes (any
status, root or nested), unseen results and deferred tasks no longer prevent
a compaction. Everything else in R8f.2 (threshold, rest of
`compact_idle_seconds`, no pending usage-limit warning, probe, not disabled,
once per crossing) and in R8c.1 (after a clean, unlimited turn; not
unsupported) is unchanged.
Verified by: table tests on both paths with a running root agent, a stuck
one, an awaiting_user one, an unseen result and a deferred task, each
asserting compaction proceeds; existing tests asserting the old blockers are
changed deliberately by the tester, citing CW-R1.

**CW-R2 — a safe stop.** Before the driver terminates the CLI for a
compaction, the Runner owned by the orchestrator's MCP server reaches a safe
point and says so: it admits no new launch, and every transition it owns that
is in progress — a launch between `_claim` and its pid being recorded, a
steer that stopped its predecessor but has not launched the replacement, a
consult refreshing its worktree, a launch retry — has either completed or
been left in a durable state the next server resumes or reports. Only then
does the stop proceed. If the safe point is not reached within a bounded time
(a config value following R8f.9), the compaction is cancelled
(`compact_cancelled` with a reason), the CLI is not stopped, and it is
proposed again under R8f.14. The handshake mechanism is the developer's.
If the server must be force-terminated anyway (it does not exit after
acknowledging), the state it leaves is recoverable by SV adoption.
Verified by: a test where a launch is held between claim and pid record when
the compaction becomes due: the CLI is not stopped until it completes; a
test where the safe point never comes: cancelled, CLI untouched; a test that
an admission attempted after the safe point is not launched by the stopping
server.

**CW-R3 — the announcement says what is running.** The R8f.3 line keeps its
grace period and cancel wording but replaces "nothing running" with the
count, e.g. `compacting this session in 30s (<tokens> tokens, 3 agents
running) — type anything to keep it`; `compact_scheduled` gains
`active: <int>`. With none: `nothing running` as today.
Verified by: output and event assertions for 0 and for N.

**CW-R4 — the return message.** The driver snapshots the session's root
agents (ids and statuses, driver roles excluded) at the safe point, keeps it
until relaunch, and compares it with the state read at relaunch. When
anything was in flight at the stop (an active node, an unseen root result, a
deferred task or a parked question), the session is resumed **with** a
prompt — interactive: replacing R8f.4.4's "no prompt" in that case only;
unattended: prepended once to the next turn's prompt, and, if the driver
exits before that turn (idle-turn or turn limit), persisted durably and
delivered once on the next invocation. With nothing in flight, R8f.4.4 is
unchanged. It contains, in this order:
1. one line: the session was compacted by the driver at `<UTC time>`
   (`<before> → <after>` tokens, `unknown` where a figure is unavailable) —
   or, if the compaction failed or was unsupported after the stop, that it
   failed and why; and that the orchestrator's interrupted MCP waits did not
   survive and any other wait or wake it had armed must be checked;
2. the root agents not yet merged or discarded, each with status at the stop
   and status now, labelled where it changed: `finished during compaction`,
   `adopted`, `adoption failed: <reason>`, `awaiting decision`; adoption is
   asynchronous, so a row may still read `not yet adopted`;
3. unseen results (ids, including those already terminal at the stop),
   parked questions (ids), deferred tasks (count and earliest restart), open
   tickets (count);
4. the fixed checklist:
   - re-read the latest handoff/progress entry of `BRIEF.md`; durable state
     outranks the summary of your conversation;
   - check `agent_tree` for adoption still in progress or failed;
   - `collect_agent` every unseen result before deciding anything about it;
     never restart an agent that is still running — resume an interrupted one
     with `steer_agent`;
   - `list_questions` if any are parked;
   - re-arm what was lost: a `wait_for_agents` on the running agents, the
     quota wake if tasks are deferred;
   - a judgement not written down before the compaction is gone: rebuild it
     from the ledger, the specs and the branches, never from memory.
It names no provider and is bounded to 4 000 characters as a whole: every
list truncates with `+N more — see agent_tree` (or the matching tool) so the
cap always holds.
Verified by: a fake compact action that flips one running node to `done`
between stop and relaunch → listed `finished during compaction`; an
adoption failure → listed with its reason; a failed compaction → the
failure line and still the list; the size cap with 200 agents and 200
unseen ids; nothing in flight → no prompt; unattended: delivered on the next
turn, and after a driver exit on the next invocation, exactly once.

**CW-R5 — agents are preserved through the stop.** The compaction stop uses
SV's intentional-stop path: no agent is cancelled or marked `orphaned` by it;
the relaunched server attempts adoption of every detached run; an adoption
that fails (e.g. `_unadoptable`) is reported in CW-R4, never silent.
Verified by: an R8f stop with agents running → none cancelled/orphaned,
adoption attempted for each, one forced failure appears in the message.

**CW-R6 — why it did not fire, on record.** While the reading is at or above
`compact_at_tokens` and no compaction is proposed, the driver emits
`compact_blocked` with a `reason` (`usage_limit_pending`, `not_at_rest`,
`probe_failed` + `probe_exit`, `disabled`, `already_this_crossing`,
`safe_point_timeout`; the developer may add others) — once per change of
reason, never once per poll. When several hold, the first in that order is
reported. `not_at_rest` is emitted only after it has held longer than ten
times `compact_idle_seconds` (a working session is not news). A failed probe
is reported once per rest episode. Both paths emit it, with `path:
interactive|unattended`.
Verified by: a poll-sequence test asserting one event per reason change,
none on repeated polls, and the precedence.

**CW-R7 — the orchestrator brief.** `_orchestrator.md`'s `## Your own context
window` says: the driver may now compact while agents run; before a long
wait, write which agents you are waiting on and why into `BRIEF.md`; on
return you receive the CW-R4 message and follow its checklist first. Any
sentence saying the driver compacts only with nothing running is removed.
Verified by: a test on the rendered brief.

**CW-R8 — the compaction is told what to keep.** Both paths pass focus
instructions to the provider's `compact` action, which forwards them to the
provider's own compaction when it supports it (claude: `/compact <focus>`, as
one quoted argument) and ignores them otherwise. The text comes from
`limits.compact_focus` (a string; empty means none) with a shipped default in
`defaults/project.yaml`, in substance: keep the ids of agents in flight and
what each is for, decisions made but not yet written down, the next steps,
and where durable state lives (`BRIEF.md` handoff, specs, ledger); drop raw
tool output, test logs and diffs. Bounded (≤ 1 000 characters; longer is
truncated, never an error). The success check of R8c.3/R8f.15 is unchanged.
It does not replace CW-R4: what happens during the compaction cannot be in
its summary. Decided after advisor ag-d20e1e t7: keystroke injection into the
live CLI (tmux/PTY) and installing a PreCompact hook are **out of scope** —
the CLI inherits the terminal directly (`driver.py` ~196-249) and injection
cannot know the CLI is at its prompt.
Verified by: script tests — empty focus → today's exact invocation; non-empty
focus with spaces, quotes, `$`, backticks and a newline reaches the CLI as
one argument unaltered; over-long focus is truncated; the transcript
verification still decides success. Plus one live smoke check by the
orchestrator against the installed CLI before merge.

## Unchanged

R8f.1 probe, R8f.3 grace and cancel, R8f.5 failure/once-per-crossing,
R8f.6 exec path never compacts, R8f.12/R8f.19 user exit wins, all R8f.9
config parsing.

## Decisions on the tester's questions (ag-db8d42), 2026-10-01

- **CW-R8 interface:** the focus reaches the `compact` action in the
  environment variable `MULTIAGENTS_COMPACT_FOCUS` (empty or unset = none).
- **CW-R2 bound:** `limits.compact_safe_point_seconds`, default `60`, parsed
  as R8f.9. With no launch or transition in flight the stop does not wait for
  the bound.
- **CW-R2 test seam:** the `sitecustomize` gate in `tests/support/cw_gate/`
  holding the agent wrapper's `Popen` is accepted; the implementation keeps
  that launch path or adapts the seam and says so.
- **CW-R4 `awaiting decision`:** the row label when the node is
  `awaiting_user` at relaunch. Deferred earliest restart: ISO-8601 UTC.
- **CW-R5, amended:** the CW-R4 message reports what is known at relaunch;
  adoption being asynchronous, a row may say `not yet adopted`. A later
  adoption failure surfaces through `agent_tree`, the `adopt_failed` event and
  the node's `failed` status — never silently. The checklist's `agent_tree`
  step is the orchestrator's route to it.
- **CW-R6:** `compact_at_tokens: 0` emits no `compact_blocked` (the feature
  is off, not blocked). `probe_failed` is emitted once per rest episode; every
  other reason once per change.

## CW-R2a — the safe point cannot be forged (decided 2026-10-01, after review ag-db4940 #4 and advisor ag-d20e1e t8)

- **Where:** the handshake (registration, request, acknowledgement) lives in a
  host-only directory under the host state root, keyed by project (e.g.
  `<state>/safepoints/<project-key>/`), never in the project's `launch/` and
  never in anything mounted into a container. Driver and server resolve it
  from one explicit, trusted source; the test harness is adapted, never a
  second root silently chosen. No discovery through container-writable state.
- **Exposure check:** if the effective container mounts (state-root override,
  `extra_mounts`) expose that directory or an ancestor, CW compaction is
  refused for that run (`compact_blocked`, reason `safepoint_exposed`). Paths
  are resolved, symlinks not followed.
- **Binding:** an acknowledgement counts only if it names the request's nonce,
  the session, and the server's pid **and start time**.
- **Fail closed:** any error registering, writing the request, enumerating
  servers or reading acknowledgements cancels the compaction; an empty
  enumeration is "no server", never "all acknowledged", unless the driver can
  positively establish no root server exists for the session.
- **Late servers:** a server registers and checks for an active barrier
  **before** serving tools or starting launch-capable work, with admission
  closed until that check passes; barrier publication and registration are
  ordered so a server arriving after enumeration is either seen by the driver
  or sees the barrier.
- **No reopening while the driver may still stop:** admission stays closed
  through a committed shutdown; a server-side expiry never reopens admission
  while a matching acknowledgement could still be accepted (expiry removes or
  invalidates the acknowledgement first, and the driver checks its own
  deadline before accepting).
- **Scope:** this protects against containerised agents. Local agents running
  as the host user are outside that guarantee, as they are for every other
  host-authority file.

## Second attack (ag-6ebca1, 2026-10-01) — decisions

- **Exposure is judged on resolved paths.** Mount sources, the handshake
  directory and every ancestor are compared after resolving symlinks; a
  symlink at `<state>/safepoints` or `<slug>` (or anywhere on the path) makes
  the handshake refuse (`safepoint_exposed` or `safe_point_error`) rather than
  write through it. Tests: tests/test_cw_attack2.py.
- **Accepted limitation:** with `/proc` mounted `hidepid=1` (entries listed
  but unreadable), an unrelated unreadable process makes enumeration unknown
  and interactive compaction never proceeds. This follows "unknown blocks";
  documented, not fixed. (`hidepid=2` hides such processes entirely and is
  unaffected.)
- **Mount sources under container-writable places (review ag-3e9254 r5).**
  A running bind keeps its original target even if the configured source
  alias is later renamed. Decided, conservatively: if any configured mount
  source has, lexically or resolved, an ancestor that a container can write
  (the project root, worktrees, agent homes, or any other mount that is not
  read-only), and that source is not provably unrelated to the handshake by
  being both lexically and resolved outside the state root's `safepoints`
  tree *and* not a symlink at any component, the handshake is treated as
  exposed (`safepoint_exposed`). Exposure is re-evaluated on every proposal,
  never cached across proposals.
