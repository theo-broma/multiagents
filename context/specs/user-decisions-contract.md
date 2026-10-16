# User decisions queue — interface contract

Approved by the user on 2026-10-17.

Requirements: `context/specs/user-decisions-queue.md` (UD-D1..D4,
UD-R1..R12). Plan: `context/plans/2026-10-15-user-decisions-queue.md`.

Contract ids are `UD-C<n>`. They are never renumbered. A withdrawn one is
marked `withdrawn: <why>`.

## Slice 1 — the record, the store and the transitions (UD-R1, R2, R3, R11)

### Decisions pinned by slice 1

- **D-UD1.1 — one store, the existing one (UD-D1).**
  - Decisions are the records of the tree store's existing `questions`
    section (`tree.py`). They live under the same flock and the same
    scrub-on-write.
  - There is no new file and no second list.
  - A legacy record (written before this slice) is read through the
    defaults of UD-C5 and rewritten in the new shape on its next
    transition. There is no bulk migration.
- **D-UD1.2 — one module.** A new module, `multiagents.decisions`, is the
  only code that reads or writes those records. `tree.py`'s
  `add_question`, `open_questions`, `get_question` and `answer_question`
  become thin wrappers over it. Their signatures and return shapes are
  unchanged (UD-C5).
- **D-UD1.3 — ids.**
  - Agent questions keep `q-` + 6 hex.
  - Decisions created by `create()` for the orchestrator or the
    initializer get `d-` + 8 hex.
  - Ids are never reused.
- **D-UD1.4 — `handled` is a status.**
  - The statuses are `open`, `answered`, `handled`, `withdrawn` and
    `superseded`.
  - `handled` is reachable only from `answered`. The `answer` is kept
    unchanged.
  - The last three statuses are terminal.
- **D-UD1.5 — authority is not this slice.** Every operation takes the
  actor (`by`: {role, agent, channel}) as an argument from its caller. It
  records the actor and does not judge it.
  - Slice 2 (UD-R4) adds the channel-derived checks on the MCP, RPC, CLI
    and monitor surfaces.
  - Until then, nothing new is exposed outside the Python module except
    what UD-C5 keeps working.

### Behaviours

**UD-C1 — the schema file (UD-R1).**
- `schemas/decision.schema.json` (JSON Schema 2020-12) describes one
  decision as returned by `decisions.get()`, `decisions.list()` and, later,
  `--json` and the API.
