# TS Run A: outcome manifests (TS-R0, TS-R1, TS-R1a)

Each `.tsv` holds one line per collected node id, `nodeid<TAB>outcome`
(`passed`, `failed`, `error`, `skipped`, `xfailed`), sorted. They were built
from pytest `--junitxml` output by `context/ts/manifest.py`. When pytest
writes two `<testcase>` elements for one id (a failure in call, then in
teardown), the builder merges them, for example `failed+error`.

- **Base:** 7fd909d, untouched.
- **After:** 29db3b2, with the five test fixes below, the new launch-window
  test, and pytest-xdist. After that commit only this directory changes.

Machine: 16 cores. "Serial" is `scripts/test-chunk.sh K 4` for K = 1..4, the
four chunks running at the same time, each chunk a single pytest process.
"Disk" means `--basetemp` under `/var/tmp`, one path per process, as
AGENTS.md documents.

| Manifest | Commit | Mode | basetemp | Wall | Diff vs base |
|---|---|---|---|---|---|
| base-serial.tsv | 7fd909d | serial, 4 chunks | /tmp (tmpfs) | 741 s (chunks 667/458/741/706) | n/a |
| after-serial.tsv | 29db3b2 | serial, 4 chunks | disk | 794 s (chunks 687/409/792/725) | +1 id, xfailed (below) |
| after-parallel-1.tsv | 29db3b2 | `-n auto` | disk | 216 s | +1 id, xfailed (below) |
| after-parallel-2.tsv | 29db3b2 | `-n auto` | disk | 205 s | +1 id, xfailed (below) |

- **Serial vs parallel:** the serial manifest and both parallel manifests are
  byte-identical.
- **Against the base,** the only difference is one new id, the expected one:
  `tests/test_ts_launch_window_stop.py::test_ts_a_stop_inside_the_launch_window_leaves_no_setsid_child`,
  xfailed, strict (see "Finding").
- Every base id is present, with the same outcome. The runs collect
  4373 ids: the base's 4372 plus that one.
- A parallel run leaves 1.5 GB in its basetemp.

**Base outcomes:** 4181 passed, 171 failed, 14 skipped, 6 xfailed.
The 171 reds, exactly:
- all 72 of `test_phase2_entry_semantics.py`;
- 97 in `test_ps_provider_sharing.py`;
- 2 in `test_c2_auth_characterization.py`, which expect PS-R8's `agy-partner`.

`test_h1_h2_review2.py::test_rf_r3_r1_adopted_opencode_filter_without_exit_status_is_refused`,
which the spec lists as a known red, **passes** at the base.

## The test fixes

1. **`test_d2_adversary.py::test_tmux_open_approves_foreign_agent`**
   - **Defect:** it replaced `tmux.check_tmux` and never restored it.
   - **Effect:** a later test on the same worker found tmux on an empty PATH.
   - **Fix:** `monkeypatch`.
2. **`test_budget_adversary.py`, the two severity-boundary tests**
   - **Defect:** they wrote under the fixed paths `/tmp/test-sev-75` and `/tmp/test-sev-90`.
   - **Fix:** they now write under `tmp_path`.
3. **`test_subagent_mcp_live_inside.py`**
   - **Defect:** it set `server._runner = None` and never restored it.
   - **Fix:** `monkeypatch`.
4. **`test_h7_adversary.py::test_probe_claim_*` (2 tests)**
   - **Defect:** a 10 ms real-time cooldown, then an assertion that the
     provider was still down. `StartupHealth._write`'s fsyncs outlast that on
     a loaded disk.
   - **Fix:** `multiagents.startup`'s clock is frozen in this process, and
     moved on together with a real sleep for the subprocess.
   - **Mutation checks,** each red on its intended assertion:
     - `_reconcile` keeping the probe;
     - `and` → `or` in `_reconcile`;
     - `_blocked` ignoring `until`.
   - **Also checked:** with 50 ms injected into `_write`, the base tests fail
     and the new ones pass.
5. **`test_sandbox_git_docker_stop.py`, wrapped route**
   - **Fix:** the fixture now waits for the wrapper to record `container.pid`
     before stopping.
   - **Why, and the product finding it exposed:** see "Finding" below.
   - **Mutation checks** of `_KILL_SCRIPT`, each red on
     `still running: ['child-left-group']` for all three routes:
     - `walk` not recursing into children;
     - no kill by pid.
   - **Also checked:** with a 1 s delay before the pid write, the new fixture
     passes and the old one fails.

## Finding, not fixed here (src is out of scope): a stop during launch leaves setsid children running

Ticket: `context/tickets/2026-09-30-unfiled.md`.

1. `agentwrap.main` starts the agent (`Popen(..., preexec_fn=os.setpgrp)`),
   then writes `container.pid`, then installs its TERM handlers.
2. `DockerExecutor.kill_detached` reads `container.pid` on the host, then
   runs `_KILL_SCRIPT` in the container.
3. If the stop reads the file before step 1 has written it, `a` is empty.
   Only the wrapper is signalled, and a child that called `setsid` survives
   for good.

**How it stays covered.** `tests/test_ts_launch_window_stop.py` reproduces
the race deterministically:
- A `sitecustomize`, passed to the container's Python through the env file,
  holds the wrapper at the `os.replace` that lands `container.pid`.
- On the host, a wrapped `_recorded_pid` releases the wrapper right after
  the stop has read the file inside the gap.
- The test is `xfail(strict=True, raises=AssertionError)`, and fixture
  failures raise `RuntimeError`, so they are not excused as the expected failure.

**Checked:**
- **Today:** it xfails; 6 of 6 when run as concurrent processes.
- **The reason:** with `--runxfail` it fails on
  `still running in the container: ['child-left-group']`.
- **With a scratch fix** (`kill_detached` reading `container.pid` again
  until it appears): XPASS(strict), so it goes red, and the 8 sg_r7 tests
  stay green.
- **Duration:** about 7 s while the defect exists, because it waits out
  `SETTLE` for a child that never ends.

## History: runs before the final fixes (only the diffs kept)

| Run | Commit | Mode | Wall | Diff vs base |
|---|---|---|---|---|
| serial + parallel ×4 | 3c29c71 (fixes 1–5, before the launch-window test) | serial and `-n auto`, disk | 755 s; 205, 210, 190, 211 s | 0 in all five |
| par0 | xdist, before any fix | `-n auto`, /tmp | 204 s | 1: `test_d2_monitor.py::test_tm_r4_without_tmux_the_action_fails_cleanly` (fix 1) |
| par1 | fix 1 | `-n auto`, /tmp | 196 s | 0 |
| shuffled | fixes 1–3 | `-n auto`, /tmp, test files shuffled | 208 s | 0 |
| n4 | fixes 1–3 | `-n 4`, /tmp | 635 s | 0 |
| serial | 043c436 (fixes 1–3) | serial, /tmp | 721 s | 0 |
| parallel | 043c436 | `-n auto`, /tmp | 197 s | 0 |
| parallel ×2 | 043c436 | `-n auto`, disk | 220 s, 197 s | 2 and 3: the h7 pair in both, sg_r7 wrapped_stop once (fixes 4, 5) |
| basetemp inside the worktree | 043c436 | `-n auto` | 220 s | 55: the h7 pair, plus 53 git tests that expect `tmp_path` not to be inside a repository (test_core 12, h1h3_round2_* 25, h3_* 9, sandbox_git_branch_delete 5, h1_host_authority 1, q2_cli_path 1) |
| (discarded) | 043c436 | `-n auto`, /tmp | 159 s | 735 errors, "could not create numbered dir": /tmp was cleaned during the run (ENOSPC incident) |
