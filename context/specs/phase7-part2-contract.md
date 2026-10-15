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

**K13 — the scalar `executor:` form (IC-C1; 2026-10-15, after the combined
full suite).** `executor: <kind>` in `project.yaml` is shorthand for
`executor: {kind: <kind>}`, at every reader of that section. The shorthand
already exists: `executor: docker` is written by the sandbox-git tests. The
round-2 change that refused any non-mapping turned about 80 of those tests red.
- A null section is `{}`, as before.
- A list, a number or a boolean is refused with the same `ValueError`,
  naming the file.
- A string kind goes through the same validation as a `kind:` key. An
  unknown kind is refused as it would be in the mapping form. `isolated`
  given as a string selects isolated execution; it never falls back to local.
- Verified by: tests/test_sandbox_git_mounts.py and
  tests/test_sandbox_git_protect_project.py green unchanged, plus an IC-C1
  test of `executor: isolated` (scalar) selecting isolated execution and of
  `executor: [docker]` being refused.

**K14 — Unknown while resolving a predecessor (IC-C4, K6, K7; 2026-10-15,
adversary pass 2).** Before provisioning, `start` resolves the predecessor
recorded in `previous`. If `find`, `stop`, `inspect` or `remove` returns
`Unknown`, `start` does not loop on it:
- It stops promptly, within one backend round trip plus the stop grace, and
  provisions nothing. It surfaces an `Unknown(reason)` outcome naming the
  predecessor's creation key and the failing operation. No new resource is
  created and no new key is recorded.
- The caller treats that outcome as K6's unknown: the reservation and the
  liveness hold are kept, and the attempt is held, not failed and not
  retried in a tight loop.
- Resolution is retried only by a later activation or recovery pass, which
  starts again from the recorded predecessor key.
- A predecessor confirmed `Exited` or `Absent` and removed is resolved, as
  today.
- Verified by: tests/test_ic_adv2_predecessor_hang.py (the adversary's test,
  unchanged), plus a tester test that the held attempt keeps its
  reservation and records the predecessor key and the failing operation.

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

### Slice 2 clarifications (after the tester's first pass)

**Q1 — constructor.** `SeedRegistry(paths: ProjectPaths)`, built by the host
only.

**Q2 — wiring.**
- The reference backend is `ReferenceBackend(root, *, seeds=None)`.
- The host calls the K3 test factory as `factory(root, seeds=registry)`. Slice
  2 extends K3's signature this way.
- A backend given no registry refuses any non-empty `seed` with
  `InvalidSpec("seed_unavailable: no registry")`.

**Q3 — refusal type.**
- Registry failures raise `SeedError(code, reason)` from
  `multiagents.provision.seeds`. Codes are `unauthorised`, `seed_unavailable`
  and `corrupt`.
- The backend turns them into `InvalidSpec("<code>: <reason>")`.

**Q4 — where import reasons appear.**
- P4's reason codes appear in the `capture_error` detail of the existing
  `result_capture_failed` hold.
- An attempt counts as isolated by its recorded `launch_evidence.executor.kind`.

**Q5 — HOME.**
- K9's private HOME per invocation applies to resources with `attempt`
  lifetime.
- A `conversation`-lifetime resource (an alias) keeps one HOME across its
  invocations and activations, as IC-C12 requires.
- A change of kind gives a fresh HOME. The legacy HOME is never carried into
  an isolated resource; conversation continuity goes through the
  session-state handle.

**Q6 — stable guest path.** It cannot be observed on the reference backend,
so it is not tested there. It is checked under Docker in slice 3.

**Q7 — empty seed.**
- `LaunchSpec.seed` empty means no repository is created at `guest_repo` (an
  empty directory). Slice 1's conformance tests rely on this.
- Admission always sets a seed for a scheduler attempt under `isolated`. One
  without a seed is refused with `InvalidSpec("seed_required")`.

### Slice 2 work split

1. `tester`: IC-C9..C13 against the reference backend and the scheduler's
   public entry points (C10 extends the IC-C5 conformance suite). Red.
