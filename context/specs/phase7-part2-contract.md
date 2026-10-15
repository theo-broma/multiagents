# Phase 7 part 2 — interface contract

**Status:** slice 1 approved by the user 2026-10-15. Slices 2..6 follow the milestones of
`phase7-part2-containers.md` and are appended here, each shown to the user and
committed before its tester starts.

- **Requirements:** `context/specs/phase7-part2-containers.md` (IC-D*, IC-R*).
- **Plan:** `context/plans/2026-10-13-phase7-part2-containers.md`.
- **Ids:** `IC-C*` are contract behaviours, never renumbered. A withdrawn one is
  marked `withdrawn: <why>`. Tests are named after them (`test_ic_c3_...`).

## Slice 1 — the backend seam, the selection, the isolation gate

Slice 1 changes no behaviour of the existing `local` and `docker` executors. It
adds: the executor selection for the new mode (IC-D7), the attempt's record of
its backend, the backend lifecycle protocol that every later slice builds on
(IC-D2), a reference backend that runs no container, the backend conformance
tests, the IC-R13 isolation suite written red, and the latency benchmark.

### Decisions pinned by slice 1 (cross-slice invariants)

- **D-S1.1 — one seam, beneath Runner.** The backend lifecycle protocol sits
  beneath `Runner`; the scheduler keeps calling Runner, and Runner keeps
  admission, provider preparation, supervision and Git publication. No second
  scheduler-to-backend path. Slice 1 adds the protocol beside today's
  `Executor`; from slice 3 on, Runner's adoption, liveness,
  stop and predecessor handling go through the protocol and the recorded
  identity only; no code above the backend branches on a backend name.
- **D-S1.2 — execution location is not backend selection.** Code that today
  tests `kind == "docker"` to mean "the CLI runs inside a container" (credential
  projection, `scripts.py`, provider auth location) asks a property of the
  executor instead. An isolated attempt has the in-container semantics of
  `docker` for credentials and scripts; a new kind string never selects host
  credentials.
- **D-S1.3 — the launch specification is semantic and reserved now.** It names
  what later slices fill, so they do not change the protocol: the authorised
  seed (IC-R1), writable paths and read-only paths, stable guest paths for the
  repository, HOME and temp, the session-state handle (IC-R3, IC-R6), resources
  (IC-R5), the credential projection handle (IC-R4), the scoped transport
  handle (IC-R11), the owner (an attempt id or a conversation id, IC-D6) and the
  lifetime (`attempt` or `conversation`). There is no generic "extra mounts"
  entry: the project's declared toolchain mounts appear as read-only paths.
  Fields a slice has not implemented yet are accepted and refused at
  provisioning with `unsupported:<field>`, never silently ignored.
- **D-S1.4 — host ownership.** Git publication, capture and admission stay on
  the host (IC-R2). Nothing a backend reads from inside the resource
  authorises a host mutation (H1).

### Behaviours

**IC-C1 — executor selection.**
- `project.yaml` `executor.kind` accepts `local`, `docker` and `isolated`.
  `isolated` reads `executor.isolated.backend`, default `docker`, the only
  value accepted in this part; any other value is a config error naming the
  accepted values. An unknown `kind` stays a config error.
- A per-agent `executor:` pin in `agents.yaml` keeps its precedence over the
  project kind and accepts the same values.
- **Selection comes only from host configuration.** Node pins, task text,
  templates and anything written from inside a run cannot change it.
- A config change applies to attempts not yet recorded; it never migrates,
  re-adopts or removes existing resources.
- Verified by: config-load tests for each value, the default, the unknown
  backend, the per-agent precedence, and a node pin that tries to set the
  executor (refused or ignored, never applied).

**IC-C2 — each attempt records its backend, frozen before launch.**
- Before anything is created, the attempt's launch evidence
  (`launch_evidence.executor`, where `kind` and the Docker `container` already
  live) holds `kind`, `backend` (empty for `local`/`docker`), the backend
  version string, the spec schema version and the spec digest (IC-C4). The
  same fields are projected into each row of `get_node().runs[]` as
  `executor: {kind, backend, backend_version}`.
- Automatic retries of the same node (TF) keep the recorded selection; only
  an explicit root action (relaunch with a changed config) produces a new
  attempt under a new selection, and that attempt records it.
- **C2a (slice 1):** recording, projection and freezing, observable through
  `get_node` after a config change.
- **C2b (slice 3):** adoption, liveness, stop and cleanup act on the recorded
  backend, never the current config. A record with no `executor` evidence is
  **unknown**: an unlaunched claim may be abandoned as today; a possibly
  launched one gets the existing liveness hold, never a guess at `local`.
  When the backend's version changed since the record, adoption proceeds only
  if the backend declares the recorded version compatible; otherwise the
  attempt is `unknown` and held.
