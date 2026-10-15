# User decisions queue (UD)

Requested by the user on 2026-10-15, in an init-agent session. The advisor
reviewed it (ag-aa1387, turn 26).

## Problem

The orchestrator often stops to wait for the user: "spec draft AP11-AP13
uncommitted, awaiting user yes", "Run D: merge or not?". These waits exist
only in chat and in BRIEF prose. No tool lists them. Nothing says precisely
what is awaited, what each answer would trigger, or whether the wait is
still pending. A GUI cannot show them.

The existing question store (`tree.py`, "questions" section, around line
1243) covers only an agent's `NEED_DECISION`. Its answer is free text, and
its status is `open` or `answered`.

## Decisions (user, 2026-10-15)

- **UD-D1. One queue.** The orchestrator's and the initializer's own waits,
  and the agents' `NEED_DECISION` questions, are records in one queue. The
  CLI, the MCP server, the monitor and a future GUI read the same records.
  There is no second authoritative store.
- **UD-D2. An answer to an orchestrator wait wakes the orchestrator.** It
  goes through the same Stop-hook path as a bg job end (DJ-R5). The
  orchestrator also reads the queue at every wake, because the hook does
  not fire after an interrupt or an API error.
- **UD-D3. Answers.** An answer is a choice of one or more proposed options,
  with an optional comment. It may instead be free text ("Other"). A
  recommendation or a default is never an answer until someone submits it.
- **UD-D4. `blocks` is descriptive.** A record says what is held while it
  waits. The scheduler places no hold because of it. The asker pauses its
  own work, as today.

## Terms

- **Decision**: one record in the queue. Agent questions are decisions too;
  the store keeps their `q-` ids.
- **Asker**: the role and agent that created the decision, taken from the
  calling channel, never from an argument.
- **Audience**: who may answer. `orchestrator` means the orchestrator may
  settle it (agent questions within its remit). `user` means only the user
  may.
- **Answered**: an answer is recorded. **Handled**: the asker has received
  it and acted on it, or said why not. These are separate facts (UD-R6).

## Requirements

**UD-R1. Record schema, versioned.** The GUI depends on it, so a JSON
Schema file in the repository documents it, with a `schema_version`. Each
decision has:
- `id`, `schema_version`, `revision` (incremented on every change);
- `asked_by` {role, agent}, `audience`, `asked_at`;
- `topic` (short), `title` (one line), `context` (what is awaited, with
  enough detail to answer without reading the chat);
- `implications`: what is held meanwhile (`blocks`, descriptive, UD-D4),
  and what happens with no answer;
- `mode`: `single`, `multi` or `free_text` (legacy agent questions);
- `options[]`: {`id`, `label`, `description`, `implies`: what choosing it
  triggers}; `min` and `max` for `multi`;
- `recommended[]`: option ids, possibly several for `multi`;
- `allow_free_text` (default true);
- `refs[]`: what the decision is about (UD-R5);
- `status` and its history (UD-R2);
- `answer` (UD-R3) and `handled` (UD-R6).

**UD-R2. Statuses and transitions.** `open` can go to `answered`,
`withdrawn` (the asker no longer needs it, with a reason) or `superseded`
(with the id of the decision that replaces it). An answered decision can
only be marked handled; it is never reopened. Every transition is one
locked transaction that checks the expected `revision`. Each emits an event
to `events.jsonl`. An answer to a `withdrawn` or `superseded` decision is
refused with a message. Today `answer_question()` only special-cases
`answered` (`tree.py`, around line 1288).

**UD-R3. Answer validation.** An answer holds `options[]` (chosen ids),
`comment`, `free_text`, `answered_by`, `answered_at` and the `revision` it
answers.
- In `single` mode, exactly one option, or free text in place of an option
  when `allow_free_text` is true.
- In `multi` mode, between `min` and `max` options, or free text in place of
  options.
- In `free_text` mode, free text only.
- A comment may go with any choice.
- The same answer submitted twice is idempotent. A different answer to an
  answered decision is refused. A `request_id` makes retries safe.

**UD-R4. Who may answer.** `answered_by` is taken from the channel, never
from an argument.
- Over MCP, agents and the orchestrator can answer only `audience:
  orchestrator` decisions. A `user` decision answered through MCP or the
  scoped host RPC is refused.
