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

### Slice 1 clarifications (2026-10-15, after the tester's first pass)

These settle what the tester had to guess and what slice 1 left unsaid. They
refine the ids above; they add no new behaviour id.

**K1 — Python names (IC-C4, IC-C5).** In `multiagents.executor.lifecycle`:
- `ReferenceBackend(root: Path)`, with `.version: str`.
- `LaunchSpec(spec_version=1, owner, lifetime, labels, writable_paths,
  readonly_paths, guest_repo, guest_home, guest_temp, seed, session_state,
  resources, credential_handle, transport_handle)`.
  - Optional fields default to empty.
  - Guest paths default to `/work/repo`, `/work/home` and `/work/tmp`.
- `InvocationSpec(argv, env, cwd, stdin, kind)`.
- `ResourceIdentity`: frozen and hashable, with `.id`.
- Outcomes: `InvalidSpec(reason)`, `Unsupported(field)`, `IdentityConflict`,
  `AlreadyInvoked`, `Absent`, `Created`, `Running`,
  `Exited(code, oom, finished_at)`, `Unknown(reason)`.
- The invocation id returned by `run` equals the invocation key.

**K2 — idempotency by key also covers the invocation (IC-C4).** If `run` is
called with the same key but a different `InvocationSpec`, it returns
`IdentityConflict` and starts nothing. Same rule as `provision`.

**K3 — test injection of a backend (IC-C5, IC-C6).**
- The environment variable `MULTIAGENTS_TEST_ISOLATED_BACKEND=module:callable`
  names a callable `(root: Path) -> backend`.
- It is read only by the host scheduler or runner process, when it constructs
  the backend, and only for `kind: isolated`. It is never read from
  `project.yaml` or from run state.
- If the import or the factory call fails, isolated execution is refused
  (IC-C3). There is no fallback.
- This variable, `IC_TEST_PLAN` and `IC_BACKEND_ROOT` are removed from every run
  environment. They are stripped last, after all overlays and passthrough, in
  the same way as `safepoint.strip_key`.

**K4 — what is recorded under injection (IC-C2).**
- An injected reference backend is recorded truthfully: `kind: "isolated"`,
  `backend: "reference"`, and its actual `.version`.
- C2b resolves a recorded backend name only through the host's injection or
  registry, never through anything supplied by run state.

**K5 — evidence field names (IC-C2, IC-C3).**
- Alongside the existing fields, record:
  - `launch_evidence.executor.creation_key` and `invocation_key`, each written
    before the operation that uses it;
  - `identity` and `invocation_id`, written after the operation returns them.
- Writing launch evidence merges into the `executor` mapping. It never
  replaces the mapping and drops these fields (today's write at `runner.py`
  replaces it).

