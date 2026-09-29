# BRIEF — multiagents repairs itself

**Phase: implement.** The review is done. `context/review/REPORT.md` has the
findings, the ledger has their state, and ~380 characterization tests are merged.
This phase turns the chosen findings into work.

`team: implement` is already set in `.multiagents/config/project.yaml`.

The review phase's brief is kept at
`context/review/BRIEF-review-phase.md` — read it for the two invariants
(providers are plugins, agy carries Gemini only), which still hold.

---

## Current work (from 2026-09-28, evening): Phase 6 — hardening, then two features

**Complete:** everything up to and including Phase 5 (Codex), merged on `main`
at `e12e605`. New work branches from `main`.

**What this phase is.** Close the security holes and false completions found
at the end of Phase 5, fix the routing and reliability defects, then build
the two features the user asked for. The user approved this order on
2026-09-28. They asked for the advisor to be consulted regularly and for the
work to run as autonomously as possible. The advisor (ag-3f9bba, turn 4)
reviewed the plan, and its corrections are already applied.

**Requirements, in order:**
- `context/specs/phase6-hardening.md` holds H1–H13 and D2.
- `context/specs/limit-notices.md` holds D1.
- Short order:
  1. **H1**: a forged `branch_pending_delete` can destroy another agent's
     branch. With an adversary.
  2. **H2**: a refused or filtered run is reported as `done`.
  3. **H3**: agent code runs on the host during merges. Base hooks default
     off, plus an audit of Git's other command hooks. With an adversary.
  4. **H4 + H14**: an empty fallback model on steer and start, and
     built-in agent defaults that bypass `limits:`.
  5. **D1**: limit-hit notices. The contract can be written early.
  6. **H5–H7**: the claude budget under Docker, `refresh-models`, and
     opencode routing together with explicit `model:` pins.
  7. **H8**: the 128 KiB prompt transport.
  8. **H9–H13**: the old queue and cosmetic items.
  9. **D2**: tmux, step 1 (viewer only).

**Open decisions for the user** (emit them as `NEED_DECISION`; each has a
default, so work can proceed):
- **`merge-hooks`** (H3): base hooks are off during host merges, with a
  `project.yaml` opt-in. It touches the "hooks kept" decision of CI-R5,
  which was about agent commits rather than host merges.
- **D1 surfaces** (LN-R3): default to an event, the tool result and the
  terminal plus the monitor.

**Progress log (phase 6):**
- 2026-09-28 23:10: claude weekly_all at 94% (resets Oct 02 11:59 UTC).
  Advisor ag-3f9bba (turn 6) agreed on routing:
  - Phase 6 goes to codex. `agents.yaml` gained `tester.models.codex:
    gpt-6-sol`, and implementer-deep's codex fallback is now gpt-6-sol.
  - The orchestrator stays on claude, because codex has no interactive
    launch.
  - H1's contract and implementation can proceed. Its security sign-off (the
    adversary) waits for a checker from another family, meaning claude after
    the reset, unless the remaining quota can safely cover one pass.
  - H1 research started: ag-4da90f (deletion lifecycle), ag-d0cd7b
    (container-writable vs host-only paths).
- 2026-09-28, 23:40: the H1 research changed the item's premise.
  - Every agent in the container can write `refs/heads/agents`, `.git`
    objects, logs and worktrees, and every agent's worktree and HOME. So
    inter-agent isolation inside the container does not exist. Recorded as
    a **known limitation**, for the user to decide later: accept it, or
    move to per-agent containers.
  - H1 is re-scoped to "container-written state never authorises a host
    mutation outside the container domain". The contract is
    `context/specs/h1-host-authority.md` (HA-R1..R8), reviewed by the
    advisor at turns 7–8.
  - The advisor found a worse hole: a forged `parent` makes the host
    auto-merge into `main` (HA-R3).
  - Tester ag-b5f92a is running on codex gpt-6-sol.
  - Researcher ag-94c9f0 is mapping H2's refusal signals, in parallel and
    read-only.
  - H3's scope gained the host `commit_all` on stop and resume.
- 2026-09-29, around 00:30: all three contracts are written and reviewed
  by the advisor (turns 8–10):
  - H1: `h1-host-authority.md`;
  - H2: `h2-refusal-status.md`;
  - H3: `h3-host-git-execution.md`.

  Tests:
  - H1 tests merged (a7544fb): 19 red, 6 green.
  - H2 tests merged (d776bb0): 15 red, 6 green.
  - H3 tester ag-d85ea7 is still adding coverage; merge it when done. It
    changed 5 SG-R5 tests to the hooks-off default.

  Implementation:
  - H1 implementer-deep ag-ff8301 is running on codex gpt-6-sol.
  - Next, in order:
    1. H1 adversary. It needs a checker from a family other than codex:
       claude after the reset, or decide.
    2. H2 implementer, which can start once H1 merges because both edit
       `runner.py`.
    3. H3 implementer.

  Other notes:
  - dev-advisor is on agy, which is out of quota, so implementers are told
    not to consult it and to raise NEED_INFO instead.
  - **2026-09-29, around 04:35 UTC: paused.**
    - Codex's 5 h window is at 89% and resets at 08:25 UTC.
    - The container's claude token has expired (401). The user was asked
      to run `multiagents auth login claude --account <label>`.
    - opencode and agy are unusable.
  - **H1** is on branch `agents/implementer-deep/ff8301` (ag-ff8301),
    **not merged yet**.
    - Green: H1 25/25 and the 3 unrecorded-node regressions.
    - Host run on 2026-09-29: chunk 1 fully green. Chunks 2–3 show only the
      known reds (72 phase2 and H2) **plus 3 old tests whose fixtures put
      worktrees outside the worktree root**, the same class as the 15
      fixed by ag-ccf9cd:
      - `test_conversation_provider_change::test_cx_c28_a_conversation_on_a_listed_fallback_is_still_resumed`;
      - `test_conversation_provider_sibling::test_cx_c28_a_sibling_instance_is_resumed_with_the_rosters_model`;
      - `test_steer_resume_steps::test_bug_1b2612_steer_reports_success_for_a_live_stuck_run`.
    - Next:
      1. a tester moves those fixtures under `Paths.worktree(...)`;
      2. merge the tester, then the H1 branch. Its
         `tests/test_h1_swap_guard.py` gets reverted at merge, which is
         fine, because the case is covered by `test_h1_unrecorded_nodes.py`;
      3. the H1 adversary runs on claude opus
         (`adversary.models.claude: opus`) once the container is
         re-authenticated;
      4. a reviewer on the H1 diff (ag-7b7132 fell to opencode and died);
      5. then the H2 implementer.
    - The advisor's turn-12 finding (unrecorded nodes in resume and steer)
      is fixed in `2c59ac7`.
  - Contracts and tests merged since: H4+H14 (`h4-h14-routing-limits.md`,
    tests 784fe09), D1 (`d1-limit-notices-contract.md`, tests 08efd68),
    and the RT-R1 example correction.
  - 2026-09-29, around 07:10 UTC:
    - `.multiagents/config/project.yaml` `budget.reserve_headroom` is set
      **temporarily** to 0.02. The claude reader sees only one account
      (97%, H5), while the user says vault account `a` has quota.
      **Restore to 0.05** after H5, or when the week resets on Oct 02.
    - The user re-logged `a` and `b` from two browsers, and the auth
      sidecar was restarted. Claude is still not verified: the tree keeps
      re-pausing because deferred tasks restart on opencode, which fails
      and cools down again.
    - Tooling bugs to file:
      1. the authproxy does not fail over from a 401 to the next account
         (`authproxy.py` ~301);
      2. `docker rm` does not restart the auth sidecar;
      3. deferred tasks restart on a provider that just failed
         (opencode), which re-pauses the tree.
  - 2026-09-29, around 07:50 UTC: **claude in the container is still 401**
    ("OAuth access token has expired") after all of the following:
    - `a` and `b` re-logged from two browsers, confirmed on REDACTED;
    - a workspace recreate;
    - an auth sidecar recreate;
    - the user clearing the claude cooldown and pause in `tree.json`.

    What is known:
    - The sidecar logs only `listening`. It is unknown whether it logs each
      request.
    - The workspace env has `ANTHROPIC_BASE_URL`, and `docker exec
      --env-file` inherits it.
    - Researcher ag-f9a508 blamed a missing `ANTHROPIC_BASE_URL`. That is
      **not confirmed**: the container env has it.

    Open, for a bug-reporter on a working provider:
    - does the sidecar get the request at all;
    - which vault (`shared` or project scope) did the login write to;
    - can a read-only vault mount block the token refresh.

    Temporary config:
    - `limits.provider_down_cooldown_seconds: 10` is set at the user's
      request, for testing. **Remove it afterwards.**
  - **Pending user request (2026-09-29):** once H3's implementer
    (ag-99daa6) finishes, switch `advisor` to `codex/gpt-6-astra` and set
    `implementer-deep.models.codex` to `gpt-6-astra` in `agents.yaml`.
    Then use `model=gpt-6-astra` for implementer-deep runs.
  - Next after H3: an implementer fixes review ag-64181b's findings (tests
    in `tests/test_h1_h2_review2.py`, 3 red), then a re-review.
  - **2026-09-29, around 09:50 UTC:**
    - Merged:
      - H1, plus both review-fix rounds (e1d2296);
      - H2 (4b934c9);
      - H3 (336f14f).
    - Roster: advisor, adversary and implementer-deep's codex fallback are
      now on gpt-6-astra.
    - **agy is disabled** in `.multiagents/config/providers.yaml`, because
      the router had sent two adversaries to it. Those were stopped and
      discarded. One had come back "blocked by Gemini's filters", and was
      still reported `done`: the running MCP server predates H2.
    - **The MCP server must be reconnected (`/mcp`) while nothing runs**, so
      that H1, H2 and H3 are live in the server process.
    - Codex's 5 h window is at 88% and resets at 13:30 UTC; a wake-up is
      armed for 13:35.
    - Next steps:
      1. H1 and H3 adversaries on codex astra. Their briefs are in the
         contracts; restate them as properties.
      2. H4 + H14 implementation.
      3. D1.
  - **2026-09-29, around 10:00 UTC:** both adversaries died on codex quota.
    **Resume them with steer_agent after 13:30 UTC, do not restart them.**
    - ag-25c242 (H1): codex's cyber filter refused the first phrasing. It
      was restated as an edge-case and property test job for
      `tests/test_h1_edge_*.py`, then hit the quota.
    - ag-afe388 (H3): hit the quota mid-run.

    Config changed at the user's request:
    - `limits.wind_down_seconds` 120 and `wrap_up_seconds` 60 (were 600 and
      420), so codex is used to the end of its windows.
    - No per-window percentage reserve exists for non-orchestrator
      providers. That would be a feature to add.
  - The MCP server was reconnected by the user around 10:10 UTC: H1, H2
    and H3 are live.
  - **Queued (user request, 2026-09-29):** reword the agent instruction
    files away from attack vocabulary, towards verification vocabulary.
    - Targets:
      - `defaults/agents/team/adversary.md`, the main target;
      - `library/spec-adversary.md`;
      - `library/README.md`;
      - both `pipeline.md` files;
      - `library/security-advisor.md`;
      - the single mentions elsewhere.
    - Leave `pentester.md` alone.
    - Keep the agent name.
    - Add to adversary's "Calling this agent" section that OpenAI's cyber
      filter also trips on TOCTOU, forge and sandbox wording, so state them
      as invariants and inputs.
    - Delegate the rewrite after 13:30 UTC; the orchestrator reviews the
      diff.
  - Tooling: the doom-loop watchdog false-fires on codex `file_change`
    events, because repeated edits to one file carry identical args (path
    and kind only). File a bug-reporter ticket at a natural stop.

**HANDOFF, 2026-09-29, around 10:30 UTC (orchestrator context wind-down):**

State:
- **Merged on main:**
  - H1: host authority, HA-R1..R8 plus HA-R2a, with two review-fix rounds.
    Last fix e1d2296.
  - H2: refusal status (4b934c9) and its RF-R2 finalize fix.
  - H3: host git execution (336f14f).
- **Tests merged, red, implementation pending:**
  - H4+H14: `tests/test_h4_h14_routing_limits.py`, contract
    `h4-h14-routing-limits.md`.
  - D1: `tests/test_d1_limit_notices.py`, contract
    `d1-limit-notices-contract.md`.
- **Executor is now `local`.** The user edited it on 2026-09-29, "route 2",
  because container claude returns 401. A claude ping works locally.
  - Revert to `docker` once the container auth is fixed.
  - Under local, H1/H3 protect nothing, which is accepted by the user.
- **Codex:** the 5 h window was exhausted and resets at 13:30 UTC. agy is
  disabled in `providers.yaml`. opencode is dead (H7).
- **Temporary config to restore later:**
  - `limits.provider_down_cooldown_seconds: 10`: delete that line.
  - `wind_down_seconds` 120 and `wrap_up_seconds` 60 were the user's
    choice; keep them unless the user says otherwise.
- **Roster:** advisor, adversary and implementer-deep's codex fallback are
  on gpt-6-astra. The tester has a codex fallback of gpt-6-sol.
  `adversary.models.claude: opus` exists.

Next, in order:
1. H1 and H3 adversaries on **claude opus** (`model=opus`), which is the
   cross-family check. Codex runs ag-25c242 and ag-afe388 died on quota
   under the docker executor: discard them and start fresh.
   - Phrase the tasks as invariants and inputs to test. The first H1
     phrasing tripped OpenAI's cyber filter.
   - Findings go back to an implementer, with tests first.
2. Reword the attack vocabulary in the instruction files (queued above).
   Delegate, then review the diff.
3. H4+H14 implementation (implementer-deep), then D1 implementation.
4. Then H5–H13 and D2, per `phase6-hardening.md`. H7 gained binary
   resolution.

Open tooling bugs to file with a bug-reporter, which now runs on claude
locally:
- container claude 401 while the auth sidecar logs no request;
- the authproxy does not fail over from a 401;
- `docker rm` leaves the auth sidecar running;
- deferred tasks restart on a provider that just failed;
- the doom-loop false positive on codex `file_change`;
- `test_core` `doctor_clear` tests fail when run outside the project dir.

Also: `test_h1_swap_guard.py` was written by an implementer and merged. It
is harmless, but review it once.

**~10:40 UTC, after compaction:** codex adversaries ag-25c242/ag-afe388 discarded. In flight, all on claude (local executor): adversary ag-387314 (H1, opus), adversary ag-e98308 (H3, opus), implementer ag-736dd1 (instruction rewording, sonnet), implementer-deep ag-2a0da5 (H4+H14). Advisor ag-3f9bba consult failed (codex: `resume failed: requested '01a0ea08…', observed ''`, turn 15) — retry after 13:30; add to the bug-reporter batch if it persists. Pending advisor asks: review tests/test_h1_swap_guard.py; D1 sequential after H4. Next: route adversary findings back with tests; review the rewording diff; D1 after H4 merges; then the bug-reporter batch.

**~11:00 UTC:** H1 adversary ag-387314 VERDICT rejected: the host resolves the authority record from tree.json's inner `id` rather than the key (5 red tests, merged 934db09). Contract HA-R9 added (7943284). Fix: implementer-deep ag-a4bb6e. Queued for the next free slot: tester for the 11 mutation survivors on authority.py listed in ag-387314's result (seeded flag, complete(), check-ref-format, branches history, safe_nested_path, pinned `..`, remove_worktree owns-checks, st_dev, safe_seeded_path, rebind completion, add overwrite) plus an HA-R9 mismatch-event assertion.

