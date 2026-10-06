# Push notifications through a self-hosted ntfy (NT)

Source: user, 2026-10-06: "j'aimerais aussi un mcp ntfy pour pouvoir envoyer des
notifications". The user's choices:
- the sender is the orchestrator AND the scheduler;
- the ntfy server is self-hosted on the user's tailnet;
- the implementation is in-house, with no third-party MCP package and no new
  dependency.

Advisor ag-aed397 checked the contract for silences.

Goal: the user's phone learns about the moments that need them, even when no
Claude session is open. "Sent" here means *accepted by the ntfy server*.
Delivery to the phone is ntfy's business, and every message and output says
"accepted", never "delivered".

## Configuration

A new optional `notify:` section in `project.yaml`. It is absent from the
shipped defaults, so the feature is off. Addresses in this spec, in tests and in
shipped docs are generic placeholders (`<host>.<tailnet>.ts.net`, or
`example.invalid` in tests). A user's real address lives only in their own,
untracked `.multiagents/config/project.yaml`.

```yaml
notify:
  ntfy_url: https://<host>.<tailnet>.ts.net:8443   # base URL of the ntfy server
  topic: multiagents-<something>                 # one topic per project
  token_file: ~/.config/multiagents/ntfy-token   # optional; access token, never inline
  events: [question, held, anomaly, done]        # optional; this is the default
  min_interval_seconds: 30                       # optional; default 30
```

## Behaviours

**NT-R1. Config validation.** The config is refused at load, the way existing
invalid settings are refused, when:
- `ntfy_url` is not an `http`/`https` URL with a host;
- `topic` is empty, longer than 64 characters, or holds characters outside
  `[A-Za-z0-9_-]`;
- `events` names an unknown event;
- `min_interval_seconds` is not a positive number;
- a token is given inline instead of through `token_file`.

Sending is refused, with a reason, and nothing is sent, when the `token_file`
is set and is:
- missing, unreadable or empty;
- readable by group or others (it must be 0600 or stricter).

Without a `notify:` section, nothing in the system sends anything.
Verified by: config tests, one per refused value and one per token-file case,
plus one with the section absent.

**NT-R2. Root-only `notify` tool.**

`notify(title, message, priority="default", tags=[])` publishes one message to
`<ntfy_url>/<topic>` using ntfy's documented HTTP publish:
- the `Title`, `Priority` and `Tags` headers;
- the message as the body;
- `Authorization: Bearer <token>` when a token is configured.

Inputs:
- `priority` is one of `min`, `low`, `default`, `high`, `urgent`.
- Tags follow the topic's character rule.
- CR and LF never reach a header.
- A non-ASCII title is encoded in a way ntfy documents (RFC 2047), so it
  arrives intact.

Result:
- `{"ok": true}` when the server accepted the message.
- Otherwise `{"ok": false, "reason": ...}`: not configured, invalid argument,
  token file refused, network, HTTP status, timeout, or redirect.
- It never raises.

Limits:
- It returns within 10 s overall, covering the connection, the response and
  cleanup.
- Redirects are never followed, so a token never goes to a destination other
  than the one configured.
- The tool is not subject to NT-R5's rate limit.

Who can call it:
- It is offered to the root orchestrator only. It is not in any subagent's
  tool list, legacy runs included.
- A subagent calling it directly anyway is refused.

Verified by:
- tests against a local fake HTTP server, asserting the path, headers, body,
  Bearer header and a Unicode title;
- one test per failure reason, including a redirect that is not followed and
  a hanging server bounded at 10 s;
- a test that a subagent neither lists the tool nor can call it.

**NT-R3. Scheduler events.** The host scheduler publishes notifications for
these event kinds, when listed in `events`:
- `question`: an agent parked a NEED_DECISION question. Questions are
  identified by their question id, whether or not the run belongs to a node.
- `held`: a node became held, for any reason (`loop_max`, `unresolved_round`,
  `admission:refused`, `run_failed`, …). There is one notification per hold
  of a node; a later hold of the same node is a new event.
- `anomaly`: an AN `anomaly` transition.
- `done`: a top-level node finished, whatever its outcome.

The scheduler's notifications are built from templates:
- the project name and the event kind in the title;
- in the message, the id (node, run or question), the kind and the reason
  code or outcome, drawn from the system's own enumerations;
- never the free-text `detail` of a transition, the text of a question, or
  any other free text.

Priority is `high` for `question` and `held`, and `default` otherwise.

Edge cases:
- An event that is no longer true when its turn comes is dropped: the
  question was answered, or the node left `held`.
- On activation, the first start with `notify:` present (or a destination
  changed since the last start), the scheduler sends one summary: the counts
  of open questions and held nodes at that moment. It does not send the
  history.