- The user answers through the interactive CLI, the monitor (with its page
  token, C20), and later the GUI through the same API.
- Residual risk, stated: the host orchestrator can run the same CLI as the
  user, so the CLI cannot prove a human answered. The orchestrator's
  instructions forbid it, and the event log records which channel was used.
  A stronger guarantee needs a channel the orchestrator cannot reach. That
  is out of scope here and recorded as such.

**UD-R5. A "yes" binds to an exact version.** A ref is {`path`, `sha256`},
or {`commit`}. For an uncommitted draft, the queue keeps a snapshot of its
bytes when the decision is created, under `.multiagents/`.
- The answer records the digests it approved.
- Before acting, the asker checks that the file still has the approved
  digest. If the draft changed, the asker opens a new decision that
  supersedes the old one. It never acts on the earlier "yes".
- The GUI and CLI can show the snapshot and its diff from the current file.

**UD-R6. Delivery and handling.** An answer to an orchestrator or
initializer decision is delivered to the session bound to that role, not to
a made-up agent id.
- The delivery state is durable: `delivered_at` and the session id it was
  delivered to. Retries follow the DJ watcher's rule: redelivered after
  900 s or to a new session.
- The asker then marks the decision handled: `handled_at`, `handled_by`,
  `outcome` (`applied`, `not_applied` with a reason, or `error`).
- An agent question keeps today's behaviour: the live runner resumes the
  agent, and that counts as handled.

**UD-R7. Wake (UD-D2).** The Stop-hook watcher (`bg.py`, `watch()`, around
line 320) gains a second notification source: decisions answered and not
yet delivered whose asker role is the watcher's owner. Its report names the
decision id, the chosen options and the comment. No background job is
manufactured for it. The orchestrator's and the initializer's instructions
say to read the queue at every wake.

**UD-R8. Asking.** A new MCP tool `ask_user(...)` lets the orchestrator and
the initializer create a decision with the fields of UD-R1. It returns the
id at once and never blocks. It also takes `supersedes` and `withdraw`.
Agents keep `NEED_DECISION(topic): question` plus `DEFAULT:`. The parser
makes it a `free_text` decision with `audience: orchestrator`. When the
orchestrator judges an agent decision to be the user's, it escalates it: a
transition to `audience: user`, recorded, never a copy.

**UD-R9. Reading.** Every reader goes through one module, never through
`tree.json` directly:
- CLI: `multiagents decisions [--status S] [--audience A] [--json]`, and
  `multiagents ask`, which becomes interactive with numbered options,
  comment and free text;
- MCP: `list_decisions`, `get_decision`;
- monitor: `/api/decisions` and `/api/decisions/<id>`, plus an answer action;
  the page shows the open user decisions first.

`--json` output and the API share one shape, which UD-R1's schema
describes. History is paginated: answered and handled decisions stay
readable, but open ones come first. A later move to SQLite must keep the
ids and the API unchanged.

**UD-R10. Notifications.** ntfy (NT-R3) keeps its no-text rule. A decision
event carries the id, the audience and the asker role, never the title,
context or options.

**UD-R11. Compatibility.** Existing `questions` records read as
`mode: free_text`, `audience: orchestrator`, with their ids, statuses and
answers intact. `list_questions`, `answer_question` and the monitor's
`answer_question` action keep working on top of the new module.

**UD-R12. Instructions.** `_orchestrator.md` and `_initializer.md` say:
- every wait on the user goes through `ask_user`, with options and
  implications whenever the choice is not open-ended;
- the answer is acted on only after the digest check (UD-R5);
- the decision is marked handled;
- the orchestrator never answers a `user` decision.

BRIEF may still mention a wait, citing its id.

## Out of scope

- The GUI itself. This spec gives it a stable schema and API.
- Scheduler holds driven by decisions (UD-D4).
- A channel that proves a human answered (UD-R4 residual).

## Adversarial scenarios to close

- Answering a `user` decision over MCP or the scoped RPC.
- A draft edited between the question and the "yes".
- Two answers racing, from the CLI and the monitor.
- An answer to a withdrawn or superseded decision.
- An answer given while no orchestrator is running, then a new session
  started: the answer is delivered once, to the new session.
- A decision whose text holds a secret or an email: it is scrubbed on
  write, as today, and nothing of it reaches ntfy.
