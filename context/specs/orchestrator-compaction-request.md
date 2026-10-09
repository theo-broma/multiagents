# Compaction requested by the orchestrator (OC)

## Background

User, 2026-10-09: when the orchestrator judges that compaction is due, it must
happen automatically and with certainty. Today only the driver's own
thresholds trigger it: `compact_at_tokens`, then `compact_idle_seconds` of
unchanged transcript.

Researcher ag-224c05 mapped the driver:
- Active agents are not a gate; the driver compacts while agents run (CW-R1,
  driver.py:524-546, 830-839).
- The orchestrator has no channel to request a compaction (driver.py:1452-1473).
- The mechanism already exists and needs no change: a safe-point barrier,
  SIGTERM of the CLI, external `/compact`, then a relaunch with resume and the
  return prompt (driver.py:706-762, compact_return.py:28-36).

## Behaviours

**OC-R1.** A root-only MCP tool `request_compaction(reason: str = "")` records
a compaction request for the calling orchestrator session. It returns
`{request_id, status: "pending"}`.
- A call from a non-root agent is refused with `not_root`.
- While a request is pending, a second call returns the same `request_id`
  instead of creating a new request.

Verified by: a call from the root, a call from a child agent, two calls in a
row.

**OC-R2.** At the next closed turn of the session, the driver honours a pending
request:
- It skips the `compact_at_tokens` threshold, the `compact_idle_seconds` idle
  wait and the "already compacted in this crossing" rule.
- It keeps the safe-point barrier and the compaction itself.
- The session resumes with the existing return prompt, which also names the
  `reason`.

A closed turn means the CLI is back at its prompt after the turn in which the
request was made. Running agents do not delay it.

Verified by: a request with tokens below the threshold and an agent running.
The session stops, compacts and resumes, and the agent is still running and
adopted after the resume.

**OC-R3.** The request is consumed exactly once. On success its status becomes
`completed`. A driver restart before it is honoured keeps it `pending` and
honours it afterwards.

Verified by: a restart of the driver between the request and the turn
boundary.

**OC-R4.** A message sent by the user before the stop delays the compaction to
the next closed turn and does not cancel it. This differs from the automatic
compaction, which a sent message cancels.

Text the user has typed but not sent is invisible to the driver. The tool's
documentation says so.

Verified by: a request, then a sent message. Compaction happens after the
reply to that message.

**OC-R5.** If the compaction fails, or the safe point is not reached within
`compact_safe_point_seconds`:
- the request becomes `failed` with its reason;
- the session continues without compaction;
- a new call to `request_compaction` creates a new request.

Verified by: a safe point that times out.

**OC-R6.** The tool and the driver's decision are generic and contain no
provider-specific code. The provider plugin performs the compaction itself
(the provider's script, e.g. the existing `compact` entry point in
`defaults/providers/claude.sh`), and the plugin declares that it can.

If the session's provider does not declare this capability, or the session
runs without a driver (`--no-supervise`):
- the tool returns `{status: "unsupported", reason}`;
- it records nothing;
- nothing is stopped.

Verified by:
- a fixture provider that declares the capability: the request is honoured
  through its entry point;
- a fixture provider that does not declare it: `unsupported` is returned and
  the session is untouched;
- a call with no driver;
- a scan of the generic code: it never names a provider.

**OC-R7.** Adding compaction to another provider is a plugin change only:
declare the capability and provide the entry point. No change to the core is
needed. The provider documentation describes the contract for that entry point:
- its inputs (the session id);
- its success and failure exit codes;
- its timeout.

Verified by: a second fixture provider gains compaction through its plugin
alone.

**OC-R8.** The plugin declares a compaction **mode** along with the capability.
The core handles at least these two:
- `restart`, today's claude path. The core reaches the safe point and stops
  the CLI. The plugin compacts the session from outside, and the core
  relaunches it with resume and the return prompt.
- `in_session`, for CLIs that expose compaction to a running session (e.g.
  opencode v2). The core reaches the safe point. The plugin then compacts the
  live session through the CLI's own command, and the CLI is neither stopped
  nor relaunched. After success, the return prompt is delivered to the session
  as its next input. Background processes owned by the CLI survive.

The core chooses the sequence from the declared mode alone, never from the
provider's name. OC-R2 to R5 apply to both modes.

An unknown or missing mode on a provider that declares the capability counts
as `unsupported` (OC-R6). A later mode is added without changing R1 to R7.

Verified by:
- one fixture provider per mode, showing the CLI process is replaced in
  `restart` and is the same process in `in_session`;
- a fixture with an unknown mode returns `unsupported`.

## Out of scope

- Changing the automatic thresholds.
- Preserving background shells in `restart` mode. They die with the CLI, as
  today, and the return prompt says to re-arm waits.
- Writing the opencode v2 plugin itself. OC delivers the `in_session` mode
  and its test fixture; plugging in a real CLI is a separate item.