2. `implementer-deep`, then reviewer, then **adversary** (mandatory:
   provisioning and import decide what one attempt can read of another's),
   then the full suite, then merge.

## Slice 3 — the executor: one container per attempt, by container identity

**Status:** approved by the user 2026-10-15. Milestone 3 of `phase7-part2-containers.md`
(IC-R6..R9, IC-R15, and C2b of IC-C2). It builds on the K1 names and the slice 2
seed only, never on slice 1 or 2 internals.

### Decisions pinned by slice 3

- **D-S3.1 — scope.**
  - Slice 3 adds:
    - the real backend `docker` for `executor.kind: isolated`, implementing
      IC-C4;
    - Runner's handling of isolated attempts through that protocol and the
      recorded identity only: adoption, liveness, output, stop, retention,
      discard and orphan reporting (D-S1.1, C2b).
  - The legacy `local` and `docker` executors are unchanged (IC-C8), except
    where an IC-R15 finding lives in code both paths share (IC-C19).
- **D-S3.2 — what an isolated attempt cannot do yet, and says so.** These are
  refused with `Unsupported(<field>)`, i.e. TF class `other` (IC-C3), never
  silently left out:
  - a non-empty `credential_handle` (credentials are slice 4);
  - a non-empty `transport_handle` (the scoped RPC, slice 4);
  - a non-empty `session_state` (IC-R6 session hand-over moves to slice 4 with
    the provider-specific credential work, because which files hold a
    session is a provider adapter fact).
  - The container has **no network** until slice 4 (IC-R10).
  - So in slice 3 only workloads that need no credentials, no network and no
    RPC run isolated: the C6 probe provider and the test workloads. A real
    agent selected onto `isolated` is refused, visibly. That is the IC-D7
    opt-in working as intended, not a gap.
  - **The refusal predicate is the provider's declaration, not empty
    handles.** Each provider adapter declares whether it needs credentials,
    network or the RPC. On `isolated`, a provider that declares any of them
    is refused with `Unsupported(<the matching field>)` in slice 3. Only
    providers that declare none run: the probe provider and the test fakes.
    A provider with no declaration counts as needing all three.
  - **Steer** of an isolated attempt in slice 3 means stop, then a new
    attempt.
    - It keeps the authorised repository work, through the retained
      repository and the slice 2 alias re-seed.
    - It visibly starts without session resume: the run records
      `session_resume: "unsupported"`.
    - Session continuity on steer or reopen (IC-R6) is an acceptance
      obligation of the completed part, deferred to slice 4.
  - **Lifetime `conversation`** is `Unsupported(lifetime)` until slice 6
    (IC-D6). In slice 3, a resource carries exactly one invocation.
- **D-S3.3 — Docker is reached through one seam.**
  - The backend talks to Docker only through the `docker` CLI, found by a
    configured path (default: `docker` on the host scheduler's PATH).
  - A test can therefore substitute a fake CLI. Agents' containers have no
    Docker socket (unchanged), so implementers verify the backend against
    that fake and against the reference backend. The orchestrator runs the
    real-Docker tests on the host (`SG_TEST_DOCKER=1`) before merge and
    hands failures back.
- **D-S3.4 — the container is the supervisor (IC-D4).**
  - Restart policy `no` (ST1).
  - Every inspect, signal, stop and removal names the recorded immutable
    container id. Names and labels are used only by `find` and
    `list_owned`.
  - **The agent invocation is the container's main workload.**
    - `provision` creates the container but does not start it. Its command is
      fixed at creation: a host-supplied entry point that reads the
      invocation spec from a host-issued, read-only input directory and
      runs it.
    - `run` writes the invocation spec (argv, env, cwd, stdin file) into that
      directory, then starts the container.
    - The container's exit code is the invocation's, and its OOM evidence is
      the invocation's: one attempt resource, one invocation.
    - There is no `docker exec` for agent invocations. Exec-based turns are a
      slice 6 matter.
    - `inspect_invocation` therefore derives from the container's own
      record.
  - Nothing written inside the container (pid files, run-dir files, exit
    files) is ever trusted for liveness, exit, OOM or stop. This closes
    the C15 residuals for isolated attempts: forged `wrapper.pid` and
    `container.pid`, and descendants escaping through `setsid()`.

### Behaviours

**IC-C14 — the Docker backend (IC-R6, IC-R7).**
- `probe()` reports, without side effects:
  - the Docker daemon unreachable;
  - the configured image absent;
  - each missing prerequisite.
  - Any problem gives the IC-C3 block `isolation_unavailable: <reason>`.
  - There is no image build at launch.
- `provision(spec, key)` behaves as follows.
  - Order (F64): validate the spec and check the image **before any side
    effect**.
  - Then create one container whose name is derived from `key` and recorded
    before creation (K5 `creation_key`). It returns the immutable container
    id as the identity.
  - The container carries the reserved project label (K12), the owner, the
    lifetime and the spec digest.
  - Idempotency and `IdentityConflict` follow IC-C4. A crash between
    `docker create` and recording the id is recovered by `find(key)`.
- **What the container gets, and nothing else:**
  - **Writable:** the attempt's own private storage, under a host-issued root,
    at the guest paths for repository, HOME and temp (K1 defaults
    `/work/repo`, `/work/home`, `/work/tmp`). The repository is the slice 2
    fresh clone.
  - **Read-only:** the declared toolchain mounts. A `readonly` attempt also
    has its repository and Git metadata read-only.
  - **Never:** the host run directory, another attempt's storage, the
    project root, the Docker socket, or host network, pid or ipc namespaces.
  - **Process:** no added capabilities; `no-new-privileges`; no network
    (D-S3.2).
  - **User and ownership.** The container runs as the uid:gid of the host
    scheduler process, with no supplementary groups and umask 077.
    - The host creates the private storage owned by that uid, directories
      0700, so slice 2 capture and import read it as today.
    - Supported Docker mode: a rootful daemon without user-namespace
      remapping. In rootless or userns-remap mode, `probe` reports
      `isolation_unavailable: docker user namespace mode not supported`,
      until a later slice proves it.
  - **Slice 2's provisioning seam.** The slice 2 backend methods beyond
    IC-C4 are, at 3127f4f7: `checkout_path`, `attach_checkout` and
    `prepare_input`.
    - They become part of the protocol text as **K15**, written when slice 2
      merges, with their observable semantics.
    - The Docker backend implements them with identical semantics. The
      conformance suite covers them on both backends.
  - The prompt reaches the invocation as its stdin input file (IC-C4).
- `run(identity, invocation, key)` starts the single agent invocation. A
  second one is `AlreadyInvoked`, as in IC-C4.
- `inspect` and `inspect_invocation` report from Docker's own records:
  - state and exit code;
  - `oom` from Docker's OOM evidence for the container;
  - `finished_at`.
  - A daemon error or timeout is `Unknown(reason)`, never `Absent`.
- `output(identity, invocation_id, stream, offset)`:
  - stdout and stderr are produced independently of any host process, so
    output keeps being produced while the scheduler is down;
  - they are kept outside the agent's write reach;
  - they can be replayed from any offset up to what was written, after a
    scheduler restart and after a machine reboot, until `remove`.
  - If Docker's log driver is the mechanism, the container's logging
    configuration is pinned at creation with no rotation and no size cap
    that drops data. Silent loss is a defect.
- `stop(identity, grace)`:
  - graceful signal, then forced termination after `grace`;
  - it ends every process in the container, including descendants that
    left the process group or session;
  - it is confirmed by a subsequent `inspect` reporting `exited`;
  - otherwise it returns `Unknown`.
- `remove(identity, keep_artifacts)`: as IC-C4.
- `list_owned(project)`: the containers carrying this project's reserved label.
- **Resources applied at create time:**
  - a per-run hard memory limit;
  - a pids limit;
  - an equal CPU weight;
  - an optional `cpus` cap, off by default;
  - all from `executor.isolated.resources` in `project.yaml`, with stated
    defaults (hard memory 6 GB, pids 4096, CPU weight equal, no cap).
  - Admission against a memory budget is slice 5 (IC-R5).
  - Values are validated (F32): 0 and negatives are config errors naming the
    key; "unset" means the default.
- Verified by:
  - the IC-C5 conformance suite against this backend under
    `SG_TEST_DOCKER=1`, including the `ic_milestone(3)` containment cases
    (escaped descendants, sibling reads);
  - the same suite against the fake CLI for command construction and outcome
    parsing, without Docker.

**IC-C15 — Runner on isolated attempts: adoption and output without loss or
duplication (IC-R7, C2b).**
- **Launch evidence (K5) is the only authority.** For an isolated attempt,
  every Runner path consults the recorded `kind`, `backend`,
  `backend_version`, `creation_key`, `identity` and `invocation_id`, and
  never the current config. Those paths are adoption, liveness, output,
  stop, steer, cancel and cleanup. None of them falls back to pid files,
  process groups or the single project container.
- **Adoption after a host restart** (scheduler or CLI). For each launched
  isolated attempt:
  - `running` → resume following output from the durably recorded
    consumed offset of each stream. The run's output shows every byte once:
    no loss, no duplication.
  - `exited` → collect the exit evidence, finish as a normal end.
  - `absent` → the resource is gone; the attempt ends with TF class `launch`,
    as a lost launch today, and nothing is retried silently in another kind.
  - `unknown` → the existing liveness hold (K6); reservation kept.
  - No identity yet → resolve `find(creation_key)` first.
- **C2b**, as already written in IC-C2: adoption uses the recorded backend.
  - A recorded backend version that the backend does not declare compatible
    gives `unknown` and a hold.
  - A record without executor evidence is unknown.
- **The replay rule** (no loss, no duplication).
  - Runner appends consumed output to the host run output **in whole lines
    only**. A trailing partial line waits for its newline or for the end
    of the stream.
  - After each append, it records a checkpoint durably, per stream: the
    backend offset just past the last whole line consumed, and the length
    of the host output at that point.
  - On recovery, it first truncates the host output back to the
    checkpointed length, then replays from the checkpointed backend offset.
  - The same rule holds for stderr.
  - Where the checkpoint is stored is the developer's choice: the attempt
    journal, or the host-owned run directory.
- **The five launch boundaries (IC-R6)**, recovered after a crash at each:
  1. *Before creation.* The creation key is recorded and `find(key)` gives
     `Absent`. Nothing exists, so the attempt is treated as an unlaunched
     claim under today's rule.
  2. *After creation, before start.* `inspect` gives `Created`. No invocation
     ever ran. The resource is removed, without keeping artifacts, and the
     attempt is treated as an unlaunched claim. A created container is never
     started after the fact by recovery.
  3. *After start, before `invocation_id` is recorded.* Resolve through the
     recorded `invocation_key` and the container's state, then adopt as
     `running` or `exited`. The invocation is **never run a second time**.
  4. *After exit, before the evidence is recorded.* Drain output to the
     end, record the exit code and OOM durably, and only then remove or
     retain.
  5. *After capture.* A committed capture (its generation is recorded) is
     recognised. Recovery finishes cleanup and retention without producing
     another generation.
- Verified by:
  - an injected crash at each of the five boundaries, on the reference
    backend and under `SG_TEST_DOCKER=1`;
  - restart tests on the reference backend, which kill and restart the
    scheduler while an attempt is running, after it exited, and after its
    resource was removed;
  - a crash between a host output append and its checkpoint, including a
    partial line, showing output byte-identical to an uninterrupted run;
  - output byte-identical to an uninterrupted run;
  - the same adoption tests under `SG_TEST_DOCKER=1`;
  - the existing C2b tests (`ic_milestone(3)`).

**IC-C16 — stop, cancel and steer (IC-R7).**
- Cancel → `stop(identity, grace)`, where `grace` is today's cancellation grace.
  - Compute reservations are released once `exited` or `absent` is
    confirmed (K6).
  - `Unknown` keeps the reservation and the liveness hold.
- Steer of an isolated attempt follows D-S3.2: stop, then a new attempt.
- **A config change applies to the next attempt.**
  - It never stops, recreates or re-adopts running isolated containers.
  - The legacy "config changed, recreate the project container" path does not
    apply to them.
  - Mount-drift detection is a legacy-container concept and is not run for
    isolated attempts.
- Verified by:
  - cancel while running, including a workload that forks a `setsid()`
    child: after confirmation, no process of the attempt survives
    (`SG_TEST_DOCKER=1`);
  - a workload that writes forged `wrapper.pid` and `container.pid` files:
    stop and liveness are unaffected;
  - a config change with a running isolated attempt: the attempt finishes
    normally, and the next attempt uses the new config.

**IC-C17 — exit, OOM and resource failures (IC-R9).**
- `Exited(code, oom=True)` is recorded in the run's cause as OOM evidence
  and classified by TF in the class today's OOM evidence maps to (D1,
  LN-R6).
- A non-zero exit without OOM keeps today's classification.
- The attribution is per attempt: the container is the attempt's own, so no
  shared-container occupancy reasoning applies.
- Verified by: a workload exceeding a small hard memory limit under
  `SG_TEST_DOCKER=1` (cause shows OOM, TF class as stated); the fake CLI for
  the parsing.

**IC-C18 — retention, discard and orphans (IC-R6, IC-R8).**
- **Retention.** When an attempt ends, `remove(identity, keep_artifacts=True)`
  keeps the **exited container itself** and the attempt's private storage,
  including the repository.
  - Docker's records stay authoritative, so later `inspect` and `output`
    read them as before.
  - No process keeps running.
  - Nothing is archived elsewhere, and there is no copy step that a crash
    could interrupt.
  - An attempt whose capture failed is retained the same way, never
    discarded.
  - What stays lasts until the node is discarded, disposed, or merged and
    cleaned up. Retention periods and quotas are slice 5.
- **Discard** (`discard_agent`, `dispose_node`, merge cleanup) removes the
  container and the private storage without `keep_artifacts`.
  - It runs after capture, never before.
  - If removal returns `Unknown`, the node is reported, not marked clean.
- **Orphans.** An orphan is a container from `list_owned(project)` with:
  - no recorded attempt; or
  - a recorded attempt whose node was already discarded.
  - Orphans are **reported**, never deleted automatically:
    - in `scheduler_status` (a list with container id, name, labels and
      created time);
    - by a CLI listing.
  - Removal is an explicit user command naming the container id.
- Verified by:
  - retention keeps output, exit and repository readable after the end;
  - discard leaves nothing for that attempt in `list_owned`;
  - a container created with the project label but no record is reported,
    not deleted;
  - another project's containers are never listed (K12).

**IC-C19 — the deferred executor findings (IC-R15).**
All eight findings were checked on main bfc3a230; all are still present.

- **Fixed in slice 3, in code that the isolated path shares.** Each has a
  failing test named after the finding.
  - **F34** — `prepare_home` (`executor/base.py`, called from Runner for every
    kind). When the target is a file, or its parent is not writable, it
    raises an uncaught exception. Instead, it must return a launch failure
    naming the path: TF class `launch` for isolated attempts, today's class
    otherwise.
  - **F70** — the generic `Handle.stop` (`executor/base.py`) re-resolves a
    pid's process group after the timeout and SIGKILLs it. It must not
    signal a group unless it has proven that the group is still the one it
    started. Proof means a start-time or pidfd identity; a pid alone is not
    proof.
    - Isolated attempts never use this handle (IC-C16), and a test proves
      that.
    - The fix covers `local`, which stays after IC-D7.
- **Hold by construction on the isolated path, each proven by a test;
  reconsidered for the legacy code.** The legacy `docker` code keeps the
  defect: changing it would break IC-C8, and IC-D7 removes it. When this
  part merges, each is set to `reconsidered: legacy shared mode, removed by
  IC-D7` in the ledger.
  - **F31** — the isolated spec never drops a declared mount silently.
    - A declared toolchain path that is missing is `InvalidSpec` naming it.
    - The attempt's own storage is created before `provision`.
  - **F32** — `executor.isolated.resources` rejects 0 and negatives
    (IC-C14).
  - **F35** — every isolated host path is resolved, symlinks included, and
    must lie under a host-issued root (IC-C4). Toolchain mounts are always
    read-only.
  - **F64** — `provision` checks the image before any side effect (IC-C14).
  - **F65** — an isolated attempt writes no credential file in slice 3
    (D-S3.2). Slice 4 carries the property "no credential temp file
    survives an interruption" as an acceptance test.
  - **F69** — the isolated path takes no lock whose deletion is decided by a
    pid check. A test asserts it.

**IC-C6 and the milestone-3 tests.** Under `SG_TEST_DOCKER=1`, these turn green
in slice 3:
- the `ic_milestone(3)` isolation clauses:
  - worktree, HOME, temp and run directory;
  - secrets by content;
  - refs and objects;
  - the `readonly` attempt;
  - the deliberate handover;
  - the session label;
- the C5 containment cases;
- the C10 case;
- the C2b tests.

Milestones 4 to 6 stay red.

### Slice 3 work split

1. **`tester`:** the fake `docker` CLI under `tests/support/` (scriptable
   outcomes, records argv), the IC-C14..C19 tests, and restart and adoption
   tests on the reference backend. Red.
2. **`implementer-deep`:** the Docker backend, and Runner's isolated paths.
   They are cross-cutting and touch supervision invariants.
3. **The orchestrator:** the real-Docker runs on the host (`SG_TEST_DOCKER=1`).
   - **An early smoke run** as soon as the implementer reports create, run,
     inspect and output working. It does not wait for the whole slice.
   - **The full acceptance run** before review.
   - Failures go back to step 2 with their output.
   - **Real-Docker acceptance is defined by the tests, independently of the
     fake.** It covers:
     - output produced while the scheduler is down;
     - non-zero exit;
     - OOM;
     - crash recovery at creation and at start;
     - private-storage permissions through a real capture and a discard;
     - retention.
   - Matching argv in the fake is never acceptance.
4. **reviewer**, then **adversary** (mandatory: C15 cases and container
   boundaries), then the full suite, then merge.