**Queued (user, 2026-09-29 ~11:10):** once ALL agents currently in flight have finished (ag-e98308, ag-736dd1, ag-2a0da5, ag-a4bb6e), start a `bug-reporter` to diagnose the container claude 401 (step 1): run a claude call inside the workspace container and establish where the request actually goes (auth sidecar, which logs only `listening`, vs direct api.anthropic.com through the egress proxy — check proxy logs), which credential file the CLI finds in its container HOME (without reading the vault's secret contents), and whether a CLI version change (2.1.274 → 2.1.280) stopped honouring ANTHROPIC_BASE_URL. No `docker rm`, no config edits. Then file the ticket together with the other tooling bugs in the handoff list.

**~11:45 UTC:** H3 adversary ag-e98308 VERDICT rejected (3 blocking/serious + identity): symref branch lets host commit onto main; one forged HEAD aborts `stop` for all; disable list misses includeIf-scoped drivers; includeIf gitdir identity lost. Tests merged 1b8cc8c (6 red, 11 coverage green). Contract HG-R8..R11 (f2f9888). Fix: implementer-deep ag-c2d690. Queued tester: H3 mutation survivors (process/textconv dropped from disable list, `.required=false`, push/delete_branch/worktree remove/move without host scope, move_aside ambiguity refusal, merge_agent target_branch lookup) + HG-R9's `clean --branches` clause (untested). Also still queued: H1 mutation-gap tester (above).
**D3 — in this phase, scheduled right after D1 (user decision 2026-09-29):** a machine-readable per-provider CLI dependency manifest next to src/multiagents/defaults/providers/<p>.* (flags, env vars honoured, paths, stream event fields, parsed messages, exit codes, endpoints, last verified CLI version), a `doctor` check against the installed CLI, and a test tying each declared dependency to code.

**~12:20 UTC:** rewording merged (8135e0c). Two phrases kept because test_core pins them ("You attack code that already works", "not a weapon"); tester ag-e45e53 relaxes those pins and writes H1 coverage tests (tests/test_h1_coverage.py). After it merges: an implementer-quick rewords those two phrases in team/adversary.md. The rewording agent saw 125 failures in chunk 2 and did not compare them to base; re-check chunk 2 on main at the next full-suite run.

**~13:30 UTC (after session restart):** all 4 agents had stopped on the claude window; resumed via steer: ag-a4bb6e (HA-R9, commit 70ba3bb), ag-c2d690 (HG-R8..R11, commit 5d00422), ag-e45e53 (tester, WIP), ag-2a0da5 (H4: 25/28 green; decisions sent). Decisions recorded in the spec (b3b8888): SV-R4 stays, so the timeout tests observe `stuck` rather than a kill; the root orchestrator is exempt from max_children. H1 coverage tests and the wording-pin fix are merged (5763750). The pinned phrases in adversary.md still need rewording, which is queued for implementer-quick. Tester ag-5e4d2a has started on the queued work, plus aligning the HA-R9 assertion in test_h1_adversary_id_alias with the refusal semantics. Previously queued: rewrite the 3 LM-R1 timeout tests; add a root-exemption test; fix test_sm_r3 to use the spawner's cap. The full task text is in the orchestrator's log; restate it from this line if lost. (Tooling bug for the batch: `multiagents auth login codex` refreshed the container store, not the host `~/.codex/auth.json` that the local executor uses, so the advisor kept getting 401s until the user ran a plain `codex login` on the host at ~14:10.) Codex reports "not authenticated", which is for the user: `multiagents auth login codex`. The advisor is on codex, so it is unavailable until then.

**~14:05 UTC:**
- **Merged:**
  - H4+H14 (5c6307c): its files pass 77/77 on main.
  - adversary.md final rewording (e69dccc).
- **D1 started:** implementer-deep ag-179b69.
- **Advisor still unusable, codex 401.** The error is "Missing bearer" against `api.openai.com/v1/responses`. The adapter's profile `~/.multiagents/profiles/codex` (auth.json refreshed 15:33 CEST; `codex login status` says "Logged in using ChatGPT") is logged in, yet requests go to the API endpoint without a token.
  - Correction: the earlier note blaming host `~/.codex` was wrong.
  - Add this to the bug-reporter diagnosis, together with the container claude 401. That diagnosis starts when ag-a4bb6e and ag-c2d690 finish.
- **Pending advisor asks, not yet answered:**
  - HA-R9 refusal semantics;
  - tests/test_h1_swap_guard.py, keep or send to the tester;
  - D1 contract staleness against H4.

**~14:15 UTC:** HA-R9 merged (b68474c). On main, H1 + H4 + subagent_mcp are 151/151. The conversation refresh recreate path was a real instance of the bug, and is fixed. Open point from ag-a4bb6e: `cmd_stop` commits into the worktree path read from the tree entry without checking it against the host record (cli.py ~2640, H3/HA-R2 territory). Check it after ag-c2d690 merges and give it to the H3 re-review. ag-a4bb6e ran `pkill -f "pytest.*no:cacheprovider"`, which may have killed other agents' pytest runs; results from that window are suspect. Bug-reporter started for the container claude 401 + codex 401 + the secondary tickets.

**~14:30 UTC:** HG-R8..R11 merged (f435d8d). On main, H1+H3+H4+sandbox_git_merge are 169/169. The pinned-HEAD decision is recorded in the H3 spec. Next for H1/H3: once tester ag-a43782 merges, run one re-review adversary over both (fresh opus). Its scope: the fixes, plus `cmd_stop` committing into the tree-entry worktree path without checking the host record (ag-a4bb6e note). Also: `timeout 590` around test-chunk.sh does not reach pytest (minor tooling note).

**~18:30 UTC (after a second restart and a classifier outage):**

Bug-reporter ag-a0be27 finished from source only (it had no shell). It wrote 8 tickets; the full text is in `.multiagents/runs/ag-a0be27/result.json`, but only bug-c106a9 (docker rm leaves the sidecar) reached the queue.

- **Codex 401 root cause:** the per-agent HOME makes `run_profile()` resolve an empty `<agent HOME>/.multiagents/profiles/codex`. Fix in flight: implementer ag-3d7d8b. It restores the advisor.
- **Claude container 401:** the per-account vault tokens are never renewed (`refresh_private_credentials` reads the top-level vault file); `check` is blind to `accounts/*`; there is no failover on 401; the proxy logs no forwarded requests. None of this explains the 401 right after re-login, which needs a `docker exec` probe. Needs a contract before fixing. Schedule it after D1 and D3, or earlier if the user wants docker back.
- **Minor, not yet filed:**
  - consult bypasses/is gated inconsistently by max_concurrent (a bug);
  - doom-loop false positive on codex `file_change`;
  - deferred tasks restart on the just-failed provider;
  - `args.path` is ignored by the `find_project_root()` guards in cli.py (`doctor_clear` tests);
  - `auth login codex` does not say which store it writes.

ag-179b69 (D1) and ag-a43782 (H3 coverage) were limited at 20:19 CEST; both have been resumed.

**~19:30 UTC:**
- **H3 coverage merged** (de0bee6): all 62 H3 tests are green on main.
- **Round-2 adversary ag-ab23f4, VERDICT rejected (4).**
  - `stop` commits the main checkout onto the base branch through a forged `worktree`.
  - `merge_agent(into=unrecorded)` follows a forged HEAD onto main.
  - `remove_worktree` runs an unscoped prune.
  - One malformed entry aborts stop, resume, reap and active.

  Its tests were merged in c6c18ff, and the contract was extended with HA-R10..R12 and the HG-R1 clarification (2f5d821). The fix is running as implementer-deep ag-691e46.
- **Researcher for the D3 inventory failed on codex.** Rerun it after the codex profile fix, ag-3d7d8b, merges.

**~19:45 UTC: D1 implementation ag-179b69 finished**, with 2 NEED_INFOs, both decided.
- `effective_limits` keeps the `source` string and adds `source_detail` (contract 10fcd9f). ag-179b69 was steered to implement it.
- The ln_c3 test launched the real claude CLI. Tester ag-3464b3 is fixing both D1 tests.
- **Merge order:** ag-3464b3 (tests), then ag-179b69 (code). After that, start a reviewer on D1 (the diff is about 800 lines, with a new `notices.py`).
- D1 also changed silence detection: a worktree reading is taken at launch.
- Tooling note: `scripts/test-chunk.sh` fails in agent worktrees whose `.venv` lacks pytest.

**~20:00 UTC:** codex local-profile fix merged (4e25b09, 383 codex tests green). The auth.json fail-fast applies only to engine-launched agents (MULTIAGENTS_USER_HOME set); accepted. **Not live yet:** the running MCP server still uses the old `build_env` (advisor run ag-3c7a4c had no MULTIAGENTS_USER_HOME, so it 401'd again). It needs `/mcp` reconnect by the user, ideally when no agents are running. Then retry the advisor consult (the three asks above) and the D3 researcher.

**~20:15 UTC:** **D1 merged** (db187f7). D1, H4 and subagent_mcp are 104/104 on main. The D1 tests were fixed by tester ag-3464b3; the H4 assertions were relaxed to value+source by ag-24e107.
- The D1 reviewer was deferred: it routes to codex, which is still broken until the MCP reconnect. Its task names branch agents/implementer-deep/179b69, which is now merged; when it runs, steer it to review `db187f7^..db187f7`.
- The tree was paused by that deferral.
- Still running: ag-691e46 (H1/H3 round-2 fix).
- **Next:**
  1. The user reconnects MCP when the tree is idle.
  2. Advisor consult (three asks).
  3. D3 researcher, then the D3 contract.
  4. Adversary re-check of the round-2 fix, if it is large.

**~20:50 UTC:** H1/H3 round-2 fix merged (b782387); the 154 H1/H3/round2 tests are green. Six older fixtures contradict HA-R10/R11: test_core stop-into-out-of-domain, and test_h2 rf_r6 x5, which merges into an arbitrary checkout. Tester ag-304283 is fixing them, plus adding an HA-R12 `clean` test. HA-R11 resolution order is recorded in the spec. Still open: the D1 review, the advisor consult and the D3 researcher, which all need codex, so they wait for the MCP reconnect.

**~21:15 UTC — important:** the MCP server does not run from the project checkout. It runs from the orchestrator's scratchpad worktree `.../scratchpad/wt-main`, configured in `~/.config/multiagents/mcp.json`.
- That worktree was still at ba32688 (H1's first commit), so the server was running none of the later fixes: H2/H3 server-side paths, HA-R9..R12, H4, D1, and the codex `build_env` fix. Merges and tests were unaffected, because they ran from the repo.
- It is now advanced to main (72d4ab2). **The user must `/mcp` reconnect again** to load it.
- To keep this from recurring: after every merge that touches `src/`, run `git -C <wt-main> checkout --detach main`, then reconnect. The adapter copy in `~/.config/multiagents/providers/codex.py` is already identical to the repo's.

**HANDOFF, 2026-09-29 ~21:30 UTC (orchestrator context wind-down #2)**

**State.**
- **MCP server:** runs from scratchpad `wt-main`, now at main. The user reconnected, and the advisor (codex, gpt-6-astra) works again (ag-8e7d87 replied).
- **Codex under local:** fixed.
- **Merged since the last handoff:**
  - H1: HA-R9, HA-R10..R12 and HG-R1 prune (b782387).
  - H3: HG-R8..R11 (f435d8d).
  - H3 coverage (de0bee6).
  - H1 coverage (5763750).
  - H4+H14 (5c6307c).
  - D1 (db187f7).
  - Codex local profile (4e25b09).
  - Rewording (8135e0c, e69dccc).
- **In flight:** tester ag-304283 is fixing 6 old fixtures (test_core stop out-of-domain; test_h2 rf_r6 ×5) and adding an HA-R12 `clean` test. Merge it when done, then check that the full suite has only the 72 known phase2 reds.

**Advisor answers (ag-8e7d87), accepted:**
1. **HA-R11 option 3 stays, as "host-validated selection".** Queue a tester for three cases:
   - registration-only success;
   - a duplicate registration is refused;
   - a branch held by a record is refused.

   Known limitation: `_host_scope` derives the registration location from the checkout basename, so discovery can accept a registration that execution then refuses. It fails closed; note it and leave it.
2. **tests/test_h1_swap_guard.py goes to the tester.**
   - Its first test only proves symlink refusal before removal, which overlaps test_h1_unrecorded_nodes.py:83. Consolidate it.
   - Keep the second test (explicit extra-mount rejection).
3. **The D3 contract must settle:**
   - **Location and layering:**
     - `<provider>.dependencies.yaml` beside the adapter;
     - precedence project → global → shipped (as in scripts.py:66);
     - whole-document replacement;
     - `extends`;
     - behaviour when a custom provider has no manifest.
   - **Overrides:** an override of the adapter or of `providers.yaml` voids "verified" status. Show the provenance of both the manifest and the implementation.
   - **Scope:** scripts, adapters AND YAML. Separate native CLI dependencies from the adapter interface and from the internal `MULTIAGENTS_*` protocol.
   - **"Verified":** an exact CLI version exercised against identified integration code, recorded with date, platform/executor, evidence and scope.
     - `--version` alone is discovery, not verification.
     - Fake-CLI tests prove adapter behaviour, not upstream compatibility.
   - **Doctor:**
     - probe the real binary in each execution context (today it checks host PATH only, cli.py:1545);
     - probes are bounded and non-interactive;
     - states: missing, malformed, timeout, disabled, overridden;
     - a CLI newer or older than verified gives an "unverified" warning, never a refusal;
     - define how warnings and errors affect the exit status.
   - **Lint:** an explicitly authorised static consistency lint:
     - stable dependency ids, resolvable to executable code or config (not comments or substrings);
     - reverse coverage where it can be extracted mechanically;
     - paired with doctor tests and parser fixtures;
     - it claims neither completeness nor compatibility.

**Next, in order:**
1. Merge ag-304283.
2. **D1 review.** The reviewer runs on codex now; review `db187f7^..db187f7`. The earlier task text is in this BRIEF (~19:45 entry).
3. **D3 researcher**, which needs codex. The task text was given to ag-147507; re-issue it.
4. Write `context/specs/d3-cli-dependency-manifest.md` using the advisor's points, consult the advisor on the contract, then tester, then implementer-deep.
5. **Tester** for HA-R11 option 3 and swap_guard consolidation.
6. **Then** H5–H13 and D2 per phase6-hardening.md; H7 includes binary resolution.
7. **Claude container 401:** needs a contract (per-account renewal, `check` reading accounts/*, 401 failover, proxy request log, a docker exec probe). Schedule it when the user wants docker back.
8. **Tickets:** only bug-c106a9 is in the queue. The other 7 are in `.multiagents/runs/ag-a0be27/result.json` and need filing via a bug-reporter, or recording.

**Temporary config still in place:**
- `limits.provider_down_cooldown_seconds: 10`;
- `executor.kind: local`.

**Procedure:** after merging any change to `src/`, run `git -C <scratchpad>/wt-main checkout --detach main` and ask the user to `/mcp` reconnect.

**2026-09-29 ~22:20 UTC (after handoff #2):**
- ag-304283 tripped its 1500 s timeout while still running its suite. It was left running (SV-R4).
- Started three runs:
  - reviewer ag-de331d (codex), reviewing D1 db187f7, `verifies=ag-179b69`;
  - researcher ag-a609b9 (codex), re-running the D3 inventory;
  - implementer-quick ag-c75a01, doing H9 (AGENTS.md → scripts/test-chunk.sh).

**2026-09-29 ~22:45 UTC:**
- **D3 contract** written (3588055). The advisor (ag-8e7d87, turn 2) says revise it BEFORE the tester. Accepted, to do:
  1. DM-R4: allow file-level and case-arm refs, because the shell actions are top-level `case` arms; define exact literal plus fragment matching; handle quotes, heredocs and `${#}` when stripping comments; drop the claim that it cannot be gamed.
  2. DM-R5: use JSON-Pointer selectors on the real keys (`spawn.args`, `spawn.resume`, `spawn.permission.*`, `spawn.optional.*`, `models_cmd`, `mcp.args`, `stream.session_id_paths`, `stream.rules[*].match/.fields`, `status_map`, `refusal_markers`, `truncation_markers`, `transcript.limit_markers[*].match`, `home_links`, `bin_versions_depth`); count mapping KEYS too; leaf-level only; exclude codex's internal YAML.
  3. `verified` must also match platform, executor and scope; define state precedence and stdout vs stderr; a binary missing only in the container is not counted by the host section.
  4. Container probe: add a new "exec in an already-running container" seam (`ensure_running` may create a container); kill on the container side; overall deadline; enumerate the contexts from all agent executor overrides.
  5. Digest: framed records, taken after provider inheritance; byte-identical overrides keep the digest; document that shared code (docker.py) is out of it; editing a dependency voids `verified`.
  6. CUT `extends` and the newer/older ordering; `lint()` reads shipped files only; separate the shipped refs from the installed-package case.
- **User 22:45:** pause, start no new agents while we discuss z.ai/GLM via opencode. Still running: ag-304283 (tester, H1 fixtures), ag-de331d (D1 review), ag-c75a01 (H9).

**2026-09-29 ~23:10 UTC:**
- **Merged:**
  - ag-304283 (2542bd1). The H1 fixtures are fixed, and the full suite shows only the 72 known reds plus one flake (`test_p0_r8f_9[inf_string]`, which passes on its own).
  - The new `tests/test_h1h3_round2_clean.py` is RED in 18 cases: HA-R12 is not implemented in `cmd_clean`.
    - A str or list entry crashes it.
    - Under `--branches`, it falls back to the host record and deletes the malformed node's real branch.
    - `--tree --homes` emit no event.
  - **Decision:** keep the strict reading (an event in every pass). This needs an implementer, see TODO.
  - ag-c75a01: H9 done (abe4059, AGENTS.md created).
- **D1 review, ag-de331d:** VERDICT rejected, 8 findings, still to fix (to go to implementer-deep, `verifies=ag-179b69`):
  1. P1, runner.py:1462: OOM attribution sees only this Runner's peers, so a cross-process sibling is misattributed. It must report kill_uncertain.
  2. P1, runner.py:2345: adopted docker runs have no oom_reader, so an exit 137 after a restart is silent.
  3. P1, notices.py:229: dedup state in the agent-writable tree.json lets a forged active entry suppress a real limit_hit.
  4. P2, runner.py:2675: `commit_fix_attempts: 0` suppresses the exhaustion notice.
  5. P2, runner.py:1809: deferred notices are not cleared when a pause clears.
  6. P2, notices.py:309: on a caller's first wait, a hit and clear that happened before it are lost.
  7. P2, notices.py:301: `since()` reads the whole event backlog under the exclusive lock, and cursors are never retired.
  8. P2, runner.py:2260: provenance is looked up from the current YAML instead of being captured at launch.
- **z.ai / GLM (user):**
  - The user is subscribing to the GLM Coding Plan and will log in via opencode.
  - Analysis in ~/zcode-analysis/ (README.md, USAGE.md, usage.py). Quota = `GET https://api.z.ai/api/monitor/usage/quota/limit` with `Authorization: <Coding Plan key>` (raw key); `data.limits` holds a 5 h window (`unit` 3, `number` 5) and a weekly one (`unit` 6); `percentage` is the % used; `nextResetTime` is in ms.
  - **Plan:** after H7, add an `opencode-zai` provider (extends opencode, its own XDG_DATA_HOME, `models_include` for the zai models) plus its budget action.
  - Before that: the user runs `usage.py` live once, and we check the provider key name in auth.json (keys only, never values). Egress to api.z.ai is needed only under docker, and must be ASKED for.
  - **Off-peak: FUTURE idea, not now.** It is a ticket system with a separate inference endpoint that needs the ZCode OAuth JWT and impersonates the ZCode client, so it is a likely ToS risk. A model-side skill or MCP cannot work: the ticket must wrap the whole session. The right form would be runner-level: take the ticket, defer, launch with an OPENCODE_CONFIG overlay, settle.
- **User 23:05:** launch H7, preferring codex models (claude at 86%), THEN PAUSE: nothing else starts after H7. H7 researcher: ag-aedab3.

**2026-09-29 ~23:40 UTC: H7 in progress (user: codex first, then PAUSE; nothing else starts).**
- Contract `context/specs/h7-provider-startup.md` (dbf5248), PS-R1..R7, revised after advisor turn 3.
- Tester ag-e5d2c3 (codex gpt-6-sol, timeout 2700).
- **Next:**
  1. Merge the tester.
  2. Run implementer-deep with `model=gpt-6-astra` (codex) on PS-R1..R7.
  3. Run the adversary, since this code decides routing.
  4. Merge, advance wt-main, and ask the user for `/mcp`.
  5. Then PAUSE, and wait for the user (z.ai login).
- **Queued after the pause:**
  - opencode-zai with its quota;
  - the D3 contract revision;
  - the D1 fixes (8 findings);
  - the HA-R12 `clean` implementation (18 red tests);
  - H5, H6, H8, H10–H13, D2.

**2026-09-30 ~00:10 UTC: H7.**
- **Tester ag-e5d2c3 merged (acaae5c):** tests/test_h7_provider_startup.py, 30 cases, 27 red for the right reasons and 3 green. Verified by the orchestrator outside the sandbox, in 8 s.
  - **Tooling observation, for a ticket:** codex with `permission: sandbox` cannot commit in a linked worktree (the index.lock under `.git/worktrees/<id>` is read-only). The runner's leftover commit saved the work. Fake-CLI Runner tests also stall inside the codex sandbox.
- **PS-R5 stale-generation clause:** there is no public test seam. The implementer must say where it lives, and the adversary targets it.
- **Implementer-deep ag-2abbbc** (codex gpt-6-astra, timeout 5400) is on PS-R1..R7.
  - It was told tests/ is read-only by instruction.
  - **Note:** implementer-deep has `readonly_paths: []` in agents.yaml, so the readonly revert does not protect the tests. Check the diff for any tests/ change before merging.

**2026-09-29 22:06 UTC:** ag-2abbbc failed after 42 s with `quota exhausted`: codex's 5 h window is at 86% and resets at 23:45 UTC. Claude's session is at 89%. The user prefers codex.
- A background wake is armed for 23:48 UTC.
- **On waking:** `steer_agent(ag-2abbbc, "the codex window has reset, carry on with the task")`.

**2026-09-29 ~22:20 UTC: z.ai connected by the user in opencode.**
- The auth.json provider key is `zai-coding-plan`, with fields `key` and `type: api`. Only the names were read.
- `opencode models` lists `zai-coding-plan/{glm-4.7, glm-5-turbo, glm-5.2, glm-5.2-highspeed, glm-5.3, glm-5.3-flash, glm-5.3-highspeed}`.
- A live `opencode run -m zai-coding-plan/glm-5.3-flash` works: `text` and `step_finish` with tokens, and a reported cost of 0.
- The user was asked to run `usage.py --provider zai` live to confirm the quota endpoint.

**2026-09-29 ~22:35 UTC: the z.ai quota endpoint was CONFIRMED live by the user** (`usage.py --provider zai`).
- `level: lite`.
- Both windows are `CREDIT_LIMIT`:
  - 5 h: unit 3, number 5, `usage` 2000 (capacity), `currentValue` 1, `remaining` 1998, `percentage` 1;
  - weekly: unit 6, number 1, `usage` 10000.
- `nextResetTime` is in ms.
- The single test call cost 1 credit.
- Note: `usage` = capacity and `currentValue` = used (not what the field names suggest).
- The opencode-zai budget action can be written from this: headroom = the worst of the two `percentage` values.

**2026-09-29 ~22:45 UTC: USER MANDATE (autonomous overnight).** Quote: "implement all this with claude and codex; when I come back tomorrow everything will be ready to use GLM in our dev team. When you are done with that, continue with the original plan; I will tell you when I am back." Both claude and codex may be used.
- **Order:**
  1. H7 (implementer ag-2abbbc, resuming after the codex reset at 23:45 UTC), then the adversary, then merge.
  2. opencode-zai, contract `context/specs/zai-provider.md` (ZA-R1..R5): tester, then implementer AFTER H7 merges (both touch opencode.sh and providers.yaml), then merge.
  3. Enable opencode-zai in `.multiagents/config/providers.yaml`, then refresh-models.
  4. Roster: add `opencode-zai: zai-coding-plan/glm-5.3*` to agents' `models:`. Consult the advisor first; the user explicitly authorised making GLM usable by the team.
  5. Advance wt-main. The user must run `/mcp` on return: note it in the final report.
  6. The original plan:
     - D1 fixes (8 findings);
     - HA-R12 clean;
     - the D3 contract revision, then its pipeline;
     - H5, H6, H8, H10–H13, D2.

**Not in this phase:**
- phases 2 and 3 of the review (the 72 by-design reds stay red);
- tmux step 2;
- nested codex spawns.

---

## Phase 5 — the Codex provider — **DONE 2026-09-28** (history below)

**What is complete:**
- Phase 0, with contracts A and B.
- QF, SV, SP, commit-identity and sandbox-git (smoke test passed on
  2026-09-28).
- All of it is **merged into `main`** (fast-forward from
  `refactor/split-consume`, then `b3f3910`).
- The dated progress logs further down are history. Do not redo what they
  mark as merged.

**Where new work branches from:** `main`. `refactor/split-consume` is
finished, and nothing new goes there.

**Next:** integrate Codex (OpenAI's CLI) as a shipped, fourth provider.
- The requirements, the decisions already taken with the user, and the open
  questions are in `context/specs/codex-provider.md` (ids `CX-D*`, `CX-R*`,
  `CX-Q*`). The user's original proposal is in `context/codex-proposal/`.
- In short:
  - ship it under `src/multiagents/defaults/`, with no provider name in core
    code;
  - use a dedicated profile (`~/.multiagents/profiles/codex`) and never the
    user's `~/.codex`;
  - read the real quota from Codex's rollout files, because the plan is
    ChatGPT **Plus** and its windows are tight;
  - mount the native binary in the container, and settle Codex's own sandbox
    inside Docker by a live test;
  - no interactive orchestrator on Codex in this phase.
- Egress: the user approved `openai.com` and `chatgpt.com`, and they are
  already in the project's allowlist.
- Roles planned for Codex once it works: adversary, reviewer,
  implementer-quick, researcher and advisor, plus a claude fallback for
  tester, implementer and implementer-deep. The proposal is in
  `.multiagents/proposals/agents-codex.yaml`. Models are pinned after a
  measurement (CX-R11), not before.
- Full pipeline with an adversary.

**Progress, 2026-09-28 ~08:25 UTC:**
- **Done:**
  - The contract, CX-C1 to C15 with amendments (`cc62eb1`..`980d66c`).
  - The red tests, merged:
    - provider: 115 tests (`115b54d`, `tests/test_codex_provider*.py`);
    - engine: 52 tests, 29 of them red (`425cafe`, `tests/test_codex_engine_*.py`).
- **Next:** two implementers in parallel, both from `main`.
  - **Provider half** (CX-C7..C14, `implementer`). Files:
    - `defaults/providers/codex.py`;
    - the codex block and agy `billing: plan` in `defaults/providers.yaml`;
    - egress in `defaults/project.yaml`.
  - **Engine half** (CX-C1..C6, C15, `implementer-deep`). Files: the core
    `.py` files, `executor/`, `models.py`, `budget.py`, `tui.py`.
- **Paused:** claude's 5 h window is at 92% and resets at 10:39 UTC.
  opencode fails at start (`ag-3ba37b` was discarded), and agy is excluded.
- **CX-Q3 withdrawn** (9a66fcc): quota is read live via `codex app-server` `account/rateLimits/read`, per account.
- **Tooling:** commits made inside the container print
  `packed-refs.lock: Read-only file system` (`ag-155ed5`, `ag-4aa932`),
  although the commit lands. Investigate this before it bites; it is linked
  to SG-R2's read-only `.git`.

**Found on 2026-09-28: the claude quota reading follows the wrong account under docker.**
- `budget.read_provider` calls `read_claude` with no `config_dir`
  (`budget.py:872`), so it always reads the host's `~/.claude`. That is the
  orchestrator's account.
- Docker agents spend the container profile's account (the private backing).
- The user is splitting the two accounts: the orchestrator gets a new
  login, and the container keeps its own. From then on the router sees the
  orchestrator's quota while the agents spend the container's.
- **Correction:** multi-account failover inside the container DOES exist,
  through the auth proxy (`auth_proxy: true` in this project).
  `multiagents auth login claude --account <label>` adds a labelled login in
  `vault/accounts/<label>`, and work moves onto it when the first account
  runs out of window. It has not been exercised live here yet.
- 2026-09-28 16:30 UTC: the user's first `multiagents auth login claude` did
  NOT change the vault, which is still the old account (`vault/.claude.json`
  mtime 15:41). Container agents got 429 `rate_limit` (ag-72497d, ag-14a3e5).
- **Resolved at 16:43 UTC.** The login had been run on another machine
  (`REDACTED`). Redone on `REDACTED`, it put account b
  (`claudeai.REDACTED`) in `vault/accounts/b`, and ag-72497d resumed its
  session on it. That confirms a session resumes across an account change.
  - Note: once `accounts/` exists, `authproxy.Accounts.labels()` lists only
    `accounts/*`, so the old top-level credential ("default") is no longer
    used. That is fine here, but it is not "failover" to the old account.
- **To fix next, after Codex:** give the builtin claude reader the
  executor's profile.

**User rule, 2026-09-28 ~16:10 UTC:**
- Run one agent at a time.
- Stop at 95% of claude's 5 h window.
- The user will later re-authenticate BOTH the orchestrator and the container
  on a second claude account. The two will then share an account, which
  sidesteps the budget-account mismatch above until it is fixed.

**Handoff, 2026-09-28 ~17:30 UTC: paused at 89% of claude's 5 h window (user rule: stop at 95%).**
- **Merged on `main`:**
  - the engine (be92256, a1411e2);
  - the adapter (3243ef9);
  - the attack tests and fixes (ce288fc, 4f3f185);
  - the review tests for CX-C21..C26 (26a617d, 24 red).
- Codex is disabled locally (`.multiagents/config/providers.yaml`) until the
  live checks. The container was recreated after the `/mcp` reconnect.
- **Next, after the 21:10 UTC reset, one agent at a time:**
  1. `tester`: remove or rewrite the env-allowlist cases in
     `tests/test_codex_provider_review.py`, per def4854.
  2. `implementer`: CX-C21..C26 in `codex.py` (spec section "Adapter
     review at 4f3f185" and its decisions).
  3. My own Phase 6 read of the whole codex diff against the contract.
  4. The live checks L1..L8. These need the user for
     `multiagents auth login codex`, and codex re-enabled with a container
     recreate.

**Incident, 2026-09-28 ~21:25 UTC (orchestrator error, repaired).**
- A stray `git checkout 9d34847 --` in the project root detached HEAD at
  the pre-codex commit.
- One spec commit (9f21895) and tester ag-3d32c4's branch were then based
  on it.
- **Repaired:** HEAD is back on `main`, CX-C27 is re-applied (3095671), the
  test update is cherry-picked (7b644a4), and ag-3d32c4 is discarded.
- `main` itself was never affected.
- **Lesson:** never pass a commit to `git checkout` in the project root.
  Use `git show <rev>:<path>` or a worktree.

**Codex offline work: done (2026-09-28 ~21:40 UTC, main at a3fd1a7).**
- Everything is merged: CX-C1..C27 (CX-C25 half withdrawn), the tests
  updated to four providers, and the SP-R5 drift fix.
- Codex is re-enabled. The container is recreated with its mounts, all
  verified with `docker inspect`:
  - the backing at `~/.codex`, masking the user's own;
  - the versions root, read-only;
  - the adapter, read-only.
- **Live checks L1..L8 are blocked on the user:**
  - `multiagents auth login codex` on `REDACTED`;
  - a roster decision: a codex-pinned probe agent is needed to run agents on
    codex at all. The roles planned in CX-D4 wait for CX-R11.

**Codex live status, 2026-09-28 ~22:05 UTC:**
- **Passed:** L1, L3 (codex's bwrap cannot run in the container, so
  `danger-full-access` stays), L4a, L5 (the app-server read works), L6.
- **L8 measured** (spec, "L8 / CX-R11").
- **Waiting on the user:**
  - **CX-Q1:** the model per codex role. I recommend `gpt-5.6-terra` for
    cheap roles and fallbacks, and `gpt-6-sol` for
    adversary/reviewer/advisor.
  - **The CX-D4 roster change.** Then L7 (consult on a codex advisor), L2
    (a refresh and concurrent runs) and the L4 update simulation.
- **Temporary:** `codex-probe` is in `.multiagents/config/agents.yaml` and
  in the `implement` team roster in project.yaml. Remove it after the
  roster change.
- `refresh-models` dropped the `opencode-go/*` models from `models.yaml`,
  leaving only the free ones. Check this before opencode is used again.

**Phase 5 (Codex): complete, 2026-09-28 ~22:40 UTC (main at 893365f).**
- The contract, CX-C1..C28, is implemented and merged.
- Live checks: L1, L3, L4a, L5, L6, L7 and L8 passed or were measured.
  L2, L4b and L5-exec are deferred to their natural occurrence, as agreed
  with the advisor.
- The roster is live: advisor, adversary and reviewer on codex/gpt-6-sol;
  implementer-quick and researcher on codex/gpt-5.6-terra; a codex fallback
  for implementer and implementer-deep.
- The advisor is now ag-3f9bba on codex. It received the 20-entry catch-up
  (turn 3).
- **Follow-ups found, not fixed (for the next phase):**
  1. SG: `branch_pending_delete` can be forged from the container.
     Serious: it can delete another agent's unmerged branch.
  2. SG-R5: base hooks run merged agent code on the host.
  3. Empty fallback model on the steer path (`_spec_of`) and on the start
     path, when routing picks a family sibling (`runner.py` ~1581, ~2826).
  4. Nested codex spawns get the launcher path, so `codex-code-mode-host`
     is missing.
  5. The claude budget reader reads only the host account under docker.
  6. `refresh-models` fails before the first codex use, and it dropped the
     `opencode-go/*` models.
  7. Commits inside the container print a `packed-refs.lock` error (they
     still land).
  8. Prompts go to adapters in argv, which caps them at 128 KiB (E2BIG).

**Still queued behind Codex** (from "Progress", 2026-09-27; not started):
- AGENTS.md should name `scripts/test-chunk.sh`.
- A live compaction test.
- A content-filter refusal reported as `done`.
- The config-drift warning.
- opencode startup failures, which the router still picks.
- An explicit `model:` override does not pin the provider.
- Idle nodes and about 20 unmerged `agents/*` branches.
- The limit-hit notices idea (end of this file).
- Phases 2 and 3, paused.
- The advisor catch-up consult, once agy is back
  (`context/advisor-catchup.md`).

---

## What this project is

`multiagents` is an orchestration tool: a root orchestrator delegates to
specialist subagents, each in its own git worktree on its own branch, and the
parent merges what it accepts. Agents run inside one Docker container per
project, on an internal network whose only route out is a filtering proxy.

The thing that shapes every task here:

> **The codebase you are changing is multiagents itself.** You are running
> inside it. Your worktree is a checkout of the source that spawned you, merged
> your branch, and metered your tokens.

So a defect you fix is a defect in the machine you are standing on. That is why
phase 1 exists and why it comes first.

---

## Running the suite

```
uv run --frozen pytest
```

**Green is `949 passed, 1 failed, 3 skipped`** — measured 2026-09-17 from a
fresh worktree in the container with an agent's environment. The suite grew from
548 to 953 tests when the review's characterization work merged.

**The 1 failure is expected, and it is `F100`.**
`tests/test_c2_provider_harness.py::test_read_provider_caches_until_invalidated`
passes in isolation and fails in the full suite, because `budget._cache` is
keyed only by provider name and tests bleed state through it. It is scheduled as
phase 1 item 8. **Until that lands, one red test is the baseline, not a
regression you caused.** After it lands, green means green and the number is 953.

The 3 skips shell out to `docker`, deliberately absent in the container.

`REPORT.md` states the suite fails 17 tests in an agent container with
`PermissionError: can_spawn is false`. That did not reproduce on 2026-09-17 with
`MULTIAGENTS_CAN_SPAWN=false` set — one failure, not seventeen. Treat the 17 as
unconfirmed. If you see them, say so and say what your environment had that this
measurement did not.

Two traps that produce failures which are not defects:

- **Running from the project root on the host.** The root is bind-mounted into
  the container, so host and container share `.venv`, and `.venv/bin/pytest`
  carries a stale container-path shebang. A bare `uv run --frozen pytest` there
  runs a different pytest without `mcp` and reports 7 phantom failures. Use
  `uv run --frozen python -m pytest` in the project root. This does not reach
  you: your worktree has no `.venv` and `uv` builds you a clean one.
- **A bare `docker exec` is not an agent.** No git identity → 6 git tests fail.
  No forwarded PATH → `uv: not found`. Both are the harness.

**If a failure looks environmental, it probably is.** Check before filing.

---

## Before anything: who may touch a test

`project.yaml` ships a default `readonly_paths` list — `tests/**`,
`**/test_*.py`, `conftest.py` and friends. **An agent's own list REPLACES this
one rather than adding to it.** So `tester` is exempt with `[]`, and all three
implementer tiers, which omit the key, INHERIT it.

Creating a **new** file under those globs is allowed. Modifying an **existing**
one is reverted at the merge gate. Your run still reports success. `merge_agent`
reports the loss in `readonly_reverted` and `server.py:863` documents reading it
— **read it**, and treat a non-empty value as a failed merge for those paths,
not a footnote. Two runs were lost this way during the review and one left a red
test behind; that is `bug-08f9b3`.

**Three scheduled items require changing existing tests** and cannot be done by
a restricted agent: `bug-08f9b3`'s own test fix, `F50`'s suite rewrite, and
`F150`'s wrong assertion.

**The obvious workaround does not work.** A new test file that supersedes the old
one leaves the old one in the suite, where it runs against the fixed code, goes
red, and strands the agent holding a failure it may not touch.

So: `.multiagents/proposals/agents.yaml` proposes `readonly_paths: []` on
**`implementer-deep` only**, scoped to this phase and given back when the three
rewrites merge.

**It is needed before phase 2, not before phase 1.** Phase 1 can start without
it, because `tester` already carries `readonly_paths: []` and the one assertion
phase 1 has to change is a *contract* error, which is tester's job by
definition:

```python
assert "readonly_paths" not in harness, \
    "the harness builder is the one review agent that may edit shared files"
```

The message claims the agent may edit shared files while the check asserts the
key is absent — which produces the opposite. Send that to `tester`. The other
half of `bug-08f9b3`, landing the fix in
`src/multiagents/defaults/agents.yaml`, is not a test file and blocks nobody.

The exemption earns itself at **`F50`**, where the function and its 48 tests
change together and splitting them across two agents costs a handoff per
iteration.

Everything else keeps the separation. `tester` writes the NEW tests each ticket
names, red, and an implementer makes them green. That is the normal loop and it
needs no exemption.

---

## Phase 0 — the runtime the team runs on

**Inserted 2026-09-22, ahead of everything.** The work described in Phases 1–4
is paused, not abandoned: `refactor/split-consume` carries the R14–R18
implementation and is committed and clean. Come back to it when this phase
lands.

**Eight items, from three channels.** R1–R3 come from
`context/review/C4-runtime-observed.md` (`F200`, `F201`, `F202`); R4–R6 are the
three bug tickets still `awaiting_user` (`bug-b1c130`, `bug-2138e6`,
`bug-b864b8`); R7 and R8 are features the user asked for on 2026-09-22 and are
the only things in this phase that are not repairs. None of R1–R6 was reachable by reading the code — each needed
the logs of real runs — and they are not independent chores: R2 and R4 are the
same function, R4 is why R5 cost two runs, and R1/R2/R3 are one failure
arriving in three steps. That is why they are one phase.

**Two contracts, not one.** The advisor's judgement, taken: R1–R6 are repairs
against evidence that already exists, R7–R8 are features whose shape is still
being decided, and R8 alone reaches the runner, the config layer and the
provider script contract. One contract spanning both would be negotiating a
design while landing fixes. So **R1–R6 first, as one contract; R7–R8 second, as
another.** Nothing in R7 or R8 is blocked by that order, and R1 has to land
before either of them can be tested anyway.

**Close each ticket with `resolve_ticket` when its fix merges.** A ticket whose
fix landed and still reads `awaiting_user` gets refiled by the next review —
which is exactly how `bug-49c1c1`, `bug-7c4b78`, `bug-2a0af0` and `bug-087fee`
came to be declined duplicates.

**Before any agent can be launched at all**, a human runs, once:

```
multiagents docker rm && multiagents docker up
```

The container was created against a claude CLI version that no longer exists
and refuses every spawn. Nothing is running (checked: the newest result-less
run is hours old), so this costs nothing today. It is a manual step because
recreating a container kills whatever is inside it, and that is not a decision
an agent gets to make.

### Status — contract A (R1–R6) DONE, 2026-09-23

Contract: `context/specs/phase0-runtime-repairs.md` (ids `P0-R1.1`…`P0-R6.6`,
with dated amendments from the testers, the adversary and the reviewer).
All merged on `refactor/split-consume`; verified live after a container
recreate and an MCP reconnect. Tickets `bug-b864b8`, `bug-b1c130`, `bug-2138e6`
resolved `fixed`; findings F200–F202 `fixed`. The local `max_steps: 600`
workaround is removed. Full suite: 1179 passed, 73 failed = 72 in
`test_phase2_entry_semantics.py` (Phase 2 R14/R15, paused) + 1
environment-dependent `test_core` test that already failed before Phase 0.
Mutation checks: 12 of 12 caught.

Found on the way: the watchdog's timer loop had been dying on its first poll
(a `quiet_for` method/property collision), so no `timeout` or `silence` trip
had ever been reported. Fixed under P0-R2.9.

**Open follow-ups, small, not yet scheduled** — decide before or alongside
contract B:

1. **agy guidance may be too weak.** In one run (`ag-eafe1e`)
   gemini-3.8-flash-medium had the `agent_guidance` in its prompt and still
   re-read one file head 5×. The seam works; the content may need to say
   "read ranges with the shell". One run is a hint, not evidence.
2. **`stuck` is sticky.** Once set it stays even when the agent resumes normal
   work, and `wait_for_agents` then returns immediately for that agent, so it
   can no longer be waited on. Contract A said when to *report*, never when
   to *clear*.
3. **claude 2.1.280 emits `tool_progress` heartbeats** that no stream rule
   classifies (they land as `raw`). One rule in `providers.yaml`.
4. **The local `doom_loop_repeats: 3`** (gitignored project config) is stricter
   than the shipped 5 and trips on agy's `manage_task` updates. The user's call.
5. **Tooling:** an `implementer-deep` run reported having no `consult` tool to
   reach `dev-advisor`, contrary to the orchestrator protocol. Needs a ticket.
6. **Phase 3 leftovers already on disk:** branch `agents/tester/cf02a1` holds
   R19/R20 tests (25 red), produced when a deferred task restarted itself;
   unmerged because Phase 3 is paused.

### Status — contract B (R7, R8), started 2026-09-23 05:05, unattended

Contract: `context/specs/phase0-context-and-team.md` (P0-R7.1–R7.12,
P0-R8a–R8e). Decisions taken in it, for the user to see:

- **R8c applies to `--unattended` only.** Interactively the CLI holds the
  session for the whole conversation, so there is no gap between turns the
  driver controls; interactive protection is the wind-down notice (R8a), the
  brief (R8b) and the automatic threshold (R8e).
- **R8d opencode is deferred:** `opencode.sh compact` exits 64. The HTTP route
  needs an `opencode serve` process and cannot be verified while opencode's
  monthly quota is exhausted (until 2026-10-05).
- Thresholds shipped: `compact_at_tokens: 120000`,
  `context_wind_down_tokens: 150000` (tunable, 0 disables).

**Result, 2026-09-23 ~05:40.** P0-R7 (team choice) and P0-R8a–R8e are all
implemented and merged on `refactor/split-consume`. The contract tests total
190: 78 for R7 and 112 for R8. Full suite, run by the orchestrator with
`test_phase2_entry_semantics.py` excluded: **1348 passed, 1 failed, 2 xfailed**.
The one failure is
`test_core::test_the_claude_script_uses_the_container_profile_only_where_it_should`,
which already failed at 5249107, before contract B.

The orchestrator reviewed the R7 diff. It found two real-terminal defects the
tests cannot see: `setraw` made the redraw staircase, and a lone Escape
blocked. Both are fixed: the reader now uses cbreak mode, and reads the fd
with a `select` timeout.

**Still open on contract B:**
- **Adversary, reviewer and advisor.** None has looked at the finished diff:
  all three run on agy or opencode, and those were down. R8c/R8d is the part
  that deserves the attack. A compaction is irreversible, and the claude
  script decides success by parsing the transcript.
- **Live check.** `claude.sh compact` has never run against a real session.
  Only a fake CLI has exercised it; the invocation itself was verified by
  hand on 2026-09-22.
- **Step counter looks inflated in the wild.** A tester (`ag-48f51a`) tripped
  `runaway_steps` at 251 in about 5 minutes. Its stream shows bursts of about
  25 `step` events within the same millisecond, which should be impossible
  under P0-R4's turn-only counting. Nothing was killed and the work finished.
  This needs the raw CLI stream to diagnose; it probably joins follow-up 3
  (`tool_progress`).
- **Claude's 5-hour window.** It went from 0% to 88% in about 35 minutes of
  two or three Opus/Sonnet agents; the wrap-up at 05:38 was legitimate. It
  resets at 09:59.

**Progress, 2026-09-23 ~10:50, paused for a `multiagents run` restart
(the user re-authenticated agy):**

- **P0-R8f** (interactive compaction, stop → compact → resume, 30 s grace).
  The user asked for it; it is in the contract, with the tester's amendments.
  Its tests are merged (`tests/test_phase0_interactive_compact.py`, 56 tests,
  30 red). **Not implemented yet. Next step:** `implementer-deep` on
  P0-R8f.1–R8f.7, with `verifies` = ag-2c808f.
- **The attack on R8a/R8c/R8d is merged** (`tests/test_phase0_contract_b_attack*.py`).
  The adversary was unreachable (`model` cannot cross providers), so
  `tester` ag-57985b stood in for it. Ten real defects, red, ranked:
  1. A compaction that times out keeps running as an orphan while the next
     turn starts on the same session.
  2. The slug rule is wrong for paths with spaces or `+@~` or non-ASCII:
     Claude replaces every non-alphanumeric character, and hashes paths
     over 200 characters. This affects `watchdog.transcript_source` and
     `claude.sh`.
  3. Non-UTF-8 output from a script crashes `run_action`, which promises
     never to raise.
  4. `wc -l` is off by one on an unterminated last line, which yields a
     false success.
  5. A malformed limit (`120k`) crashes `_compact_if_due`.
  6. One non-UTF-8 byte in the transcript turns a real compaction into a
     failure.
  7. A huge line is read in quadratic time.
  8. `Infinity` in a usage field raises `OverflowError` on every tool call.
  9. String usage fields are concatenated instead of added.
  10. The failure line is printed unbounded.
  Four findings are accepted as `xfail(strict)`, with reasons in the tests.
  **Next step:** route 1, 3, 5, 10 (driver/scripts) and 2, 4, 6, 7, 8, 9
  (reading/claude.sh) to an implementer. Finding 2 also touches the
  pre-existing `launch` arm of `claude.sh`.
- The reviewer and advisor pass on contract B is still to do, now that agy
  is back.
- `runaway_steps` fired falsely again (ag-57985b at 251), which confirms the
  inflated step counter noted above.

**Progress, 2026-09-23 ~11:30 (after the restart):**
- **Advisor review of contract B (ag-25c350, turns 3–4).** It first said to
  drop R8f and claimed the invariant was broken and the thresholds inverted.
  After checking, it withdrew three of those points: the cited lines predate
  this work, compaction only happens while idle so the wind-down still fires
  while agents run, and the tester did find the slug bug. It kept one point:
  unsubmitted typing is invisible to the driver. Decided with the user in
  c886f79: idle default 300 s, a bell (`compact_bell`), "send … to cancel"
  wording, and all values configurable (R8f.8, R8f.9). The tester's questions
  were decided in e3c1b96.
- **Merged:** the updated R8f tests (4800277, 47 red until implemented).
  Attack findings 2, 4, 6, 7, 8 and 9 are fixed (52105e7). The long-path
  (>200 chars) hashed slug is **not** done, because no test asks for it. It
  stays open.
- **In flight:** `implementer-deep` ag-829577 on R8f and driver findings
  1, 3, 5 and 10. `implementer-quick` ag-9c3885 on the monitor history sort
  (the user decided it). `implementer-quick` ag-77113c on the researcher
  brief.
- **The monitor history "missing" advisors.** Consult runs are in the
  history but buried: roots are sorted by `started_at`, and a conversational
  agent keeps one node across days (ag-25c350 started 2026-09-22 20:42). The
  user decided to sort roots by last activity.
- **Tooling defects seen today** (Phase 0 follow-ups):
  1. `stuck` stays on a node after the agent recovers or finishes.
  2. The silence watchdog fires while an agent waits on its own long
     background test run.
  3. `doom_loop` false positive on agy: `view_file` events carry the path
     but not the line range, so paging through a file looks like a loop.
  4. `total_tokens` excludes cache reads. ag-4548ac showed 1.86M, but
     processed about 8.1M.
  5. An agy agent can end its turn while its own background job is still
     running, and so commit nothing (ag-9c3885).
- **Researcher token cost; the experiment is running.** ag-4548ac (a
  researcher on agy) used 128 steps, with 1.8M uncached input plus 6.3M
  cache reads, and ended with a context of about 157k. It paged whole large
  files (`runner.py` 10×, `tree.py` 7×) instead of searching. Its brief
  said "read widely", and my task broke "one question per run". The user
  approved a brief change: locate with `rg -n`, read line ranges only, stop
  once answerable (ag-77113c). **Evaluate it on the next researcher runs**:
  record steps, uncached input and cache reads, and the final context, per
  run, against the ag-4548ac baseline. A small single-question baseline is
  ag-85e40d, at 34k.
  **ag-77113c was DISCARDED; the change is not applied yet.** It force-added
  a new `.multiagents/config/agents/researcher.md` to git. `.multiagents/`
  is gitignored, and that path already holds an old untracked project
  override from 2026-09-05, which a merge would have clobbered. Its version
  also lacked the "Calling this agent" section. Its edit of
  `src/multiagents/defaults/agents/team/researcher.md` was correct; redo it
  alone. The live copies are the global
  `~/.config/multiagents/agents/team/researcher.md` (identical to the old
  default) and that old project override. First find out, with a researcher
  or by reading the config loader, which one wins. Then have the user update
  the live copy, or run `multiagents` config sync if there is one. Never
  commit under `.multiagents/`.
  **If it works, consider the same rule for other read-heavy agents**:
  reviewer, advisor, dev-advisor, auditor, cartographer, characterizer.
  Their briefs have not been checked for similar "read widely" wording yet.

**Handoff, 2026-09-23 ~12:40 (orchestrator context wind-down at 155k):**
- **Merged since ~11:30:**
  - R8f and driver findings 1, 3, 5 and 10 (d4ae4ec).
  - The monitor history sorted by last activity (29218c6).
  - The F110/F111 pins inverted (137f37f); the ledger still needs
    `set_finding_status` for F110 and F111 → fixed, naming d4ae4ec.
  - Spec decisions b23fb61 and c483d51: a malformed limit now falls back to
    its **shipped default**, not off. **The code still falls back to off.**
- **Next implementer task** (driver.py/scripts.py/claude.sh, one run,
  `implementer` tier):
  1. Implement c483d51: malformed `compact_at_tokens`,
     `context_wind_down_tokens` and `compact_timeout_seconds` fall back to
     the shipped default. Check the server's `_limit` too.
  2. Safe-parse `restart_attempts`, `restart_delay_seconds` and
     `limit_max_waits` (same crash class as finding 5, `driver.py` ~505).
  3. Reviewer ag-e8565d findings (VERDICT rejected; its report is in
     `review.md` on its branch, which is not merged):
     - SIGTERM leaves the terminal dirty (`driver.py` ~204, `cli.py`
       picker);
     - `run_action` leaks the child on KeyboardInterrupt (`scripts.py` ~186);
     - a clean CLI exit racing with grace expiry is relaunched instead of
       honoured (`driver.py` ~810);
     - `claude.sh` ~523 uses `echo` on CLI output: use `printf`.
     The tester should first write red tests for the first three. Declined:
     `transcripts._usage_of` using "message"/"usage" (it predates this work,
     and the invariant lists `compact_boundary`, `compactMetadata` and
     `/compact`); caching `_launched_spec`; moving the claude.sh parse into
     Python (it would move provider vocabulary INTO Python).
- **Not yet run:** the adversary on R8f (task text: attack stop → compact →
  resume, signals during grace, compaction and relaunch, loops, the
  process-group kill). Also a live check of `claude.sh compact` against a
  real session.
- **The advisor reviews stale code.** A conversational agent's worktree stays
  at the commit where its conversation began (ag-25c350: 2026-09-22 20:42),
  and it could not run `git show`. Its contract-B review read pre-contract-B
  code. File a ticket with the bug-reporter, or start a fresh advisor
  conversation. Its turn-5 advice was partly taken (c483d51); its
  `setsid` → `setpgrp` point was declined, because `login` uses
  `exec_action` and a tty read in a background group gets SIGTTIN.
- **More tooling defects:**
  6. The implementer-deep toolset lacked `consult` (dev-advisor), ag-829577.
  7. Stuck and idle nodes seem to count against `max_concurrent`:
     `start_agent` refused with "4 running" when 2 were really running.
  8. `test_core.py` has 18 tests that read the live `MULTIAGENTS_CAN_SPAWN=0`
     etc., so they fail inside any agent that cannot spawn (ag-b3d873).
  9. agy agents repeatedly end their turn with a background test still
     running (ag-9c3885, ag-77113c twice).
- **Leftover nodes to clean:**
  - ag-4548ac, a researcher: done but marked stuck; nothing to merge.

**Progress, 2026-09-23 ~13:50:** the red tests have merged: R8f.10-13
(42fa3de), CF (consult refreshes the worktree, bug-7f6ba7, 62e5a37), and the
adversary's R8f findings (12d893f). The decisions are cd59fec. The researcher
brief merged as 5603870, but the project copy
`.multiagents/config/agents/team/researcher.md` still shadows it. **Next, at
15:01 (a pause the user asked for):**
- implementer-deep on R8f.10-17, plus echo→printf (driver.py, scripts.py,
  cli.py, claude.sh, server.py);
- a second implementer-deep, in parallel, on CF-R1 to CF-R7 (runner.py).
The tickets bug-7f6ba7 and bug-2cebea are parked for the user.
**15:01:** the R8f implementer-deep was DEFERRED (tree paused until 15:16,
claude's reading still stale), and it restarts on the next `wait_for_agents`.
The CF implementer-deep (runner.py, tests/test_consult_fresh_worktree.py,
spec consult-fresh-worktree.md) has NOT been started yet: start it once the
pause clears. The researcher brief is now live: the project copy was
overwritten at the user's request.

**Progress, 2026-09-23 ~17:00:**
- **Merged:**
  - R8f.10-17 (a8e695f), with the review fixes in 478f276;
  - the CF fix for bug-7f6ba7, where consult refreshes a conversational
    worktree (6035c65, round 2 in 6b24462);
  - the tests: 42fa3de, 62e5a37, 12d893f, 3832697, 4d860fa, 575193e.
  The full suite was green on the round-2 branch (1549 passed).
- **Declined:**
  - an empty commit counts as absorbed;
  - `fcntl` on Windows, since the project is POSIX-only already;
  - the `stopping` attribute on a callable (style only);
  - the headless "no shrink" loop test, because R8c.3 wins.
- **Not yet done:**
  - a live check of CF, which needs the MCP server restarted (/mcp),
    because the running server still has the old runner code;
  - then one consult of the advisor, to confirm it reads current code;
  - a live `claude.sh compact` check against a real session.
- **Noted, not fixed:**
  - `claude.sh launch` still looks under `$HOME/.claude`, not
    `CLAUDE_CONFIG_DIR`;
  - the usage-limit stop has the same exit race as R8f.12;
  - `limit_wait_seconds`, `restart_min_runtime_seconds`,
    `supervised_turns` and `spend_limit_pause_hours` still parse unsafely;
  - implementer-deep again had no `consult` tool (ag-d67496), which is
    tooling defect 6, still without a ticket.

**Handoff, 2026-09-23 ~17:30 (orchestrator wind-down at 205k):**
- **CF verified live after the /mcp restart.** The advisor ag-25c350 was
  moved from 7e6b8b1 to d5c9a82 and got the "updated" line. Its final
  review of `22fed69..HEAD -- src/` found nothing to regret. bug-7f6ba7 is
  resolved as fixed.
- **Standing rule (user):** tickets are fixed here, not sent upstream.
- **The merged base is sound:** 1548 passed and 4 failed. The 4 fail the
  same way on 22fed69, because the orchestrator's environment leaks
  `MULTIAGENTS_*` into them (3 `test_doctor_clear_*` tests and
  `test_the_claude_script_uses_the_container_profile_only_where_it_should`).
- **Next, in the order I would take them:**
  1. **bug-2cebea:** finished runs marked `stuck` keep holding
     `max_concurrent` slots, and the `stuck` label never clears. It costs a
     manual discard several times a session. Also fold in the agy doom_loop
     false positive: `view_file` arguments carry line ranges that the
     comparison drops.
  2. **Test isolation:** make `test_core.py` independent of the ambient
     `MULTIAGENTS_*` / `CLAUDE_*` environment. That covers the 18
     CAN_SPAWN tests and the 4 above. It is cheap, and it makes every
     agent's "suite green" trustworthy.
  3. **Tooling defect 6:** implementer-deep has no `consult` tool, so it
     cannot reach dev-advisor (ag-829577, ag-d67496).
  4. **R8f leftovers:**
     - `claude.sh launch` should honour `CLAUDE_CONFIG_DIR`;
     - the usage-limit stop has the same exit race as R8f.12;
     - safe parsing for `limit_wait_seconds`,
       `restart_min_runtime_seconds`, `supervised_turns` and
       `spend_limit_pause_hours`.
  5. **bug-c050b0:** the burn-rate projection is built from a 39 s burst,
     which triggers a premature wrap-up and pause. Its proposed fix is in
     the ticket.
  6. **agy agents end their turn** with a background test still running,
     which has happened repeatedly.
  7. **A live `claude.sh compact`** against a real session.
  8. **Agents survive a restart of the orchestrator's CLI** (the user
     agreed to this, 2026-09-23 ~20:50). Today the Runner lives in the MCP
     server, a stdio child of the CLI. When the CLI stops cleanly, the
     running agents are cancelled (`runner.py` ~1326-1353). After a crash,
     the next `multiagents run` reaps them as `orphaned` (`cli.py`
     ~970-1013). No spend runs away, but the work in progress is lost.
     Cases where this costs us:
     - R8f compaction can only fire while the tree is idle;
     - a usage-limit restart of the orchestrator's own CLI
       (`driver.py` ~1010-1030);
     - a `multiagents run` restart to pick up new code or re-auth;
     - a stray `/exit`.
     The advisor's analysis is ag-25c350, turns 11-12. The smallest
     version:
     - agents write their stream to a file in their run dir, not to a pipe
       owned by the server;
     - on an intentional stop (compaction, limit wait, restart) the server
       leaves live agents running;
     - the next server adopts live nodes (replay the file, then follow it)
       instead of reaping them.
     Rejected, for now: a separate daemon (lifecycle, auth prompts), and
     an HTTP MCP server hosted by the driver (the driver itself dies on
     `/exit`). To settle in the contract:
     - a lock on `tree.json` (two servers, if the old CLI was suspended);
     - following a file across the docker bind mount;
     - whether a docker agent really loses its output when its
       `docker exec` dies (the advisor's claim, unverified);
     - exactly what a limit restart does to agents in each mode.
     Full pipeline, on `implementer-deep`. Adversary: yes, since it deals
     with processes and concurrency.
- **Researcher experiment:** the new brief is live (the project copy was
  overwritten). No researcher has run on it yet; compare the next run
  against ag-4548ac and ag-f2cb6d.

### Awaiting the user

- **(re-authenticated by the user ~10:40)** **agy could not authenticate in the container (2026-09-23 05:03).** The stored
  token expired at 00:10 UTC and the silent refresh fails with
  `Post "https://oauth2.googleapis.com/token": Unable to connect`
  (`~/.multiagents/container-state/shared/agy/.gemini/antigravity-cli/log/cli-20260923_030345.log`),
  although `googleapis.com` is in `egress_allowlist`. `auth_status` still says
  "authenticated". Fix: `multiagents auth login agy` (needs a person), then
  find out why the refresh cannot reach Google through the proxy — probably
  a multiagents defect worth a ticket (the bug-reporter itself runs on
  opencode/agy, so it could not be filed tonight). Consequence: advisor,
  reviewer, dev-advisor, researcher, adversary and bug-reporter were all
  unavailable; contract B was written without an advisor review, and runs on
  claude only.
- **`autocompact:` on the orchestrator** (P0-R8e.2): no shipped default;
  whether to set one locally is your call.
- Follow-up 4 above (`doom_loop_repeats`).

### R1 — the versioned mount (`F200`)

`src/multiagents/executor/docker.py:340-352` mounts both a provider's launcher
and the path its symlink resolves to. The second is resolved at container
creation, which pins one version number into a mount list that is fixed for the
container's life. The claude CLI updates itself; the next update bricks the
project until someone does the manual step above, killing every in-flight agent
with it.

**Mounting the versions directory is necessary and NOT sufficient.** The
advisor raised this and it checks out, though not for the reason it gave. It
said docker binds the launcher symlink and the container would keep a stale
symlink. Measured instead, inside the live container:

```
host:      ~/.local/bin/claude  ->  symlink, 52 bytes, -> versions/2.1.280
container: /home/.../.local/bin/claude  ->  regular file, 233,709,640 bytes
```

Docker **resolved** the symlink at mount time. The launcher path inside the
container is a single-file bind of one version's *inode*, wearing the
launcher's name. So mounting the versions directory makes new versions visible
and changes nothing about which one gets executed — the container would still
run the binary it was born with.

And the old versions are not cleaned up (`2.1.261`, `2.1.273`, `2.1.274`,
`2.1.278`, `2.1.280` are all on disk today), so nothing would fail. **The
container would silently keep running a superseded CLI**, which is worse than
the refusal we have now. The refusal is ours (`f963ba3` compares the declared
mounts against the config); the kernel would not have complained.

So the question the contract has to answer is not which path to mount, it is
**how the container resolves the binary at exec time instead of at creation
time**. Mounting `~/.local/bin` wholesale is one answer and a blunt one — it is
a directory of unrelated binaries. Resolving inside the container, against a
mounted versions directory, is another. Settle it there, with the constraint
below.

**Do not special-case claude by name.** Any provider whose launcher resolves
into a versioned directory has the same exposure, and naming one in the
executor is the hardcode this project exists to avoid.

### R2 — re-arm the watchdog (`F201`)

`src/multiagents/supervisor.py:199-203`. `_trip` latches for the whole run, so
the first alert is the only alert — including for a *different* condition
firing later. Measured: `ag-179bc2` tripped correctly at 5 identical
`view_file` calls and then ran to 116 events in silence.

Three requirements, and the third is the one that is easy to get wrong:

- a condition that differs from the one already reported is reported (a doom
  loop followed by a wall-clock timeout is two facts, not one);
- a *recurring* condition re-arms after a further N repeats, with N
  configurable and sitting beside `doom_loop_repeats`;
- **a monotone condition never re-arms.** `runaway_steps` fires on
  `self.steps > self.max_steps`, and `self.steps` only grows — so a naive
  re-arm makes it trip on *every subsequent event* for the rest of the run.
  Same for `timeout`. These are terminal states, not recurring ones: report
  once, never again. Only `doom_loop` is genuinely repeatable.

That third requirement came out of the advisor's review, which argued R2 and R4
should not land together because R2 would "weaponize" R4 — an inflated step
count plus a re-arming watchdog equals `runaway_steps` spam on every claude
agent. The premise is right and the conclusion does not follow: the spam comes
from re-arming a monotone condition, which is wrong on its own terms whatever
R4 does. Fix the re-arm rule and the interaction disappears, which is why R4
stays scheduled rather than parked.

**This will still make runs noisier.** That is the point. It is scheduled
before R3 because it is provider-agnostic, and because it is the only thing
that will tell us whether R3's compensation actually worked.

### R3 — a seam for provider-specific prompt guidance (`F202`)

`notes:` is parsed off every provider (`src/multiagents/providers.py:153` and
`:196`) and read by nothing. So a provider's configuration cannot influence what
its own agents are told, and `compose_prompt` has no provider-dependent branch
at all.

That matters because of a defect we do not control. agy's `view_file` returns,
on truncation:

> The above content does NOT show the entire file contents. If you need to view
> any lines of the file which were not shown to complete your task, call this
> tool again to view those lines.

It says "call this tool again" and names no pagination argument. A model that
follows it literally re-reads the same head forever — and this is **not a model
problem**: two of the seven loops measured on 2026-09-22 were
`agy/claude-opus-4-6-thinking`, and the same model under the claude CLI never
loops. It is the tool's message.

Add a key to `providers.yaml` that the runner appends to the prompt **for that
provider's agents only**. Requirements:

- absent key means the prompt is byte-for-byte what it is today;
- the fragment reaches agents on that provider and no others;
- **it is not `notes:`** — configuration commentary written for a human reader
  must not start being sent to models because the two shared a field;
- agy's tool name and its English error string appear in `providers.yaml` and
  **nowhere in `src/multiagents/*.py`**. That is the providers-are-plugins
  invariant, and it is the specific thing the user asked the review team to
  watch.

Then carry the pagination guidance in agy's block.

### R4 — the claude step counter (`bug-b1c130`)

**Same function as R2** (`Observer.observe`), so land them in an order that
keeps the diffs legible — but they are **not** coupled, once R2's third
requirement holds. The advisor argued for parking R4 until R2 shipped, on the
grounds that R2 would amplify it; that amplification is the monotone re-arm
bug, and R2 fixes it rather than causing it. R4 stands on its own evidence.

`supervisor.py:71-72` has two counting paths: `self.steps = max(self.steps,
event.step + 1)` when the provider reports a step index, and `self.steps += 1`
otherwise. agy maps one (`providers.yaml:174,179` →
`step_update.step_index`), so its count is monotonic per turn. The claude block
maps `assistant`, `user` and `system` to `as: step` with `fields: {}`, so
`event.step` is always `null` and every streaming delta increments. Verified in
`.multiagents/runs/ag-329af1/stream.jsonl`: every `step` event carries
`"step": null`, and they arrive in bursts sharing a millisecond.

Cost so far: `ag-329af1` killed at 121 steps after 16 tool calls, uncommitted
work dropped; `ag-6f5a9c` reached 295 step events across 37 tool calls.

**The open question the contract phase has to settle**, because it is not a
mapping: claude's stream-json carries no step index to map. Counting distinct
`message.id` values would give turn semantics, but `providers.yaml` has no way
today to express "count distinct values of a field" — only "lift this path".
So either the stream rules gain that, or the supervisor derives a turn boundary
from something claude does emit. **Whichever it is, the answer belongs in
`providers.yaml`, not in a `if provider == "claude"` in the supervisor.**

**There is a live workaround to unwind.** `.multiagents/config/project.yaml`
carries `max_steps: 600`, raised by hand to survive this; the shipped default is
250. That file is gitignored, so no agent can see it — the person merging this
has to revert it, and a run that still needs 600 afterwards means the fix did
not work.

### R5 — config staleness in the MCP server (`bug-2138e6`)

The MCP server calls `load_config` once at startup and holds it for the life of
the process; `multiagents run` reloads per command (`cli.py:72,204,474`). So an
operator who edits `project.yaml`, verifies it through the CLI, and then spawns
agents through MCP gets the old value with no warning.

This is not hypothetical and it is not cheap: on 2026-09-22 an operator raised
`max_steps` from 120 to 600 to work around R4 and lost two more runs to a
ceiling that no longer existed on disk.

The precedent for what to do is already in this codebase and is named in the
ticket: `executor/docker.py:961-975` refuses a container whose mounts no longer
match the config and says so, rather than running stale (that is also what
produced R1's diagnosis). Either reload, or refuse and say which key went
stale — but silence is the one option ruled out.

### R6 — `wait_for_agents` and the paused queue (`bug-b864b8`)

A pause recorded by an unrelated deferred task makes `wait_for_agents` return
immediately with `paused: true` and **no `still_running` field at all**, while
`check_agent` on a live agent in the same tree reports it running and making
progress at the same timestamp.

Two requirements:

- a pause on the deferred queue does not mean the tree is idle; live agents are
  still waited on;
- `still_running` is reported whether or not a pause is in force. Omitting a
  field is how a caller concludes there is nothing running.

Filed 2026-09-17 and set aside then for budget. It is scheduled now because R1
through R5 will have the orchestrator waiting on agents constantly.

### R7 — choose the team when `init-agent` starts

**The one feature in this phase**, and it is here rather than in a queue of its
own because it lives in the same command surface as R1–R6 and because the
mistake it prevents is expensive: `init-agent` today shapes a project under
whatever `team:` happens to be left in the config from last time. Getting that
wrong is not a typo — it is an initializer having a long conversation about the
wrong phase.

`cmd_init_agent` (`src/multiagents/cli.py:69`) loads the config, reports the
state, runs its checks and launches. It never mentions the team. Make it offer
the choice first, write the answer, then launch.

**What it shows.** `teams:` in `project.yaml` already carries a `description:`
per team (`src/multiagents/defaults/project.yaml:192+`), reachable as
`config.teams` (`src/multiagents/config.py:415`). Today that is `implement` and
`review`. The list is therefore self-documenting and **must be read from
config** — a hardcoded list of two names is a third place to update when a
third team is added.

**How it behaves.**

- A cursor moving over the list, the current `team:` highlighted on entry, so
  Enter on an unchanged project is a no-op.
- Cancelling (Escape, `q`, Ctrl-C) leaves the config untouched and does not
  launch. Choosing the team already set writes nothing.
- **Not a tty → no prompt.** Keep the configured team, print which one is in
  force, carry on. Every other prompt in this file is guarded that way
  (`cli.py:313`, `:367`, `:492`, `:509`) and for a stated reason: a closed stdin
  must never let `make init` change a project's setup with nobody deciding.
- `--team <name>` for scripts and for re-running without the prompt. An unknown
  name fails with the list of real ones rather than launching on a default.

**The trap, which has a precedent in this file.** Writing `team:` back must not
round-trip the YAML. `project.yaml` is mostly comments explaining the choices,
and dumping it through the parser deletes all of them — `_set_executor`
(`cli.py:442-460`) already solved exactly this with a targeted line edit and
says so in its docstring. Follow it: a `_set_team` beside it, same shape, same
reason. Note one difference — `executor.kind` is always present to substitute,
but a project that has never set `team:` inherits it from the defaults layer and
has no line to replace, so this one has to insert as well as substitute, and a
test should cover the empty case.

**Reconcile the initializer's brief.**
`src/multiagents/defaults/agents/team/_initializer.md:94-95` tells the
initializer that changing the team is "a one-line change to `team:` in
`project.yaml` — put it to the user rather than editing it yourself". That stays
true and stays right, but it now reads as if hand-editing were the only route.
Say instead that the user picks the team when they start `init-agent`, and that
what the initializer proposes is the team for the **next** phase.

### R8 — the orchestrator and its own context window

**Compaction can be triggered from outside the session. Verified on
2026-09-22**, end to end, against `claude 2.1.280`:

```
claude -p "/compact" --resume <session_id> --output-format json
```

returns `subtype: success`, `num_turns: 0`, an empty `result` and the **same**
`session_id` — the slash command is consumed by the CLI rather than sent to the
model, so it costs no turn of its own. The session's transcript then carries:

```json
{"type": "system", "subtype": "compact_boundary",
 "compactMetadata": {"trigger": "manual", "preTokens": 27729,
                     "postTokens": 1607, "durationMs": 23771,
                     "cumulativeDroppedTokens": 26122}}
```

`trigger: "manual"` is the same value the binary carries as
`compactionRequestKind: "manual"`. A real compaction, on demand, 27.7k → 1.6k in
24s, and the next `--resume` continues from the summary.

**So the orchestrator does not do this — multiagents does, between turns.** A
session cannot resume itself while it is running, and it does not have to:
`--unattended` already spawns a turn, waits for it to end, and starts another
(`driver.py:317-320`). The gap between two turns is where a compaction belongs —
no re-entrancy, and the same place that already knows the session id.

The second lever is `--autocompact <auto|tokens>` (documented in `claude
--help`, accepts `auto` or 100k–1M), which sets where *automatic* compaction
fires. That one is **one line in `providers.yaml`** and no Python at all: the
claude block already has an `optional:` map turning a config key into a flag
(`providers.yaml:66-67`, `max_budget_usd`). Adding `autocompact` there is the
plugin seam working exactly as intended.

The requirement is therefore both halves: **never be in a state where a
compaction loses something**, and **compact deliberately, at a boundary we
choose**, rather than being surprised by the automatic one.

**This is the quota problem again, and the answer is already written.** When a
provider window is about to close, `_wind_down` (`runner.py:318-340`) stops
sending new work and `WRAP_UP` (`runner.py:62-77`) tells the agent to commit,
write a handoff, and stop — because *"the work resumes from your branch and
this handoff, not from your memory of this conversation."* That paragraph is
exactly as true of a context boundary as of a quota one. `_wind_down`'s
docstring also carries the lesson that matters most here, learned from an
advisor: interrupt **early**, because a handoff written while the window is
still draining is cut off too.

Three parts, in order of value.

**R8a — a wind-down for context.** The sensor exists:
`transcripts.context_tokens(usage)` (`transcripts.py:123`) computes it, and
`walk_session` already reads the live transcript whose location the provider
declares (`providers.yaml`, claude's `transcript.dir`). When the orchestrator's
own context crosses a lead threshold — a `context_wind_down` beside
`wind_down_seconds`, same shape, same reason — it gets the `WRAP_UP` treatment
adapted to this pressure: finish the merge in hand, record the statuses, write
the handoff, start nothing new.

**State the limit rather than hiding it:** only claude declares a transcript
location. opencode keeps sessions in sqlite and agy in an opaque brain
directory, and `providers.yaml` says so already. So this senses a claude
orchestrator and nothing else. That is fine today — the orchestrator is pinned
to `claude/opus` — but it must degrade to "no reading" rather than to "plenty
of room", which is the same mistake `budget_status` warns about with
`known: false`.

**R8b — name what does not survive.** The brief should say which state is
durable and which is only in the conversation, because the orchestrator cannot
judge that in the moment. Durable, and therefore free: `BRIEF.md`, the ledger
(`list_findings`), tickets (`list_tickets`), the tree (`agent_tree`), branches
and commits. Not durable: which agents it is waiting on and why, the reasoning
behind a merge it has decided but not yet made, a finding it has judged but not
yet given a status, a question it meant to ask the user. The instruction is
**record it when you decide it, not when you are about to lose it** — a habit,
not a boundary ritual. A `set_finding_status` costs one call; reconstructing the
judgement after a compaction costs a re-read of the evidence.

**R8c — the right moment, in interactive mode.** The right moment to compact is
not a token count, it is **a work boundary that has just closed with its result
on disk**: a phase finished, a branch merged, a ticket resolved. A compaction
there loses nothing by construction, because everything the next turn needs is
in a file. Mid-task it is the opposite — the reasoning that has not been written
down yet is precisely what compaction drops first, and `transcripts.py:51-52`
already records that tool results go first.

So the driver compacts there, and only there: between turns, when the last turn
ended at a closed boundary and context is over the threshold. Interactively it
says what it did and what the figures were, the way `compactMetadata` already
reports them. It must **not** nag, and it must never compact mid-task — a turn
that ended with work still in the orchestrator's head and not on disk is the one
turn where this is destructive, because compaction drops tool results first
(`transcripts.py:51-52`).

Requirement, stated because it is the part that can silently regress: the
compaction is **verified**, not assumed, and a missing confirmation is a failure
rather than a success.

**The verification belongs in the provider script, not in the core** — the
advisor caught this and it is right. `compact_boundary`, `compactMetadata`,
`trigger: "manual"` and the transcript layout that carries them are all claude's
vocabulary; parsing them in `src/multiagents/` would put provider logic back in
the core one item after R3 took it out. So `providers/claude.sh compact <sid>`
performs the call, reads back its own record, and reports through its exit code.
The driver learns success or failure and never learns what a `compact_boundary`
is. `preTokens`/`postTokens` make the script's own check exact, and it may print
them for the log.

**R8d — the other two providers, which do not work like claude.** Measured on
2026-09-22 against the installed binaries. Three providers, three unrelated
mechanisms — which is the whole argument for putting this behind the provider
seam rather than in the runner.

| provider | route | state |
|---|---|---|
| `claude` | `claude -p "/compact" --resume <sid>` | **works**, verified above |
| `opencode` | `POST /session/{id}/summarize` on its HTTP server | route confirmed in the binary; the CLI route is broken |
| `agy` | none found | compaction is internal |

**opencode** is a client/server design, and the server exposes the operation:
the literal `"/session/{id}/summarize"` is in the binary, alongside
`session.compact`, `session.summarize` and `session.compacting`. `opencode
serve` starts that server and `opencode run --attach <url>` joins one, so the
call is reachable. The documented CLI route is **not** usable as it stands:
`opencode run --command compact --session <sid>` is recognised — it does not
report an unknown command — and returns

```json
{"type":"error","error":{"name":"UnknownError",
 "data":{"message":"Unexpected server error. Check server logs for details.",
         "ref":"err_0ab962ed"}}}
```

against a healthy 9,888-token session. Sending `/compact` as an ordinary message
just reaches the model, which answers it. So: use the HTTP route, and treat
`--command compact` as unavailable. That failure is a third-party defect, not
ours — no `TICKET`, which is the channel for multiagents' own bugs — but it is
worth reporting upstream and worth re-testing on each opencode release.

**agy has no external trigger, and the attempt is expensive.** `agy
--conversation=<id> -p "/compact"` does not compact: print mode *expands* slash
commands into the prompt (`agy --help`: "Disable slash command and skill
expansion in print mode"), so the text reached the model, which ran two turns,
tried to invoke a command tool, was auto-denied for lack of a permission rule,
and produced nothing. **It cost 42,752 tokens.** Do not retry it. agy's
compaction is configured through a protobuf message —
`genai.AntigravityAgentConfig.AntigravityCompactionConfig` and
`antigravity.localharness.CompactionConfig` are both in the binary — which is
internal, versioned with the CLI, and not something to depend on. For agy the
honest answer is that the context wind-down (R8a) and the durable-state
discipline (R8b) are the whole protection, and R8c does not apply.

**Where this goes.** Not in the runner. A provider script already answers
actions — `check`, `login`, `budget`, with `exit 64` meaning "I cannot, use the
fallback" (`providers/claude.sh:374-382`). Add a `compact` action on the same
contract: `providers/claude.sh compact <session_id>` runs the CLI invocation,
`providers/opencode.sh compact <session_id>` makes the HTTP call, and
`providers/agy.sh compact` exits 64. The driver asks the provider and does not
know which of the three it got. Any `if provider == ...` in Python here is the
hardcode the plugin invariant exists to forbid, and this is the case that would
tempt it most, because the three mechanisms genuinely have nothing in common.

**One concrete gap, raised by the advisor and confirmed:** a `compact` action
needs the session id, and no action takes one today — `run_action`
(`scripts.py:172-175`) passes only the action name. The seam for it already
exists though: the same signature ends in `extra_env`, which `build_env` merges
into the script's environment. So the session id reaches the script as an
environment variable, the way the action contract already carries everything
else, and no new parameter is needed.

**Under `--unattended` this matters most**, not least: nobody is there to notice
a window filling, and the automatic compaction will fire mid-task at whatever
moment it chooses. Deliberate compaction at a boundary is the whole difference
between the two.

### Not in this phase

`F203` (four opencode agents fall back onto agy, which is the looping CLI, and
opencode is at 99% of its monthly cap until 2026-10-05) and `F204` (the prompt
preamble ordering) are both **accepted, with reasons in the ledger**. R2 and R3
between them remove what was damaging about F203. F204 was measured rather than
judged: cache read beats creation 26.7:1, the addressable surface is under 3.5%
of claude-side tokens, and reordering recovers almost none of it.

`critic` has no fallback and stops when opencode's cap binds. Raised with the
user on 2026-09-22; it is not used on this project, so that is accepted and is
not work.

**None of these three is a cost optimisation and none should be sold as one.**
Measured on 2026-09-22: all seven looping runs were agy, which is unmetered, so
the loops cost nothing in money. R2 will *increase* orchestrator requests, not
reduce them. These are robustness fixes.

---

## Phase 1 — repair the tool, before using it — **DONE**

**Landed. Kept here because it records what was decided and why, and an agent
that cannot tell finished work from planned work will redo it.**

All seven tickets below read `fixed` in `list_tickets`, and two were spot-checked
in the code rather than trusted: `--init` is at
`src/multiagents/executor/docker.py:989`, and the `readonly_paths` corrections
are at `src/multiagents/defaults/agents.yaml:426` (`harness: []`) and `:501`
(`reporter: ["src/**", "tests/**"]`). The assertion at `tests/test_core.py:10592`
that used to encode `bug-08f9b3` is gone.

Phase 0 above is the current phase.

The review filed **7 blocking tickets** against multiagents itself. They are not
an upstream queue here — the user is the maintainer, and three of them actively
break the tool this team is running on. Fixing the allowlist while the container
is killing its own agents is work you will do twice.

Read each with `list_tickets`. **Every one carries a proposed fix and names the
test that should have caught it** — write that test, not merely a test.

| # | ticket | what it costs |
|---|---|---|
| 1 | `bug-cfdc71` | The workspace container has no PID 1 reaper. Fork-heavy work — running this suite, i.e. your normal job — leaves zombies until `pids_limit: 512` is exhausted. Measured: 509 processes, 505 zombies, then every agent dies with signal-shaped exits whose text blames Bun and innocent providers. Proposed fix: add `--init` to `run_args()`. **Do this one first.** |
| 2 | `bug-565863` | `budget_tag` enforcement and reporting sum raw usage keys instead of `token_count()`, which already exists and fixes exactly this elsewhere. Claude spend counts as zero. **This is why the review covered 2 of 7 contexts.** |
| 3 | `bug-08f9b3` | **Do this second, right after `--init`.** `harness` and `reporter` cannot revise their own output. Already fixed in the LIVE config during the review (`harness: []`, `reporter: ['src/**','tests/**']`) — so the fix is proven and the work is landing it in `src/multiagents/defaults/agents.yaml`, plus correcting `tests/test_core.py:10592`, which asserts `"readonly_paths" not in harness` and so encodes the bug. **Send that last part to `tester`**, which is already exempt and for which a wrong contract is its own remit. |
| 4 | `bug-ad011c` | Steering a fallback-routed run rebuilds the command with the *preferred* provider's model and effort flag, killing the run. |
| 5 | `bug-97a0c7` | The documented recovery for a truncated run (`steer_agent`) fails with `Cwd must be an absolute path`, turning a truncated run into a permanent loss. |
| 6 | `bug-8195f2` | `wait_for_agents` reports a live, being-resumed agent as terminally `cancelled: "stopped by parent"`, because `steer()` reuses the user-facing `stop()` path. |
| 7 | `bug-e1cb10` | `budget_status` omits claude's short rolling window, reporting `severity: normal` and "all providers have headroom" while the quota guard sees minutes to empty. |
| 8 | **`F100`** | `budget._cache` keyed only by provider name; tests bleed state. The one live red test. Scheduled here because it is what makes the suite trustworthy for everyone after it. |

**Close each ticket with `resolve_ticket` when its fix merges.** A ticket whose
fix landed and which still reads `awaiting_user` will be refiled by the next
review.

---

## Phase 2 — the three rewrites

Accepted by the user, 2026-09-17, as rewrites rather than fixes. They are the
expensive decisions and they were taken deliberately rather than sliding past
inside a list.

All three live in `write_proxy_config`. Ten of the review's findings come from
one line: `host.replace(".", r"\.")` as the entire escaping strategy.

**1. Escape every ERE metacharacter** — `F13`, and with it `F1`, `F12`, `F14`.

Today only the literal dot is escaped. POSIX ERE alternation has the lowest
precedence of any operator, so an entry `evil.com|.*` produces a pattern
matching any host — `F1`, and it is critical. Unbalanced `(` or `[` produce an
invalid line that crashes tinyproxy's evaluation on every request — `F13`.

One change to a complete escape resolves all four. **Verify each id explicitly
against its own reproduction; do not assume three went green because the fourth
did.**

**2. Make the proxy suite load-bearing** — `F50`, and with it `F51`–`F54`.

`F50` is the review's most consequential finding: deleting one line,
`FilterDefaultDeny Yes`, inverts the proxy from allow-list to open relay, and
**all 48 characterization tests still pass**. The suite validates pattern
generation and never validates the directives that decide what those patterns
mean.

Rewrite the function and its suite together. The tests must assert
`FilterDefaultDeny Yes`, `FilterType ere`, `FilterCaseSensitive Off`,
`FilterURLs Off` and the `FilterFile` path are each present and correct.
`F51`–`F54` are the same work: each is a directive mutation nothing catches.

**3. Reject bare generic suffixes** — `F10`.

An entry like `com` acts as a wildcard across unrelated hosts, because the regex
anchors on `(^|\.)` and accepts any prefix. Separate from item 1 on purpose:
escaping metacharacters does not make `com` safe — the defect is the anchor, not
the escaping. Either validate each entry as a plausible hostname, or require a
dot-separated structure.

---

## Phase 3 — the rest of the six the report led with

**1. The cache that defends its own defect** — `F150`, `F122`, `F154`.

`F150` first, and as the opening move rather than a surprise:
`test_cache_hit_overwrites_spent_instead_of_merging_and_mutates_the_cached_object`
asserts the broken behaviour under a name that reads like a deliberate
invariant. Whoever fixes `F122` without knowing this sees a red test and
reverts. Delete or invert it, then fix `F122`, then pin `F154` — the correct
fresh-read merge is itself untested, so a fix matched against it leaves the
thing it was matched against undefended.

`F50` and `F150` together are the review's real lesson: a characterization suite
can fail in both directions, by not noticing a defect and by protecting one.

**2. Unreachable multi-account quota** — `F120`, critical and cheap.

`read_claude(config_dir=...)` exists to read per-account credential files, but
the builtin branch calls `builtin()` with no arguments and silently discards the
caller's `config_dir`. The `builtin is read_claude` identity check that causes it
is the same wart the review-phase brief flagged as where the fallback table leaks
into the dispatcher.

**3. `build_env` hands out the ambient environment** — `F112`, with `F33`.

`scripts.build_env` copies `dict(os.environ)` into every provider script call,
so a project-local script — which wins by precedence — receives the calling
process's live credentials. `F33` is the same shape in `base.build_env`: the
`blocked` list guards only the passthrough loop, so `BASE_ENV_KEYS` are
forwarded even when explicitly blocked.

**Treat `F33` at `F73`'s severity (medium/security), not its own
(low/correctness).** The review recorded that disagreement deliberately rather
than resolving it; forwarding a variable the configuration blocked is the
security reading, and that is the one to act on.

**4. Presence is not validity** — `F130`, `F131`, `F132`, `F140`.

All three shipped provider scripts and the Python `_claude_token` treat "the
file exists and I could not read a clock from it" as "authenticated". The runner
finds out when the agent's first turn 401s. One pattern, four sites, one change.

**`F131` overrides its author's proposed `accept`**, and that answers the report's
open question. The author's reasoning is that agy's token format may carry no
expiry — a claim about the format, so establish what the token actually contains.
If it genuinely has no expiry, the honest result is `cannot verify`, not
`authenticated`.

---

## Phase 4 — amending a filed bug report

**Requested by the user on 2026-09-22.** Not a defect in what multiagents does;
a capability it lacks.

Today `submit_ticket` and `resolve_ticket` are the only things that can touch a
ticket once a `bug-reporter` has filed it, and neither changes its content.
When a filed ticket turns out to contain an error — a wrong figure, a claim
that does not survive checking — there is no way to correct it in place.

**Steering the `bug-reporter` does not do it.** Measured, twice, on
2026-09-22: a steer asking for a correction produced a **second ticket**
rather than an amended one. Five tickets existed for three defects until the
duplicates were declined by hand, and declining leaves the wrong version in
the record with a note pointing elsewhere. The two survivors were `bug-b1c130`
and `bug-2138e6`; the discarded ones `bug-2a0af0` and `bug-087fee`.

That matters because the orchestrator is the one reader who checks a ticket
before it is published, and finding an error is the expected outcome of
checking rather than an exception. A review step whose only remedy is "file it
again" is not a review step.

**What is wanted:** an orchestrator can revise a filed ticket's body, title,
severity or proposed fix, keeping its id and its filing time, so that the
version the user sends is the corrected one and the history shows it was
corrected. Whether that is a new tool, an argument to `submit_ticket`, or a
`bug-reporter` mode that targets an existing id is the contract's question,
not this note's.

**Where to look:** `submit_ticket`, `resolve_ticket` and `list_tickets` in
`src/multiagents/server.py`, and whatever holds the ticket store underneath
them.

---

## What is NOT scheduled

**54 findings remain `open`. That is honest: nobody has decided about them yet**
— not "rejected", and not "unimportant". Do not treat the ledger's silence as
permission to skip them, and do not pick them up opportunistically either.

`list_findings` is the index. `read_finding(F12)` gets one. **Never open a
findings file to browse**: each holds every finding for a whole context, and
reading it to answer a question about one loads all of them.

Two are already `accepted` with reasons recorded — `F71` and `F73`, both
duplicates (of `F24` and `F33`). The report's "Defects in the review itself"
section resolves `F74` (withdrawn, never existed as a distinct defect) and
`F134` (a skipped number, nothing retracted).

---

## What the review did not cover

Not a criticism of the review — a statement of what is unknown, so nobody reads
a clean ledger as a clean codebase.

| context | state |
|---|---|
| C1 sandbox & egress | full review — 38 findings |
| C2 provider seam | full review; adversary hit `budget.py` only, 70 of 188 tests. **The seam and auth surfaces have never been attacked.** |
| C3 agent lifecycle | harness merged and proven, 7 green proof tests. **No characterization, no audit, no adversary, no findings.** `runner.py` is 61% covered with 435 uncovered statements — the largest block of untested logic in the codebase. |
| C4–C7 | mapped and ranked, never started. C5 (MCP server) has the lowest coverage at 36%. |

Both gaps were caused by `bug-565863`, not by the work being too large. Fixing it
in phase 1 is what makes finishing the review affordable.

---

## Housekeeping

Eleven agent worktrees from the review are still on disk with unmerged branches
(`git worktree list`). One is named in the report: `agents/reporter/15f9e7`
carries a REPORT.md amendment reverted by `bug-08f9b3`. **Check it before
deleting anything** — the rest are spent, but that one holds work.

**Nothing prunes the per-run provider state, and it grows without bound.**
multiagents starts a fresh conversation for every agent run, and agy keeps each
one as a directory under `antigravity-cli/brain/`. Measured 2026-09-22:

- host: 262 MB over 142 conversations, one of them 174 MB on its own
- container: 60 MB over 90 conversations, inside
  `~/.multiagents/container-state/shared/agy/`, which is **our** directory
- `~/.multiagents/container-state/` in total: 227 MB

Three weeks, two projects. Nothing deletes any of it, and a run that loops —
which is what R2 and R3 are about — writes the most.

agy ships no pruning of its own; the `/cleanup` skill that looks like it does is
a user-installed helper for the `/resume` menu (see R8d) and is not reachable
from a headless run. So the container side is ours to handle: a retention rule
on `container-state`, applied by age or by count, with the run's own
`.multiagents/runs/<id>/` left alone — that is the evidence a ticket or a
finding cites, and it is small.

Low severity, stated so it is a decision and not a surprise when a disk fills.

---

## Where things are

- `context/review/REPORT.md` — the review. **The index; read it first.**
- `context/review/MAP.md` — the seven contexts, ranked with measurements.
- `context/review/ledger.yaml` — finding state. Prefer `list_findings`.
- `context/review/C4-runtime-observed.md` — F200–F204, found on 2026-09-22 by
  measuring this project's own runs rather than by reading it. Phase 0 works
  from these.
- `context/review/BRIEF-review-phase.md` — the review phase's brief. The two
  invariants in it still hold.
- `docs/open-questions.md` — what this project believes, with the evidence and
  how to check it. **Read the entries for your area before filing anything**;
  several were wrong the first time and the corrections are recorded in place.
- `README.md` — 153 KB of reference manual. Send `researcher` at it.

## This project does not specify before it builds

No `context/specs/`. Requirements are expressed as tests, as findings, and as
`docs/open-questions.md`. The interface contract for each phase is the
orchestrator's to write.

## Progress, 2026-09-23 ~20:05 (handoff)

Order unchanged (items 1–7 of the ~17:30 handoff). Tickets are fixed here,
not sent upstream.

- **Item 1, bug-2cebea:** contract `context/specs/stuck-lifecycle.md`
  (SL-R1–R7, commits bd6c903 and 1285b22). Tests `tests/test_stuck_lifecycle.py`
  are merged (42cc404): 31 tests, 22 red. Implementer **ag-698a01** is running
  (the first attempt, ag-3b4d7a, died at the claude quota with nothing done and
  was discarded). Next: adversary (the status machine), then reviewer, then
  merge, then `resolve_ticket(bug-2cebea, fixed)`.
- **Item 2, test isolation:** tester **ag-d88a82** holds 47602d9 (an autouse
  conftest fixture that clears MULTIAGENTS_*/CLAUDE_*). It was steered to
  verify both ways. Merge once the counts match.
  Note: the 72 failures in `test_phase2_entry_semantics.py` are the paused
  phase 2, red by design, not a regression.
- **Item 3, tooling defect 6:** root cause found (ag-4faa57). Claude subagents
  are spawned with `--strict-mcp-config` and no config, and opencode subagents
  get no server either, so no claude agent with can_spawn has `consult`.
  Contract `context/specs/subagent-mcp.md` (SM-R1–R5, 3554520). Next: tester,
  then **implementer-deep** (container: no `uv` inside; see the spec's facts),
  then a live check by the orchestrator.
- **Item 5, bug-c050b0:** contract `context/specs/burn-rate-baseline.md`
  (BR-R1–R4, 37ace75). Next: tester, then implementer-quick or implementer.
- Researcher experiment: ag-4faa57 is the first run on the new brief (353k
  uncached, 1.76M cache reads, 195 s, a correct answer). See memory.
- Budget at 20:00: the claude session window has reset, weekly is at 69 %.
  opencode is at its monthly cap until Oct 05. agy is available.
- ~20:30: tester ag-d88a82 ended its turn with two background suite runs still
  in flight. Item 6 therefore also happens on claude, not only on agy. It was
  resumed with steer_agent, told to run in the foreground and not to stop
  before it has the totals. Orchestrator brief rule added (8a6a834): resume an
  interrupted agent with steer_agent, never discard and restart from zero.
- Add to the bug-c050b0 work: the wrap-up message was sent **6 times** to
  ag-3b4d7a (runs/ag-3b4d7a/prompt.1-6.md). The config says once. Each resend
  interrupted its turn, so it never started writing.

## Progress, 2026-09-23 ~21:40 (handoff; claude 5h window at 95 %, resets 01:00 CEST)

- **Item 1, bug-2cebea:** implemented and merged (904e7f0; watchdog-test fix
  d06bd8d). All 31 SL tests pass. Still open:
  - adversary ag-c0f64f (agy) was steered to finish its findings
    (`tests/test_stuck_lifecycle_adversary.py`);
  - reviewer ag-98e037 rejected with 4 points. My triage:
    - (1) the retry takes over the original `done` Event after
      `await _launch`: plausible, cheap to fix;
    - (2) `wait_for_agents`'s `classify()` ignores liveness, contrary to
      SL-R4: real;
    - (3) `_maybe_clear_stuck` calls `tree.get` per event after an external
      status change: minor;
    - (4) `suppress(Exception)` predates this work: declined.
  - **Next:** route adversary findings plus reviewer points 1-3 to an
    implementer (claude, after 01:00), then `resolve_ticket(bug-2cebea,
    fixed)`. The running MCP server still has the old code (the stuck
    labels, and wait returning at once on stuck agents) until
    `multiagents run` is restarted.
- **Item 2, test isolation:** merged (4d80cdf). Verified by the
  orchestrator: `tests/test_core.py` 560 passed with 32 ambient variables
  set, using `.venv/bin/python`. The system python lacks `mcp`.
- **Item 3, defect 6:**
  - tests merged (2e87c4b, 49); decisions recorded (9882b22);
  - implementer-deep **ag-009901** got a legitimate wrap-up at 21:33 and
    stopped with everything committed (433600c): 49/49 SM tests pass, and
    the full suite is NOT yet run (its test_core run was killed, exit 137);
  - **resume it with steer_agent after 01:00**: full suite, then its
    result;
  - then reviewer and adversary (it touches executors and docker), then
    merge;
  - then the live SM-R1 check (steps in its handoff: `collect_agent
    ag-009901`).
- **Item 5, bug-c050b0:** BR-R5 added (c26169b); tests merged (e1d7139,
  39 cases, 33 red). The cause of the 6 resends is confirmed: the flag
  lives on the Run, and steer replaces the Run. **Next:** implementer.
- **Item 8** added (7d7b6a2): agents survive a restart of the
  orchestrator's CLI.
- Local config: `compact_at_tokens: 200000`,
  `context_wind_down_tokens: 300000` (user's request). R8f compaction
  becomes active only after `multiagents run` is restarted (the current
  driver dates from 10:45).
- **~21:50:** adversary ag-c0f64f on bug-2cebea, merged as red tests
  (990fc28, `tests/test_stuck_lifecycle_adversary.py`, 7 red). Findings, worst
  first:
  1. `consult()` loses the reply after a free retry: `_consult_turn` reads
     the dead Run's text;
  2. `wait_for_any` misses a second trip after a clear, because
     `baseline_stuck` is static;
  3. an opaque tool never clears `stuck`, since `last_digest` is unchanged;
  4. the trip reason leaks into `running`/`done`: `set_status` ignores an
     empty reason;
  5. a `stuck` node with `pid=None` holds a slot for ever;
  6. `wait_for_any` hangs on a dead stuck process (the same as reviewer
     point 2);
  7. the `_preflight` `max_children` check counts dead stuck children.
  **Next (after 01:00):** one implementer on these 7 plus reviewer points 1
  and 3, `verifies=ag-698a01`. Done means both SL test files are green.
- **The tree is idle at ~21:50.** A good moment for the user to restart
  `multiagents run`: the server picks up the stuck fix, and R8f compaction
  becomes active.

## Progress, 2026-09-24 ~02:30

- The user restarted `multiagents run` at 21:43; the server now has the stuck fix
  and R8f. User: pace claude on the 5 h window only, ignore the weekly one.
- **Item 3 (defect 6):** implementation merged as 2a39b13 (implementer-deep
  ag-009901). SM 49/49 pass, and the full suite shows only the 72 phase-2
  failures. Adversary ag-da2c22's red tests are merged as e2585a9
  (`tests/test_subagent_mcp_adversary.py`). Findings:
  1. unquoted pid_file/`$@` in `_start_inside` (shell injection);
  2. `DockerExecutor.inside()` trusts `MULTIAGENTS_CONTAINER` alone, so a
     host process with that variable spawns agents outside docker (no proxy,
     no cgroups);
  3. `_hand_server` writes `runs/<id>/mcp.json` through a pre-planted
     symlink into the user's config;
  4. `provider.env` can override `MULTIAGENTS_AGENT_ID`, and the server env
     does not pin it to node_id;
  5. a leftover agy `mcp_config.json` in `homes/<id>` reaches a later
     `can_spawn:false` run;
  6. `OPENCODE_CONFIG` passthrough gives a `can_spawn:false` agent a config;
  7. an unresolvable server command emits no `mcp_unavailable` on
     opencode/agy.

  Reviewer ag-9eacdb (rejected, 2):
  - (1) `stale_mounts` uses `server_mounts([])`, so an explicit mount of an
    install path causes an endless rebuild (`docker.py` ~741);
  - (2) OSError in `_hand_server` crashes the run, contrary to SM-R5;
  - (3) design: `_start_inside` is a hidden second executor;
  - (4) the server env is built ad hoc and has no PATH.

  **Next:** a fresh implementer-deep on all of these (ag-009901's worktree
  is gone once merged: point it at `.multiagents/runs/ag-009901/`). Done =
  `test_subagent_mcp.py` + `test_subagent_mcp_adversary.py` green. Then the
  SM-R1 live check.
- **Running:** ag-004177 (bug-2cebea fixes), ag-d5bc6d (bug-c050b0).
- **Item 6, the claude cause found (ag-d5bc6d's own narration):** Claude Code's
  Bash tool moves a command to the background after 2 minutes unless the call
  passes `timeout` (up to 600000). The agent then waits for a completion
  notice that never comes in `-p` mode, and ends its turn. **The fix is
  guidance** in the claude seam (P0 R3 provider-specific prompt guidance):
  long commands take `timeout: 600000` and are split to fit within 10 min;
  never end a turn with a background job pending. agy's equivalent is
  `manage_task` polling.
- **Tooling defect seen 2026-09-24 (not yet scheduled):** agy run ag-da2c22's
  result was "This request was blocked by Gemini's filters…", with 0 commits.
  It was classified `done` with an empty reason. A provider-declared pattern
  in `providers.yaml` (the plugin seam) should classify it as `failed` with
  reason `content_filter`. Then the orchestrator is not told "done" for work
  that never happened. The adversary's calling contract now covers the wording
  side (6d17362).

## Progress, 2026-09-24 ~05:00 — items 1-6 done; 3's live check and 7, 8 open

- **Item 1, bug-2cebea:** DONE, ticket fixed. Commits: 904e7f0, d06bd8d,
  d3dfa0a.
- **Item 2, test isolation:** DONE (4d80cdf).
- **Item 3, defect 6:** implemented (2a39b13). The 7 adversary findings and
  4 reviewer points are fixed (e06a43d): 56 tests green.
  - `inside()` now requires `/.dockerenv` plus `/proc/1/environ`, and the
    container is created with the variable set. **Pending:** the live SM-R1
    check. It needs the user's go-ahead to restart `multiagents run` and to
    run `multiagents docker rm && multiagents docker up` (the old container
    lacks the variable, so its inner spawns fail closed). Steps are in
    `.multiagents/runs/ag-009901/result.json`.
- **Item 4, R8f leftovers:** DONE (spec 1bf7123, 06962c3; tests 4c96b19;
  implementation 2a64289; test stubs 3054953).
  - R8f.19 uses the `stalled.stopping()` record, not exit codes: the
    heuristic was rejected, since SIGKILL escalation and a CLI that traps
    SIGTERM both break it.
- **Item 5, bug-c050b0:** DONE, ticket fixed (ea06791, ef68cf1).
- **Item 6:** agent guidance for claude (Bash `timeout`) and agy (no
  `manage_task` polling), f5b5a18. Watch whether it holds.
- **Full suite on the merged base** (before item 4): 1685 passed, 6 xfailed,
  0 failed, excluding phase2's by-design reds.
- **Open:** item 7 (live `claude.sh compact`; R8f is active in the driver
  since the 21:43 restart, but has not fired yet); item 8 (agents survive a
  CLI restart), still to be specified; the content-filter refusal reported
  as `done`.
- **Item 3 live check (2026-09-24 ~08:54, user approved, container
  recreated):** it FAILS. Run ag-c65ee1 got the multiagents tools through
  `--mcp-config` (the transport works), but `consult("dev-advisor")`
  returned a bare `Error executing tool consult` within 3 s and created no
  node. implementer-deep ag-9c3192 is finding the cause and fixing it, with
  a test, and must also make tool exceptions visible to the agent. Merge
  it, then rerun the PONG check.
- **Item 8:** contract `context/specs/agent-survival.md` (eed7db0), reviewed
  by the advisor (turns 13-14). tester ag-0e7618 is writing the red suite.
  Decision to confirm with the user: `/exit` detaches too; the wrapper's wall
  clock and `multiagents stop --all` bound it.
- **New defect, step counter (P0-R4 regression):** tester ag-0e7618 was
  stopped at "251 steps > 250" after about 35 tool calls. Its stream shows
  bursts of `step` events 3 ms apart during one long Bash call (68 in one
  call). Every providers.yaml copy maps claude `system`, `user`,
  `assistant` and `rate_limit_event` as `step`, so in-turn progress events
  are counted as steps. To fix next (implementer): count one step per model
  turn only, with a test built from a real long-Bash stream.
- **Item 3, layer by layer (09:00-10:00):**
  - 99be9d6: a child's server tried to seed read-only config. Tool errors
    now name their exception.
  - 5afd545: the docker executor needed the `docker` binary even inside the
    container.
  - ag-996ff1 (running): an inside spawn loses `HTTPS_PROXY`/`NO_PROXY`/
    `ANTHROPIC_BASE_URL`, so dev-advisor (agy) fails its DNS lookup.
  - Rerun the PONG check after each merge.
- **Item 8:** red suite merged (7a6004e, 45 tests, 37 red). The spec
  decisions from the tester's questions are committed. The implementation
  (implementer-deep) waits until ag-996ff1 releases `executor/docker.py`.
- **Step counter:** ag-8f28e2 is on it.
- **Step counter: FIXED, and the cause was config drift, not code.**
  - `.multiagents/config/providers.yaml` was a full copy of the shipped file
    from 2026-09-16. Lists replace wholesale when layers merge, so it
    shadowed P0-R4's turn rules (75bbd44) and every provider fix since.
  - I emptied it to `providers: {}`; the backup is
    `providers.yaml.stale-2026-09-16.bak`.
  - Merged 04b585d: a `tool_progress` rule, plus a test from a real long-Bash
    stream (3 turns = 3 steps, not 222).
  - The global copy was resynced from the shipped file.
  - **Systemic hazard, not scheduled:** any full copy of a shipped config
    file (global `providers.yaml`, project `agents/*.md`) silently freezes
    it. Worth a drift warning in `multiagents doctor` or at server start,
    naming the files that shadow a newer shipped version.
- **Item 6, new data point:** ag-996ff1 had the guidance in its prompt and
  passed `timeout: 600000`, but on the FULL suite (~14 min > 10), so the
  run still went to the background. Proposed fix: a repo script that runs
  the suite in chunks under 10 min, named in the guidance.
- **Item 3: DONE, verified live (2026-09-24 ~10:05).** ag-a3cfa5
  (implementer, claude, in docker) → `consult("dev-advisor")` → ag-4a29a4
  (agy) replied "PONG". Three layers were fixed on the way: 99be9d6,
  5afd545, 18d90ee.
- **Item 8:** implementer-deep ag-12951a is implementing SV-R1..R10 (SV-R11
  later, alone). After that: docker variants on the host
  (`SV_TEST_DOCKER=1`), adversary, reviewer, then SV-R11 (the tester first
  updates `test_p0_r8f_2_2_...[running]`).
- **Defect reported by the user (2026-09-24):** `multiagents run` was
  refused as "credit exhausted" about 3 minutes AFTER the claude credit had
  come back. Suspects: a cached quota reading or a cooldown that outlives
  the reset (`blind_cooldown_seconds` 900, the budget cache, or the
  paused/deferred state). Not yet investigated.
  User's proposal: a `multiagents refresh-quota` command (name open) that
  drops the cached quota readings and cooldowns, re-reads every provider,
  prints the result, and lets `run` proceed. It complements fixing the
  cause, it does not replace it: the refusal must also stop happening on
  its own once the window has reset.
- **Pause requested by the user (weekly usage 94 %):** finish what is in
  flight (ag-12951a item 8, ag-4dabbf chunk script), then start nothing new.
  The next steps for item 8 after the merge: docker variants on the host,
  adversary, reviewer, SV-R11.

## Handoff at the pause (2026-09-24, weekly usage 94 %)

Nothing is running. Done today:
- item 3, verified live;
- the step counter;
- the chunk script (c8be04d): `scripts/test-chunk.sh K 3` runs about
  3.5 min per chunk, and the three chunks cover the suite.

Resume in this order:
1. **Item 8:** `steer_agent("ag-12951a", ...)`. The session is intact, no
   code written yet, and its 10-step plan is in its result
   (`.multiagents/runs/ag-12951a/result.json`). It read my weekly-limit
   note as a stop, so the steer must say plainly to carry on: consult
   dev-advisor on the wrapper first, then implement. Then run the docker
   variants on the host (`SV_TEST_DOCKER=1`), then the adversary, the
   reviewer, and SV-R11 (the tester first).
2. **Item 6:** there is no project conventions file (no CLAUDE.md or
   AGENTS.md), so every task that asks for a full run must name
   `scripts/test-chunk.sh K 3` for K = 1..3. A project AGENTS.md saying it
   once is worth considering.
3. **The `run` refusal after a credit reset**, and the user's
   `refresh-quota` proposal.
4. **Item 7** (live compaction) is still unobserved. The content-filter
   refusal is still reported as `done`. The config-drift warning is not
   scheduled.

## Resumed (2026-09-26)

The container now runs on a second Claude account (weekly 0 %, resets
2026-10-02 12:00 UTC); agy's Gemini weekly pool is at 83 %.
- **Item 8:** ag-12951a could NOT be resumed ("No conversation found":
  the container's claude profile holds no transcripts; container-state was
  recreated 2026-09-24 22:16). Discarded; restarted cold as ag-a29f15
  (implementer-deep, SV-R1..R10, given ag-12951a's plan as a proposal).
  Next after it: docker variants on the host, adversary, reviewer, then
  tester for SV-R11. Bug-reporter ag-32b8ac is filing the lost-transcript
  defect.
- **Run refusal after a credit reset:** researcher ag-cdb2c0 (retry of ag-d34838, opencode server error) is tracing
  the refusal path and every stale state (read-only). Its answer feeds the
  contract for the fix and for `refresh-quota`.
- **Ticket bug-4a0446 (blocking, open):** the mount-drift refusal prescribes
  `docker rm`, which wipes claude transcripts (they live in the container
  layer, not in container-state). Also: `steer_agent` reports success before
  the resume fails, and a steer with a missing worktree cuts a `-2` branch
  off base instead of reattaching `node.branch`. The proposed fix is in the
  ticket. Schedule: right AFTER item 8 merges, because it touches
  `executor/docker.py` and `runner.py` (steer), both of which item 8 holds.
  Full pipeline. Until then: no `docker rm` while a session matters. The
  running ag-a29f15's own transcript is exposed the same way.
- **opencode** failed twice in 4 s with "Unexpected server error"
  (ag-d34838, ag-cdb2c0); the router now falls back to agy. The refusal
  trace is ag-92d428 on agy.
- **Run refusal after a reset:** traced by ag-92d428 (stale CLI cache 900 s,
  shared usage file 300 s, tree cooldowns; no command clears them).
  Contract `context/specs/quota-freshness.md` (QF-R1..R5, incl. the user's
  `refresh-quota`, QF-R6 pause lift). Advisor-reviewed (ef29219); tester ag-00375d
  writing red tests; then implementer.
  Runs in parallel with item 8: budget.py/driver.py are free, cli.py only
  gets a new subcommand.
- **Item 8 status (2026-09-26 ~16:00):** ag-a29f15 done, SV-R1..R10 on
  its branch (5 commits); survival file 37 pass / 3 skip (docker) / 5 fail
  (4 SV-R11 out of scope + the harness bug). SV-R4 decision recorded
  (af1a759). In flight: ag-a29f15 steered for the detached crash guard and
  lock-release enforcement; tester ag-653182 fixing the `.gitignore` harness
  bug. Then: merge both → adversary + reviewer on the merged range → user runs
  `SV_TEST_DOCKER=1` on the host → tester for SV-R11 → bug-4a0446.
- **Quota freshness:** red suite merged (aa5ad84, 58 tests, 45 red + 13
  guards); decisions on the tester's points in 3c4009f. Implementer
  (`implementer` tier) starts AFTER item 8 merges: it touches `tree.py` and
  `runner.py` cooldown call sites, which item 8 also changed.
- **Item 8 MERGED (1eb16f2, 2026-09-26).** Crash guard included; mid-run lock
  release verified by hand. Running on it now: adversary ag-852615, reviewer
  ag-f48509. QF implementer ag-bec80b started on top of it. Still open for
  item 8: the harness fix (ag-653182), the host docker variants
  (`SV_TEST_DOCKER=1`, needs the user), SV-R11 (tester first), then
  bug-4a0446. The running MCP server still runs pre-merge code until the
  next restart.
- **~17:30:** harness fix merged (78f5050). Reviewer ag-f48509: VERDICT
  rejected, 7 findings (worst: a docker stop fallback could `killpg` a
  container PID on the HOST; a dead `docker exec` client is read as a dead
  agent). They were sent to ag-a29f15 to verify and fix. Adversary retried on agy
  as ag-90d8ab (opencode failed 3× at startup: ag-d34838, ag-cdb2c0,
  ag-852615). QF implementer ag-bec80b was hit by a server-side rate limit and
  resumed; its branch has a HANDOFF-QF.md that must not be merged.
- **~18:30:** all 7 review findings confirmed and fixed by ag-a29f15, merged
  (ce1e11d). Tester ag-7acb79 is fixing the SV-R9 merge/discard test (no git
  identity in the harness; discard needs force). The adversary ag-90d8ab is
  still running (its doom_loop alert was a false positive: it was polling
  its own test task).
- **New gap to schedule (from ag-a29f15):** `gitops.commit_all` silently
  fails without a git identity. The agent's work stays staged, never reaches
  its branch, and nothing reports it. Small; `implementer` after item 8.
- **SV-R11** red tests already exist (4 `test_sv_r11_*`); the implementation
  comes after the adversary's findings are fixed.
- **~19:00:** SV-R9 test fixed (4870b3b): survival 38 pass / 4 SV-R11 red /
  3 docker skip. Adversary ag-90d8ab: VERDICT rejected, 4 findings with red
  tests (fdf012b): stop <id> leaves children, an unterminated last line breaks
  steer, `cancelled` overwritten by `failed`, and adoption retries a corrupt
  node forever. Sent to ag-a29f15. Held behind it (both touch runner.py):
  the commit_all gap, bug-4a0446, SV-R11.
- **~20:00:** the adversary's 4 findings were fixed and merged (0f9800b):
  survival + adversary files are 42 pass / 4 SV-R11 red / 3 docker skip.
  In flight: ag-a29f15 on SV-R11; tester ag-4fe3ea updating
  `test_p0_r8f_2_2[running]` for SV-R11; QF implementer ag-bec80b.
  Contract for bug-4a0446 written: `context/specs/session-persistence.md`
  (SP-R1..R5), under advisor review. Its tester can start in parallel. Its
  implementer starts after SV-R11 merges.
- **Container memory (2026-09-26 ~21:00):** the container is capped at
  4 GiB, and the kernel logged 3 OOM kills. A pytest run died with exit 137
  (ag-a29f15) when several agents ran the chunks at once. Until the limit is
  raised: one full-suite run at a time, and other agents run their own files
  only. Raising it needs a container recreate, so do it only AFTER
  session-persistence (SP-R1) lands, or transcripts are lost again, and
  ask the user.
- **SV-R11** is written (7a1b484 on ag-a29f15's branch) but untested; targeted
  runs are in flight. **QF:** 58/58 green on ag-bec80b's branch; its full chunks
  are running (the agent ended its turn with a background test running, which
  is known defect 9, now seen on claude too). SP tester: ag-9c02be.

## Handoff, 2026-09-26 ~17:50 UTC (claude 5 h window at 88 %, resets 21:59 UTC)

Nothing new was started after this point. Still running when written:
ag-a29f15 (SV-R11 targeted tests), ag-4fe3ea (R8f test update + unseen
test), ag-9c02be (SP red suite). They may be wrapped up by the quota: resume
each one with `steer_agent`, never discard it.

Resume in this order:
1. **QF (ag-bec80b)**: the implementation is complete, 58/58 green on its
   branch. It was interrupted before merging the base (0f9800b; expect
   conflicts in `runner.py`/`tree.py` around `cause=`) and before the full
   suite ran. Steer it: merge the base, delete `HANDOFF-QF.md`, run the 3
   chunks in the FOREGROUND one at a time (4 GiB container; re-run on 137),
   and report. Then merge it. QF does not touch untrusted input, so no
   adversary run; the reviewer is optional (about 475 lines; budget.py is the
   core).
2. **SV-R11 (ag-a29f15, 7a1b484)**: collect its targeted counts. Merge it
   together with ag-4fe3ea's test update (it updates all 3
   `test_p0_r8f_2_2_a_live_agent_blocks_it` variants and adds the
   unseen-result test). Then one full-suite run. After that, item 8 needs
   only the host docker variants: tell the user to run
   `SV_TEST_DOCKER=1 uv run --frozen pytest tests/test_agent_survival.py`
   while no agent is running.
3. **SP (bug-4a0446)**: merge the ag-9c02be red suite, then implementer-deep
   on SP-R1..R5 (docker.py, runner.steer, cli docker rm). Then the adversary.
   Once it lands, propose to the user: raise `memory: "4g"` →
   `"8g"` in project.yaml plus a container recreate (with nothing running).
4. The `commit_all` silent failure without a git identity (small,
   `implementer`).
5. The rest of the earlier queue: item 6 (AGENTS.md), item 7 (a live
   compaction), the content-filter refusal reported as `done`, and the
   config-drift warning.
- **After the reset (2026-09-26 evening):** SV-R11 is done (0c9876e on
  ag-a29f15's branch; targeted runs green apart from the 3 old R8f variants).
  It merges TOGETHER with ag-4fe3ea's test update. SP: premise corrected
  (21e564e). Since 2026-09-24 22:16, claude transcripts ARE host-backed via
  container_private_home. The SP tester ag-9c02be is reworking SP-R1 to
  assert the guarantee, not the mechanism. QF: ag-bec80b is merging the base
  and running the chunks.
- **QF MERGED (5e962d9).** The implementer's edits to 2 existing tests
  (a characterization test and `test_core` fixture dates voided by QF-R1)
  were reverted at the gate. Tester ag-ff3382 is vetting and re-applying
  them, then running the ONLY full suite. After it: `refresh-quota`
  exists; close the user's proposal. SP tests merged (8fa5507). SP
  implementer-deep ag-261e5d is running. SV-R11 merges with ag-4fe3ea. Known
  defect 9 (a turn ending with a background test running) now also hits
  claude/sonnet: ag-bec80b did it twice.
- **SV-R11 MERGED (a5568a9 + ade9966).** Item 8 is code-complete. Remaining:
  the host docker variants (the user runs them) and the MCP restart below.
- **Tooling defect (2026-09-26 ~22:25):** `wait_for_agents` crashes with
  `Node.__init__() got an unexpected keyword argument 'adopted_at'`. The root
  MCP server still runs the pre-merge code (started 11:56). A process on the
  new code (probably a nested server started from a worktree) wrote the new
  SV fields into the shared `tree.json`. `check_agent` and `merge_agent` still
  work. Fix: restart the MCP server once NO agent is running (the old code
  cancels agents on exit). Lasting fix: `Tree` must ignore unknown node
  fields (forward compatibility). Add that to the small-fixes list.
- **~23:00:** merged the QF test fixes (e769a14) and SP-R1..R5 (b6610ca,
  bug-4a0446). The full suite on 5e962d9 had no unexpected reds apart from
  **one QF regression**: `test_p0_r5_4_a_spawn_under_a_broken_config_…`. A
  malformed project.yaml crashes `start_agent` through `budget._reset_margin`
  (it parses without a guard). Next, after the MCP restart:
  1. implementer-quick: guard `_reset_margin` (P0-R5.4: old limits + a report);
  2. tester: the sp_harness `wait_until` re-export
     (`test_sp_r2_docker_node_session_at_the_host_backed_path_is_resumed`);
  3. adversary on SP (steer pre-check, worktree reattach, docker rm);
  4. `Tree` ignores unknown node fields (the forward-compat defect above);
  5. resolve ticket bug-4a0446 as fixed (b6610ca) once 2-3 are done;
  6. ask the user: host docker variants (SV_TEST_DOCKER=1 and
     SP_TEST_DOCKER=1), and 4g → 8g with a container recreate;
  7. `commit_all` without a git identity.
- **Waiting on the user:** `/mcp` reconnect of multiagents. Nothing is running;
  the server is still on the 11:56 code.

### 2026-09-26, after MCP reconnect
- Host docker variants (SV+SP): 71/72 passed; only failure is the known sp_harness `wait_until` gap.
- Container memory raised 4g -> 8g (user-approved), container recreated.
- In flight: ag-a51827 implementer-quick (`_reset_margin` guard, P0-R5.4); ag-b760dc tester
  (sp_harness `wait_until` + red test tests/test_tree_forward_compat.py); ag-18167b adversary on SP (b6610ca).
- Next: after tester merges, implementer-quick on Tree ignoring unknown fields; findings from the adversary go
  back to implementer-deep; then resolve bug-4a0446 as fixed (b6610ca).
- ~later: merged 3815982 (sp_harness wait_until + red tree forward-compat tests), 9896790 (Tree ignores
  unknown fields, incl. runner.py), 8d3af4c (P0-R5.4 regression: limits threaded to `_reset_margin`),
  fa07d3c (SP adversary tests, 8 findings, all accepted; decisions a83ee30).
- In flight: ag-7697c2 implementer-deep fixing the 8 SP findings; ag-862edc tester on the new
  context/specs/commit-identity.md (CI-R1/R2); ag-8d967d bug-reporter (steer false-negative after
  runaway_steps, doom_loop false positives on agy task polling, wait_for_agents omitting a startup failure).
- After ag-7697c2 merges: full suite once (chunks), then resolve bug-4a0446 fixed (b6610ca + fix commit).
- ~23:17 local: claude 5 h window at 93 % (resets Sep 27 03:00 UTC). Tree paused; a tester (amend
  `test_sp_r1_path_traversal_…` per the "After ag-7697c2" decision) is deferred and restarts by itself.
  Running: ag-7697c2 (steered: vocabulary invariant + per-file checks; branch has 2c26e36, 17/18 adversary
  tests green), ag-f233a2 (CI-R1/R2), ag-1d9832 (bug-d6310f + bug-8615db).
  Tickets open: bug-4a0446 (resolve fixed after 7697c2 merges), bug-1b2612 (steer false negative +
  cumulative runaway_steps; touches supervisor.py and runner.steer, so start after 7697c2 merges),
  bug-8615db, bug-d6310f (ag-1d9832). Reporting is off, and `gh` is missing, so tickets are fixed here.
- ag-7697c2 done (2c26e36, 4a786fd, 2ed752a): 8 SP findings fixed, per-file checks green except the traversal
  test (tester amendment deferred) and test_adversary_tree_busy_mid_grace_… (pre-existing at a83ee30). Reviewer
  ag-39521a (agy) on it before merge.
- ag-f233a2 (CI) and ag-1d9832 (bug-d6310f/8615db) were cut by the claude quota with NO code written: steer
  them after 03:00 UTC ("window reset, carry on"; 1d9832 left HANDOFF.md on its branch — tell it not to merge
  that file). They finished before the wait began and wait_for_agents never reported them: bug-d6310f live.
- Reviewer ag-39521a rejected 7697c2 (10 findings). Accepted #1-8, #10; DECLINED #9 (runtime symlinks inside
  the container image: provider declarations are trusted config; the lexical guard targets mistakes). Steered
  ag-7697c2 to fix them. Merge after that + a re-check; then resolve bug-4a0446 fixed.
- 2026-09-27: merged 655c2ec (traversal test amended + `..`-after-slug case) and 954c207 (CI-R1/R2, commit
  identity). Running: ag-7697c2 (reviewer fixes), ag-1d9832 (bug-d6310f/8615db), researcher ag-a339c3: do tests
  run from a worktree import the MAIN checkout's src via the editable install? (ag-f233a2 says yes for bare
  pytest). If so, every "green" an agent reported may have tested base code: fix the pytest config and re-verify.
- opencode startup failures: 6 so far (latest ag-a51827, ag-6f2868, ag-2c8dfc). Router keeps picking it; use
  `model:` overrides to agy/claude for opencode-default roles until fixed.
- Merged b2ef5f4 (SP adversary + review fixes; advisor checked the docker HOME change: no host-file exposure),
  946ca10 (pytest `pythonpath = ["src"]`: bare pytest in a worktree tested MAIN's src), 68f1c62 (bug-d6310f,
  bug-8615db). Tickets fixed: bug-4a0446, bug-d6310f, bug-8615db (reporting off, gh missing).
- In flight: ag-b12784 (bug-1b2612: resumed-run step counting + steer false negative). Full suite running on
  the HOST in 3 chunks (logs in the session scratchpad).
- Merged 98b95be (bug-1b2612, ticket fixed). Host full suite + bisect (researcher ag-bb371e): host-only failures
  from missing git identity (gitops.merge commit → CI-R3 added to commit-identity spec) and a test fixture that
  strips /usr/bin on hosts; regressions: `agy` named in supervisor/providers comments (68f1c62, 98b95be);
  pre-existing: r8f adversary test (driver `_AttachedCompaction._cancel`).
  In flight: ag-d0c34f (CI-R3 + vocabulary), ag-a409db tester (host fixtures), ag-a7494f deep (r8f driver).
  After merge: rerun the host full suite in chunks; expect only phase2's 72 by-design reds.
- Merged 081409e (host fixtures: docker-less PATH, commit-tree identity), 8ef266e (r8f adversary test made busy
  the SV-R11 way — driver was right, ag-a7494f), 47e10e0 (CI-R3 fallback identity on every gitops commit +
  vocabulary rewording). Host full suite rerunning (r2chunk*.log in the scratchpad).
- 2026-09-27 host full suite at a7bb2f5: chunk 1 553 passed, chunk 2 960 passed, chunk 3 72 failed / 473 passed —
  the 72 are exactly phase2's by-design reds; nothing else fails. This phase's work is done.
  Left for the next phase (not started): AGENTS.md naming scripts/test-chunk.sh (item 6), live compaction (item 7),
  content-filter refusal reported as done, config-drift warning, opencode startup failures ("Unexpected server
  error", router still prefers opencode), idle nodes ag-4a29a4 / ag-cb1c70 to clean up.
- 2026-09-27 catch-up advisor review of commit-identity → CI-R4 (bounded failure text), CI-R5 (hook failure fed
  back to the agent, bounded), CI-R6 (agent commits unsigned). User decided hooks kept, signing skipped on agent
  commits. Tests merged: r4 (459e5ec), r6 (97c9708). CI-R6 merged ed3696c (reviewed by me, small diff; no adversary).
  In flight: tester ag-c057bd (CI-R5 tests); CI-R4 implementer-quick deferred (agy exhausted, restarts itself).
  Next: merge CI-R4, then implementer-deep on CI-R5 (end-of-run path, use _finalize's relaunch pattern per advisor),
  then adversary + reviewer on CI-R5.
- 2026-09-27 ~07:30 UTC: CI-R5 tests merged (df5ed22). Decisions on CI-R5 silences + CI-R2 `git add` gap in spec
  (eeb376f). All providers down: claude session 93% (resets 08:00 UTC), agy gemini weekly 98% (resets Sep 30),
  opencode failing at startup. Tester ag-7ecd49 (CI-R2 add gap + r5 amendments) fell back to opencode, crashed →
  stopped; relaunch its task on claude after 08:00. CI-R4 implementer-quick still deferred (gemini override).
  Then: implementer-deep on CI-R5 (+ CI-R2 add gap), adversary, reviewer.
- USER RULE 2026-09-27: do not use agy (low credit) — no advisor/reviewer/researcher/implementer-quick on agy, no
  gemini overrides; route to claude. Log every decision the advisor would have seen in
  context/advisor-catchup.md, to put to it in one consult later. The deferred CI-R4 task (gemini override) must be
  stopped if it starts; relaunch CI-R4 on claude.

## Feature ideas from the user (not scheduled; for the next phase via `multiagents init-agent`)

- **Limit-hit notices (user, 2026-09-27).** Whenever a limit set in a config file is reached and actually
  constrains execution, write a log entry that says so. The user must also be told, with the config file's
  path and the line of the limiting setting, so they can change it easily if they want. Examples:
  `limits.max_concurrent`, `max_depth`, `max_children`, a watchdog timeout/step cap, `commit_fix_attempts`, a
  budget tag ceiling, container `memory`.
  Open questions for the contract: where the notice surfaces (event, tool result, `multiagents run` terminal,
  tree); de-duplication so a limit hit every second does not flood; and the defaults case, i.e. which file and
  line to show when the value comes from a built-in default rather than the user's file.
- 2026-09-27: CI-R4 merged 5558946. Tests for CI-R2 add gap + CI-R5 decisions merged 721642e. In flight:
  implementer-deep ag-b7c2ff (claude opus) on CI-R5 + CI-R2 add gap. Next: adversary (claude, not agy) and reviewer
  (agy-only? route to claude or skip and log in advisor-catchup), then merge. Orchestrator brief gained an
  "Autonomy" section (f041f27): never end a paused turn without an armed wake-up.
- 2026-09-27: USER APPROVED sandbox-git (context/specs/sandbox-git.md) as the next item after CI-R5. In flight:
  ag-c2af1d implementer-deep (CI-R7 FIFO hang + fix-turn NEED_DECISION), ag-f8b8f7 implementer on sonnet as
  read-only researcher (facts for the sandbox-git contract). Next: write the SG contract, tester, then implement
  after CI-R5 merges (both touch commit_all). Adversary/reviewer/researcher have no claude model; opencode is
  broken (~10 days), agy is user-excluded → tester/implementer on claude stand in.
- 2026-09-27 SG progress: contract + decisions in context/specs/sandbox-git.md. Red tests merged: merge (SG-R5,
  8983445), mounts (SG-R2 static/R6, ff88125), reads (SG-R4, 1500b45 — uses `repo=`; must be renamed to `root=` per
  decision 5b9e633). Deferred (claude exhausted): tester for SG-R5 refusal-cleanup + docker-live file.
  Next when claude is back: tester to (1) rename repo=→root= in test_sandbox_git_reads.py, add root-path, missing
  gitdir, and new SG-R2 protected paths (config.worktree, modules, refs/heads|tags except agents/, base loose ref)
  to test_sandbox_git_mounts.py; then implementer-deep for SG (after tests), host-run of docker-live tests,
  adversarial tester, then container recreate (only when idle).
- 2026-09-27 ~13:30 UTC: all SG red tests merged (reads b4a8890, mounts b4a8890, merge+docker-live 9c8ff0a).
  Host baseline of docker-live: 6 failed / 1 passed — the escape is real (container moved `main` via update-ref).
  In flight: ag-ab5211 (SG-R2/R6 docker mounts), ag-9e10d8 (SG-R4/R5 gitops). Next: SG-R3 (commit_all/restore_paths
  inside the executor) after 9e10d8 merges; then docker-live on host, adversarial tester, container recreate when idle.
  Note: user asked to exclude untracked codex-plugin/ locally (.git/info/exclude) — it is theirs.
- 2026-09-27 ~13:55 UTC: SG-R3 merged (4b9dbda; docker-live 7/7 on host). Follow-up tests merged 88f4b0d
  (bounds, objects/info, protect_project symlink/FIFO). Tester ag-281f2b finishing runner_reads + env_files (SG-R7).
  Claude at 96% until 18:00 UTC → next implementer (for 88f4b0d tests, then runner_reads/env) after the reset.
  An implementer-deep started with model=opus was silently routed to opencode/minimax (claude constrained) and
  crashed: an explicit model override did not pin the provider — possible ticket.
- 2026-09-27 ~14:00 UTC: all SG follow-up red tests merged (5a4de9c: runner_reads, env_files; 88f4b0d: bounds,
  objects_info, protect_project). Nothing running. Wake-up armed for 18:01 UTC (claude reset). Then: implementer-deep
  on 88f4b0d files (gitops._pinned + docker.py protect_project/mounts), then implementer-deep on runner_reads +
  env_files (runner.py + docker.py start/ContainerGit) — sequential, both touch docker.py. Then host docker-live,
  host full suite, container recreate when idle (inspect .git/hooks and .git/config for agent-written content first).
- 2026-09-27 ~20:00 UTC: sandbox-git COMPLETE and merged (SG-R1..R7, through 5e56bc8). Host full suite at 7a32ff0:
  665 + 1053 passed, chunk 3 only the 72 phase2 by-design reds. docker-live 7/7 + host_writes_more on host.
  Pre-recreate inspection: .git/hooks empty, .git/config clean, no alternates, main == origin/main.
  Project container recreated with the SG layout (root ro, verified by a write probe).
  BLOCKER: the running multiagents MCP server still has pre-SG code and refuses the new container as drift
  (ag-0f07a1). Needs the user to reconnect the MCP server (/mcp) so it loads the new code; then smoke-test.
  Still open: adversary/reviewer passes on sandbox-git by a proper adversary (providers down/excluded; testers
  stood in), advisor catch-up consult when agy is back (context/advisor-catchup.md), leftover test containers
  multiagents-proj-* (from harness runs, not ours to delete without asking).
- 2026-09-27 ~19:55 UTC: LIVE DEFECT found by smoke test after /mcp reconnect: single-file ro mounts vanish when the
  host renames the file, so .git/config and .git/index were writable from the container (a smoke agent appended a
  line to .git/config; removed; backup in session scratchpad). SG-R2 revised: .git ro + writable dirs only; branch
  deletion from container routed to host (branch_pending_delete). Tests updated 4ef3985. Next: tester for
  test_sandbox_git_branch_delete.py (was started, fell back to opencode, crashed), then implementer-deep for revised
  SG-R2 + branch_pending_delete, then host live tests, recreate container, smoke again.
  DO NOT run agents for other work until the container is fixed. Claude at 99% until 23:00 UTC; wake-up armed 23:01.
- 2026-09-27 ~23:50 UTC: revised SG-R2 merged (acc54d2) + host-routed branch deletion (36c37b5). Live tests 8/8 on
  host incl. host-rewrite-by-rename check. Container recreated with .git ro (mountinfo verified). The MCP server
  still runs pre-acc54d2 code and refuses the container as drift (ag-609011) → needs another /mcp reconnect by the
  user, then smoke test (SMOKE.txt commit + write probes on root, .git/config, .git/).
- 2026-09-28: after /mcp reconnect, smoke test ag-849cf5 PASSED: commit in the container works; writes to the root,
  .git/config and .git/ all refused ("Read-only file system"). Discard deleted the branch on the host. A harmless
  stderr line on commit: git tried packed-refs.lock (auto-maintenance), commit still succeeded. sandbox-git is closed.

### tmux to watch the tasks (the user, 2026-09-28)

- **The idea.** Launch subagents and their commands inside tmux, so that
  their progress can be watched on demand. Nothing is attached by default.
  The monitor would have one button to attach to a task, and one to copy
  the attach command.
- **The orchestrator's notes, for whoever designs it:**
  - **What a pane would show.** Agents run headless (`-p`/`exec --json`),
    so a raw pane shows NDJSON. It is only worth watching through a pretty
    viewer that follows `runs/<id>/stream.jsonl`: text, tool calls, tests
    running.
  - **Read-only by default** (`tmux attach -r`). Keystrokes typed into a
    pane could reach the agent's stdin; codex reads its prompt from stdin.
  - **Synergy with "agents survive a restart of the orchestrator's CLI"**
    (Handoff 2026-09-23, item 8). A tmux server outlives the CLI, and so
    would be a natural process supervisor. It is the same "stream to a
    file, adopt on restart" design.
  - **Suggested in two steps:**
    1. Viewer windows only. The runner keeps owning the process, and tmux
       only runs the viewer. Low risk.
    2. Only after that, tmux as the supervisor, which touches `runner.py`,
       adoption and the docker `exec` path.
  - **To decide:**
    - one tmux session per project, or one per tree;
    - where the tmux socket lives on the host, with permissions 0700;
    - whether the command tools (tests, builds) launched by an agent get
      their own window, which is harder: they are the agent's child
      processes inside the container;
    - which providers have to be excluded;
    - the fallback when tmux is not installed.
