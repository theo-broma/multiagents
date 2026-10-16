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

**K16 — own launch evidence versus a predecessor's (IC-C4, K14, IC-C12/P7,
IC-C15; 2026-10-15, adversary pass 3, finding F1).**

When recovery or `start` finds launch evidence with a `creation_key` but no
`invocation_id`, the response depends on whose evidence it is.

- **Telling them apart.** Evidence is the attempt's own or a predecessor's
  according to the host-recorded attempt identity. It is never inferred from
  which launch fields are missing.
- **Own evidence** (the attempt was interrupted, and the scheduler is
  recovering it):
  - `Absent`, or a confirmed never-invoked `Created`, follows IC-C15
    boundaries 1 and 2. The `Created` resource is removed. The attempt
    returns through the normal unlaunched-claim recovery; the recovery
    path never provisions or starts it again.
  - Before the attempt's storage is released or deleted, its P7 swap record
    is settled: the swap is completed or rolled back, HOME is preserved, and
    the old checkout is archived.
  - A `Running` or `Exited` invocation is adopted or finalised (IC-C15), and
    never run again.
- **A predecessor's evidence** (TF retry, relaunch, steer): K14 applies
  unchanged. The predecessor is resolved, then stopped and removed, or the
  attempt is held on `Unknown`. The new attempt then provisions under its
  own fresh key and never reuses the predecessor's.
- **Any uncertainty holds** (K6). Recovery never guesses.
- Backend-level idempotency (IC-C4: the same key and the same digest return
  the same identity) still holds. It allows an operation to be repeated; it
  does not oblige Runner to resume an abandoned claim.
- Verified by:
  - tests/test_ic_c4_predecessor.py and tests/test_ic_adv2_predecessor_hang.py,
    green on the combined slices;
  - test_ic_c12_p7_the_swap_recovers_across_a_scheduler_restart, still
    green: it requires activation on a fresh repository with HOME preserved,
    not the same attempt id;
  - the IC-C15 boundary tests (slice 3).

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

### Slice 3 clarifications (2026-10-15, after the tester's first pass)

