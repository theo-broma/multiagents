# Stabilisation (ST)

From plan `context/plans/2026-10-12-stabilization.md`, items 1 to 9. Research
on main 161f75fe:
- ag-ac5a0d (containers and proxy);
- ag-a49445 (effort, collect, watchdogs);
- ag-c53382 (waits, `scheduler stop`, §4);
- ag-a4cb1a (F10, F120);
- ag-dcb561 (flakes and environment reds).

## Background

- **Restart policies.** Three containers are created with `--restart
  unless-stopped` (executor/docker.py):
  - the egress proxy (`ensure_proxy`, about line 2232);
  - the auth proxy (`ensure_auth_proxy`, about 2313);
  - the workspace container (`run_args`, about 2428).

  Their mount sources can be under `/tmp` through env overrides, a project
  under `tmp_path`, or `extra_mounts`. At boot, dockerd recreates a missing
  source as root, which left `/tmp/claude-1000` unusable three times. Most
  tests fake Docker; the two opt-in live tests remove their container in a
  finalizer that a killed pytest never runs. No label marks a test-created
  container.
- **The proxy filter.** It is a single-file bind mount. `ensure_proxy`
  returns early when the proxy is running, so an allowlist change never
  reaches a running proxy.
- **opencode effort.** opencode declares only `optional.variant`, and the
  runner supplies `effort`, so an opencode run never gets an effort.
- **Waits.** Neither wait emits MCP progress, and Claude Code aborts a tool
  call that is silent for 1800 s.
- **`scheduler stop`.** It waits 10 s for `process.json` to disappear. The
  daemon removes that file last, after an `engine.stop()` that can itself
  take 10 s.
- **The §4 trial defects.** All three are resolved or by design:
  - `capacity` is always present (TB2-R3);
  - `windows` lists only non-terminal nodes;
  - a child of an already-finished dependency launching at once is the
    contract (phase7-part1-contract.md:316).
- **F10 is still present.** The filter is `(^|\.)<entry>$` for every entry,
  and validation accepts `com`, `co.uk` and `1.1`; `1.1` admits
  `192.168.1.1`.
- **F120 is fixed.** It was set `fixed` on 2026-10-12 with the evidence.

## Behaviours

### ST1 — no container comes back at boot onto a vanished mount source

**ST1-R1.** A container that multiagents creates (workspace, egress proxy,
auth proxy) carries a restart policy other than `no` only if none of its
bind-mount sources is on a filesystem that does not survive a reboot. That
means under `/tmp`, `/run`, `/var/run` or `/dev/shm`, or on any tmpfs. A
source reached through a symlink counts by its resolved path.
Verified by:
- a source under `/tmp` gives policy `no`;
- a tmpfs source elsewhere gives `no`;
- a symlink resolving into `/tmp` gives `no`;
- all-persistent sources give `unless-stopped`.

All of these are asserted on the `docker run` argv.

How a source's volatility is decided:
- it is decided on the resolved path, by mount inspection;
- path-boundary matching applies: `/tmpfoo` is not under `/tmp`;
- a source whose filesystem cannot be determined counts as volatile.

**ST1-R2.** Existing containers are not exempt. Whenever multiagents
ensures or starts one of these containers, it checks the mounts the
container actually has (`docker inspect`), not the configured ones. If its
policy breaks R1, it corrects the policy with `docker update --restart no`,
without recreating or stopping the container, so running agents are
untouched. A failed update fails the ensure, with the reason.
Verified by:
- a fake Docker reporting an existing container with `unless-stopped` and
  a `/tmp` source receives the update and no `rm`, `stop` or `run`;
- a failed update refuses the ensure.

**ST1-R3.** Policy `no` also gives up automatic restart after a container
crash; that is accepted. The next `multiagents run`, agent launch or
`docker up` starts a stopped container, as `ensure_proxy` already does for
an exited proxy. Before starting an existing container, multiagents checks
that every mount source exists. If one is missing, it refuses with a
message naming the source and the fix (recreate the container). It never
creates the source, and never lets Docker create it as root.
Verified by:
- an exited workspace container with policy `no` is started on the next
  launch;
- one with a missing source is refused, and the `docker start` is never
  issued.

**ST1-R4.** Every container the test suite creates against a real Docker:
- has restart policy `no`;
- is removed at teardown;
- carries a label naming its owner: the host, the pid of the pytest
  controller process (the xdist controller, not a worker) and that
  process's start time.

