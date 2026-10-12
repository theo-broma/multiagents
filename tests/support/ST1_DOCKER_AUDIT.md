# ST1-R4/R5 Docker test audit

The reported PS recovery test selected the real Docker executor without a
daemon double. On a Docker host its post-recovery launch could create the
workspace, egress proxy and project network. Its assertions concern cooldown
recovery, so both PS context cases now use the real executor with an unavailable
daemon double through the existing `_run` / `docker_available` seams. The
provider check scripts and every original assertion remain. Each case asserts
that it adds no Docker binary records to the session guard.

Ordinary CLI setup also probes Docker availability even for local projects.
`st_docker_isolation.py` now installs an unavailable executable behind the R5
recording guard and ahead of ambient Docker, unless the test has **both**
`SG_TEST_DOCKER=1` and the `st_live_docker` fixture. Test-provided executable
Docker doubles continue to precede this fallback. The two C5 doctor cases
narrow PATH themselves; their helper now preserves the guard and fallback.

The guard's policy parser, R1 exception, forwarding, ownership and reaper are
unchanged. Regression assertions require its failure to include the full
`test_nested.py::test_forbidden (call)` identifier. A deliberately unsafe run
through the new fallback still fails at teardown.

## Inspection and executable audit

Searched every test/support Python file for executor kind Docker,
`make_docker_executor`, `DockerExecutor`, all ensure entry points, Docker
subprocesses, daemon-read methods and PATH replacements. Constructor, mount,
argument and environment-only tests do not contact Docker. Executing ST tests
use `DockerWorld` or `_run` doubles; SG mount/stop tests, SP/PF transport tests,
Codex executor tests and OA tests install fake executable daemons. Login tests
replace launch/exec; NC Docker cases inspect mount inventories. Liveness and
OOM tests mostly replace their public probe or subprocess seams; the M test
listed below missed a real verdict/status probe. No hard-coded real Docker
binary path that executes was found.

