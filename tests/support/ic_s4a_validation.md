# Slice 4A red-suite validation

Baseline: main `16773dbe`, plus the previously committed eleven IC-C22 config
cases (`a0c30601`). No implementation branches were merged or copied.

Command:

```sh
PYTHONPATH=src uv run --frozen python -m pytest -q -p no:cacheprovider -n 4 \
  --basetemp=/var/tmp/pt-3be306 --durations=15 \
  tests/test_ic_c20_scoped_rpc.py tests/test_ic_c21_endpoint_lifetime.py \
  tests/test_ic_c22_session_config.py tests/test_ic_c22_storage.py \
  tests/test_ic_c23_session_resume.py tests/test_ic_c24_migration.py
```

Final observed result: **29 failed, 56 setup errors, 60 skipped, 0 passed**, in
4.42 seconds. Thus 85 red / 0 green against main. There are 145 collected cases,
including the eleven already committed config cases; this continuation adds
134 cases.

| Contract | Cases | Assertion/stub failures | Setup errors | Skipped |
| --- | ---: | ---: | ---: | ---: |
| IC-C20 | 32 | 0 | 14 | 18 |
| IC-C21 | 12 | 0 | 6 | 6 |
| IC-C22 | 38 | 16 | 11 | 11 |
| IC-C23 | 59 | 9 | 25 | 25 |
| IC-C24 | 4 | 4 | 0 | 0 |

Failure reasons:

- Fourteen config cases reach their intended validation assertions: main accepts
  the invalid `session` declarations instead of raising `ValueError`.
- Fifty-six end-to-end cases hit the existing slice 3 harness's deliberate
  `K17.3 provisioning seam is missing` assertion. Main lacks
  `ReferenceBackend.attach_checkout`. These are setup errors, not collection
  errors, and must be replayed after the prerequisite implementation lands.
- Fourteen loop/migration cases hit the existing `NotImplementedError` stub for
  the missing public seed registry (`multiagents.provision.seeds`).
- The remaining IC-C24 prior-record case hits the same K17.3 assertion from
  its test body.

No product stubs or implementation changes were introduced. The probe is a new
subclass of the existing probe provider, with RPC-only needs, all three session
modes, matching/wrong/withheld resume events, missing and corrupt payloads,
provider rejection, and a live unconfirmed resume. Its generated CLI was also
executed standalone to check session creation, payload readback, and matching
resume evidence. Both generated provider scripts were compiled successfully.

The slowest observed calls under four workers were the export loop continuity
case (0.32 s), home loop continuity (0.30 s), and model incompatibility (0.28 s).
No case took a second. This measures red prerequisites, not implemented feature
latency. Wait bounds are 4 s for one transition and 8 s for multi-launch loops;
the live deadline probe uses the declared 0.25 s deadline and polls its outcome.

58 Docker cases were not run because `SG_TEST_DOCKER` was unset; exact node ids
are in `ic_s4a_docker_not_run.txt`. Two additional reference parameter instances
of Docker containment tests are skipped deliberately. Reference backends make
no containment claim.

The IC-C21 cases originally pending here are now covered by the K18
continuation below. No other existing test files were changed.

## IC-C21 K18 continuation

Baseline: main `2a825f9f`. Added `tests/test_ic_c21_boundaries.py`, with nine
reference cases and nine opt-in Docker variants:

- K18.1: recovery at `provision_done`, `run_entry`, `exited_seen` (boundary 4),
  and `remove_entry` leaves no live endpoint for any ended attempt, and does
  not classify recorded attempts as orphan endpoints.
- K18.1: recovery at `run_done` recreates the endpoint at the declared attempt
  path for a running invocation, with unchanged run/attempt ids and one probe
  report (no reinvocation).
- K18.2: an explicitly planted endpoint directory is discovered with
  `reason: orphan_endpoint`, its endpoint id, and null container fields;
  CLI/status discovery agrees, does not delete it, and never serves it,
  including after a scheduler restart.
- K18.2 / K17.6: explicit removal deletes the directory and discovery entry;
  repeated removal succeeds; an id prefix is refused as `invalid_id` without
  deletion; a recorded running endpoint is refused as `not_orphan` and keeps
  serving authorised requests.

Command actually run:

```sh
PYTHONPATH=src uv run --frozen python -m pytest -q -p no:cacheprovider -n 4 \
  --basetemp=/var/tmp/pt-98d73b --durations=15 tests/test_ic_c21_*.py
```

Observed final result: **15 setup errors, 15 skipped, 0 passed**, in **1.47 s**.
Against main that is **15 red / 0 green**, including **9 new red / 0 new green**.
All reference cases, both existing and new, stop at the existing deliberate
`K17.3 provisioning seam is missing` assertion: main lacks
`ReferenceBackend.attach_checkout`. Collection succeeds. Test bodies therefore
remain unvalidated against an implementation and must be replayed once that
prerequisite lands; the red result is not evidence of individual endpoint
assertions failing. No product stubs or implementation changes were added.

Docker was not opted in (`SG_TEST_DOCKER` unset): all 15 Docker cases were
skipped, including nine new variants. No Docker command was run. No toolchain
request was needed. Dependencies were installed only into the worktree's normal
locked uv environment. No full suite was run.

The slowest durations under `-n 4` were setup for orphan removal/idempotence
(0.10 s), `provision_done` recovery (0.09 s), `exited_seen` recovery (0.09 s),
and `run_done` recovery (0.08 s). Every recorded duration was below 0.11 s;
these timings measure the missing-prerequisite guard, not endpoint behavior.
All new explicit waits use the shared 4 s bound; there are no fixed sleeps.

No K18 behavior is left pending for a missing contract seam. Cancel/replacement
revocation is already exercised in the existing IC-C21 lifetime suite; K18's
four required boundary points use the existing K3 injector without new seams.