A later suite session removes labelled containers whose owner is provably
dead on this host. The owner is dead when no process has that pid with that
start time. This covers a killed pytest. Unlabelled containers, and
containers owned by another host, are never touched.
Verified by:
- the existing opt-in live tests, under `SG_TEST_DOCKER=1`;
- unit tests of the reaper on a fake Docker listing, covering a dead owner
  (reaped), a live owner (kept), a reused pid with another start time
  (reaped), another host (kept) and no label (kept).

**ST1-R5.** A guard test fails if any `docker run` argv issued while the
suite runs has a restart policy other than `no`, unless it is the R1 case
under test. This includes argv issued from subprocesses, which a fake
`docker` on `PATH` records.

### ST2 — an allowlist change takes effect by itself

**ST2-R1.** After `egress_allowlist` changes, the next ensure of the egress
proxy makes the running proxy enforce the new list for new connections.
That ensure happens on any agent launch, `multiagents run` or `docker up`.
Verified by:
- a fake Docker test: the proxy is restarted or recreated when the
  generated filter differs from the one it was started with, and left
  untouched when it does not;
- a live check (opt-in, `SG_TEST_DOCKER=1`): a host added to the list is
  reachable without manual action.

How the proxy's state is managed:
- **Applied filter.** The digest of the filter a running proxy enforces is
  recorded durably, for example as a label on the proxy container. It is
  recorded only after the proxy has started and is attached to its
  networks, and a replaced container never inherits a stale digest.
- **Reconciliation.** When the digest is absent, the proxy is reconciled.
- **Concurrency.** Ensures of one proxy are serialised.
- **Failure.** A failed reconciliation refuses new launches with the reason
  and stays retryable on the next ensure.

**ST2-R2.** The workspace container and the auth proxy are never stopped,
recreated or restarted by this. A replaced egress proxy keeps:
- its container name, which the workspace proxy URLs use;
- every network attachment it had.

Connections in flight may drop when it restarts; TF's existing
classification applies to what that breaks.
Verified by:
- the fake Docker sees no command addressed to the workspace container or
  the auth proxy;
- in the live check, the workspace reaches an allowed host through the
  replaced proxy.

**ST2-R3.** The proxy is not affected by in-place rewrites of the file. A
regenerated filter reaches a recreated or restarted proxy whichever way the
file was written: replaced through a new inode, or rewritten in place.
Verified by: both write styles in the fake test.

**ST2-R4.** Unchanged:
- adding a host still requires the user's approval;
- nothing here widens the list;
- F10's rules (ST9) apply to what is written.

### ST3 — effort reaches opencode

**ST3-R1.** A run on an opencode provider whose spec or pin has `effort: E`
launches with `--variant E`. With no effort, no `--variant` flag is passed.
The mapping lives in provider configuration (providers.yaml); core code
names no provider.
Verified by: the built argv for `opencode-zai` and `opencode-go` with
effort `high`, with `low`, and with none.

**ST3-R2.** A provider may map effort names to variant names in its
configuration. An effort with no mapped name is passed through unchanged.
Verified by: a configured mapping is applied, and an unmapped value passes
through.

**ST3-R3.** Codex and claude still receive `--effort` exactly as today.
Verified by: their existing argv tests, unchanged.

### ST4 — `collect_agent` sees a node run's commits

**ST4-R1.** For a run that belongs to a scheduler node, `collect_agent` and
the `wait_for_agents` entries report:
- `commits` and the diff stat from the node's own range, that run's
  `input_commit` to its generation commit on `refs/heads/nodes/<top node>`;
- `no_commits` only when that range is empty.

This holds for every agent kind, the read-only tester tier included, and
whether the generation was recorded before or after the collect.
Verified by: a real scheduler run of a tester-kind agent that commits once
reports `commits: 1` and no `no_commits` in both tools. This reproduces
ag-e2ebaa (commit 938dfa96, reported `no_commits`).

**ST4-R2.** A node run that truly made no commit still reports
`no_commits`. Two further rules:
- **Unrecorded generation.** A run whose generation is not recorded yet
  reports `commits_pending: true`, never `no_commits`.
- **Fixed per-run range.** A run's range is fixed once its generation is
  recorded. Commits made later by another run on the same node never count
  toward an earlier run.

Verified by:
- the existing no-commit node test;
- a collect before the generation is recorded;
- a loop node whose second run commits, after which the first run's count
  is unchanged.

### ST5 — the watchdogs do not fire on an agent that is running a suite

**ST5-R1, the doom loop.** Result digests already exist (providers.py:210,
supervisor.py:201). The rule:
- **What counts.** A repeated tool call counts toward
  `limits.doom_loop_repeats` only once it has completed, with identical
  arguments, an identical result digest, and no change in the sampled tree.
