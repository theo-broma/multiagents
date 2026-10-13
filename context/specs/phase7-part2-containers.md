# Phase 7, part 2: one container per run

**Status: requirements, agreed with the user on 2026-10-13.**
- **Written by:** the initializer, with the advisor (ag-aa1387, turn 11).
- **What it is:** the user's decisions and the requirements that follow
  from them. It is **not** the interface contract. The orchestrator writes
  that contract in its phase 2, citing these ids, then runs tester →
  implementer → reviewer as usual, with the adversary on every milestone
  that touches an isolation boundary.
- **Ids:** `IC-D*` for decisions and `IC-R*` for requirements. They are
  never renumbered.
- **Background:**
  - `phase7-nodes-and-containers.md`, part 2: PAC-R1..R13 and the C15
    residuals. This spec **supersedes** those as the source of truth; each
    one is folded in below and cited.
  - `phase7-part1-nodes.md` and `phase7-part1-contract.md` (NS, NC): the
    node, attempt, generation and alias model, and the scoped host RPC.
    They stay.
  - Plan `2026-10-12-stabilization.md` (ST1, ST2, ST9): the container
    restart policy, proxy replacement, and the allowlist based on the
    Public Suffix List.

## Why

Inside today's single project container, agents are not isolated from each
other. Any agent can modify another agent's worktree, HOME, refs and
objects (H1, "threat model"). On 2026-10-03 the user accepted this for the
time being and chose to remove it here, with one container per run created
by the host scheduler. Intermediate per-agent uids were rejected.

## The user's decisions (2026-10-13)