**K6 — when reservations are released (IC-C3; replaces "not released before
the backend confirms the resource is gone").**
- **Compute reservations** are released once termination is confirmed, that
  is, when `inspect` reports `exited` or `absent`. A retained resource stays
  `exited` by design, so waiting for `absent` would hold memory forever.
- **Storage quota** is held until the resource is discarded.
- If the outcome is `unknown`, the reservation and the liveness hold are both
  kept.

**K7 — unexpected exceptions (IC-C4).**
- The caller (Runner, or the seam adapter) wraps an operational exception into
  `Unknown(reason)`. Cancellation propagates unchanged.
- An exception can arrive after the creation or the start succeeded, so it
  never means "nothing exists". Before TF retries, the caller resolves the
  recorded keys (`find(creation_key)`, `inspect_invocation`) to learn what
  started.
- Verified by: an injected backend that raises at provision and at run.

**K8 — the D-S1.2 property.**
- `Executor.uses_guest_environment: bool` is false for `local` and true for
  `docker` and `isolated`.
- The call sites that test `kind == "docker"` for credential and auth location
  use this property instead.
- Sites that need Docker transport details (for example `.container` in
  `scripts.py`) are not converted mechanically. They keep a transport-specific
  path and must not assume a container name exists for `isolated`.

**K9 — what the reference backend implements (IC-C5).**
- It implements: owner, lifetime, labels, writable and read-only paths, and the
  guest paths, mapped under its private root. Each invocation gets a private
  HOME and TMPDIR, and declared path access is enforced where the backend
  claims it.
- Every other field, when non-empty, is refused with `Unsupported(field)`.
  Path validation (`InvalidSpec`) comes first.
- An unset `resources` field must not inherit production limits, which would
  make every reference launch unsupported.

**K10 — TF bound (IC-C3).**
- The bound is TF's existing one: `scheduler.retries.budget` additional
  retries per activation. The default is 3 retries (4 attempts), with backoffs
  `[30, 120, 600]`.
- Tests read the effective value through `failures.retry_policy()` and assert
  `budget + 1` failures followed by `retries_exhausted`. Shortened backoffs in
  tests are fine.

**K11 — the benchmark's interface (IC-C7).**
- Flags: `--backend`, `--repetitions` (default 5), `--image cold|warm`,
  `--seed-cache cold|warm`.
- Output: one JSON document:
  `{"backend", "image", "seed_cache", "samples": [{"loadavg": [..],
  "stages": {"seed", "provision", "start", "first_output", "exit",
  "cleanup"}}]}`.
  - Stage values are seconds, or the string `"unsupported"`.
  - On the reference backend, `seed` is `"unsupported"` and the top level
    carries `"image_status": "unsupported"`. The test asserts both.

**K12 — labels and reaping (IC-C6, IC-C4 `list_owned`).**
- Every resource-creating command carries the test-session label. That
  includes `docker create` as well as `docker run`, and any network or volume
  a test creates. The Docker test shim is extended to match.
- Resources carry a reserved project label, which `list_owned(project)` uses.
  A test checks that two projects' resources are kept apart.
- Reaping at session start removes only resources whose owning test session is
  verifiably dead. A live concurrent suite's resources are never touched.

### Slice 1 work split

1. `tester`: IC-C1..C3 (C2b marked milestone 3), IC-C5 conformance suite,
   IC-C6 isolation suite and its probe workload, IC-C7 test. Red.
2. `implementer` (deep tier: it is the seam everything hangs off): selection,
   recording, refusal, the protocol, the reference backend, the benchmark.
3. reviewer, then adversary (IC-C2/C3 decide what runs where), then the full
   suite, then merge.

## Slice 2 — provisioning: seed, fresh repository, import, alias re-seed

**Status:** approved by the user 2026-10-15. Milestone 2 of
`phase7-part2-containers.md` (IC-R1..R3). It builds on the K1 names only, never
on slice 1 internals.

### Decisions pinned by slice 2

- **D-S2.1 — scope: isolated attempts only.** The seed and the fresh clone
  apply to `executor.kind: isolated` (in this slice: through the reference
  backend). The legacy `local` and `docker` paths keep `clone --shared` and are
  unchanged (IC-C8 still holds). They are removed with the shared mode
  (IC-D7), not converted in this slice. Removing alternates from the shared
  Docker mode alone would not isolate it, because its other mounts stay.
- **D-S2.2 — the seed is the prepared input, frozen.**
  - What is authorised is what `Results.prepare()` actually produces for the
    attempt:
    - the base, the accumulated branch, the synthetic merge of the inputs and
      any RR recovery merge;
    - not a list re-derived from the node.
  - The seed records:
    - `checkout_commit` = the attempt's recorded `input_commit`;
    - the authorised root object ids;
    - the object format;
    - the seed format version.
- **D-S2.3 — import keeps its mechanism, gains validation before promotion.**
  - The host keeps reading the result through the existing guarded,
    host-owned quarantine (capture, H3, RP, generations).
  - It never runs Git against the attempt repository's configuration or
    hooks.
  - Slice 2 moves the ancestry and validity checks *before* any object reaches
    the host's object store. Today they run after the copy.
  - Whether the transfer itself is a bundle or a guarded object copy is the
    developer's choice.
- **D-S2.4 — no external payloads.** There is no recursive submodule
  checkout and no automatic LFS or network hydration. Gitlinks are kept as
  recorded commit ids. The seed carries complete history of the authorised
  ancestry, never a shallow cut.

### Behaviours

**IC-C9 — the seed (IC-R1).**
- The host builds, for an isolated attempt, a **self-contained** Git bundle:
  - no prerequisites;
  - exactly the objects reachable from the authorised roots (D-S2.2);
  - one ref pointing at `checkout_commit`.
  - No other refs, no alternates, no unreachable or unrelated objects.
- It is handed to the backend as an opaque `SeedRef` in `LaunchSpec.seed`.
  - The handle resolves only through the host's seed registry.
  - A backend never accepts a bundle path supplied by a run.
  - A missing, corrupt or unauthorised handle is `InvalidSpec(reason)`.
- **Cache.** Seeds are cached by a digest of the sorted authorised object ids,
  the object format and the seed format version.
  - An entry is published atomically, complete and verified. A reader never
    sees a partial bundle, and a failed build leaves no usable handle.
  - An entry in use by a provisioning in progress is never evicted.
  - An evicted entry is rebuilt from its pinned, still-authorised ids, or the
    provisioning fails `InvalidSpec("seed_unavailable: …")`. It never falls
    back to something else.
- Verified by:
  - the bundle's objects equal the reachable set of the authorised roots;
  - a sibling attempt's unmerged commit object is absent;
  - a second attempt with the same inputs reuses the entry;
  - two concurrent builds of one key yield one entry;
  - a corrupt entry is refused;
  - an unauthorised handle is refused.

**IC-C10 — the fresh repository (IC-R1).**
- The backend clones the seed into the stable guest repository path
  (`guest_repo`). The clone has its own object store and checks out
  `checkout_commit`. It has:
  - no alternates;
  - no shared `.git`;
  - no linked worktree;
  - no remote pointing at the host repository.
- Provisioning is idempotent by creation key (IC-C4).
  - A retry finds the same completed clone.
  - A retry safely removes and rebuilds an incomplete clone.
  - A retry never resets a running checkout.
- The reference backend implements `seed` from this slice on. K9's list of
  unsupported fields loses `seed`.
- Verified by: conformance tests on the reference backend for each point
  above, including absent sibling refs, objects and reflogs.

**IC-C11 — import with validation before promotion (IC-R2).**
- The result is read from the attempt's own repository. Before any object
  enters the host's object store, the host checks:
  - the designated result descends from the attempt's recorded `input_commit`;
  - every object it reaches is present, well-formed and bounded in total
    size, decompressed size and time;
  - the source has no alternates, and its config and hooks are never executed
    by the host.
- Objects not reachable from the designated result are discarded, not
  promoted.
- A failure leaves the host store untouched and the attempt's result
  quarantined with a reason, as capture reports today.
- New commits and blobs made by the attempt are of course accepted. Import is
  not restricted to seed objects.
- `nodes/<id>`, generations, capture, quarantine, RP restoration and
  retained-result replay behave as today. H1 holds with the attempt as the
  domain.
- Verified by:
  - a result descending from `input_commit` is imported;
  - a result not descending from it is refused, with the host store unchanged;
  - an unreachable extra object is not promoted;
  - a source with alternates is refused;
  - a hook or config in the source is never run;
  - an oversized object is refused within bounds.

**IC-C12 — alias re-seed (IC-R3).**
- Each activation of a session alias gets a fresh clone from its attempt's
  seed (IC-C9, IC-C10) at the alias's stable guest path. It carries:
  - no old objects;
  - no old refs;
  - no old reflogs.
  - The conversation HOME is kept.
- The existing sequence is kept:
  - `check_clean` archives a dirty checkout and **refuses** the activation,
    with the hold reported as today (TB2-R6);
  - cleanup is a separate recorded operation.
- Only when the checkout is clean and the predecessor is confirmed dead is the
  fresh clone staged and swapped in atomically. The old attempt's recovery
  artefacts stay host-side.
- The archive and the old `.git` are never mounted into the next activation.
- Alias exclusivity holds as today.
- **Stated limit:** the conversation itself remembers earlier inputs.
- Verified by:
  - a second activation sees none of the first's objects, refs or reflogs,
    and keeps HOME;
  - a dirty checkout holds the activation and swaps nothing;
  - a live predecessor blocks the swap.

**IC-C13 — migration and coexistence.**
- Upgrading never replaces an active legacy checkout or alias repository.
  An alias moves to a seeded repository at its next eligible activation under
  `isolated`. Dirty holds and frozen captures made before the upgrade are
  preserved.
- Legacy `local`/`docker` attempts keep their current provisioning.
- Verified by:
  - an alias activated under `local`, then reconfigured to `isolated`, is
    re-seeded at its next activation;
  - its earlier dirty hold survives the upgrade.

### Slice 2 precisions (advisor pass on the draft)

**P1 — seed authority and names.**
- Seeds live in `multiagents.provision.seeds`:
  - `SeedRegistry.build(attempt_id, input_commit) -> SeedRef`;
  - `SeedRegistry.resolve(ref, attempt_id) -> path`.
  - `SeedRef` is frozen and opaque, with `.id`.
- Only the host's admission path builds a seed, from the attempt's recorded
  preparation.
- A handle is bound to one attempt of one project. `resolve` with any other
  attempt id is refused, even when the cache entry behind it is shared.

**P2 — one root.**
- The authorised root set is exactly `[input_commit]`.
- `Results.prepare()` already folds the base, the accumulated branch, the
  inputs and any RR recovery into that commit.
- Anything not reachable from it is not authorised.
- The bundle therefore has one ref and one root.

**P3 — the durable record.**
- `launch_evidence.executor.seed` holds `{ref, checkout_commit, roots,
  object_format, seed_version, cache_key}`. It is written before `provision`,
  with the creation key.
- A retry or restart rebuilds the handle from this record, never from the
  node's current state.
- The cache entry for `cache_key` is pinned by a host ref
  `refs/multiagents/seeds/<cache_key>` while any unfinished attempt records
  it. It may be evicted afterwards.
- The handle's authorisation lasts as long as its attempt. Cache lifetime and
  object pinning are separate from that.

**P4 — bounds and failure classes.**
- Config `isolation.import`:
  - `max_bytes`, default 2 GiB;
  - `max_inflated_bytes`, default 8 GiB;
  - `timeout_seconds`, default 300.
  - Tests set small values.
- Failure classes:
  - Seed build I/O failure: TF class `launch` (retryable).
  - A corrupt cache entry is rebuilt once from its pinned id. If the rebuild
    fails, or pinned objects are missing, the attempt fails with
    `InvalidSpec("seed_unavailable: …")`, TF class `other`.
  - An invalid result (any IC-C11 check fails) is refused through capture's
    existing quarantine path, with reason codes `import_ancestry`,
    `import_bounds`, `import_malformed` and `import_alternates`. It is not
    retried.

**P5 — failure atomicity.**
- A refusal at validation leaves the host object store byte-for-byte
  unchanged.
- An interruption *during* promotion may leave validated, unreferenced
  objects in the host store (ordinary `gc` removes them). It never leaves a
  ref, a generation or a published branch.
- A refused result is kept for inspection in the attempt's retained artefacts
  (`remove(keep_artifacts=True)`), never in the host store.

**P6 — clone completion and retries.**
- A clone is complete only when a host-owned completion marker records it.
  The marker lives outside every path the run can write.
- A non-empty destination without a marker is removed and cloned again.
- Two provisionings under one key are serialised and yield one clone.
- The run can neither read the cache directory nor write the cache, the
  registry or the marker.

**P7 — the alias swap.**
- The alias record carries a durable swap intent:
  `{state: staging|swapping|done, old, new}`.
- After a restart:
  - a complete staged clone is swapped forward;
  - an incomplete one is discarded.
- The old repository is deleted only after `done` is recorded and its
  recovery artefacts are archived host-side.
- A failed staging or swap keeps the prior clean checkout and the HOME.
- Cleanup of a dirty alias is a host-side operation on the **old** repository.
  It runs before any re-seed and never inside a fresh activation.

**P8 — what "no old objects" means.**
- The fresh repository contains no object, ref or reflog that is not
  reachable from its own seed's root.
- Objects that activation one also had, and that are still authorised, may be
  present.
- **Stated limit:** HOME is kept, so files a previous activation left there
  remain, beyond what the conversation remembers.

**P9 — migration direction and Git payloads.**
- When a run returns to `local` or `docker` from `isolated`, it gets today's
  legacy checkout. The seeded repository is never reused by a legacy
  activation.
- Pending TF retries keep their recorded selection (IC-C2).
- The clone and the import run with:
  - system and global Git config ignored;
  - no hooks;
  - every `filter.*`, smudge and clean driver disabled.
- LFS pointers stay pointer files. Submodule directories stay empty, with
  their gitlinks recorded.

### Slice 2 work split

1. `tester`: IC-C9..C13 against the reference backend and the scheduler's
   public entry points (C10 extends the IC-C5 conformance suite). Red.
2. `implementer-deep`, then reviewer, then **adversary** (mandatory:
   provisioning and import decide what one attempt can read of another's),
   then the full suite, then merge.
