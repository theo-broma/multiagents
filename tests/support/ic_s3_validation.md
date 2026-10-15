# Slice 3 test handoff

Contract: `context/specs/phase7-part2-contract.md`, Slice 3 and K17.
This is a red-suite handoff, not implementation acceptance.

## Coverage

| Requirement | Tests |
| --- | --- |
| IC-C14 / K17.1 | `test_ic_c14_docker_backend.py`, `test_ic_c14_scheduler_docker.py`: public constructor, lazy construction, selection, configured CLI/image, legacy settings independence, resource validation/defaults/overrides, Docker state/output, loss-of-response recovery |
| IC-C14 / K17.3 | `test_ic_c14_seam.py`, `test_ic_c14_seam_execution.py`: typed outcomes, attachment/idempotence, invalid paths, live-state refusal, source lifetime/snapshot independence, actual attached checkout/HOME/stdin, retention/discard, daemon uncertainty |
| IC-C14 / K17.4 | Exact generated label keys/digest, reserved-label refusal (including project), preserved test-session label, project filtering |
| IC-C15 | `test_ic_c15_adoption.py`: replay, partial lines, five launch boundaries, adoption of running/exited/absent/unknown resources, recorded backend after config change; reference + opt-in Docker |
| IC-C16 / K17.2 | `test_ic_c16_stop_cancel_steer.py`, `test_ic_k17_provider_needs.py`: validation, inheritance, omitted defaults, refusal precedence, no resource or preparation side effect, credentialless launch, cancel, forged pids, steer unsupported session resume, config change, opt-in setsid containment |
| IC-C17 | `test_ic_c17_exit_oom.py`: exit/OOM parsing, cause attribution and today's TF crash classification; opt-in hard-memory OOM |
| IC-C18 / K17.5 | `test_ic_c18_retention_orphans.py`: retained output/exit/repository, failed capture retention, discard; sorted CLI/status parity, both orphan reasons, project filtering, creation window, uncertainty, scoped visibility |
| IC-C18 / K17.6 | Exact full ids, live/exited removal, repeated removal, retained/non-orphan and foreign-project refusal, uncertainty, no agent RPC removal |
| IC-C19 | `test_ic_c19_deferred_findings.py`: F34, F31, F32, F35, F64, F65, F69. F70 remains unexpressed; see questions below. |
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
  --basetemp=/var/tmp/pt-f3eff5 --durations=15 \
  tests/test_fake_docker_selftest.py tests/test_ic_c14* tests/test_ic_c15* \
  tests/test_ic_c16* tests/test_ic_c17* tests/test_ic_c18* tests/test_ic_c19* \
  tests/test_ic_k17_provider_needs.py
```

A comparison uses exactly `3127f4f7:src` temporarily overlaid in this worktree;
no slice 2 implementation is committed or merged. Main's source is restored.
Existing milestone files were also checked with `-m ic_milestone` (same pytest
flags): `test_ic_c2_recording.py`, `test_ic_c6_isolation.py`,
`test_ic_c10_fresh_clone.py`. Counts and timings follow below.

## Open contract questions

- **NEED_INFO(K17.4):** K17 reserves and refuses `multiagents.project` in
  `spec.labels`, while the existing K12/C5 public fixture supplies project
  ownership there. What public host-issued input supplies the project to
  `DockerBackend(root, *, seeds, cli, image, resources)`? Until answered,
  direct-backend acceptance still uses the existing project fixture, alongside
  the newly required reserved-project refusal test. These cannot both be green
  until the project authority input is specified and fixtures are aligned.
- **NEED_INFO(IC-C19/F70):** Which public injected identity/clock seam may force
  PID/group reassignment for the local `Handle.stop` contract? Real kernel PID
  reuse cannot safely be forced in a fast test, process fakes are forbidden,
  and inspecting private state or call order would violate the black-box rule.
  No placeholder test pretending to prove this race was added.
- **NEED_INFO(K17.3):** Which public seam can deterministically put the reference
  backend's checkout/input association into uncertain state? Docker daemon
  uncertainty is covered; the reference seam's Unknown branches are not.

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

Main ef8318c8: **21 passed, 78 failed, 176 errors, 91 skipped**, 6.61 s.
Slice 2 source 3127f4f7: **50 passed, 93 failed, 131 errors, 92 skipped**, 75.22 s.
Errors are fixture-level explicit missing-surface assertion guards; neither run had collection errors.

| File | Main pass / red / skip | Slice 2 pass / red / skip |
| --- | --- | --- |
| `test_fake_docker_selftest.py` | 8 / 0 / 0 | 8 / 0 / 0 |
| `test_ic_c14_docker_backend.py` | 0 / 98 / 0 | 0 / 98 / 0 |
| `test_ic_c14_docker_conformance.py` | 0 / 0 / 55 | 0 / 0 / 55 |
| `test_ic_c14_scheduler_docker.py` | 9 / 47 / 0 | 9 / 47 / 0 |
| `test_ic_c14_seam.py` | 0 / 22 / 0 | 7 / 14 / 1 |
| `test_ic_c14_seam_execution.py` | 0 / 1 / 1 | 1 / 0 / 1 |
| `test_ic_c15_adoption.py` | 0 / 23 / 23 | 14 / 9 / 23 |
| `test_ic_c16_stop_cancel_steer.py` | 0 / 24 / 10 | 7 / 17 / 10 |
| `test_ic_c17_exit_oom.py` | 0 / 5 / 1 | 0 / 5 / 1 |
| `test_ic_c18_retention_orphans.py` | 0 / 19 / 1 | 0 / 19 / 1 |
| `test_ic_c19_deferred_findings.py` | 0 / 7 / 0 | 0 / 7 / 0 |
| `test_ic_k17_provider_needs.py` | 4 / 8 / 0 | 4 / 8 / 0 |

The 21 green main cases are the eight fake-CLI self-tests, nine accepted resource-config cases, and four accepted provider-declaration/inheritance config cases. The source does not yet validate the new fields, so accepted-config greens alone are not proof of semantics.

Under -n 4, main's slowest case was fake lifecycle/output (0.29 s). Slice 2's slowest were boundary-3 recovery (17.70 s), F34 HOME failure (14.78 s), unconfirmed-stop recovery (13.82 s), and cancel (13.64 s). These include real scheduler/worker startup, crash/restart and teardown; the red implementations consume the explicit state-transition deadline. Partial-line recovery (10.39 s) additionally requires a real producer paused across the crash. Full --durations=15 output is in `/var/tmp/f3eff5-{main,slice2}-verified.log`.

Existing milestone-only check: **2 passed, 2 failed, 11 skipped** on both main (13.92 s) and slice 2 (14.89 s). The two red cases are the existing C2b expectations; the Docker C6/C10 cases were skipped because the opt-in switch was absent. These 15 existing cases are separate from the 366-case added-suite counts above.