- **Pairing.** Calls and results are paired by call id when the provider
  gives one, so parallel calls are not mixed up.
- **Empty is not unavailable.** An empty result is a result. Where the
  stream carries no result at all (claude's mapping today), the rule falls
  back to today's arguments-plus-disk rule.
- **Kept.** The existing two-step cycle detection is kept.

Polling a growing log, or a progress line that changes, is not a loop.
Verified by:
- three identical commands with differing outputs do not trip;
- three with identical outputs and no disk change still trip;
- a call does not trip before its result has arrived;
- two parallel calls with crossed results are paired correctly;
- the fallback trips as today on a stream without results.

**ST5-R2, silence.** An agent is not silent while any of these holds:
- a tool call it started has not finished (paired by call id when given);
- for providers that announce a tool only when it ends (opencode), a live
  process descends from the agent CLI's own process inside its execution
  environment. Under the docker executor that is the container, observed
  through the recorded container pid and the executor's process
  inspection, not through the host `docker exec` client.

Processes that do not descend from that agent's CLI never count: other
agents, helpers, the wrapper. When the open tool or the last such child
ends, the silence count restarts from that moment. If the observation
fails, the exemption does not apply and the failure is logged. Polling
stays at the existing watch-loop interval or slower. The wall-clock limit
still applies.
Verified by:
- a started tool with no end event over the silence window does not trip;
- a fake opencode with a live descendant over the window does not trip;
- a live process of another agent does not exempt;
- an observation failure does not exempt;
- an idle agent with no child still trips.

**ST5-R3.** A watchdog reason names the evidence that tripped it: the
arguments and result digest, or the unchanged disk, or the silence
duration. When an open tool or a live child kept an agent from tripping
earlier, that is logged.
Verified by: the reasons asserted in the R1 and R2 tests.

### ST6 — waits and `scheduler stop` tell the truth

**ST6-R1.** `wait_for_agents` and `wait_for_nodes` never stay silent long
enough for the MCP client to abort them. A requested timeout above 1500 s
is clamped to 1500 s, and the reply carries `timeout_clamped: true` and the
effective value. It is otherwise a normal timed-out reply, with nothing
lost and everything still deliverable on the next call. Progress
notifications are allowed, but the cap does not depend on them.

The rules around the clamp:
- the timeout is validated before it is clamped, so a negative or
  non-numeric value is refused as today;
- clamping never acknowledges or consumes a transition;
- cursor and filter behaviour is unchanged;
- a wait that covers both agents and nodes has one total deadline.

Verified by:
- a request of 3000 s returns by 1500 s on an injected clock, with the
  clamp fields;
- a request of 300 s is unchanged;
- after a clamped return, a second read before `ack_nodes` still delivers
  the same transitions.

**ST6-R2.** `multiagents scheduler stop` reports:
- `stopped: true` when the daemon exits within its own shutdown bound (the
  engine's stop timeout plus a margin);
- `stopped: false` with the pid only if that process is still alive after
  the bound.

Exit is judged by the identity of the process, its pid and start time, not
by `process.json` disappearing.

Verified by:
- a daemon whose `engine.stop` takes 12 s reports `stopped: true`;
- a daemon that ignores SIGTERM reports `false` with its pid.

**ST6-R3.** The §4 trial defects need no work: two are fixed and one is the
contract (see Background). Recorded here, and no test is added.

### ST7 — a red main means a defect

**ST7-R1.** The baseline on main is exactly the 72 `test_phase2_*` reds,
listed by id in a pinned manifest `context/ts/manifests/baseline-reds.txt`.
The tester writes that manifest. A red outside it is a regression.
For each of these, the cause is fixed where it lies, in the test or in the
product:
- the flakes `sr_r5` (`test_sr_steer_exit_race`) and `nc_m1_adv3`;
- `test_breaker_lapsed_admission::test_refused_scheduler_activation_keeps_the_lapsed_trial_available`;
- `test_tb2_r6_alias_cleanup` (the `does_not_start_another` case);
- the environment reds `test_gg_r7_r9_*` (9) and `test_oa_*` (2).

No test may be skipped or weakened to get there.
Verified by:
- each fixed flake passes 20 consecutive runs under `-n 4` load;
- the environment tests pass from a clean worktree on this host and do not
  read the ambient git config, network or files they are not given.

Causes found by researcher ag-dcb561:
- **sr_r5: the test is wrong, deterministically.** Its fixture emits
  `NEED_INFO: …` without the `(<topic>)` the product requires
  (runner.py:535). Fix the fixture.
- **nc_m1_adv3: a contract mismatch.** The host issuance can wait up to 30 s
  on SQLite `BEGIN IMMEDIATE`, but the harness RPC client gives up after
  10 s, while the server allows 60 s. The tester aligns the harness deadline
  with the documented contention bound. If the product is meant to answer
  within 10 s under that load, that is a product fix and comes back to me.
- **The two others are test synchronisation.** `breaker_lapsed` and
  `tb2_r6` passed under load. `tb2_r6` asserts after a fixed `quiet(3)` and
  not on a settled event. They should wait on observable events.
- **gg/oa pass inside the agent container and fail on the host** (my
  suites run there). Their worlds are meant to be hermetic, so something
  on the host escapes them. The same tester must reproduce them on the
  host, through me, before fixing them.

### ST8 — test fixtures commit as exempt addresses

**ST8-R1.** No test commits under an email address that the git guard does
not exempt, except the guard tests that generate one on purpose. Done in
938dfa96 (three fixtures).
Verified by: a grep of `tests/` for commit identities.

The fingerprint `f8c4fcdc3fec9d79` stays in history; the user adds it to
`git.guard.allow_fingerprints`.

### ST9 — F10: an allowlist entry cannot act as a wildcard

**ST9-R1.** An `egress_allowlist` entry is refused if it is any of:
- a single label (`com`);
- a public suffix (`co.uk`, `github.io`);
- an IP address or IP fragment written as a suffix (`1.1`).

The public suffix rule:
- It follows a **pinned snapshot of the Public Suffix List**, shipped with
  the package, covering the ICANN and private sections, wildcard and
  exception rules. Nothing is downloaded at runtime.
- The snapshot is refreshed deliberately, by a script, in a commit of its
  own.
- Entries are normalised (IDNA A-label, lowercase, no trailing dot) before
  every check.
- IPv6 stays refused, as today.

An IPv4 address is accepted only whole, and then matches only exactly.

**ST9-R2.** A refused entry is a **non-fatal diagnostic**, not a startup
refusal. It must not go through `configuration_problems()`, which
`ensure_running()` treats as fatal. What happens to it:
- it is never written to the filter;
- it is reported with the entry and the reason at `multiagents run`, in
  `doctor`, and when the filter is written.

The valid entries remain active. If no valid entry remains, the filter
denies everything. An existing configuration holding a refused entry
therefore fails closed for that entry only. The configuration file itself
is never rewritten.
Verified by:
- `com`, `co.uk`, `github.io`, a wildcard-rule suffix and `1.1` are
  refused and absent from the filter;
- an exception-rule name is accepted;
- `example.com` admits `api.example.com` and not `evil-example.com`;
- `192.168.1.1` admits only itself;
- a config holding only `com` yields a deny-all filter, a reported
  diagnostic, and a project that still starts.

ST9 goes to the adversary after the implementer: it is the egress control.

## Routing

- **ST1, ST2, ST9:** one implementer, since they share `executor/docker.py`.
  The adversary covers ST9, the policy migration (ST1-R2) and the proxy
  replacement (ST2).
- **ST5:** its container process inspection is an executor interface; the
  ST5 implementer starts after the docker work is merged, or uses only the
  existing inspection (docker.py:2940).
- **ST3:** quick.
- **ST4 and ST6:** one implementer.
- **ST5:** deep.
- **ST7:** after the research, tester or implementer according to each
  cause.

Each implementer run gets a reviewer.

## Decisions

1. Where a restart policy would bring a container back onto a volatile
   mount source, the policy is dropped. The container is not moved, and its
   sources are not changed (plan item 1).
2. An allowlist change restarts only the egress proxy; a dropped in-flight
   connection is acceptable, and TF covers it (plan item 2).
3. Waits are capped at 1500 s rather than relying on MCP progress
   (ag-c53382: unverified whether progress resets Claude Code's idle timer).
4. A child depending on an already-finished node launching at once is the
   contract and stays.
5. A refused allowlist entry is dropped with a diagnostic, never fatal, and
   never widened. The PSL is a pinned snapshot shipped in the package
   (advisor 2026-10-12).
6. The advisor's contract review on 2026-10-12 (ag-aa1387, turn 10) is
   folded in: ST1 volatility, owner protocol and missing sources; ST2
   applied digest; ST4 pending generations; ST5 call pairing and container
   observation; ST6 ack safety; ST7 manifest.
7. The ST5 executor interface is `has_live_descendants(agent_id) -> bool |
   None`, on the executor (local and docker). It answers whether a live
   process descends from that agent's CLI in its execution environment.
   `None`, or an exception, means the observation failed, and grants no
   exemption. The tester proposed it (ag-9015b1) and the orchestrator
   decided it on 2026-10-12.