- **IC-D1 — the isolation unit is the execution attempt.**
  - Each attempt (NS-R6's attempt id) gets its own container, created by
    the host scheduler.
  - Nodes, attempts, CLI turns and conversations have different lifetimes.
    State that must outlive an attempt, such as a conversation's session,
    is kept separately and handed to the next attempt (IC-R6).
- **IC-D2 — Docker containers now; microVM later, as an option.**
  - The executor interface must allow a microVM backend (Docker Sandboxes
    / `sbx`, Firecracker) to be added without touching the scheduler.
  - None is built in this part. Whether one runs on Linux without Docker
    Desktop is still unchecked.
- **IC-D3 — memory: 8 concurrent runs, about 2.5 GB reserved each.**
  - `max_concurrent` stays 8.
  - The scheduler admits by a total memory budget as well, about 20 GB on
    this host, configurable. Each run reserves a modest amount, about
    2.5 GB by default, and has a higher hard limit, for example 6 GB, so
    that heavy suites fit.
  - The user accepts the rare risk of a simultaneous peak; OOM is reported
    per run (IC-R9).
- **IC-D4 — the container is the supervisor; tmux only shows.**
  - The container keeps the agent alive across a restart of the host CLI.
  - tmux becomes the viewing and attach front end, extending step 1 (D2).
  - **This explicitly reverses** the earlier plan for "tmux step 2, tmux as
    the process supervisor" (BRIEF, 2026-09-28). The user confirmed the
    reversal on 2026-10-13. tmux itself stays; Zellij is not adopted
    (2026-10-10).
- **IC-D5 — a live node view: reading for everyone, a recorded shell for
  the user.**
  - For each node it shows:
    - the node's state;
    - its run;
    - the diff;
    - the live log;
    - a read-only attach.
  - A shell inside a run's container is possible for the **user only**, by
    explicit action. It is recorded, and the attempt is marked "modified by
    hand" wherever a review or a merge looks at it.
  - Agents cannot open the view or the shell, and there is no MCP tool for
    them.
- **IC-D6 — conversations get their own container too.**
  - The advisor and other consult agents get one container per
    conversation:
    - the repository read-only;
    - a private HOME and temp;
    - kept as long as the conversation lives.
  - No agent keeps the shared mode. **The host orchestrator stays outside
    this part**: it is a root session on the host.
- **IC-D7 — rollout behind an explicit executor selection, never a silent
  fallback.**
  - The executor is chosen explicitly in the config, and each attempt
    records the backend it ran on.
  - When an isolated launch fails, the attempt is **refused**, never
    retried silently in the shared mode.
  - The shared mode is removed only once the isolation suite (IC-R13) and
    the lifecycle, resume, recovery, credential and resource tests all pass.

## Requirements

### Git, without shared write access (PAC-R3)

**IC-R1 — a fresh repository per attempt, seeded by the host.**
- **The seed.** The host produces a bundle containing only the **reachable
  ancestry** the attempt is authorised to read:
  - the approved base;
  - its declared inputs;
  - recovery commits explicitly authorised by RR.

  Bundles are cached by the identity of those authorised inputs.
- **The attempt clones from that bundle.** This replaces `clone --shared`
  (`results.py` ~261), whose alternates expose the host's whole object
  store.
- **Never** mount the shared `.git`, never use alternates into it, never
  use a linked worktree.
- **Measured on this host, warm, without Docker:**
  - a bundle of HEAD only: 11.7 MB, created in 1.23 s;
  - clone and checkout: 0.59 s, giving 31.1 MB of files.

**IC-R2 — publication keeps its model and strengthens import.**
- `nodes/<id>`, generations, capture, quarantine and the ancestry checks
  stay as NC defines them.
- What changes: the result is imported from the attempt's own repository,
  under H3's protections.
- **H1's invariant holds with the attempt as the domain.** State written
  by the container never authorises a host mutation outside that attempt.

**IC-R3 — session aliases get a fresh repository each activation.**
- An alias, such as reviewer session B across rounds, keeps its
  **conversation HOME** and a stable container path. Its repository is
  re-seeded fresh each activation:
  - no old objects;
  - no old refs;
  - no old reflogs.

  Today `sessions.py` ~354 resets in place.
- **Known limit, stated, not hidden.** The conversation itself remembers
  earlier inputs; isolation cannot make a resumed alias forget them.
- NC's dirty-checkout behaviour (hold, archive, cleanup, TB2-R6) is kept,
  not silently replaced.

### Credentials (PAC-R5, R11)

**IC-R4 — the host owns refresh; runs get access tokens only.**
- **This continues today's model.** Refresh is centralised, and access
  credentials are projected without the refresh token (`executor/docker.py`
  ~1644, ~2160). claude stays behind the auth proxy.
- **Prove each provider works with access-only credentials:** codex, agy,
  opencode and its variants. A provider that cannot is named, with the
  reason, and gets a stated fallback.
- **Define:**
  - renewal **during** a long run, not only at spawn;
  - atomic projection into the run's private HOME;
  - locking scoped to the account;
  - revocation when the attempt ends.
- Discarding a CLI's own refresh result is not enough if the refresh has
  already rotated the host's token: CLI-side refresh must not reach the
  refresh token at all.
- **Stated residual risk:** a private copy still exposes that account's
  access token to that run, until it expires.

### Resources (PAC-R6)

**IC-R5 — memory and CPU per run, admitted against a durable budget.**
- **Memory** follows IC-D3: a reservation and a hard limit per run, and a
  total budget.
- **The reservation is durable.** It survives adoption and counts paused
  and live containers.
- **A job too large for the budget is refused** with a reason; it is never
  left queued silently.
- **Fairness.** Delegators waiting on their children must not hold all
  admission capacity indefinitely.
- **CPU: a weight, not a cap** (user, 2026-10-13).
  - By default there is no hard CPU limit per run. Every run gets the same
    relative CPU weight (`--cpu-shares` / cgroup `cpu.weight`).
  - On an idle machine, a run can use every core. Under contention, cores
    are shared equally, so no run starves the others. Eight runs × `-n 4`
    is 32 workers on 16 cores; the kernel arbitrates.
  - A fixed cap would leave the machine idle exactly when a long suite
    could use it. This matches the user's decision of 2026-10-03 for the
    single container: no CPU cap.
  - An optional per-run `cpus` cap stays configurable, for example to
    protect other services on the host. It is off by default.
  - `-n 4` stays the rule in AGENTS.md. Adapting `-n` to the host's load is
    out of scope.
- pids limit per run.

### Lifecycle, supervision and adoption (PAC-R7, R8, R12; C15 residuals)

**IC-R6 — a durable identity, recorded before creation.**
- Before `docker create`, the host records a host-generated container
  name, together with:
  - the node;
  - the attempt;
  - the repository;
  - the session;
  - the capabilities.

  After creation, it records the immutable container id. Creation cannot
  depend on an id Docker has not assigned yet.
- **Crash recovery is specified at every launch boundary:** before
  creation, after creation, after start, after exit, after capture. A
  retry is idempotent.
- **Retention after success is separate from discard,** so a reopened node
  can resume. Steer or reopen creates a **new attempt** from the retained
  repository and session state.

**IC-R7 — the container supervises; adoption is by container identity.**
- The restart policy is `no`: automatic resurrection would repeat commands
  (ST1).
- Stdout and stderr are kept durably, with replay offsets, so a host
  restart adopts a live container and resumes reading without loss or
  duplication.
- Exit code and OOM evidence come from Docker's view of the container.
- Cancellation has a grace period and then stops the container.
- **Every signal, liveness verdict and stop goes through the host-recorded
  container identity**, never through pid files the agent can write. This
  closes the C15 residuals:
  - forged `wrapper.pid` / `container.pid`;
  - descendants escaping through `setsid()`.
- Adopted: SV (survival and adoption), SP (session persistence), SF/SR
  (steer, death detection) and mount-drift detection, which no longer
  assume one long-lived container.
- **A config change applies to the next attempt**, and no longer requires
  killing every run.

**IC-R8 — cleanup and orphans.**
- Discard removes the container, its repository and its private storage.
- An orphaned container, left by a crashed host, is found by its label and
  its recorded identity. It is **reported**, never deleted blindly.

**IC-R9 — out of memory and resource failures are reported per run,** with
evidence (D1, LN-R6), and classified by TF.

### Network (PAC-R10)

**IC-R10 — one internal network per run.**
- Each attempt has its own internal network. On it are only the egress
  proxy and the auth proxy, which become trusted gateways attached to
  several run networks.
- **Tests prove** that a gateway cannot relay traffic to:
  - a sibling run;
  - a sibling's scoped RPC endpoint;
  - a sibling's credentials.
- **Per-run authentication on egress is new work.** Today egress has none.
  It is specified here, together with:
  - the endpoint transport;
  - capability revocation;
  - proxy replacement (ST2) across every attached run network.
- **The egress allowlist rules stay as they are:**
  - the user approves each host;
  - no wildcard;
  - no public suffix.

**IC-R11 — the scoped RPC reaches the run through a per-container
transport** (PAC-R4, PAC-R9, NS-R5).
- The host issues the capability. The run never names its own identity.
- The docker socket is never given to a run.

### Storage and caches

**IC-R12 — a concrete layout, with quotas.**
- **Shared and immutable:**
  - toolchains;
  - the image layers.
- **Private to the run:**
  - the environment (`.venv`, about 53 MB here);
  - HOME;
  - temp.
- **Package caches** (uv, pub). The contract chooses a real copy-on-write
  or prewarming mechanism and tests it: the attempt must be unable to
  poison a shared cache (PAC-R13). "Read-only plus overlay" is not a
  design until it is tested with the real tools.
- **Temp is disk-backed, with a quota and a retention period.** Eight
  suites write about 12 GB of temp before repositories, caches and
  retained attempts are counted.
- Avoid running `uv sync` on every short activation. Reuse a prepared
  environment keyed by the lock file.

### Isolation acceptance (PAC-R2, R13)

**IC-R13 — the black-box isolation suite, written first, run under real
Docker.** For any two runs A and B, A cannot:
- modify, delete or read B's worktree, HOME, temp or run directory;
- move or delete B's refs, make B's objects unreachable, or read B's
  committed blobs;
- reach B's network services, RPC endpoint or credentials;
- poison a shared cache that B will read.

In addition:
- a `readonly` attempt cannot write its worktree or its Git metadata;
- an RPC identity cannot be forged;
- **a deliberate handover works:** a dependency's approved work reaches its
  dependent, and nothing unrelated does.

### Visibility (IC-D5)

**IC-R14 — the node view.**
- **Live working diff and captured generation diff are both shown,**
  labelled differently.
- **After a container exits,** the log, the captured diff and the exit
  evidence remain viewable.
- **The viewer never executes** commands or configuration from the
  repository. The stream is untrusted, as in TM-R1.
- **Read-only attach** reuses D2's private tmux server and socket (TM-R*).
- **The shell:**
  - user-only and explicit;
  - recorded, with the user, the time and the attempt;
  - the attempt is flagged "modified by hand" in what `merge_node` and
    reviewers see.

### The executor findings deferred to this part

**IC-R15 — the rewritten executor carries these review findings as
acceptance requirements**, and each is set `fixed` or reconsidered when
this part merges:
- **F31:** built-in mounts dropped without warning when the directory does
  not exist yet.
- **F32:** a limit of 0 is the same as "not set", and a negative value is
  passed through.
- **F34:** `prepare_home` raises an uncaught exception when the target
  path is a file or its parent is not writable.
- **F35:** string-form mounts can never be read-only, and relative paths
  are never resolved.
- **F64:** side effects happen before the image is checked.
- **F65:** a credential temp file is left behind on interruption.
- **F69:** lock deletion is decided by a pid check open to a race. It may
  disappear with the new provisioning; that must be shown, not assumed.
- **F70:** a stop may SIGKILL a process group that has been reassigned.
  It is resolved only when **every** stop path uses the trusted container
  identity.

## Milestones (suggested; the contract decides)

1. **The isolation suite (IC-R13) as red tests,** and a latency benchmark
   for creating a container and seeding a repository, measured on short
   reviewer attempts.
2. **Provisioning:** the bundle, the fresh repository, alias re-seeding,
   and import (IC-R1..R3). Adversary.
3. **The executor:** create, start, supervise, adopt, stop and clean up by
   container identity (IC-R6..R9, IC-R15). Adversary, for the C15 cases.
4. **Credentials (IC-R4)** and **network (IC-R10, R11).** Adversary.
5. **Resources and admission (IC-R5)** and **storage (IC-R12).**
6. **Conversations in containers (IC-D6)** and **the node view (IC-R14).**
7. **Live checks under real Docker,** then the gate. The shared mode is
   removed only after the whole suite passes (IC-D7).
