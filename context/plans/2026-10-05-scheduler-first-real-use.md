---
status: applied
applied_in: 473908e
---
# The scheduler's first real use: re-verify and close the scheduled review findings

The user agreed on 2026-10-05: first push, then a real trial of phase 7
part 1 with the scheduler on, then the review findings, then part 2. The
scheduler is now on: `scheduler.enabled: true`, the container was recreated,
and `multiagents run` was relaunched.

This plan is the trial. The work is real: it is the 16 review findings in
`scheduled`. It is planned **entirely as nodes**, so that every primitive of
part 1 runs on real work at least once.

## Apply now

**1. Re-verify the 16 `scheduled` findings, with one read-only researcher
node.**
- **The findings:** F10, F33, F50–F54, F112, F120, F122, F130–F132, F140,
  F150 and F154.
- **Why.** They date from the September review, and phase 6 has fixed some
  of them in passing. The initializer checked two cases on 2026-10-05:
  - F50–F54: the six tests in `tests/test_phase2_proxy_directives.py` pass
    on main;
  - F120: the three `test_c2_budget_characterization.py` tests on
    `config_dir` and `extends` pass.
- **The researcher's verdict for each finding:**
  - `fixed`, with the test or the code that proves it;
  - `still present`, with a reproduction on main;
  - `changed`, saying what remains.
- **After it, the orchestrator updates the ledger** with
  `set_finding_status`. A finding that has been fixed becomes `fixed`,
  citing the researcher's evidence.

**2. Fix what remains, with one instance of the `implement` template per
group.**
- **The template.** Each instance is a sequence of two loops:
  - tester ↔ reviewer;
  - implementer ↔ reviewer, with the same reviewer session (letter B).
- **Each instance depends on the researcher node.**
- **A group whose findings are all `fixed` is not instantiated.**
- **The groups**, each with the same lock as every other group that
  touches the same files:
  - **auth:** F130, F131, F132 and F140. These are connection checks that
    report "logged in" for a corrupt or empty file. Files:
    `defaults/providers/*.sh`, and `_claude_token` in the audit.
  - **budget:** F122, F150 and F154. A cache hit overwrites `spent` instead
    of merging it, and the characterization suite pins that defect as
    correct. Lock `budget.py`.
  - **env:** F33 and F112. `build_env` forwards the whole ambient
    environment. Lock `executor`.
  - **allowlist:** F10. A short suffix acts as a wildcard. Lock `executor`
    as well, since the proxy config is generated there.
- **Roles.** Each group picks its spec from its findings, which it cites
  by id. The roster's usual agents take the parameters:
  - `tester`;
  - `reviewer`;
  - `implementer`, on the tier that suits the size of the group.

  The orchestrator sets the number of rounds for each instance.
- **When a loop reaches its maximum,** the orchestrator decides, as NS-R12
  amended requires.
- **When an instance finishes,** the orchestrator reviews the result and
  merges it with `merge_node`, then updates the ledger:
  - `fixed`;
  - or `accepted` / `deferred`, with the reason, if a finding is set aside.
- **The user's standing rules still apply:**
  - a reviewer after every implementer, which the template guarantees;
  - one implementer per item;
  - escalation to opus at round 3, now one of the orchestrator's decisions;
  - tickets are fixed in-house.

**3. Two small defects seen while the scheduler was being turned on.**
They are fixed as simple nodes, or through the `implement` template if
the orchestrator prefers.
- **Tests leak scheduler processes.** Before `docker rm`, the project
  container held dozens of `scheduler start --foreground` and
  `scheduler.worker` processes that tests had started, some of them
  ~7 h old. They came from M2, M5, NC-R97 and the adversaries' tests.
  - The tests in the part 1 suite must stop what they start, even when
    they fail.
  - Add a regression test that counts processes before and after.
- **agy-b's breaker trips on reviews that succeeded.** `doctor` shows
  "agy-b stopped after 3 consecutive failures: failed: I have reviewed the
  branch against `main` and ran the required…". The output looks like a
  normal verdict.
  - Find out why those runs ended `failed`. It may be related to the
    "reported ERROR after a complete verdict" already seen on agy.
  - Fix it if it is a misclassification.

**4. A report on the trial itself.**
- **What the orchestrator logs in BRIEF:**
  - what worked;
  - what was missing or awkward in the node tools;
  - every defect in the scheduler found along the way.
- **Those defects** are fixed before part 2 is specified, since part 2
  builds on the scheduler.

## Next phase

- **The 48 `open` findings**, which have never been triaged. The initializer
  takes them up with the user after this trial, the five that the report
  leads with first.
- **Phase 7, part 2: one container per run**, to be specified with the user
  afterwards (`phase7-nodes-and-containers.md`, PAC-R*).

## Config changes

- **Already applied, at the user's request (2026-10-05):**
  `scheduler.enabled: true` in `.multiagents/config/project.yaml`. The
  container was recreated and `multiagents run` relaunched.
- **No other change** to the config or to `agents.yaml`.

## Notes considered

`context/notes/` contains only README.md: there are no user notes.
