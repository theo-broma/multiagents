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

Pending IC-C21 cases, per the task's explicit exception:

- crash at grant;
- crash at start;
- crash at end;
- crash at revoke;
- orphan endpoint discovery and removal.

These await the promised crash seam and orphan endpoint location clause. No
additional contract silence or missing toolchain was raised. There is no full
suite run. Existing slice 3 tests were left unchanged as instructed; their
workload declares no session mode, while new mode-aware steer tests assert the
new continuity outcomes.