**K17 — names the slice 3 contract left unsaid (D-S3.2, D-S3.3, IC-C14,
IC-C18; the tester's NEED_INFO on nd-8a27cb3a).**

**K17.1 — the Docker backend and its configuration (D-S3.3, IC-C14).**
- Constructor: `multiagents.executor.lifecycle.DockerBackend(root: Path, *,
  seeds=None, cli="docker", image="multiagents/workspace:latest", resources=None)`, next to
  `ReferenceBackend`.
  - Construction has no Docker or filesystem side effect.
  - It implements IC-C4 and K15, and has `.name == "docker"` and a non-empty
    `.version`.
- Selection: `executor.kind: isolated` with `executor.isolated.backend:
  docker`, the default.
- Docker settings: `executor.isolated.docker.cli` and
  `executor.isolated.docker.image`, defaulting to the constructor defaults.
  - `cli` is an executable name resolved through the host scheduler's PATH,
    or an absolute path to an executable. It is never a shell command.
  - Nothing is inherited from the legacy `executor.docker` block.
- Resources: `executor.isolated.resources`, with these keys and defaults:
  - `memory: "6g"`;
  - `pids: 4096`;
  - `cpu_shares: 1024`;
  - `cpus: null`, meaning no cap.
- Rules for resource values:
  - An omitted or null value takes its default.
  - Zero, a negative or a malformed value is a config error that names the
    full key (F32).
  - An explicit `LaunchSpec.resources` entry overrides the backend default.
- `probe()` reports unavailable prerequisites. A launch never builds an
  image.
- Verified by:
  - an import and constructor test;
  - config validation tests;
  - fake-CLI tests of selection and flags;
  - the real-Docker conformance suite.

**K17.2 — the provider's declaration of what it needs (D-S3.2).**
- A provider declares `needs` in its `providers.yaml` entry, alongside
  `spawn` and `auth`:
  ```yaml
  needs:
    credentials: false
    network: false
    rpc: false
  ```
- Defaults and validation:
  - Each value is a boolean.
  - An omitted member is `true`. An omitted block means all three are `true`.
  - Null, a non-mapping, an unknown member or a non-boolean is a config
    error naming the provider and the field.
  - The block follows normal provider layering and `extends` inheritance.
- On `isolated`, slice 3 checks the declaration before any provisioning or
  provider preparation. Precedence is credentials, then network, then RPC:
  - `credentials: true` is refused with `Unsupported("credential_handle")`;
  - `network: true` is refused with `Unsupported("network")`;
  - `rpc: true` is refused with `Unsupported("transport_handle")`.
  - Each refusal is TF class `other`.
- Declaring all three false never waives an actual need. A non-empty
  `credential_handle` or `transport_handle` is still refused (K9).
- Verified by:
  - parsing, inheritance and default tests;
  - one test per refusal, showing no resource and no provider preparation;
  - a provider that declares all three false runs.

**K17.3 — K15, the slice 2 provisioning seam (IC-C4).** Both backends expose
these async methods:
- `checkout_path(identity) -> Path | Absent | Unknown`:
  - returns the absolute host path of the resource's current repository,
    including after retention;
  - a discarded or absent resource gives `Absent`.
- `attach_checkout(identity, checkout: Path, home: Path | None = None) ->
  ResourceIdentity | Absent | InvalidSpec | Unknown`:
  - associates a host-authorised checkout, and optionally a HOME, with the
    resource before invocation;
  - later execution sees them at the declared guest paths, and
    `checkout_path` returns the attached checkout;
  - an omitted `home` keeps the current association;
  - an identical repeated attachment is idempotent;
  - it preserves the resource identity, and attaching never starts a
    process;
  - a running resource is refused, and uncertain state gives `Unknown`.
- `prepare_input(identity, source: BinaryIO) -> str | Absent | InvalidSpec |
  Unknown`:
  - snapshots the already-framed input bytes into host-controlled storage;
  - returns an absolute path accepted by `InvocationSpec.stdin`;
  - leaves `source` open, and later changes to `source` cannot change the
    snapshot;
  - the snapshot lasts until discard.
- Error outcomes:
  - A path not authorised for that resource gives `InvalidSpec`, and no
    association changes.
  - A missing resource gives `Absent`.
  - Operational failures follow K7.
  - These typed outcomes are binding, even where the slice 2 code at
    3127f4f7 raises instead.
- Verified by: conformance on both backends covering attachment, the actual
  guest content, stdin bytes, retention and discard, invalid paths and
  uncertain state.

**K17.4 — reserved labels (K12, IC-C14).** A Docker container carries:
- `multiagents.project`, supplied by the host;
- `multiagents.owner`, which is `spec.owner` (the attempt id in slice 3);
- `multiagents.lifetime`, which is `spec.lifetime`;
- `multiagents.creation_key`, the provision key;
- `multiagents.spec_digest`, the IC-C4 canonical digest.

Rules:
- The last four are generated by the backend and are excluded from the
  digest of `spec.labels`.
- Supplying `multiagents.owner`, `multiagents.lifetime`, `multiagents.creation_key` or
  `multiagents.spec_digest` through `spec.labels` gives `InvalidSpec`.
  `multiagents.project` is host-supplied through `spec.labels`, as in K12.
- The test-session label `multiagents.test.owner` is preserved.
- Verified by: tests of inspected labels, digest consistency, reserved-label
  refusal and project filtering.

**K17.5 — orphan reporting (IC-C18).**
- Root `scheduler_status` gains `orphans` and `orphans_error`.
  `multiagents scheduler orphans` prints the same two fields as JSON, from
  the same discovery result. Existing status fields are unchanged.
- `orphans` is a list sorted by `container_id`. Each entry is
  `{container_id, name, labels, created_at, project, attempt_id, reason}`:
  - `labels` is the full label mapping;
  - `created_at` is an RFC 3339 string;
  - `attempt_id` may be null;
  - `reason` is `unrecorded_attempt` or `discarded_node`.
- Discovery reconciles recorded identities and creation keys before
  classifying, so a container in the window between create and recording
  its id is not reported.
- Successful discovery gives `orphans_error: null`. Failed or incomplete
  discovery gives `orphans: null` and `orphans_error: {reason}`, never an
  empty list.
- A scoped, non-root `scheduler_status` omits both fields.
- Listing never removes anything.
- Verified by:
  - parity between the CLI and status output;
  - both reasons;
  - retained resources are excluded;
  - the creation window;
  - project filtering;
  - daemon failure;
  - scoped visibility.

**K17.6 — orphan removal (IC-C18).** `multiagents scheduler remove-orphan
CONTAINER_ID` is a host-user command. No agent RPC exposes it.
- It accepts only an exact, full container id. Names and prefixes are
  refused.
- It rechecks project ownership and orphan status, resolving recorded
  creation keys first.
- A confirmed orphan is stopped with the cancellation grace, then removed
  with `remove(identity, keep_artifacts=False)`. Storage is deleted only on
  backend authority, never at a path taken from a label.
- Uncertainty stops any further deletion, and the container stays reported.
- Output:
  - success is `{ok: true, container_id, state: "absent"}`, and an
    already-absent container also succeeds;
  - failure is `{ok: false, container_id, error, reason}`, with `error` one
    of `not_orphan`, `wrong_project`, `unknown` or `invalid_id`, and a
    non-zero exit.
- Verified by:
  - removing an orphan;
  - stopping a live orphan;
  - refusing a retained or non-orphan container;
  - refusing another project's container;
  - daemon uncertainty;
  - a repeated removal.

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

## Slice 4A — the scoped RPC and session continuity in isolated attempts

On the advisor's recommendation (2026-10-15), slice 4 is split in three:
- **4A** (this slice): the scoped RPC reaches an isolated attempt, and a
  session survives steer and reopen.
- **4B**: a network per attempt, with authenticated gateways (IC-R10).
- **4C**: credentials by provider, with renewal and revocation (IC-R4), plus
  the live resume checks that need both credentials and a network.

Until 4B and 4C land, K17.2 still refuses `needs.credentials` and
`needs.network`. After 4A, a provider that declares only `needs.rpc` is no
longer refused.

### Decisions pinned by slice 4A

- **D-S4A.1 — one private RPC endpoint per attempt (IC-R11).**
  - **Location.** The host creates a host-owned directory per attempt that
    holds one Unix socket. It is mounted read-only into the container at
    `/run/multiagents/rpc/`, and the socket is `rpc.sock`. The mount is the
    directory, never the socket inode, so a recreated socket stays visible.
    The guest gets `MULTIAGENTS_RPC_SOCKET=/run/multiagents/rpc/rpc.sock`.
  - **Principal.** The listener holds the attempt's host-recorded principal,
    with the fields today's authorisation uses: `subject` (the attempt's
    run id), `root: false` and `permissions`. Scope follows the subject's
    current subtree, computed per request as today.
  - **Authorisation.** Each request goes through the existing path:
    permission checks, target checks on child nodes and runs, and mutation
    deduplication. Deduplication is unchanged: mutations only, keyed by
    `(subject, request_id)`, and a reused id with a different payload is
    refused. A new attempt has a new subject.
  - **Envelope.** The request envelope is today's, without the bearer
    token. A request carrying a token, or any field that asserts the
    caller's identity, is refused with `forbidden`. Target arguments, such
    as a child node id, keep today's authorisation.
  - **Replies and reconnects.** Replies use today's format. A client that
    loses its connection may reconnect to the same path. Deduplication makes
    retrying a mutation safe.
  - **Never mounted:** the scheduler store, the root socket directory, or
    another attempt's endpoint directory. There is no RPC over TCP or a
    network in 4A.