- Verified by: C2a tests now; C2b tests written now, marked `ic_milestone(3)`.

**IC-C3 — an isolated launch that cannot happen is refused, never downgraded.**
- No process of an `isolated` attempt ever starts in the shared mode, and no
  retry switches kind.
- **Probe failure** (backend unavailable, prerequisite missing): the
  scheduler's admission probe fails before any attempt exists; the node stays
  queued with `blocked` reason `isolation_unavailable: <backend reason>`, as
  other admission blocks are reported. Nothing is created.
- **Unsupported configuration** (a spec field the backend refuses,
  `unsupported:<field>`, `invalid_spec`): the attempt fails with TF class
  `other`, not retryable, detail code `isolation_unavailable` and the reason.
- **Operational failure at provision or start**: TF class `launch`, retried
  within TF's existing bound, with the isolated selection preserved; detail
  code `isolation_unavailable`.
- A failed provision or start leaves its creation key recorded, so the
  partial resource is found and cleaned up (IC-C4 `find`), and reservations
  are not released before the backend confirms the resource is gone.
- Verified by: reference backend failing at probe, provision and start; assert
  the block reason or the TF class and detail, and that no shared-mode
  process was started.

**IC-C4 — the backend lifecycle protocol.**
- **Location and shape.** Types and the protocol live in
  `multiagents.executor.lifecycle`. Operations are `async`, like
  `Executor.start`. Every expected condition is a typed outcome, never a bare
  exception; an unexpected exception is reported as `unknown(reason)` by the
  caller, never as success.
- **Two levels: a resource and its invocations** (IC-D6). A resource is the
  isolated environment (a container), with a lifetime of `attempt` or
  `conversation`. An invocation is one command run inside it. An attempt
  resource carries exactly one agent invocation; a conversation resource
  carries one per turn; a future user shell (IC-R14) is an invocation of kind
  `user_shell`, recorded as such.
- **The launch spec** (resource level), schema `spec_version: 1`:
  - owner (attempt id or conversation id), lifetime, labels;
  - the authorised seed reference (IC-R1), writable paths, read-only paths,
    stable guest paths for repository, HOME and temp;
  - session-state handle (IC-R3/R6), resources (IC-R5), credential projection
    handle (IC-R4), scoped transport handle (IC-R11).
  - Handles are opaque host-issued references; no secret value ever appears in
    the spec or in attempt metadata.
  - Every host path in it must lie under a host-issued root (the attempt's own
    storage root, or a toolchain mount declared in `project.yaml`); anything
    else is `invalid_spec`. No generic "extra mounts".
  - Unset optional fields have stated defaults (empty; resources from
    `project.yaml`); a non-empty field the backend does not implement yet is
    `unsupported:<field>`, never ignored.
  - The digest is SHA-256 of the canonical JSON (sorted keys, UTF-8, no
    insignificant whitespace) of the spec.
- **The invocation spec:** argv, environment, working directory (a guest
  path), stdin source (none, or a host-issued input file), kind (`agent`,
  `turn`, `user_shell`).
- **Operations and outcomes:**
  - `probe()` — side-effect free; a list of problems, empty when ready.
  - `provision(spec, key)` — `key` is host-generated and recorded before the
    call. Same key and same digest returns the same identity; same key and a
    different digest returns `identity_conflict` and changes nothing. Returns
    an immutable resource identity.
  - `find(key)` — the identity created under `key`, or `absent`; used after a
    crash between recording the key and recording the identity.
  - `inspect(identity)` — exactly one of `absent`, `created`, `running`,
    `exited(code, oom, finished_at)`, `unknown(reason)`. Callers never treat
    `unknown` as `absent` or dead.
  - `run(identity, invocation, key)` — starts an invocation; idempotent by
    key. On an `attempt` resource a second agent invocation is refused
    (`already_invoked`); on an exited attempt resource, `exited`.
  - `inspect_invocation(identity, invocation_id)` — same states as `inspect`.
  - `output(identity, invocation_id, stream, offset)` — stdout and stderr are
    separate byte streams; any offset up to what was written can be replayed;
    order within a stream is kept, none across streams. Output stays readable
    after exit until `remove`.
  - `stop(identity, grace)` — graceful signal to everything in the resource,
    then forced termination after `grace`, descendants that left the process
    group or session included. Idempotent; `absent` on an absent resource.
    When termination cannot be confirmed, returns `unknown(reason)` and the
    caller keeps the reservation and the liveness hold.
  - `remove(identity, keep_artifacts)` — with `keep_artifacts`, output and exit
    evidence stay readable (retention, IC-R6), state `exited`; without,
    everything goes (discard, IC-R8), state `absent`.
  - `list_owned(project)` — every resource this project owns, by label and
    recorded identity, for orphan reporting. Deletes nothing.
