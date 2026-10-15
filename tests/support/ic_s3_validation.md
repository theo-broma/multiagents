# Slice 3 test handoff

Contract: `context/specs/phase7-part2-contract.md`, Slice 3 and K17.
This is a red-suite handoff, not implementation acceptance.

## Coverage

| Requirement | Tests |
| --- | --- |
| IC-C14 / K17.1 | `test_ic_c14_docker_backend.py`, `test_ic_c14_scheduler_docker.py`: public constructor, lazy construction, selection, configured CLI/image, legacy settings independence, resource validation/defaults/overrides, Docker state/output, loss-of-response recovery |
| IC-C14 / K17.3 | `test_ic_c14_seam.py`, `test_ic_c14_seam_execution.py`: typed outcomes, attachment/idempotence, invalid paths, live-state refusal, source lifetime/snapshot independence, actual attached checkout/HOME/stdin, retention/discard, daemon uncertainty |
| IC-C14 / K17.4 | Exact generated label keys/digest, four generated-label refusals, host-supplied project ownership, preserved test-session label, project filtering |
| IC-C15 | `test_ic_c15_adoption.py`: replay, partial lines, five launch boundaries, adoption of running/exited/absent/unknown resources, recorded backend after config change; reference + opt-in Docker |
| IC-C16 / K17.2 | `test_ic_c16_stop_cancel_steer.py`, `test_ic_k17_provider_needs.py`: validation, inheritance, omitted defaults, refusal precedence, no resource or preparation side effect, credentialless launch, cancel, forged pids, steer unsupported session resume, config change, opt-in setsid containment |
| IC-C17 | `test_ic_c17_exit_oom.py`: exit/OOM parsing, cause attribution and today's TF crash classification; opt-in hard-memory OOM |
| IC-C18 / K17.5 | `test_ic_c18_retention_orphans.py`: retained output/exit/repository, failed capture retention, discard; sorted CLI/status parity, both orphan reasons, project filtering, creation window, uncertainty, scoped visibility |
| IC-C18 / K17.6 | Exact full ids, live/exited removal, repeated removal, retained/non-orphan and foreign-project refusal, uncertainty, no agent RPC removal |
| IC-C19 | `test_ic_c19_deferred_findings.py`: F34, F31, F32, F35, F64, F65, F69; F70 starts real local groups, checks a reaped stale handle and a forged public start identity with syscall spies. |
| Milestone 3 | `test_ic_c14_docker_conformance.py` reuses the public C5 conformance cases including containment; existing C2/C6/C10 milestone cases remain acceptance tests. `ic_support`/`ic_probe` explicitly declare the fixture providers' needs. |

`tests/support/fake_docker.py` is a CLI/daemon simulator, not a backend double.
It records argv and scripts daemon/image failures, create/start response loss,
state, exit/OOM, logs, injected containers, removal and daemon uncertainty.
Eight self-tests exercise the simulator. K3's permitted backend wrapper adds
crashes at public lifecycle boundaries; its journal captures the provision key
without trying to query a dead scheduler. No production surface was stubbed.

## Run

```sh
PYTHONPATH=src uv run --frozen python -m pytest -q -p no:cacheprovider -n 4 \
  --basetemp=/var/tmp/pt-8856fd --durations=15 \
  tests/test_fake_docker_selftest.py tests/test_ic_c14* tests/test_ic_c15* \
  tests/test_ic_c16* tests/test_ic_c17* tests/test_ic_c18* tests/test_ic_c19* \
  tests/test_ic_k17_provider_needs.py
```

A comparison uses exactly `3127f4f7:src` temporarily overlaid in this worktree;
no slice 2 implementation is committed or merged. Main's source is restored.
Existing milestone files were also checked with `-m ic_milestone` (same pytest
flags): `test_ic_c2_recording.py`, `test_ic_c6_isolation.py`,
`test_ic_c10_fresh_clone.py`. Counts and timings follow below.

## Clarifications resolved

- K17.4: the host supplies `multiagents.project` through `spec.labels`, as in K12.
  Only owner, lifetime, creation_key and spec_digest are backend-generated and refused.
- F70: the deterministic substitute for PID reassignment is now covered. The first
  real local process exits and is reaped through `Handle.wait` before stale stop.
  The second remains live with its public `pid_start` identity forged to `"0"`.
  Spies delegate to real `os.kill` and `os.killpg`; neither may send a signal to
  the recorded process or group. Teardown reaps both processes even on failure.
  On both baselines the second case observes SIGTERM sent to the unverified group.
- K17.3: reference-backend uncertainty injection is **not applicable**, per the
  clarification. The Docker fake CLI covers checkout/attachment/input uncertainty;
  the existing K3-injected adoption test covers the Runner keeping the attempt
  and reservation on Unknown, then recovering without relaunch once it clears.
  No separate reference checkout/input uncertainty case is required.