- **D-S4A.2 — the endpoint's lifetime follows the attempt's authority.**
  - **Grant.** The endpoint is granted before the invocation starts, in the
    IC-C15 boundary order.
  - **Revocation.** It is revoked when the attempt ends, is cancelled, or is
    replaced by a new attempt. Revocation is one ordering point in the
    scheduler's store:
    - a request committed before it keeps its effects;
    - a request ordered after it is refused and has no effect, deferred
      effects included;
    - accepted connections are then closed, and new ones are refused.
  - **Every request is checked.** Authority is checked on each request, not
    only at connect.
  - **Scheduler restart.** The listener is recreated for every attempt that
    is still authorised, at the same path, without restarting the
    container. An attempt whose authority cannot be confirmed gets no
    listener and is held (K6).
  - **Adoption.** Adoption (IC-C15) never mints a new principal. It
    recreates the listener for the recorded one.
- **D-S4A.3 — session continuity is a provider declaration.**
  - **Schema.** A provider may declare, in `providers.yaml`:
    `session: {mode, path, resumed_event, resume_deadline_seconds}`.
  - **Modes.** `mode` is one of:
    - `home`: the session lives at `path`, relative to the conversation
      HOME;
    - `export`: after the attempt, the host copies `path` out of the
      attempt HOME into conversation storage, and copies it back before
      the next attempt;
    - `none`: no resume.
  - **Precedence.** An explicit `session` block wins. Without one, a
    provider that already declares `session_store` is read as
    `mode: export`, with `path` taken from `session_store.dir` and
    `session_store.glob`, and `forbidden_profile` kept as a forbidden
    pattern. Any other provider is `none`.
  - **Resume evidence.** `resumed_event` names the stream event that proves
    the provider accepted the resumed session. Its session id must equal
    the recorded one. `resume_deadline_seconds` defaults to 120.
  - **Layering.** The block follows provider layering and `extends`.
  - **Ownership.** Conversation storage belongs to the alias binding, or,
    for an ordinary node, to the node (a per-node conversation store,
    created on its first isolated attempt). It is reached through a
    host-issued handle, never a path a run supplies.
  - **Isolation from secrets.** No credential, proxy credential or endpoint
    material is ever projected into it. A legacy profile is never imported
    wholesale.
- **D-S4A.4 — steer and reopen resume when they can.**
  - Steer of an isolated attempt is a stop followed by a new attempt, as in
    slice 3. Reopen, through `relaunch_node` or a new loop round, also
    starts a new attempt.
  - The new attempt resumes the recorded session only when the provider's
    mode allows it and every check in today's alias binding holds:
    - provider, family, account and model compatibility;
    - availability and cooldown;
    - no other attempt of the conversation is claimed, launched, captured
      or suspended.
    For an ordinary node's conversation, the same checks and the same
    locking apply.
  - **One confirmed-live writer per conversation.** A conversation whose
    previous attempt is not confirmed stopped is not resumed, and the new
    attempt is held (K14 applies to the predecessor).
  - **Ordering.** An `export` copy completes, and is recorded, before that
    attempt's resources may be removed (IC-C18).
  - **Forced fresh start.** `relaunch_node(..., new_session=[...])` is the
    existing root-only operation that forces a fresh session. It renews the
    binding, keeps the old session store as an archive until the
    conversation is disposed, and records `fresh`.
- **D-S4A.5 — what is recorded.** The run records `session_resume`, with
  one of these values:
  - `fresh`: no session was recorded for this conversation, or a new
    session was forced.
  - `resumed`: the provider emitted `resumed_event` with the recorded
    session id before the deadline.
  - `unsupported`: the mode is `none`, or a compatibility check failed. The
    reason is recorded.
  - `failed`: a session was recorded but its store is missing or corrupt,
    or the provider did not confirm it before the deadline, or the provider
    rejected it. The reason is recorded.

  A `failed` resume never silently continues as fresh. The attempt ends
  with TF class `other`. `unsupported` caused by a compatibility change
  holds the attempt, unless it was relaunched with `new_session`.

### Behaviours

**IC-C20 — the private RPC endpoint.**
- An isolated attempt whose provider declares only `needs.rpc: true` runs.
  It reaches the scoped RPC only through `MULTIAGENTS_RPC_SOCKET`.
- Every RPC operation open to that agent today works through the endpoint,
  with the recorded principal and with deduplication unchanged.
- These requests are refused with `forbidden` and change nothing:
  - a request carrying a token or a caller-identity field;
  - a request targeting a node or run outside the subject's scope.
- The endpoint directory is mounted read-only and contains only the socket.
  From the guest, no other endpoint and no scheduler store is reachable.
- Verified by:
  - an RPC round trip from inside the attempt, on the reference backend and,
    under `SG_TEST_DOCKER=1`, on Docker;
  - forged-identity and out-of-scope requests refused;
  - a repeated request id deduplicated, and a reused id with a new payload
    refused;
  - guest-side checks of what is visible.

**IC-C21 — the endpoint's lifetime and revocation.**
- After the attempt ends, is cancelled or is replaced:
  - a new connection is refused;
  - an open connection is closed;
  - a request ordered after revocation is refused and has no effect.
- After a scheduler restart, an authorised running attempt reconnects at the
  same path and its requests succeed, with no container restart. An attempt
  whose authority cannot be confirmed is held and gets no listener.
- Crash tests run at each boundary: grant, start, end and revoke. A crash
  after grant but before start leaves no live endpoint once recovery
  completes.