- It has `"schema_version": 1` as a const property of the record.
- Fields:
  - `id`, `schema_version`, `revision` (integer ≥ 1);
  - `asked_by` {`role`: `orchestrator`|`initializer`|`agent`, `agent`};
  - `audience`: `orchestrator`|`user`;
  - `asked_at`;
  - `topic` (1..60 characters, one line);
  - `title` (1..200 characters, one line);
  - `context` (1..20000 characters);
  - `implications` {`blocks`: string, `if_unanswered`: string};
  - `mode`: `single`|`multi`|`free_text`;
  - `options[]` {`id`, `label`, `description`, `implies`}, where `id`
    matches `^[a-z0-9][a-z0-9_-]{0,31}$` and is unique within the
    decision;
  - `min`, `max` (in `multi` mode only);
  - `recommended[]` (option ids);
  - `allow_free_text` (default true);
  - `default_text` (the legacy `DEFAULT:` proposal, free text, may be
    empty);
  - `refs[]` (shape only in this slice: {`path`, `sha256`} or
    {`commit`});
  - `status`, `history[]` {`status`, `at`, `by`, `reason`,
    `superseded_by`};
  - `answer` (null or UD-C3's object);
  - `delivery` (null in this slice; slice 3 fills
    {`delivered_at`, `session`});
  - `handled` (null or {`at`, `by`, `outcome`: `applied`|`not_applied`|
    `error`, `reason`}).
- Every record the module returns validates against the file.
- Verified by: a test validating records in every status, the legacy
  records included, against the schema with `jsonschema`; and a test that
  every field the module returns is declared (`additionalProperties:
  false`).

**UD-C2 — creating and reading.**
- `decisions.create(tree, *, by, audience, topic, title, context,
  implications, mode, options=(), min=None, max=None, recommended=(),
  allow_free_text=True, default_text="", refs=()) -> dict` validates the
  record, stores it `open` with `revision` 1, and returns it.
- An invalid record raises `DecisionError("invalid_record", field=...)`
  naming the first offending field. Nothing is stored. It is invalid when:
  - a length or one-line rule is broken;
  - `single` mode has fewer than 2 options;
  - `multi` mode breaks 1 ≤ `min` ≤ `max` ≤ `len(options)`;
  - `free_text` mode has options;
  - an option id is duplicated;
  - a `recommended` id is unknown, or `single` mode recommends more than
    one;
  - `free_text` mode has `allow_free_text` false.
- `decisions.get(tree, id) -> dict | None` returns the record in the
  schema's shape.
- `decisions.list(tree, *, status=None, audience=None, cursor=None,
  limit=50) -> {"decisions": [...], "next": str | None}`:
  - `open` decisions come first, oldest `asked_at` first;
  - then the others, most recent last transition first;
  - `cursor` is opaque, and paging is stable while records are added;
  - `limit` is between 1 and 200.
- Verified by: one test per invalid rule; a round trip of each mode; and a
  pagination test that adds records between pages and sees neither a
  duplicate nor a gap.

**UD-C3 — answering (UD-R3).**
- `decisions.answer(tree, id, *, revision, by, options=(), comment="",
  free_text="", request_id=None) -> dict`.
- The stored `answer` is {`options`, `comment`, `free_text`,
  `answered_by`, `answered_at`, `revision`, `request_id`}, where
  `revision` is the one answered. The status becomes `answered`.
- Validity:
  - `single` mode: exactly one option, or free text and no option
    (only if `allow_free_text`);
  - `multi` mode: `min`..`max` options, or free text and no option;
  - `free_text` mode: non-empty free text, no option;
  - a `comment` (≤ 4000 characters) may go with any of them;
  - unknown option ids, duplicates, or an empty answer raise
    `invalid_answer`.
- Order of checks:
  1. an unknown id raises `unknown`;
  2. a `withdrawn` or `superseded` decision raises `not_open`, with its
     status (and `superseded_by`) in the error;
  3. an `answered` or `handled` decision returns the stored record with
     `already_answered: true` and no new event, when the submitted answer
     equals the stored one (same option set, comment and free text) or
     carries the stored `request_id`. Otherwise it raises
     `conflicting_answer`;
  4. a `revision` other than the current one raises `stale_revision`,
     with the current revision;
  5. otherwise the answer is validated and recorded.
- A recommendation or a `default_text` is never recorded as an answer by
  the module (UD-D3).
- Verified by: each mode's valid and invalid answers; the idempotent retry,
  both by equal content and by `request_id`, after another field changed
  the revision; a conflicting answer refused; two processes racing on the
  same revision, of which exactly one wins and the other gets
  `already_answered` or `conflicting_answer`.

**UD-C4 — the other transitions (UD-R2).**
- `decisions.withdraw(tree, id, *, revision, by, reason)`: from `open`
  only, with a non-empty reason.
- `decisions.supersede(tree, id, *, revision, by, superseded_by)`: from
  `open` only. `superseded_by` must name an existing decision that is
  `open` and is not `id` itself.
- `decisions.mark_handled(tree, id, *, revision, by, outcome,
  reason="")`: from `answered` only. `reason` is required unless
  `outcome` is `applied`.
- `decisions.escalate(tree, id, *, revision, by)`: from `open` only,
  `audience` from `orchestrator` to `user`. This is a recorded history
  entry with status unchanged, never a copy (UD-R8).
- Every transition:
  - happens in one tree transaction;
  - checks `revision` (`stale_revision` on mismatch);
  - increments `revision`;
  - appends to `history`.
- A transition not allowed from the current status raises `not_open` or
  `bad_transition`, and nothing changes.
- Every transition, creation included, emits one `decision` event to
  `events.jsonl` with `transition`, `id`, `revision`, `audience` and
  `asker_role`. It never carries the title, the context, the options, the
  comment or the free text (UD-R10 later relies on this).
- Verified by: the full transition matrix (every status × every
  operation); one event per transition with exactly the allowed fields;
  and a `stale_revision` test per operation.

**UD-C5 — compatibility with agent questions (UD-R11).**
- A legacy record reads as:
  - `mode: free_text`, `audience: orchestrator`;
  - `asked_by` {`agent`, `role: agent`};
  - `title` = its topic, and `context` = its question;
  - `default_text` = `proposed_default`;
  - `revision` 1 and `history` [{`open`, `asked_at`}], plus an `answered`
    entry if answered;
  - when answered, `answer` = {`free_text`: the old answer string,
    `answered_by`, `answered_at`}.
- `tree.add_question` creates through UD-C2. It also still emits the
  existing `question` event, alongside the `decision` event.
- `tree.answer_question(id, answer, answered_by)` answers through UD-C3
  as free text at the current revision. It keeps returning the legacy
  shape, including `already_answered`, and still emits the existing
  `answered` event.
- `list_questions`, `answer_question` (MCP and runner) and the monitor's
  `answer_question` action behave exactly as today on legacy and on new
  agent questions. Any test of them that exists now stays green unchanged.
- `tree.answer_question` on a `d-` decision that is not `free_text`
  refuses with a message naming `decisions.answer`. It never coerces.
- Verified by: a `tree.json` fixture holding legacy records in each status,
  read through the module and through the legacy calls; the existing
  question tests unchanged and green; and a legacy record rewritten in the
  new shape after its first transition, with its id, its answer and its
  times intact.

**UD-C6 — nothing else changes in slice 1.**
- No MCP tool, CLI command, monitor route or instruction text is added or
  changed. Those come in slices 2 to 5.
- Verified by: the existing server, CLI and monitor test files stay green.

### Slice 1 precisions (advisor pass, ag-662db9 turn 13)

These override the behaviours above where they differ.

- **P1 — legacy readers go through the module.** Code outside `tree.py`
  reads `questions` directly today:
  - the monitor's snapshot and TUI;
  - `notify_sender.py` (around lines 351 and 660);
  - the driver;
  - `tree.py` around line 2104.

  In slice 1 each of them reads through
  `decisions.legacy_questions(tree, *, status="open", agent=None)`, which
  returns today's legacy shape. That projection:
  - selects on `status == "open"` explicitly, never on a missing
    `answered_at`;
  - excludes `d-` decisions (they reach readers in slice 4).

  No new UI surface is added. D-UD1.2's scope includes these adapters.
- **P2 — the legacy projection, field by field (replaces UD-C5's first
  bullet where they differ).**
  - An open legacy record reads with `revision` 1 and `history`
    [{`status: open`, `at`: asked_at, `by`: {`role: agent`, `agent`,
    `channel: legacy`}}].
  - An answered legacy record reads with `revision` 2, and `history` adds
    {`status: answered`, `at`: answered_at, `by`: {`role`: the legacy
    `answered_by` string, `agent: ""`, `channel: legacy`}}.
  - Its `answer` is {`options`: [], `comment`: "", `free_text`: the old
    string, `answered_by`: that same actor object, `answered_at`,
    `revision` 1, `request_id`: null}.
  - `title` is the topic's first line, cut to 200 characters with a
    trailing "…" if longer.
  - `implications` is {`blocks`: "", `if_unanswered`: ""}.
  - `answered_by` in new records is the actor object {`role`, `agent`,
    `channel`}. The legacy wrappers keep returning the legacy string.
- **P3 — limits are create-time rules.**
  - The 60/200/20000 limits are enforced by `create()` and `answer()`.
  - The schema bounds `topic` and `title` only. `context`, `comment` and
    `free_text` are unbounded in the schema, so a legacy record always
    validates.
- **P4 — scrubbing.** The tree scrubs strings on write.
  - **Structural values** are ids, option ids, `recommended`, `min`/`max`,
    `refs` (path, sha256, commit) and `request_id`. If the scrubber would
    alter one of them, the operation is refused with `invalid_record` or
    `invalid_answer` naming the field. They are stored exactly.
  - **Human text** is topic, title, context, implications, labels,
    descriptions, `implies`, comment and free text. It is scrubbed
    *before* validation and storage.
  - Every returned record is the stored, scrubbed form, never the caller's
    in-memory copy.
  - Answer equality (UD-C3, step 3) compares scrubbed forms.
- **P5 — `request_id` is bound to its payload.** A retry with the stored
  `request_id` is idempotent only if its scrubbed payload equals the stored
  answer. The same `request_id` with a different payload raises
  `conflicting_answer`.
- **P6 — return shapes.**
  - `decisions.answer()` returns {`decision`: record, `already_answered`:
    bool}.
  - The other operations return the record.
  - Records never carry operation metadata, so the schema's
    `additionalProperties: false` holds.
  - The legacy `tree.answer_question` keeps its legacy return shape,
    including `already_answered`.
- **P7 — events.**
  - An event is emitted after its transaction commits. Delivery is
    best-effort, as for every tree event today: it may be lost if the
    process dies between the commit and the emit, and it is never emitted
    for a transaction that did not commit. `tree.json` is the source of
    truth.
  - UD-C4's field list is the event *payload*. The envelope fields that
    `Tree.emit` adds (`t`, `agent`, `kind`) are allowed. `agent` is the
    asker's agent id, or its role when it has none.
  - An idempotent duplicate answer emits no event, neither `decision` nor
    the legacy `answered`.
- **P8 — pagination is live keyset pagination.**
  - The sort key is (group: open first; `asked_at` ascending for open,
    last transition time descending for the others; then `id`).
  - The cursor encodes the last key returned and the filters it was issued
    for. A cursor reused with other filters, or a malformed one, raises
    `invalid_cursor`.
  - Guarantee: no duplicate and no gap among records whose sort key did not
    change between the first page and the last. A record created or
    transitioned in between may appear once later, or not at all.
    Completeness needs a restart from the first page.
  - (Replaces UD-C2's "stable while records are added".)
- **P9 — terminal statuses never resume an agent.**
  - The runner's resume path acts only on a transition to `answered` that
    it made itself.
  - `handled`, `withdrawn` and `superseded` never trigger a resume.
  - Marking an agent question handled after a resume is slice 3 (UD-R6).
    In slice 1 it stays `answered`, as today.
- **P10 — `jsonschema`.** The tester declares `jsonschema` directly in the
  dev dependencies (`pyproject.toml` and `uv.lock`). It is not relied on
  as a transitive dependency of `mcp`.

### Slice 1 work split

1. tester: `tests/test_ud_c1_schema.py` .. `tests/test_ud_c5_compat.py`,
   black-box through the module and the legacy calls. It must not edit
   existing tests.
2. implementer (default tier): `decisions.py`, `schemas/`, and the
   `tree.py` wrappers.
3. reviewer. No adversary on slice 1: the adversary is mandatory on
   slices 2 and 3.

## Slices 2–5

These are written after slice 1 merges, from UD-R4..R10 and R12. UD-R4's
scoped-RPC refusal relies on the 4A endpoint, which is now merged.
