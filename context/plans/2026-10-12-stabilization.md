---
status: ready
---
# Stabilisation before phase 7 part 2

Discussed with the user on 2026-10-12. The batch RR, TF, RM, PS2 and DJ is
merged (b13bfa9b). Nothing is running and no questions are open. The
orchestrator's last handoff in BRIEF (2026-10-12 ~00:00Z) lists tooling
defects that cost time every day. The plan of 2026-10-05 already said that
scheduler defects are fixed before part 2 is specified, since part 2 builds
on the scheduler.

The order the initializer proposed to the user is: this plan, then a triage
of the open findings, then phase 7 part 2.

## Apply now

Each item goes through the standing pipeline: tester, implementer, then a
reviewer on every implementer run. An item that is only a test change goes
to a tester and a reviewer. The orchestrator writes the contract, citing
these item names, and may group items that touch the same files.

**1. Containers created by tests must not come back at boot.**
- **The problem.** Tests create containers with `--restart unless-stopped`
  (`executor/docker.py`, three call sites). At boot, dockerd restarts them
  and recreates their missing bind-mount sources as root on the fresh
  `/tmp`. That left `/tmp/claude-1000` owned by root, and Claude Code would
  not start. The user had to `sudo rm -rf` it on 10-01, 10-05 and 10-10.
  See BRIEF, 2026-10-10 19:30Z.
- **The fix:**
  - containers created by tests get no restart policy and are removed at
    teardown, including after a killed pytest (the SL leak family,
    `context/specs/test-process-leak.md`);
  - the real project container keeps `unless-stopped` only if none of its
    mount sources can vanish at boot, which means nothing under `/tmp`.
    Otherwise it gets no restart policy, and `multiagents run` recreates
    it.
- **A test** that fails if any container created by the suite carries a
  restart policy.

**2. A change to `egress_allowlist` takes effect by itself.**
- **The problem.** The proxy container bind-mounts the filter file by inode.
  When the file is rewritten, the running proxy keeps the old content, and
  a HUP does not help. On 2026-10-10 the user had to restart the proxy by
  hand.
- **The fix.** Regenerating the filter recreates or restarts **only the
  proxy**, never the workspace container, so no agent is killed. Mounting
  the directory instead of the file would also avoid the inode trap.
- **What does not change.** Adding a host still needs the user's approval,
  every time.

**3. `effort` is translated for opencode.**
- A pin's `effort` (low, high) is not passed on as opencode's `--variant`,
  so glm-5.3 ran on its default variant on 10-10.
- **The fix.** Map `effort` to the variant for opencode providers, or
  accept `variant` in pins. The contract chooses one; mapping is
  preferred, so pins stay provider-neutral.

**4. `collect_agent` reports `no_commits` for node runs** that do have
commits: it reads `agents/<agent>/<id>`, not `nodes/<node>`. This is in the
§4 trial report and was seen again on 2026-10-11.

**5. False alarms from the watchdogs while an agent runs a long suite.**
- The doom-loop detector flags an agent that is tailing a growing log.
- The 900 s silence watchdog fires while opencode runs a suite, because
  opencode emits the bash tool event only when the command finishes.
- **The fix.** A live child process, or a tool start, counts as activity.

**6. Small scheduler and MCP defects:**
- `wait_for_agents` and `wait_for_nodes` beyond about 30 min hit the MCP
  idle timeout. Cap the wait, or keep the connection alive, so a long
  wait cannot fail silently.
- `scheduler stop` answers `{"stopped": false}` although the daemon did
  stop.
- A researcher first checks which of the remaining defects in the §4 trial
  report are still present on main. Only those are fixed:
  - `wait_for_nodes` returns an empty `capacity`;
  - `windows` entries pile up for every node ever created;
  - a child depending on a node that has already finished launches at
    once. That one is a decision: the researcher says whether it is by
    design.

**7. Tests that fail without a code defect:**
- the flakes `sr_r5`, `nc_m1_adv3`,
  `test_breaker_lapsed_admission::test_refused_scheduler_activation_keeps_the_lapsed_trial_available`
  and `test_tb2_r6_alias_cleanup::…does_not_start_another`;
- the 11 reds that depend on the environment, `test_gg_r7_r9` (9) and
  `test_oa_*` (2), red on main. The `oa` tests see real network
  connections.

A researcher finds the cause first. A tester fixes the test, unless the
cause is in the product. The goal is a main baseline of exactly the 72
known `test_phase2_*` reds.

**8. A test fixture that trips the git guard.** The commit identity
`f@x.invalid` in `tests/test_tb2_r1_collect_node_commits.py:149` (commit
540fd97) is fictional. GG-R3 still flags it, because only `example.invalid`
is exempt; the guard is right. A tester changes it to `f@example.invalid`.
- **This alone does not unblock the push.** The address stays in history,
  so the user adds the fingerprint `f8c4fcdc3fec9d79` to
  `git.guard.allow_fingerprints` themselves.
- **A check** that no other test commits as an address the guard does not
  exempt. A grep over `tests/` for `user.email=` is enough.

**9. The ledger, F10 and F120.**
- **F120** (claude's multi-account quota) is believed fixed: the
  characterization tests on `config_dir` and `extends` passed on
  2026-10-05. The orchestrator verifies it and sets it `fixed` with the
  evidence.
- **F10** (a short suffix acting as a wildcard in `egress_allowlist`) is
  still `scheduled`. A researcher reproduces it on main:
  - if it is still present, it goes through the pipeline like the other
    items, with the adversary, because it is the egress control;
  - otherwise it is set `fixed` with the evidence.

## Next phase

- **Triage of the 48 `open` findings with the user,** led by the five the
  report leads with. The high-severity findings in the docker executor and
  the proxies (F24, F30, F60, F61, F62) are decided **before** part 2,
  which rewrites that executor.
- **Phase 7 part 2: one container per run.** It is specified with the user
  from `phase7-nodes-and-containers.md` (PAC-R*), as amended. It covers:
  - tmux step 2 (tmux as the process supervisor; the user confirmed on
    2026-10-10 that tmux stays, not Zellij);
  - a live node view, for visibility only, with each node's diff, attach
    and shell. The scheduler and the orchestrator keep every decision;
  - microVM isolation as an option to evaluate. It still needs checking
    whether it runs on Linux without Docker Desktop.
- **Still queued, not in this plan:**
  - DJ-b (DJ-R7, needs real Stop-hook payloads);
  - specs 2b (hang detection, stop attribution), 4b (publication) and 4c
    (short outputs with pagination);
  - the kimi-k2.6 server errors at startup;
  - the review's phases 2 and 3.

## Config changes

- **None applied by this plan.**
- **For the user, outside the plan:** add the fingerprint
  `f8c4fcdc3fec9d79` to `git.guard.allow_fingerprints`, so that the 310
  unpushed commits can be pushed.

## Notes considered

`context/notes/` contains only README.md: there are no user notes.