- An endpoint directory with no recorded attempt is an orphan:
  - it is reported in `orphans` (K17.5) with reason `orphan_endpoint`;
  - `container_id`, `name`, `labels` and `created_at` are null, and
    `endpoint_id` names the directory;
  - it is never served;
  - `remove-orphan` accepts the `endpoint_id` with the K17.6 outcomes.
- Verified by: each case with an observable outcome.

**IC-C22 — the session declaration and its storage.**
- The `session` block is validated. Each of these is a config error naming
  the provider and the field:
  - an unknown mode;
  - `path` missing for `home` or `export`, or absolute, or containing `..`;
  - a non-mapping;
  - a non-positive deadline.
- Conversation storage survives the attempt's discard. It is removed when
  the conversation is disposed, either by alias disposal or by node
  disposal.
- Before launch, the host checks that nothing it projects into the attempt
  HOME or the conversation storage matches the provider's forbidden
  patterns. At export, a file matching those patterns is not copied. The
  export is then recorded `quarantined` with the file names, and the next
  resume is `failed`.
- Verified by: config validation, storage lifetime, the forbidden-pattern
  refusal at export, and the `session_store` precedence rule.

**IC-C23 — resume on steer and reopen.**
- Steer of a running isolated attempt whose provider has mode `home` or
  `export`: the next attempt resumes the session and records `resumed`.
- The same holds for a node relaunch and for the next round of a loop alias.
- Mode `none` records `unsupported`.
- A changed provider, family, account or model records `unsupported` with
  the reason, and the attempt is held unless it was relaunched with
  `new_session`. With `new_session`, it records `fresh` and keeps the old
  store archived.
- A missing or corrupt store, a provider rejection, or no `resumed_event`
  before the deadline: `failed`, and the attempt ends (D-S4A.5).
- If the predecessor is not confirmed stopped, there is no resume and the
  attempt is held (K14).
- With two concurrent reopens of one conversation, exactly one attempt runs.
  The other is held.
- Verified by:
  - each case, with the probe provider declaring each mode and emitting or
    withholding `resumed_event`;
  - the tester updates the slice 3 `session_resume: "unsupported"` test so
    that providers with a mode expect the new outcomes.

**IC-C24 — migration (Q5 still holds).**
- An alias moving from `local` or `docker` to `isolated` gets a fresh HOME
  (Q5). Only the declared session payload, `path` under `session`, is
  copied into conversation storage, through a host-issued handle.
  Forbidden patterns are excluded.
- If the legacy binding recorded a session id and its payload is found, the
  attempt resumes. If the id was recorded but the payload is missing, the
  outcome is `failed`. With no recorded id, the outcome is `fresh`.
- Runs recorded before 4A with `session_resume: "unsupported"` are not
  rewritten.
- Verified by: a legacy alias with a payload migrates and resumes; one with a
  recorded id and no payload fails; one without a recorded id starts fresh;
  a forbidden file is never copied.

### Slice 4A clarifications