The broad targeted audit used a recording, exit-127 `docker` executable ahead
of the host PATH (and behind the suite's original recording shim), with all
live opt-ins unset. It ran core, C1/C2 (including C23), PS, OA, subagent MCP,
NC M1, C17, AB, Q3, C5/C6, SG, SP, survival, DK and M tests under `-n 4`.
Result: **3612 passed, 12 skipped**, in 565.00 s. This was a subset, not a full
suite. The exit-127 boundary records daemon reachability without creating
containers; on the reported host the PS images existed, so its same path
continued into the two real `docker run` calls supplied in the task.

The 60 ordinary test instances below can reach ambient Docker before the
fix. The C5 cases were identified by inspection because their hard-coded
system PATH bypassed the audit binary; the others were recorded executing the
binary. Most calls were `docker info` from initialization, two core cases
inspected image/container state, and M queried container liveness/status.
PS2's entry is a nested pytest session's owner-reaper listing.

### tests/test_c5_doctor_probe.py

- `test_c5_r1_the_doctor_row_reports_the_version_not_missing`
- `test_c5_r2_a_missing_container_cli_is_a_doctor_problem`

### tests/test_core.py

- `test_a_container_that_cannot_take_the_new_mount_says_so`
- `test_a_live_session_elsewhere_is_not_reaped`
- `test_a_session_cut_off_is_told_so_when_it_comes_back`
- `test_declining_is_distinct_from_fixing`
- `test_docker_refuses_to_mount_the_socket`
- `test_init_agent_makes_the_same_checks_as_run`
- `test_init_commits_the_project_without_its_runtime_state`
- `test_init_exits_non_zero_when_it_leaves_a_project_unspawnable`
- `test_init_exits_zero_once_the_project_can_actually_run`
- `test_init_leaves_a_non_git_project_alone_when_declined`
- `test_init_names_the_repository_when_it_is_not_the_project_root`
- `test_init_refuses_inside_an_existing_project`
- `test_nested_overrides_the_refusal`
- `test_orphaned_agents_are_reaped_before_a_new_session`
- `test_reporting_state_is_not_refusing_to_start`
- `test_resolving_a_reported_ticket_says_it_is_still_open_upstream`
- `test_resolving_an_unfiled_ticket_offers_to_file_it`
- `test_resolving_an_unknown_ticket_is_an_error`
- `test_run_refuses_when_the_container_could_not_be_built`
- `test_stop_commits_what_a_killed_agent_left_behind`
- `test_stop_ends_the_thing_that_starts_more_agents`
- `test_stop_leaves_the_state_that_makes_a_resume_possible`
- `test_the_checks_report_shows_the_graph_not_a_score`
- `test_the_docker_offer_is_declined_without_a_terminal`
- `test_the_rework_rate_counts_only_what_was_judged`

### tests/test_m_opus_fixes.py

- `test_an_unanswerable_container_keeps_a_crashed_owners_hold`

### tests/test_ps2_ambient.py

- `test_ps2_r7_t1_per_id_outcomes_ignore_scheduler_enabled_start_directory`

### tests/test_ps_review_fixes.py

- `test_1_a_matching_context_recovers`

### tests/test_sandbox_git_branch_delete.py

- `test_sg_r2_container_then_host_end_to_end`
- `test_sg_r2_control_discard_agent_deletes_at_once_and_records_nothing`
- `test_sg_r2_control_merge_agent_deletes_at_once_and_records_nothing`
- `test_sg_r2_discard_agent_emits_an_event_for_the_pending_delete`
- `test_sg_r2_discard_agent_records_branch_pending_delete`
- `test_sg_r2_discard_agent_succeeds_when_the_branch_cannot_be_deleted`
- `test_sg_r2_discard_of_a_commitless_branch_also_records_it`
- `test_sg_r2_host_cleanup_of_an_already_gone_branch_is_harmless[discard_agent]`
- `test_sg_r2_host_cleanup_of_an_already_gone_branch_is_harmless[merge_agent]`
- `test_sg_r2_host_cleanup_of_an_already_gone_branch_is_harmless[reconcile]`
- `test_sg_r2_host_cleanup_of_an_already_gone_branch_is_harmless[runner_start]`
- `test_sg_r2_host_deletes_pending_branches_of_finished_nodes[discard_agent]`
- `test_sg_r2_host_deletes_pending_branches_of_finished_nodes[merge_agent]`
- `test_sg_r2_host_deletes_pending_branches_of_finished_nodes[reconcile]`
- `test_sg_r2_host_deletes_pending_branches_of_finished_nodes[runner_start]`
- `test_sg_r2_host_never_deletes_a_branch_no_tree_node_owns[discard_agent]`
- `test_sg_r2_host_never_deletes_a_branch_no_tree_node_owns[merge_agent]`
- `test_sg_r2_host_never_deletes_a_branch_no_tree_node_owns[reconcile]`
- `test_sg_r2_host_never_deletes_a_branch_no_tree_node_owns[runner_start]`
- `test_sg_r2_host_never_deletes_a_live_nodes_branch[discard_agent]`
- `test_sg_r2_host_never_deletes_a_live_nodes_branch[merge_agent]`
- `test_sg_r2_host_never_deletes_a_live_nodes_branch[reconcile]`
- `test_sg_r2_host_never_deletes_a_live_nodes_branch[runner_start]`
- `test_sg_r2_host_never_deletes_outside_refs_heads_agents[discard_agent]`
- `test_sg_r2_host_never_deletes_outside_refs_heads_agents[merge_agent]`
- `test_sg_r2_host_never_deletes_outside_refs_heads_agents[reconcile]`
- `test_sg_r2_host_never_deletes_outside_refs_heads_agents[runner_start]`
- `test_sg_r2_merge_agent_emits_an_event_for_the_pending_delete`
- `test_sg_r2_merge_agent_records_branch_pending_delete`
- `test_sg_r2_merge_agent_succeeds_when_the_branch_cannot_be_deleted`

## Real-daemon tests (14 instances, explicitly opted in)

- `tests/test_sandbox_git_docker_live.py`:
  `test_sg_r3_end_of_run_commit_hook_runs_inside_the_container`,
  `test_sg_r3_restore_paths_commit_hook_runs_inside_the_container`,
  `test_sg_r1_an_agent_rewritten_in_tree_hook_never_runs_on_the_host`,
  `test_sg_r2_live_writing_each_protected_path_fails`,
  `test_sg_r2_live_replacing_each_protected_path_by_rename_fails`,
  `test_sg_r2_live_git_cannot_move_the_base_or_the_main_head`,
  `test_sg_r2_live_worktree_and_branch_operations_still_succeed`,
  `test_sg_r2_live_protection_survives_the_host_rewriting_files_by_rename`.
- `tests/test_st1_st2_docker_live.py`:
  `test_st1_r4_existing_live_fixture_has_no_restart_and_controller_owner`,
  `test_st2_r1_r2_workspace_uses_updated_proxy_without_manual_action`.
- `tests/test_agent_survival.py`:
  `test_sv_r1_every_line_reaches_a_run_file_even_after_the_server_is_sigkilled[docker]`,
  `test_sv_r4_the_wrapper_kills_the_process_group_at_the_timeout[obeys-term-docker]`,
  `test_sv_r4_the_wrapper_kills_the_process_group_at_the_timeout[ignores-term-docker]`.
- `tests/test_session_persistence.py`:
  `test_sp_r1_docker_variant_a_transcript_survives_container_recreation`.

SG and ST live fixtures already apply the R4 restart-policy/owner-label wiring
and remove containers plus their project network. Survival already requested
that fixture and cleaned all three container names plus the network; it now
requires SG opt-in in addition to its existing SV selector. SP lacked the
fixture: it now requests it, keeps the recording guard when removing its own
fake executable from PATH, requires SG plus SP opt-in, and removes workspace,
proxy, auth sidecar and the network in `finally`.

The controller's session-start owner reaper intentionally remains independent
of live-test opt-in (ST1-R4: a later suite reaps provably dead labelled owners).
It is not a test invocation. In the post-fix audit it was the only caller of
the recording host substitute; no test-attributed calls reached it.

## Verification

Six new cases in `test_st1_test_ownership.py` cover the opt-in/fixture truth
table for direct and child-process Docker calls (four cases), strict R5
rejection through the fallback, and running the original PS/core assertions
with an available recording host substitute. The latter executes four original
tests and confirms zero test calls at the substitute, with SG opt-in absent.
No production stubs or source changes were made.

Requested targeted run, plus core and affected SG live fixture:

```sh
PYTHONPATH=src .venv/bin/python -m pytest -q -p no:cacheprovider -n 4 \
  --basetemp=/var/tmp/pt-83c8de-final --durations=15 \
  tests/test_ps_review_fixes.py tests/test_st*.py tests/test_c5_doctor_probe.py \
  tests/test_session_persistence.py tests/test_agent_survival.py \
  tests/test_sandbox_git_docker_live.py tests/test_core.py
```

Result: **1046 passed, 14 skipped**, in 222.86 s. The skips are the 14 opt-in
real-daemon instances above. The longest cases were existing survival checks:
`test_sv_r8_the_servers_downtime_is_not_silence` 9.82 s,
`test_sv_r4_the_wrapper_kills_the_process_group_at_the_timeout[ignores-term-local]`
9.55 s, and `test_sv_r5_after_the_owner_is_sigkilled_another_live_server_adopts`
8.37 s. These exercise real shutdown/adoption/timeout windows; no new case
exceeds one second in its isolated `-n 4 --durations=15` run (PS/core proof:
0.98 s, opt-in truth table: 0.62–0.66 s, strict fallback: 0.47 s).

No missing-toolchain request was needed. Docker is absent as stated in the
task; real-daemon teardown could only be inspected here. Ownership/restart
wiring is exercised against a fake host daemon by the existing controller
label test. No contract behavior was left without either executable fake-daemon
coverage or the existing opt-in host tests.

Follow-up for the last discovered callers:

```sh
PYTHONPATH=src .venv/bin/python -m pytest -q -p no:cacheprovider -n 4 \
  --basetemp=/var/tmp/pt-83c8de-extra --durations=15 \
  tests/test_m_opus_fixes.py tests/test_sandbox_git_branch_delete.py \
  tests/test_ps2_ambient.py
```

Result: **72 passed**, in 33.66 s, with zero test-attributed calls to the
recording host substitute across both post-fix runs. The existing PS2 nested
pytest comparison took 24.05 s; other follow-up cases were at most 1.54 s.
The broader audit's 91.39 s NC concurrency and 62.01 s SP drift/recovery cases
are existing tests outside the changed files; they were retained unchanged.