- Cancellation during any operation leaves a state `inspect` reports
  truthfully.
- Verified by: IC-C5.

**IC-C5 — backend conformance tests and a reference backend.**
- A backend-neutral conformance suite exercises IC-C4 through the protocol
  only, parametrised by backend. In slice 1 it runs against a **reference
  backend**: processes on the host under a private directory, also the
  deterministic double for scheduler tests.
- **The reference backend tests lifecycle bookkeeping only; IC-C6 never
  accepts it as isolation evidence.** Its `stop` signals only processes it
  started and recorded; it never signals an unrelated host process.
  Containment cases (escaped descendants, sibling reads) are conformance tests
  marked for real backends only (`ic_milestone(3)`), run under Docker.
- The reference backend is never selectable from `project.yaml`; tests inject
  it.
- Verified by: the conformance suite green on the reference backend, except
  the real-backend-only cases, which skip there.

**IC-C6 — the isolation suite (IC-R13), written now, red until it passes.**
- Black-box, through the scheduler's public entry points only: configure
  `executor.kind: isolated`, start workloads with `create_node`, observe with
  `get_node`, node transitions, run output and captured results. No test
  reaches into a backend.
- **The probe workload is the tester's to build**, under `tests/support/`,
  on the existing deterministic `FakeProvider` (`tests/support/sc_harness.py`,
  used by `nc_harness.Sched`): a provider whose CLI runs the filesystem, Git,
  network, RPC and cache probes inside the launched environment and reports
  each result. It is delivered into the run like any provider binary, never by
  mounting the host test tree or a sibling's state.
- For any two concurrent attempts A and B, one test per IC-R13 clause, each
  with a **positive control** proving the probe ran and reached A's own
  equivalent. Clauses: B's worktree, HOME, temp, run directory (read, modify,
  delete); B's refs and objects (move, delete, make unreachable, read committed
  blobs); B's network services, RPC endpoint and credentials; a shared cache B
  will read; a `readonly` attempt writing its worktree or Git metadata; a
  forged RPC identity; the deliberate handover (a dependency's approved work
  reaches its dependent, nothing unrelated does).
- Real Docker is opt-in with the existing switch `SG_TEST_DOCKER=1`: without
  it the suite is skipped; with it, a missing prerequisite (no daemon, no
  image) is a **failure**, not a skip.
- Everything a test creates carries a test-session label. Finalizers remove
  it, and each session starts by reaping resources left by earlier test
  sessions' labels (only those labels), so an interrupted run is cleaned up by
  the next.
- Each test carries `ic_milestone(n)`, `n` from 3 to 6: end-to-end isolation
  cannot pass before isolated launch exists (slice 3). Milestone 2 is checked
  by provisioning conformance tests in slice 2. The whole suite is the gate for
  removing the shared mode (IC-D7).
- Verified by: the suite is collected, skips without the switch, and under
  the switch is red for the stated reasons (run by the orchestrator on the
  host).

**IC-C7 — the latency benchmark.**
- `scripts/bench-isolation` measures, for a short reviewer-like attempt,
  each stage's wall time: seed bundle, provision, start, first output, exit,
  cleanup. Parameters: repetitions (default 5), image cold or warm, seed cache
  cold or warm. Each sample records the host's load average. Output: one JSON
  document on stdout.
- A stage the selected backend cannot perform yet is reported as
  `"unsupported"`, never as a zero duration. On the reference backend, the
  image and seed stages are `unsupported`.
- Baseline only; no pass threshold in this part.
- Verified by: a test running it on the reference backend with 1 repetition,
  checking the keys and the `unsupported` stages.

**IC-C8 — nothing else changes.**
- With `executor.kind` `local` or `docker`, behaviour is unchanged: the full
  suite equals the 70-id baseline.
- Verified by: the full suite, run by the orchestrator before merge.

### Slice 1 work split

1. `tester`: IC-C1..C3 (C2b marked milestone 3), IC-C5 conformance suite,
   IC-C6 isolation suite and its probe workload, IC-C7 test. Red.
2. `implementer` (deep tier: it is the seam everything hangs off): selection,
   recording, refusal, the protocol, the reference backend, the benchmark.
3. reviewer, then adversary (IC-C2/C3 decide what runs where), then the full
   suite, then merge.