Verified by:
- scheduler tests with a fake ntfy server, one per event kind;
- a test that unlisted kinds send nothing;
- a test that a stale event is dropped;
- a test of the activation summary;
- a test that a transition detail holding transcript-like text does not appear
  in the message.

**NT-R4. Durability and retries (at least once).** Pending notifications are
kept durably, in their own outbox and cursor, separate from the orchestrator's
`ack_nodes` cursor.

Delivery is at least once:
- After a crash between the server accepting a message and its being recorded
  as sent, the message may be sent again. A message is never lost silently.

Retries:
- Network errors, timeouts, 429 and 5xx are retried with exponential backoff,
  from 30 s up to 10 min.
- 400, 401, 403 and 404 are not retried. Sending pauses until the config
  changes or `multiagents notify test` succeeds, and the pending messages stay
  queued.

Expiry and config changes:
- A pending message older than 24 h is dropped. The count of dropped messages
  goes into the next sent message ("N notifications expired").
- Removing the `notify:` section discards the pending outbox.
- Changing the destination sends the pending messages to the new one.

Verified by:
- a test that restarts the engine after a send;
- a crash-after-accept test that shows a resend, not a loss;
- a test with the fake server down and then back;
- a test that a 401 stops retries until `notify test` succeeds;
- a test of the 24 h expiry summary.

**NT-R5. Rate limit and grouping.** The scheduler publishes at most one message
per `min_interval_seconds`. Events arriving faster are grouped into the next
message, which:
- lists them in the order they happened, up to 10 lines plus "+N more";
- takes the highest priority among the events it holds.

Verified by: a test with a fake clock and a burst of mixed-priority events.

**NT-R6. Sending never disturbs scheduling.** Sending happens outside the
scheduler's tick and outside any store transaction. A slow, hanging or failing
ntfy server:
- does not delay a tick;
- never changes a node, a run or a transition.

The state of sending is visible in `scheduler_status`, and through
`multiagents notify status`:
- the pending count;
- the time of the last accepted message;
- the current failure, if any.

A failure is logged once per outage per scheduler process.
Verified by:
- a test with a hanging fake server, asserting that tick timing and node state
  are unaffected;
- a test of the status fields.

**NT-R7. Content discipline.**
- Scheduler notifications contain only what NT-R3's templates allow.
- A message sent with the `notify` tool is free text. Its content is the
  orchestrator's responsibility, and its body is truncated to 1000 characters.
- A token never appears in any message, log or status output.

Verified by:
- the NT-R3 detail test;
- a truncation test;
- a test that a configured token appears in no log or status output.

**NT-R8. CLI.**
- `multiagents notify test` sends one test message with the project's config.
  It exits 0 when the server accepts it, and non-zero with the reason
  otherwise. Its success clears an NT-R4 pause.
- `multiagents notify status` prints the NT-R6 fields.

Verified by: CLI tests against the fake server.

## Out of scope
- Installing or running the ntfy server itself. The user does this, with a
  guide given in the conversation.
- Receiving replies from the phone, or acting on them.
- Notifying from inside agent containers. Agents never reach ntfy, and
  `egress_allowlist` is unchanged.

## Order
- NT-R1, R2, R7 and R8 (the config, the tool and the CLI) are built first.
- NT-R3 to R6 (the scheduler sender) come after VR and AN merge, because they
  touch the scheduler loop.

## Clarifications to part 2 (2026-10-06, answering tester ag-7031ce)
- **Clock.**
  - Retry backoff, the rate limit and the 24 h expiry all run on the scheduler's clock (the one `--clock-file` drives in tests), never on wall time.
  - Backoff doubles from 30 s up to a 10 min cap, with at most ±10 % jitter.
  - A 429 `Retry-After` is honoured when it falls within [30 s, 10 min], and clamped to that range otherwise.
- **Status.**
  - `scheduler_status` gains a `notify` mapping with `pending` (int), `last_accepted_at` (ISO-8601 UTC, or null) and `failure` (a short reason string, or null).
  - `notify status` prints `pending: N`, `last accepted: <time or never>` and `failure: <reason or none>`.
- **Activation summary.**
  - It is a message like any other, so it is rate-limited.
  - It is sent even when both counts are zero.
  - It is not filtered by `events`.
- **`done`.**
  - Any top-level node reaching a terminal state sends it, including `cancelled`, with the outcome or state as the reason.
  - Children of a composite never send `done`.
- **Title.** The title is `<project> · <kind>`, where `<project>` is the project root directory's name.
- **Pause and expiry.**
  - A restart does not clear an NT-R4 pause.
  - "N notifications expired" counts events, not messages.
- **Grouping.** A grouped message takes the highest priority of ALL the events it carries, including those hidden behind "+N more".
- **Logging.** The once-per-outage failure line goes to the scheduler's own log, wherever its other log lines go.