**K18 — where the endpoint boundaries sit, and where endpoints live
(IC-C21; the tester's NEED_INFO on nd-d5f03257).**
- **K18.1 — ordering against the backend calls.** The endpoint boundaries are
  fixed relative to the backend operations, so the existing K3 crash points
  reach them and no new test seam is needed:
  - The grant happens after `provision` returns and before `run` is
    called. So a crash at `provision_done` is before the grant, and a crash
    at `run_entry` is after the grant and before the start.
  - The revoke happens after the attempt's end is recorded and before
    `remove` is called. So a crash at boundary 4 (the exit is seen but its
    evidence is not recorded) is before the revoke, and a crash at
    `remove_entry` is after it.
  - Cancel and replacement revoke before calling `stop`.
  - In every case, recovery leaves no live endpoint for an attempt that is
    not running, and recreates the endpoint for one that is (D-S4A.2).
- **K18.2 — the location.** Endpoint directories are host-owned, at
  `ProjectPaths(root).scheduler / "endpoints" / <endpoint_id>`. The
  `<endpoint_id>` is the attempt id.
  - A directory there with no recorded attempt is an orphan endpoint
    (IC-C21).
  - A test may create one to simulate an orphan.
  - Nothing else in the project is ever served as an endpoint.
- Verified by: the IC-C21 crash cases at `provision_done`, `run_entry`,
  boundary 4 and `remove_entry`, and the orphan-endpoint case using that
  location.
- **K18.3 — the endpoint path length.** Approved by the user on 2026-10-16.
  - The endpoint must be served, and reachable from the guest at
    `/run/multiagents/rpc/rpc.sock`, whatever the length of its absolute
    host path. Linux limits a Unix socket address to 107 bytes, and a host
    path under a long project root can exceed that.
  - A path that is too long is never a silent failure. If the endpoint
    cannot be served, the attempt does not start, and the node is held with
    a reason that names the endpoint.
  - The K18.2 location is unchanged.
  - Verified by: an endpoint whose absolute host path exceeds 107 bytes
    serves a round trip on the fake backend.

**K19 — a retry of an isolated attempt keeps its recorded settings (TF,
IC-C15).** Approved by the user on 2026-10-16.
- A TF retry of an attempt recorded as `isolated` reuses the executor
  settings recorded for that attempt, image included, and not the current
  configuration.
- If admission refuses the retry, the node is held with a reason naming the
  refusal. It never waits silently.
- Verified by: a retry after the configured image changed runs on the
  recorded image; a refused retry admission ends held with its reason.

### Slice 4A work split

1. `tester`: IC-C20 to IC-C24. The probe provider is extended to declare
   `needs.rpc`, each session mode and `resumed_event`. Docker cases are
   opt-in.
2. `implementer-deep`, then the reviewer, then the `adversary`. The endpoint
   is a trust boundary, so the adversary covers forged principals,
   revocation races and restarts.
3. My real-Docker runs, then the full suite, then the merge.

Ordering:
- The tester starts once slice 3's implementation is approved, so that the
  names it builds on exist.
- Implementation starts after slice 3 merges.

## Slice 4B — per-attempt networks, authenticated egress and the auth gateway

**Status:** approved by the user on 2026-10-17, with choices (1) redact, (2) block private destinations and (3) interruptions allowed. Extends slices 3 and 4A, implements IC-R10,
and carries the network half of IC-R4. IC-R11 remains the 4A Unix transport.
No allowlist entry or new external host is added by this slice.

### Decisions pinned by slice 4B

- **D-S4B.1 — scope and refusal.**
  - `needs.network` is no longer refused once authenticated network
    provisioning is available. `needs.credentials` still gives
    `Unsupported("credential_handle")` until 4C; 4B never silently clears it.
  - `needs.rpc` continues through 4A's endpoint. Network authority does not
    require a provider to declare RPC and grants no RPC permissions.
  - Attempts declaring no network retain `--network none`. A network-enabled
    attempt gets the topology below; there is no isolated `bridge` or
    unrestricted mode and no fallback to the legacy project network.
  - Legacy `local` and shared `docker` behaviour stays unchanged. Shared
    gateways may reuse code, but isolated and legacy authority are distinct.
- **D-S4B.2 — topology and ownership.**
  - One internal Docker network per network-enabled attempt. Its only
    members are that attempt's container and the trusted egress and auth
    gateways. The auth gateway may have no active grant for that attempt.
  - Gateways are project-scoped and attached to several attempt networks;
    they alone have a route to external services. Per-network aliases are
    `ma-egress` and `ma-auth`. Neither gateway forwards arbitrary IP traffic
    between interfaces. No gateway management port is reachable by a run.
  - No published attempt ports, host network/pid/ipc namespaces, host RPC
    socket, sibling mounts or Docker socket are given to a run. The scoped
    RPC remains `/run/multiagents/rpc/rpc.sock`, never TCP.
- **D-S4B.3 — unchanged hostname policy, checked destinations.**
  - The sole hostname policy source remains the effective existing
    `executor.docker.egress_allowlist`, through today's layered configuration,
    normalization and validation. No second list or shipped entry is added.
  - Preserve today's `_proxy_filter` semantics: case-insensitive exact
    approved domain or any descendant; an approved canonical IPv4 entry is
    exact. No wildcard entries, public suffixes, implicit new hosts or
    sibling-name exceptions. Approval is still the user's action.
  - A matching hostname is necessary but not sufficient: IC-C27 checks the
    actual destination. The address prohibition applies even to an approved
    literal IP. Existing forbidden addresses gain no grandfathered access.
  - `ma-auth` is an internal gateway alias, not an egress allowlist entry.
    Its fixed existing upstream does not broaden generic egress permission.
- **D-S4B.4 — authority is issued by the host.**
  - The host binds each grant to project, attempt, run, immutable container
    and network identities, provider and gateway service (`egress` or `auth`).
  - Gateway authority is separate from the 4A `endpoint_authority` table,
    which refers to an RPC capability. It follows the same transactional
    grant/revoke discipline but cannot be used as an RPC capability.
  - Credentials identify a grant; they cannot select a different account,
    provider, node or network through request fields. Each request also
    proves membership in its grant's source network. Caller-supplied
    `Forwarded`, `X-Forwarded-For`, Host or identity fields prove nothing.
  - New attempts get fresh grants and credentials. Restart/adoption reuses
    recorded grants and never resurrects revoked ones or mints a new identity.
- **D-S4B.5 — configuration and prerequisites.**
  - Add `executor.isolated.network: allowlist|none`, default `allowlist`.
    `none` with `needs.network: true` is a visible non-retryable refusal;
    unknown values are config errors naming the key.
  - Add `executor.isolated.docker.gateway_image`, default
    `multiagents/workspace:latest`, running trusted host-supplied gateway code.
    No image build or pull happens at launch. Docker commands use K17.1's
    configured CLI; no separate daemon or implicit socket is selected.
  - Missing image/daemon/support for the topology gives the existing
    `isolation_unavailable` refusal. A gateway image is validated before
    launch side effects. No provider refresh/login action is introduced.
  - Gateway lifecycle settings are recorded with executor settings for K19.
    The shared allowlist is reconciled as ST2 policy; it is not a frozen
    per-attempt copy. A policy change never grants hosts not approved by the user.
- **D-S4B.6 — supervision and replacement.**
  - Extend ST2's durable receipt, lock and reconciliation discipline to
    both isolated gateways. A successful CLI command alone is not readiness.
  - Gateways may be replaced without stopping/recreating attempts. Existing
    connections may fail; stable aliases and unchanged credentials permit
    reconnect. There is never an unauthenticated interval.
  - Reconstruct desired attachments from host-recorded active attempts, not
    by trusting the union of whatever Docker currently has attached.
- **D-S4B.7 — secrets and evidence.**
  - No credential material appears in logs, events, argv diagnostics, error
    bodies, policy receipts, labels, public status or attempt evidence.
    Guest credentials exist only in host-private secret storage and the
    attempt's invocation input/environment, never its conversation store.
  - Before durable capture, replace literal issued gateway passwords, auth
    bearer values, their Basic header encodings and credential-bearing proxy
    URLs with `[REDACTED]`. Match across write/chunk boundaries. No raw Docker
    log copy may persist those values. This is a logging-producer requirement,
    not only a display filter. Keep the redaction knowledge for retained logs.
  - **Placement (orchestrator, after researcher nd-175a5138).** The
    redactor sits in the existing PID-1 entrypoint, before base64 framing
    (`docker_lifecycle.py` around lines 32-76).
    - It keeps a per-stream tail of undecided bytes across reads.
    - It runs under a UID other than the agent child's, so the child
      cannot signal it, trace it or write its pipes.
    - Docker's json-file log then holds only sanitised frames. Adoption
      and `docker logs` replay are unchanged.
    - A checkpoint records only the sanitised durable length. Bytes held
      back as a possible secret prefix are never appended and then
      rewritten.
    - Host-side `--log-driver none` capture and a logging plugin are not
      used: both break adoption or retention.
  - Output replay/checkpoints operate on the sanitised streams. Non-secret
    bytes retain slice 3's ordering and exactly-once replay. The trusted
    logging path must remain outside the run's write/control authority.
  - This does not claim detection of arbitrary third-party secrets or all
    encodings a hostile workload invents. Provider credential projection and
    renewal, and its additional redaction requirements, belong to 4C.

### Behaviours

**IC-C25 — network creation, identity and recovery (IC-R6, IC-R10).**
- Add optional `LaunchSpec.network_handle`, default `None`, containing an
  opaque host-issued network reference. A non-empty handle without host
  authority is `InvalidSpec`; backends lacking support return
  `Unsupported("network_handle")`. No run-supplied network name is authority.
- The field participates in the canonical digest when non-empty. With it
  unset, preserve the existing v1 canonical digest for recorded specs. Record
  and declare backend compatibility explicitly; old attempts remain adoptable.
- Before the first network create, durably record
  `launch_evidence.executor.network` with `creation_key`, then immutable `id`
  after create; before `run`, add `policy_digest` and `gateway_generation`.
  Keys and names are host-generated and bound to the attempt. Network labels
  use K17.4's project/owner/creation-key names and preserve the test label.
- A host-only registry in `multiagents.scheduler.gateways` exposes
  `GatewayRegistry(paths, *, config, providers)`, with async operations:
  `prepare(attempt_id, run_id)`, `grant(attempt_id, container_identity)`,
  `revoke(attempt_id)`, `cleanup(attempt_id)`, and `reconcile()`.
  `prepare` returns a frozen `NetworkHandle` with `.id` (an opaque registry
  reference, not the Docker network id). Successful grant returns a host-only
  frozen `GatewayGrant` with `.id` and `.environment` (IC-C26/28's invocation
  variables); never serialise it to public RPC or evidence. Successful
  revoke/cleanup returns `None`; `Absent` also means successful cleanup.
  Successful reconcile returns non-secret `{attempt_id, state, reason}` rows,
  state `ready|revoked|absent|unknown`. Construction has no side effects.
- The registry validates attempt/project ownership from the host journal.
  Repeated prepare/grant uses the same recorded identity and immutable
  binding; incompatible reuse gives `IdentityConflict` without mutation.
- Network preparation precedes backend `provision`, since container network
  membership is part of creation. `grant` follows confirmed provision and
  precedes `run`, alongside K18's endpoint grant. Created containers have no
  usable gateway grant until then. All readiness is confirmed before start.
- Recovery resolves creation keys when ids were not recorded, completes or
  reports partial attachments, and applies IC-C15's five launch boundaries.
  Created/unlaunched attempts are never started by network recovery.
- Registry outcomes use the lifecycle vocabulary: invalid authority/config
  gives `InvalidSpec`/`Unsupported`; ambiguous daemon or authority state is
  `Unknown(reason)`. A confirmed missing object is `Absent`. Confirmed
  operational launch failure is TF `launch`; unknown state holds under K6.
- Verified by: fake-CLI command ordering, key recovery, duplicate operations,
  wrong-project handles, invalid config, old-spec digest/adoption and each
  crash boundary; real Docker for internal network membership and no bypass.

**IC-C26 — authenticated egress and guest environment (IC-R10).**
- On grant, the host generates HTTP Basic credentials: username
  `ma1-<attempt_id>`, password 32 cryptographically random bytes represented
  as 64 lowercase hexadecimal characters. Never derive the password from
  an id or copy another attempt's credential.
- Host-only durable gateway authority records contain the grant id,
  project/attempt/run/container/network/provider binding, service, generation
  and revoked flag. Authentication uses a credential verifier. Any recoverable
  raw secret is stored separately under host-private authority, never a
  public evidence field. No run can read the registry or gateway secret store.
- The host sets all four variables, after overlays and passthrough:
  `HTTP_PROXY`, `HTTPS_PROXY`, `http_proxy`, `https_proxy` =
  `http://ma1-<attempt_id>:<password>@ma-egress:8888`.
  `NO_PROXY` and `no_proxy` = `localhost,127.0.0.1,::1,ma-auth`.
  Remove conflicting `ALL_PROXY`/`all_proxy` values. Network-disabled attempts
  receive no gateway credentials or inherited proxy variables.
- Missing/bad/revoked credentials, or a valid credential presented from a
  different attempt network, give HTTP 407 without opening an upstream
  connection. Compare verifiers without timing-dependent string comparison.
- Check authority on each request. Strip `Proxy-Authorization` before
  forwarding and never echo it in an error. Authenticated CONNECT tunnels
  remain bound to the grant for their whole lifetime (IC-C29).
- RPC permissions, including delegate/verdict, do not determine egress
  rights. A network-only probe can use egress without obtaining an RPC grant.
- Verified by: fake/unit tests of format, fresh second-attempt credentials,
  environment precedence, 407 and redaction; real Docker for valid forwarding,
  copied-credential rejection, forged forwarding headers and two simultaneous
  attempt networks. Test actual client proxy-auth support, not only curl.

**IC-C27 — allowlist parity and destination enforcement (IC-R10).**
- Reuse today's allowlist entry validation and hostname matcher. Preserve
  HTTP forwarding and CONNECT to port 443 only. Match request authority,
  not URL paths, substring suffixes, or a conflicting caller Host header.
  Refuse malformed/ambiguous authorities and unsupported tunnel forms.
- A hostname mismatch or forbidden destination gives HTTP 403 and no dial.
  DNS/dial failure gives a scrubbed HTTP 502; no direct/unfiltered retry.
- Resolve at the gateway. Reject a resolution containing any non-global
  unicast address, including private, loopback, link-local, multicast,
  unspecified and IPv4-mapped equivalents, or any host, gateway-management
  or Docker attempt-network address even if otherwise globally numbered.
  Apply the same test to literal addresses and every selected A/AAAA answer.
- Mixed allowed/forbidden answer sets are refused. Dial only the checked
  numeric address: no second unchecked resolution. New connections resolve
  and check again. Any gateway-followed redirect is re-authorised; client
  redirects naturally require another checked request.
- Gateways expose no general relay between attempt networks and no route to
  sibling services, credentials or RPC endpoints. Direct external TCP/UDP,
  host-service access and cross-network traffic must not bypass them. DNS
  service supplied to the run cannot become an arbitrary external DNS tunnel;
  any required resolution is scoped to internal aliases or handled by the
  gateway. No host approval is added to make a test pass.
- Verified by: golden matcher parity, descendants/lookalikes, canonical IPs,
  IDNA/public-suffix/wildcard validation, mixed-answer and rebinding tests;
  real Docker for sibling/host/metadata/direct TCP/UDP/DNS bypass attempts and
  positive forwarding. Controlled DNS/upstreams are test-side dependencies,
  never production allowlist edits or new external hosts.

**IC-C28 — the auth gateway's attempt authority (IC-R10).**
- Preserve the existing fixed `https://api.anthropic.com` upstream, safe
  request-target construction, provider routing and account pinning of
  `authproxy.py`. It is not a CONNECT proxy and cannot target another host.
- For an isolated auth grant the host generates a separate bearer value
  `ma-auth-v1.<64 lowercase hex characters>` from fresh 32 random bytes.
  Map its verifier to the host-recorded attempt/provider/account-routing
  authority and source network, not to request-supplied identity.
- Missing, forged, revoked or wrong-network authority gives HTTP 401 before
  selecting an account or opening an upstream connection. Legacy signed
  v1/v2 agent/provider claims alone cannot authorise isolated ingress.
  The legacy listener must not be reachable from attempt networks.
- An attempt receives `ANTHROPIC_BASE_URL=http://ma-auth:8930` and
  `ANTHROPIC_AUTH_TOKEN=<bearer>` only when its host-selected provider is
  eligible for this auth protocol. Scrub inherited Anthropic auth/base-url
  overrides otherwise. Egress and auth credentials are not interchangeable.
- Add provider declaration `gateway_auth: none|anthropic`, default `none`,
  following normal provider layering/extends and validated at load time.
  This selects a trusted upstream protocol, never an account or arbitrary URL.
  It does not override `needs.credentials`: real providers remain refused
  until 4C. Offline test providers may declare only network plus this protocol.
- 4B uses a host-only injected synthetic account/upstream factory for tests,
  available only through `MULTIAGENTS_TEST_GATEWAY_BACKEND=module:factory`.
  Strip that variable last from all run environments. No project.yaml or
  guest request can install a test upstream. The public factory signature is
  `factory(root: Path, *, project: str)`, returning an adapter with synchronous
  `open(method: str, path: str, headers: dict[str, str], body: bytes,
  *, provider: str, run_id: str) -> response`. The response exposes integer
  `.status`, string mapping `.headers`, `.read(size: int) -> bytes`, and
  `.close()`. It receives a validated fixed-upstream path and synthetic account
  material, never a caller-selected URL or production vault secret. Its close
  unblocks pending reads, so revocation can be tested with an active stream.
  A factory failure refuses launch, never falls back.
- No access-token projection, refresh, login, credential migration or live
  provider acceptance is implemented here. 4C supplies the real credential
  service; 4B never mounts its vault into an attempt.
- Verified by: auth claim/routing/pinning regression tests and synthetic
  upstream calls; real Docker for forged/cross-network claims and assurance
  that no gateway can relay to sibling credentials or endpoints. Live vendor
  checks remain 4C and cannot be marked passing by these synthetic tests.

**IC-C29 — end, cancellation, revocation and cleanup (IC-R6, IC-R8, IC-R10).**
- Normal end records termination before revocation and backend removal, as
  K18.1. Cancel/replacement revokes before `stop`. Transactional authority
  ordering matches 4A: requests authorised/forwarded before revocation may
  already have upstream effects; later requests have none.
- Revocation affects new requests and accepted tunnels/streams, closes their
  connections and stops further forwarding. It does not revoke another
  attempt's authority, even when accounts or gateways are shared.
- `revoke` returns success only when gateway acknowledgement or confirmed
  ingress isolation proves enforcement. Unknown enforcement stays reported
  and held; the host still attempts to stop its own attempt container, so
  gateway failure cannot strand a deliberately cancelled process.
- Recovery serves only confirmed-running authorised attempts. Unknown
  liveness/authority grants no access and retains the liveness hold. It
  reconciles grants that were published before start or left after exit.
  Secrets/identities do not rotate merely because the scheduler restarted.
- Once termination is confirmed, disconnect the attempt container and the
  gateways from its network, then remove that network even if the exited
  container, output and repository remain retained. Retention grants no
  network resurrection. Missing objects are idempotent; unrelated members or
  unknown ownership stop deletion and remain reported.
- Partial launch rollback revokes only that attempt's grants and cleans only
  its confirmed resources. Store cleanup intent before deletion. Keep no raw
  credential once it is no longer needed for recovery; preserve only the
  redaction knowledge required for retained output.
- Verified by: fake-CLI crash/retry cases at network preparation, provision,
  grant, start, observed exit, revocation and detach/remove; real Docker for
  closing live CONNECT/auth streams, cancellation during a gateway outage,
  restart/adoption, retained-container isolation and unaffected siblings.

**IC-C30 — ST2 gateway replacement across all active networks (IC-R10).**
- Reuse ST2's host-side serialisation and durable applied-receipt discipline
  for both gateways, preserving legacy behaviour where code is shared.
  Receipts carry immutable gateway container id, start time, policy/config
  digest, authority generation and the desired immutable network-id set.
- Persist the intended set and invalidate readiness before replacement.
  Reconcile concurrent prepare/end/revoke against the host journal; policy
  application and authority revocation cannot undo one another.
- A gateway is ready only after inspect and an authenticated readiness
  check confirm its identity, applied policy/authority generation and all
  desired attachments. Stale receipts, incomplete attachments and unknown
  inspect results cannot authorise a launch. No anonymous readiness bypass
  exposes upstream routing.
- Keep `ma-egress`/`ma-auth` aliases and active credentials stable. Replacement
  reconnects every authorised running attempt network and excludes stale
  networks. Do not force-disconnect unknown external members to make the
  desired state appear correct: report the conflict and refuse readiness.
- Existing requests may break; retrying transport never re-runs the agent
  invocation. Attempt containers are never recreated by gateway replacement.
  Gateway restart policy follows ST1; recorded receipts remain the authority
  for adoption, not a successful `docker start` alone.
- Verified by: fake-CLI failures after receipt invalidation, removal, create,
  each attachment and final receipt; stale ids/start times/generations;
  concurrent network addition/removal; real Docker replacement with several
  active networks, reconnect using unchanged credentials and no open interval.

**IC-C31 — network orphans and explicit removal (IC-R8, K17.5).**
- Extend all orphan rows additively with `resource_kind` and `resource_id`:
  containers use `container`/container id, endpoints use `endpoint`/endpoint
  id, networks use `network`/network id. Preserve existing nullable fields.
  Sort mixed rows by `(resource_kind, resource_id)`; existing container and
  endpoint removal commands retain their outcomes.
- Network rows are `{resource_kind: "network", resource_id, network_id,
  container_id: null, name, labels, created_at, project, attempt_id, reason}`.
  Names/labels/creation time come from Docker; creation time is RFC 3339 and
  `attempt_id` is nullable. Reasons are `unrecorded_attempt`, `discarded_node`
  or `ended_attempt_network` (cleanup of a confirmed-ended attempt remains).
- Discover only this project's reserved-label resources; reconcile creation
  keys and recorded ids before classifying. A prepare/create crash window is
  not automatically an orphan. Active/unknown attempts' networks are never
  classified as safe to delete. Gateway infrastructure is not an attempt
  orphan and is not reaped by an attempt cleanup.
- Root `scheduler_status` and `scheduler orphans` return the same discovery
  result. Non-root status omits orphan fields. Incomplete discovery gives
  `orphans: null, orphans_error: {reason}`, never a misleading empty list;
  error reasons obey D-S4B.7. Listing deletes nothing.
- `multiagents scheduler remove-orphan-network NETWORK_ID` is host-user-only,
  accepts an exact full immutable id, and rechecks ownership, journal and
  membership before mutation. Revoke any stale grant, then disconnect only
  confirmed owned ended-attempt/gateway memberships and remove the network.
  Unexpected members or uncertain state refuse removal; never stop a live
  attempt to satisfy this command. No agent RPC exposes network removal.
- Success: `{ok: true, network_id, state: "absent"}`, including already
  absent. Failure: `{ok: false, network_id, error, reason}`, non-zero exit;
  `error` is `invalid_id`, `wrong_project`, `not_orphan` or `unknown`.
- Verified by: fake-CLI row/status parity, sorting, project filtering,
  reconciliation, unknown discovery and each removal refusal; real Docker
  orphan creation, stale attachments, explicit removal, repeated removal,
  retained-container cleanup and refusal of live/foreign networks.

### Slice 4B test prerequisites

- **Test upstream adapter.** IC-C28's protocol is new. Test blocking-read and
  close controls independently before using it to prove revocation; it is
  never a production upstream-host override.
- **Registry results.** `GatewayGrant.environment` is an invocation secret
  boundary. Assert that record/error serialisation never includes it,
  including a failed grant followed by recovery.
- **Logging approval.** D-S4B.7 is a deliberate change to original-byte
  evidence. Until the user-level choice above is resolved, no test may claim
  that display-only redaction satisfies the no-credential-logging requirement.

### Slice 4B work split (orchestrator, 2026-10-16)

4B ships in two sub-slices, each with its own tester → implementer-deep →
reviewer → adversary (mandatory) → real Docker → suite → merge.

- **4B.1 — the network and authenticated egress:** D-S4B.1..D-S4B.5 and
  D-S4B.7, IC-C25, IC-C26, IC-C27, IC-C29 (for the egress gateway) and
  IC-C31.
  - Before the tester starts, a researcher checks whether producer-side
    redaction (D-S4B.7) is feasible on the current Docker log path, and
    reports the options.
  - The authenticated egress gateway reconciles its authority generation,
    its revocations and every active attempt network from the first day
    (advisor, turn 13). ST2's current receipt does not cover these: it
    covers only the filter and the container, and keeps the attachments it
    observes.
- **4B.2 — the auth gateway:** IC-C28, IC-C29 (for the auth gateway),
  IC-C30 (for the auth gateway) and D-S4B.6 extended to it.

Both sub-slices follow these rules:
- The tester follows the test prerequisites above.
- Fake-CLI passes are never evidence of containment.
- The orchestrator runs real Docker under `SG_TEST_DOCKER=1` on the host,
  and a missing prerequisite with the switch set fails.
- The allowlist contents stay unchanged, and no external host is added.