No remaining contract silences. No production surface was stubbed.

The intermediate df1bda8a seam-Unknown Runner tests were withdrawn before
handoff: they required a launch failure specifically, whereas K6 also permits
a liveness hold. The existing K3 adoption test asserts the agreed Unknown
behaviour without inventing a seam-specific retry policy.

No missing-toolchain request was necessary. `uv run --frozen` created the normal
worktree environment. SG_TEST_DOCKER was not enabled. All 91 Docker instances
not run are listed individually in `ic_s3_docker_not_run.txt`; the existing C6
and C10 Docker milestone cases were also skipped, as expected without the switch.

Red outcomes have these intended causes: unavailable public DockerBackend or
reference provisioning seam (explicit assertion guards, not collection errors),
missing config validation, missing provider-needs refusal, wrong typed seam
outcomes/cleanup, replay duplication/partial-line behavior, recovery/cancellation
failures, missing steer metadata, and missing HOME-failure/capture-failure
handling. No tests were weakened to match those implementation failures.

## Observed results

Final 368-case targeted suite, 2026-10-15, under `-n 4`:

Main ef8318c8: **79 failed, 22 passed, 91 skipped, 176 errors in 8.28s**.

Slice 2 source 3127f4f7: **94 failed, 51 passed, 92 skipped, 131 errors in 80.33s (0:01:20)**.

Errors are fixture-level explicit missing-surface assertion guards, with no collection errors. Red includes failures and errors.

| File | Main pass / red / skip | Slice 2 pass / red / skip |
| --- | --- | --- |
| `test_fake_docker_selftest.py` | 8 / 0 / 0 | 8 / 0 / 0 |
| `test_ic_c14_docker_backend.py` | 0 / 98 / 0 | 0 / 98 / 0 |
| `test_ic_c14_docker_conformance.py` | 0 / 0 / 55 | 0 / 0 / 55 |
| `test_ic_c14_scheduler_docker.py` | 9 / 47 / 0 | 9 / 47 / 0 |
| `test_ic_c14_seam.py` | 0 / 22 / 0 | 7 / 14 / 1 |
| `test_ic_c14_seam_execution.py` | 0 / 1 / 1 | 1 / 0 / 1 |
| `test_ic_c15_adoption.py` | 0 / 23 / 23 | 15 / 8 / 23 |
| `test_ic_c16_stop_cancel_steer.py` | 0 / 24 / 10 | 6 / 18 / 10 |
| `test_ic_c17_exit_oom.py` | 0 / 5 / 1 | 0 / 5 / 1 |
| `test_ic_c18_retention_orphans.py` | 0 / 19 / 1 | 0 / 19 / 1 |
| `test_ic_c19_deferred_findings.py` | 1 / 8 / 0 | 1 / 8 / 0 |
| `test_ic_k17_provider_needs.py` | 4 / 8 / 0 | 4 / 8 / 0 |

The 22 green main cases are the eight fake-CLI self-tests, nine accepted
resource-config cases, four accepted provider-declaration/inheritance config
cases, and the reaped stale-handle regression. Accepted-config greens alone
do not prove the new semantics: main has no corresponding validation yet.

F70's reaped case passes on both baselines; its identity-mismatch case fails
on its intended syscall-observation assertion: SIGTERM reaches the recorded
process group despite the forged start identity. It completes in well under
a second. No syscall or subprocess behaviour is stubbed.

Other red reasons remain: missing public DockerBackend and reference seam,
missing configuration/provider-needs validation and refusal, incorrect typed
seam outcomes and cleanup, replay duplication/partial lines, crash-recovery
and stop/cancel failures, missing steer metadata, and unhandled HOME/capture
failures. Missing-surface guards deliberately fail before dependent actions.
No baseline implementation was changed to make these tests pass.

### Slowest tests (`--durations=15`)

Main ef8318c8:

```text
0.38s call     tests/test_fake_docker_selftest.py::test_lifecycle_start_stop_rm_and_output
0.36s call     tests/test_ic_c14_scheduler_docker.py::test_ic_c14_f32_unset_means_the_default
0.28s call     tests/test_ic_c14_scheduler_docker.py::test_ic_c14_f32_a_zero_or_negative_resource_is_a_config_error_naming_the_key[garbage-cpus]
0.26s call     tests/test_fake_docker_selftest.py::test_a_running_container_needs_stop_or_force_and_stop_is_idempotent
0.24s call     tests/test_ic_c14_scheduler_docker.py::test_ic_c14_f32_a_zero_or_negative_resource_is_a_config_error_naming_the_key[0_1-cpus]
0.23s call     tests/test_ic_c14_scheduler_docker.py::test_ic_c14_f32_a_zero_or_negative_resource_is_a_config_error_naming_the_key[0_0-memory]
0.18s call     tests/test_fake_docker_selftest.py::test_a_name_in_use_and_a_missing_image_are_dockers_errors
0.17s call     tests/test_fake_docker_selftest.py::test_ps_filters_by_label_and_hides_nothing_with_a
0.15s call     tests/test_fake_docker_selftest.py::test_rules_and_the_call_log
0.09s call     tests/test_ic_c14_scheduler_docker.py::test_ic_c14_f32_a_zero_or_negative_resource_is_a_config_error_naming_the_key[-5g-memory]
0.08s call     tests/test_ic_k17_provider_needs.py::test_k17_2_invalid_needs_names_provider_and_field[false]
0.08s call     tests/test_ic_k17_provider_needs.py::test_k17_2_invalid_needs_names_provider_and_field[bad1]
0.08s call     tests/test_ic_k17_provider_needs.py::test_k17_2_valid_boolean_declarations_load[needs2]
0.08s call     tests/test_ic_c14_scheduler_docker.py::test_ic_c14_f32_a_null_resources_section_is_unset_not_zero
0.08s call     tests/test_ic_c14_scheduler_docker.py::test_ic_c14_f32_a_zero_or_negative_resource_is_a_config_error_naming_the_key[0.0-cpu_shares]
```

Slice 2 source 3127f4f7:

```text
17.88s call     tests/test_ic_c15_adoption.py::test_ic_c15_boundary_3_started_but_the_invocation_id_was_not_recorded_is_adopted_not_rerun[reference]
15.02s call     tests/test_ic_c19_deferred_findings.py::test_ic_c19_f34_unusable_home_is_a_launch_failure_naming_the_path
13.92s call     tests/test_ic_c16_stop_cancel_steer.py::test_ic_c16_forged_pid_files_naming_a_live_decoy_never_make_stop_signal_it[reference]
13.89s call     tests/test_ic_c16_stop_cancel_steer.py::test_ic_c16_cancel_stops_the_resource_through_the_recorded_backend_and_confirms_it[reference]
13.82s call     tests/test_ic_c16_stop_cancel_steer.py::test_ic_c16_k6_an_unconfirmed_stop_keeps_the_hold_and_the_attempt_is_not_declared_cancelled[reference]
10.08s call     tests/test_ic_c15_adoption.py::test_ic_c15_a_partial_line_at_the_kill_is_not_doubled_after_recovery[reference]
9.65s call     tests/test_ic_c15_adoption.py::test_ic_c15_adopt_running_the_attempt_finishes_normally_and_its_output_is_whole[reference]
9.50s call     tests/test_ic_c15_adoption.py::test_ic_c15_a_scheduler_kill_while_streaming_loses_and_duplicates_no_byte[reference]
9.12s call     tests/test_ic_c15_adoption.py::test_ic_c15_replay_an_append_that_outran_its_checkpoint_is_discarded_and_replayed[reference-partial_line-stderr]
8.44s call     tests/test_ic_c15_adoption.py::test_ic_c15_replay_an_append_that_outran_its_checkpoint_is_discarded_and_replayed[reference-whole_lines-stdout]
8.03s call     tests/test_ic_c15_adoption.py::test_ic_c15_replay_an_append_that_outran_its_checkpoint_is_discarded_and_replayed[reference-partial_line-stdout]
7.55s call     tests/test_ic_c16_stop_cancel_steer.py::test_ic_c16_d_s3_2_a_provider_declaring_a_need_is_refused_unsupported_not_retried_reference[credentials-credential_handle]
7.49s call     tests/test_ic_c16_stop_cancel_steer.py::test_ic_c16_d_s3_2_a_provider_declaring_a_need_is_refused_unsupported_not_retried_reference[rpc-transport_handle]
7.48s call     tests/test_ic_c16_stop_cancel_steer.py::test_ic_c16_d_s3_2_a_provider_without_a_declaration_counts_as_needing_all_three
7.37s call     tests/test_ic_c16_stop_cancel_steer.py::test_ic_c16_d_s3_2_a_provider_declaring_a_need_is_refused_unsupported_not_retried_reference[network-network]
```

The slower slice 2 cases run real scheduler/worker processes, crash/restart
and reap them; broken transitions consume their bounded wait plus teardown.
Replay cases also wait for real producer output at the crash boundary. These
are targeted integration tests; no full suite was run.

Previously recorded milestone-only check (unchanged tests, not rerun in this
finishing pass): **2 passed, 2 failed, 11 skipped** on main (13.92 s) and slice 2
(14.89 s). The two reds are existing C2b expectations; Docker C6/C10 skipped
without the opt-in. These are separate from the 368-case counts above.
