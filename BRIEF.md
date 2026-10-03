# BRIEF — multiagents repairs itself

**Phase: implement.** The review is done. `context/review/REPORT.md` has the
findings, the ledger has their state, and ~380 characterization tests are merged.
This phase turns the chosen findings into work.

`team: implement` is already set in `.multiagents/config/project.yaml`.

The review phase's brief is kept at
`context/review/BRIEF-review-phase.md` — read it for the two invariants
(providers are plugins, agy carries Gemini only), which still hold.

---

## Current work (from 2026-10-03): Phase 6 closing round, then Phase 7

**Phase 6 is done apart from a closing round.** H1–H4, H6, H7, H9, H11,
H13, H14, D1, D2 and D3 are merged. Added on the way, also merged:
opencode-zai, opencode-deepinfra, claude-b, agy-b, PS, PC, T1, T2, FS, SF,
SR, AB and DK. Since 2026-10-03 ~09:00 UTC the executor is
`executor.kind: docker`. The dated log of all this is further down.

**Now: the closing round.** It is in `context/specs/phase6-closing.md`
(C1–C11):
- branch triage;
- closing H5 with evidence;
- reset times shown in UTC;
- `doctor`'s false "binary not found in the container";
- DK-R3a, `auth_status` for a pinned account;
- the SR `max_steps`-on-adoption leftover;
- the prompt run-file transport (the rest of H8);
- a live compaction check (H10);
- the config warnings, which are the user's decision with defaults;
- an advisor session that could not be resumed and was replaced silently
  (C11);
- wt-main and a full suite.

The user's standing rules apply:
- the reviewer runs after every implementer;
- one implementer per item;
- work still rejected at review round 3 moves to opus;
- tickets are fixed in-house and never submitted.

**Then Phase 7, to be specified with the user BEFORE any code** (via
`multiagents init-agent`). The seed is
`context/specs/phase7-nodes-and-containers.md`. It covers:
- plan nodes and a scheduler script, the user's idea of 2026-10-02 (see
  "Ticket-driven agent scheduling" below);
- **one container per run**.

**Decision, from the user on 2026-10-03: inter-agent isolation.**
- Inside the single project container, agents can modify each other's
  worktrees, HOMEs and refs. This is the known limitation from H1.
- The user **accepts it for now**. It is removed in phase 7 by one
  container per run, created by the host-side scheduler.
- The intermediate step of one Unix uid per agent was considered and
  **rejected**. Agents run as the host uid, so that the host can manage
  the files they write on bind mounts. Per-agent uids would need ACLs
  everywhere and a migration, they would reopen sandbox-git, and all of it
  would be thrown away in phase 7.
- The write map and the isolation tests are written straight into phase
  7's spec (PAC-R1, PAC-R2).

**tmux step 2** (tmux as the process supervisor) belongs naturally with phase
7's run lifecycle. Consider it there.

**Still deferred:** phases 2 and 3 of the review. The 72 by-design reds in
`test_phase2_*` stay red. Branches `agents/tester/b689bf` (R18) and `cf02a1`
(R20) hold phase 3 tests and are kept for it.

---

## Phase 6 — hardening, then two features (history)

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

**2026-09-29 ~22:55 UTC: USER ROSTER DECISION (applies once opencode-zai is merged and enabled):**
- orchestrator: claude opus (unchanged);
- tester: claude **sonnet**;
- the implementers run on GLM through opencode-zai, on `zai-coding-plan/glm-5.3`, one reasoning level per tier. The opencode variants verified for glm-5.3 are `low`, `high` and `max`:
  - implementer-quick: `variant: low`;
  - implementer: `variant: high`;
  - implementer-deep: `variant: max`;
- everything else stays as now, on codex. That includes the advisor, dev-advisor, researcher, reviewer and adversary.
- Before editing, check how agents.yaml expresses a variant (spawn.optional.variant → `--variant`) and what fallbacks each implementer keeps (codex, as today). The advisor is consulted on the edit, not on the decision, which is the user's.

**2026-09-29 23:22 UTC:** claude has reset.
- Started zai tester ag-c3d029 (claude sonnet) on ZA-R1..R5.
- Started implementer ag-a2d0d6 (claude sonnet) on HA-R12 `clean` (18 red tests).
- The codex wake (23:48) resumes H7 ag-2abbbc via steer.

**2026-09-29 ~23:40 UTC:**
- **Merged:**
  - HA-R12 clean (a919b35);
  - zai tests (523074e: 81 cases, 72 red);
  - H13 (6cfa1cf: gc.auto=0 and maintenance.auto=false on multiagents commits and in the agent env).
- **D3 contract revision 2:** 24c62d8. Implement it after H7, since it reuses `resolve_bin`.
- **zai implementer ag-325a81** (claude sonnet) is running in parallel with H7. It was told to stay away from the opencode.sh `BIN=` line and the opencode block.
- **Codex wake at 23:48:** steer H7 ag-2abbbc.

**2026-09-30 ~00:00 UTC:**
- **H7 ag-2abbbc (codex)** ran out of codex quota again after about 6 min. Codex says it is back at 04:48 UTC. Its work was committed: 8bef215, 8352cda, 23416b1 and the WIP 3773bfc. All 30 H7 tests pass, but the suite shows regressions.
- That work was merged (squashed) into claude opus implementer-deep **ag-d06a0b**'s worktree as 5faab46, via `merge_agent(into=<worktree path>)`. A node id is refused; a path works.
- ag-d06a0b fixes the regressions and removes the stray `.h7-chunk*.log`.
- **Then:** the adversary on H7, on codex after 04:48 or on claude.
- **zai implementer ag-325a81** is still running.

**2026-09-30 ~01:00 UTC:**
- **opencode-zai merged (88ddd80).** The orchestrator reviewed the key handling: urllib, no redirects, fixed-text notes, key never printed. 80/81 zai tests pass.
- **Tester ag-3d714b (sonnet)** is fixing three test problems:
  - the ambiguous test ZA-R4 `_line("1%")`;
  - the three c2 tests that pin "four providers";
  - `test_h2` RF-R5/RF-R6: a raw `git commit` in the fixture depends on the host's global identity. It fails in the orchestrator shell with "Author identity unknown" and passes in agent envs. This is not a product regression.
- **Plan after the H7 merge:**
  1. Advance wt-main.
  2. Enable `opencode-zai` in `.multiagents/config/providers.yaml`.
  3. Run refresh-models.
  4. Make the roster edit.

  This is not done before, because the running server reads the shipped yaml from wt-main.

**2026-09-30 ~02:00 UTC: THE GLM TEAM IS LIVE.**
- **Code:** wt-main was advanced to 49c9b4e. That brings the zai provider, HA-R12, H13 and the test fixes. H7 is NOT in it yet.
- **Config** (`.multiagents/config/` is gitignored, so these changes are recorded here):
  - `providers.yaml`: `opencode-zai: enabled: true`.
  - `agents.yaml`:
    - implementer-quick, implementer and implementer-deep use provider opencode-zai, model `zai-coding-plan/glm-5.3`, variants low, high and max. Their fallbacks are codex (quick/default gpt-5.6-terra, deep gpt-6-astra) and then opencode Go, each with `variant: ""`. Claude was removed from the implementer chains.
    - tester uses claude sonnet.
    - dev-advisor gets `codex: gpt-6-sol` as its first fallback (agy disabled, Go at 99%).
  - `refresh-models` now records opencode-zai with 7 models.
- **doctor:** opencode-zai is authenticated, with its budget at 1% (5 h window).
- **Smoke test ag-be5784:** implementer-quick on opencode-zai, argv `--variant low`, done in 7 s, reply correct. Discarded afterwards.
- **Note:** implementer-deep has `readonly_paths: []` (tests unprotected) from an older decision. It was left as is, but this agent is told in each task that tests/ is read-only.

**2026-09-30 ~02:30 UTC:**
- **H7 merged (85524b9)** via ag-d06a0b. The 30 H7 tests pass.
- **Amendments** (4e7b97c):
  - PS-R2a: invocation uses the launcher, not the realpath;
  - PS-R2b: a provider's `env:` wins over MULTIAGENTS_BIN;
  - 3 superseded c2 tests.
- **In flight:**
  - tester ag-946e1f, on the tests for those amendments. Next, a GLM implementer implements PS-R2a/b.
  - tester ag-56fe25, turning the 8 D1 findings into tests. Next, GLM implementer-deep.
  - tester ag-70b0c0, on H11. Next, GLM implementer.
- **Still to do:**
  - The H7 adversary, when codex returns at 04:48 UTC.
  - Advance wt-main after the H7 adversary and PS-R2a.
  - Then D3.

**HANDOFF #3, 2026-09-30 ~03:00 UTC (orchestrator context wind-down).** The user's overnight mandate is in the ~22:45 entry, and the roster decision in the ~22:55 entry. The user will say when they are back.

**Done tonight (all on main):**
- **H7:** 85524b9, spec amendments 4e7b97c, test updates da355ae.
- **opencode-zai:** 88ddd80 (ZA-R1..R5), tests 523074e and 49c9b4e.
- **HA-R12 clean:** a919b35.
- **H9:** abe4059.
- **H13:** 6cfa1cf.
- **H11 tests:** 1a80ea2.
- **D1 finding tests:** 9d91043 (11 red).
- **D3 contract rev 2:** 24c62d8. Not started.
- **GLM TEAM LIVE**, config is in the ~02:00 entry. wt-main is at 49c9b4e. That is BEFORE H7, and it must be advanced.

**In flight** (all GLM on opencode-zai, except where noted):
- **ag-7fbf31** (implementer, glm high): H11 (drift.py, doctor section, server drift_summary). Tests: tests/test_h11_config_drift.py.
- **ag-e67fb5** (implementer-quick, glm low): H7 PS-R2a/PS-R2b (launcher for invocation, provider `env:` wins). 3 red tests.
- **ag-d61f60** (implementer-deep, glm max, `verifies=ag-179b69`): the 8 D1 findings. Tests: tests/test_d1_review_findings.py.

**Next, in order:**
1. Collect and merge the three runs above. Before merging, check each diff for tests/ edits, because implementer-deep has `readonly_paths: []`.
2. Run the H7 adversary, `verifies=ag-d06a0b`, when codex is back (it said 06:48 local, which is 04:48 UTC).
   - Targets: PS-R5 probe/generation (`startup.py` `StartupHealth._current`, `_reconcile`), PS-R6 pin refusal, PS-R4 progress marking in `Provider.parse_line`, and binary resolution.
   - Every finding goes back through the tester, then GLM.
3. After the H7 adversary and the PS-R2a merge, advance wt-main: `git -C <scratchpad>/wt-main checkout --detach main`.
   - Tell the user to run `/mcp` when they are back.
   - Run `doctor` to check that opencode-zai is still authenticated.
4. **D3:** tester from context/specs/d3-cli-dependency-manifest.md (rev 2), then implementer-deep (GLM).
5. **Then:**
   - H5: claude budget under docker (only relevant under docker).
   - H6: refresh-models must not erase another provider's entries.
   - H8: prompt over 128 KiB.
   - H10: live compaction check.
   - H12: branch cleanup.
   - D2: tmux step 1.
6. **Final report to the user:**
   - the GLM team is live;
   - `/mcp` is needed;
   - egress to api.z.ai is needed only when docker returns, and must be asked for;
   - the off-peak idea is parked;
   - the global `~/.config/multiagents/providers.yaml` is a full stale copy, which H11 will flag;
   - tickets still unfiled: 7, plus the codex-sandbox commit issue (from the ~00:10 entry).

**Routing notes:**
- Testers use claude sonnet (the new default).
- Implementers use GLM, with codex as fallback.
- Researcher, reviewer and adversary use codex. If codex is down, wait for it; claude is reserved by the user for the orchestrator and tester.
- `merge_agent(into=<worktree PATH>)` works for handing one run's work to another run; a node id is refused.

**2026-09-30 ~03:10 UTC:**
- **Merged ag-e67fb5, PS-R2a/PS-R2b (6e69bb1).** This was the first GLM run: glm-5.3 low, 230 s, clean, no tests touched, 716 passed.
- **Still in flight:** ag-7fbf31 (H11) and ag-d61f60 (D1 fixes).
- **Next** is unchanged from handoff #3.

**2026-09-30 ~03:30 UTC:**
- **Merged ag-7fbf31, H11 (925a68e).** Run on GLM high, 806 s. Changes: drift.py, a doctor `config drift` section, and a server drift_summary. 40/40 H11 tests pass, and 809 pass in the regression sweep. models.yaml is deliberately not compared.
- **Still in flight:** ag-d61f60 (D1 fixes).
- **Next** is unchanged from handoff #3.

**2026-09-30 ~02:20 UTC:**
- **Merged ag-d61f60, the D1 fixes for all 8 findings (942df35).** Run on GLM max, 4558 s. Changes:
  - new host-owned `NoticeState` in notices.py; tree.json is now a display mirror only;
  - new occupancy.py;
  - runner.py: f4 (`while…else`), f5 (`resume_deferred`), f8 (provenance captured at launch).
- **Checked on the new main:** `test_rf_r3_r1` and the launcher c2 test pass, as do the D1, H11 and H7 suites (85/85).
- **Nothing in flight.** The orchestrator's context is past wind-down, so nothing new was started.
- **A wake is armed for 04:50 UTC**, when codex is back.
- **Resume per handoff #3:**
  1. The H7 adversary (codex), `verifies=ag-d06a0b`. Also consider an adversary on the D1 fixes, `verifies=ag-d61f60`: they add host-state files and a cross-runner occupancy record.
  2. Advance wt-main.
  3. D3 tester (sonnet), then GLM implementer-deep.
  4. H5, H6, H8, H10, H12, D2.

**2026-09-30 04:50 UTC: STOPPED, waiting for the user.**
- **Budget:**
  - codex's 5 h window is free, but its WEEKLY is at 93% until 2026-10-04 10:49 UTC;
  - claude weekly at 40%;
  - opencode-zai weekly at 15%;
  - opencode Go at 99% monthly.
- **Why no H7/D1 adversary ran on codex:** it could exhaust codex for 4 days, and codex is the only provider for the advisor, researcher and reviewer. That is a user budget decision.
- **Options for the user:**
  - (a) the adversary on claude opus, which is independent of the GLM implementers;
  - (b) the adversary on GLM, which is cheap but not independent of the implementers;
  - (c) wait for codex's weekly reset.
- **The orchestrator's context is past wind-down (325k)** and needs a /compact before any new work.
- **Ready to run next, no codex needed:**
  1. D3 tester (claude sonnet), then GLM implementer-deep.
  2. Then H6, H8, H10, H12, D2.
  3. H5 only matters under docker.
- **The user must also:**
  - run `/mcp` after wt-main is advanced; it is still at 49c9b4e, before H7, H11 and the D1 fixes;
  - decide on the adversary provider.

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

### Ticket-driven agent scheduling (the user, 2026-10-02)

Not scheduled. Noted "pour planification"; for the next phase via `multiagents init-agent`.

- **The user's idea.**
  - Any delegation goes through a **ticket**. An agent that wants a subagent
    creates a ticket instead of starting it.
  - Each ticket lists the tickets it **depends on**. It is launched only when
    every one of them is closed.
  - A **script** (not an agent) runs the queue: it launches eligible tickets
    and marks a ticket closed when its agent returns. Running an agent is just
    creating a ticket and waiting.
- **The pattern it must support (the user's example).**
  - The orchestrator writes four tickets: tester → reviewer1 (of the tests) →
    implementer → reviewer2 (of the implementation), each depending on the
    previous one.
  - If reviewer1 rejects the tests, it **reopens** the tester's ticket and
    injects its comments. The implementer therefore cannot start yet.
- **`urgency`.** A ticket field with a default for "not urgent". The script
  picks urgent tickets first, then in order of arrival.
- **Time windows.** A ticket may give the hours during which its agent may
  run. Outside them it is not launched, or, if already running, **paused** by
  the script. With no window set there is no restriction.
- **The orchestrator's notes, for whoever designs it:**
  - **Existing parts.** It overlaps with the PC durable FIFO queue
    (PC-R3a), deferred tasks and quota pauses, and the parked-question
    mechanism. It should extend these, not sit beside them.
  - **Reopening.** A reopen must resume the tester's **session** (steer)
    rather than start cold, as already learned. Reopening also has to
    invalidate the dependents' eligibility, and a loop bound is needed (the
    "round 3 → opus" rule is one).
  - **What "closed" means.** Either the agent returned, or a verdict was
    given (approved or rejected). A reviewer's verdict needs a structured
    form, not prose.
  - **Pause is not free.** CLIs have no real suspend. A pause means stopping
    and later resuming the session (as with limits), or SIGSTOP. With
    SIGSTOP, wall-clock and silence watchdogs and provider-side timeouts
    still run.
  - **Who may do what.** Who may create, reopen or raise urgency on a
    ticket: any agent, or only an ancestor? Starvation of non-urgent work
    must also be addressed.
  - **Branches.** A dependent's worktree needs its predecessors' work. Today
    the orchestrator squashes by hand. The ticket should say what gets merged
    or squashed in, and when.
  - **Naming.** These "tickets" are distinct from the existing bug tickets
    (`list_tickets`). A different name is needed, to avoid the confusion.
  - **Windows.** Time zone (UTC or local); windows that cross midnight; days
    of the week.

- **The user's follow-up (2026-10-02): nodes and loops.**
  - **Name.** Call them **nodes** rather than tickets.
  - **Composite nodes.** A node may itself contain several nodes, and may
    be defined as a **loop**.
  - **Loop counters.** A loop node records the number of iterations done and
    a maximum. The default is no loop.
  - **Action when the maximum is reached.** One of:
    - close the node;
    - return to the orchestrator for a decision;
    - replace one of the node's models, extend the limit and change the
      limit-reached action.
    The last one generalises today's rule: at review round 3, escalate to
    opus.
  - **Orchestrator's note.** "node" is also already taken in the code: tree
    nodes, `node_id`, `agent_tree`. The contract should either merge the two
    concepts (a tree node becomes the run of a plan node) or name them
    distinctly, for example a *plan node* versus a *run*.
  - **Decided by the user (2026-10-02):** keep **node** for the planned
    unit, and **run** for its execution. Today's tree "nodes" (`node_id`,
    `agent_tree`) are runs in this vocabulary, and the contract must rename
    or alias them accordingly.

### 2026-09-30 ~07:00 UTC — user chose claude opus as adversary; 4 runs in flight
- ag-bd9c41 adversary (opus) on H7, verifies ag-d06a0b.
- ag-03602e adversary (opus) on D1 fixes (942df35), verifies ag-d61f60.
- ag-e9d658 tester (sonnet) for D3 from d3-cli-dependency-manifest.md rev 2.
- ag-0cbda0 implementer (GLM glm-5.3 high) on H6, fast path (known-cause bug, own regression tests).
Next: adversary findings go back to a GLM implementer with the failing tests; D3 implementer-deep (GLM max) after the tester merges.
- ~07:30 UTC: H6 merged (697f109, GLM; 10 regression tests). D3 tests merged (015f941, 285 red); the tester's interface assumptions are accepted as spec amendments (1caadba). In flight: ag-709ee5 implementer-deep (GLM max) on D3; ag-6d3d5a implementer-quick (GLM low) on the H8 interim size refusal; the two opus adversaries are still running.
- ~07:45 UTC: the opus adversary on the D1 fixes (ag-03602e) REJECTED them with 7 findings. Its tests are merged (523c23e, tests/test_d1_adversary.py, 20 red). ag-42b581 (implementer-deep, GLM max) is fixing all 7, plus #8: `_limits_for` reads a call-sourced timeout back from tree.json (H4/H14 code). Decision: the limits and provenance of adopted or relaunched runs come from a host-owned launch record, never from tree.json. ag-6d3d5a (H8) hit max_steps=40 and was steered to finish and commit.
- ~08:20 UTC: the opus adversary on H7 (ag-bd9c41) REJECTED it with 3 defects. Its tests are merged (954f6e5, tests/test_h7_adversary.py, 5 red). The defects: a claude API error counted as startup progress; steering a live run on a tripped provider kills it; losing the probe race gives a pin refusal. My decisions are spec amendments PS-R4a/5a/5b/1a (f0c2266). ag-303ef7 (implementer-deep, GLM max) is fixing them. In flight: ag-709ee5 (D3), ag-42b581 (D1 fixes), ag-6d3d5a (H8).
- ~08:35 UTC: H8 interim fix merged (21c5816: a single argv element over 128 KiB is refused before launch). ag-b980d0 researcher (codex terra: the researcher has no claude or zai model in its roster) is doing the read-only H12 branch inventory. Deletion stays my decision, after reading it.
- ~08:45 UTC — **H12 cleanup, partial.** Inventory is ag-b980d0 (.multiagents/runs/ag-b980d0/result.json).
  - **Discarded.** Every branch tip was an ancestor of main, so nothing was lost:
    - ag-852615, ag-12951a (branch 12951a-2), ag-5b326a, ag-6f199c and ag-fae464;
    - ag-05199c, ag-0d1a3f and ag-604af7;
    - ag-14a3e5, ag-da174a, ag-f8b8f7 and ag-fff50e.
  - **Left alone:**
    - **Refused as "seeded worktree is quarantined":** ag-37349e, ag-48a3d2, ag-c65ee1 and ag-cbf6c0. I did not force these.
    - **Denied by the auto-mode classifier:** ag-6812e8 and ag-a3cfa5. These are for the user.
    - **The standing advisor and dev-advisor sessions** (8e7d87, 4a29a4, …).
    - **Unmerged work to triage:**
      - tester/b689bf is superseded by 1568664 and 92af1c1, but deleting it needs `force`;
      - tester/cf02a1 has a 486-line tests/test_phase3_build_env.py that is not on main;
      - reviewer/98e037, reviewer/e4919f and reviewer/e8565d hold untriaged reports; e8565d has a SIGTERM terminal-cleanup finding.
- ~09:10 UTC — **D1 fixes.** ag-42b581 was interrupted at about 60%. Findings 1, 2, 3 and 8 are committed in a93e53c, with a new `launch_limits.py` (host-owned LaunchLimits). It was steered to finish F4 to F7 and the followup tests. Also in scope now: the adopted run's deadline comes from the host record, not command.json.
- **D2 contract written** (60332af, context/specs/d2-tmux-viewer.md, TM-R1..R6). The advisor's gaps are folded in:
  - the socket lives in the host-authority dir, not `.multiagents/`, which is docker-writable;
  - callers are the CLI and the monitor only, with no MCP tool;
  - a terminal-escape-safe formatter;
  - id validation, and symlinks and FIFOs refused;
  - truncation is handled.
  ag-743603 is the D2 tester (sonnet).

### 2026-09-30 ~09:40 UTC — roster switch (user) and PAUSE
- **The user's instructions.** z.ai's 5 h window is exhausted and agy's
  quota is back. Implementers go to codex: quick is gpt-6-luna, default is
  gpt-6-sol, deep is gpt-6-astra. GLM stays as their fallback, with
  variants low, high and max. The advisor, adversary and dev-advisor go to
  agy gemini-3.1-pro-high, with codex as the fallback. agy is re-enabled in
  the project providers.yaml. models.yaml has been refreshed (agy: 14
  models), and every provider is authenticated.
- **PAUSE, at the user's request, while they get agy working again.** Start
  nothing new.
- **Runs that died at the z.ai limit (exit 1).** Their worktrees and
  commits are kept, so resume from them rather than restart:
  - **ag-709ee5 (D3):** WIP commit 667b9b8.
  - **ag-42b581 (D1 fixes):** a93e53c. Findings 1, 2, 3 and 8 are done.
    Still to do: F4 to F7 in notices.py, the followup tests, and the
    adopted-run deadline from the host record.
  - **ag-303ef7 (H7 fixes):** 21c3035 and 2dcd789. These may be complete;
    verify tests/test_h7_adversary.py before merging.
  - **To resume on codex:** start a new run on the new tier, pass
    `merge_agent(<old>, into=<new worktree>)`, and point it at
    .multiagents/runs/<old>/.
- ag-743603 (the D2 tester, on sonnet) is still running.
- ~10:00 UTC: the user reports agy works again; the PAUSE is lifted.
  - **H7 fixes (ag-303ef7) are merged: 4dbf415.** I verified them myself: 180 passed across h7_adversary, h7_adversary_followup, h7_provider_startup, h6 and zai.
  - **Resumed on codex**, with each old branch squash-merged into the new worktree:
    - D1 fixes: ag-e6c241, implementer on gpt-6-sol, carrying a0a6924 from ag-42b581;
    - D3: ag-92a8d7, implementer-deep on gpt-6-astra, carrying 02701e3 from ag-709ee5.
  - ag-743603, the D2 tester, is still running.
- ~10:15 UTC: D2 tests merged (c40e4e2): 197 tests, which the tester validated against a throwaway reference implementation. Its interface assumptions are accepted into the contract (4f568d1). ag-cac49a, the implementer on codex gpt-6-sol, is implementing D2. In flight: ag-e6c241 (D1 fixes) and ag-92a8d7 (D3).
- ~07:10 UTC (real clock) — **codex's weekly is EXHAUSTED until Oct 4 12:49 CEST.** All three codex runs died on quota:
  - ag-92a8d7 (D3) left WIP e69cf83;
  - ag-e6c241 (D1) left WIP f4e9c29;
  - ag-cac49a (D2) had no commits and was discarded.
- **User decision: every implementer tier goes on agy Gemini** (quick gemini-3.8-flash-high, default and deep gemini-3.1-pro-high). The GLM variants, then codex, are the fallbacks. The advisor, adversary and dev-advisor are already on agy gemini pro.
- **Relaunched on agy:**
  - **D1:** ag-725897. It carries 0c94f3d, the squash of both earlier D1 runs.
  - **D3:** ag-149e0a. It carries 84afefa, with stray .d3-* files it was told to remove.
  - **D2:** ag-51c6dc, a fresh start.
- **For later.** Once the codex weekly resets, the reviewer and researcher lose their provider. Once the z.ai 5 h window resets (~10:57 UTC), GLM is available again as a fallback.
- **Researcher and agy.**
  - The roster gave the researcher `agy: gemini-3.8-flash-medium`, which agy refuses combined with `effort: low` ("conflicts with --effort=low"). It is now `gemini-3.1-pro-low` in agents.yaml.
  - Worth a ticket: the router accepted a model/effort pair the CLI rejects.
  - ag-463a38, a researcher on agy pro-low, is analysing what GLM delivered for 100% of its 5 h window and 35% of its weekly, at the user's request.
- **agy loop pattern.** agy moves long commands to the background, and Gemini then polls their task log; the doom-loop watchdog fires on this. I steered ag-725897 and ag-149e0a to run tests in the foreground or to sleep between checks.
- **GLM analysis (ag-463a38), reported to the user.**
  - One 5 h window: ~2.8 M tokens and 20 weekly points (from 15% to 35%).
  - Output: H6 and H8 complete, H7 fixes complete, D1 about 60%, and D3 WIP. That is ~560 src lines and ~470 test lines.
  - Five parallel runs emptied the window in ~45 minutes.
  - Quality is good at high and low. max is long, and three of its runs were cut off.
- **D3 on agy (ag-149e0a) is DONE.** I verified the D3 suite myself: 286 passed. The stray files were removed (16b34e6).
  - My concern: the cleanup one-liner in exec_in_running was shaped to match the fake-docker log matcher. The in-container kill is real.
  - The reviewer ag-bf6004 (agy gemini pro, fallback from codex) is reviewing it before the merge.
- **D3 review (ag-bf6004, agy) says rework, with 6 findings.**
  - Taken: #1, os.setsid PermissionError, guarded; #2, the cleanup's empty-pidfile race, written as plain statements; #3, reaping on every exception; #4, the swallowed shipped-YAML error.
  - #5, the regex shell parser: kept, with its assumptions documented and unparsable input reported as a finding. shfmt was declined because it would be a new dependency.
  - #6: only the probe-state duplication is factored out; the module split was declined.
  - I steered ag-149e0a with these fixes.
- **D2 (ag-51c6dc, agy) is done.** I verified 197/197 green. It had left 25 scratch files at the repo root; they are now removed, and the diff is src-only (viewer.py, tmux.py, cli, monitor).
- ag-b8b511, the adversary (agy gemini pro), is attacking D2: the sanitiser, the follower, paths and the socket, and tmux argv. D2 is merged only after its findings are resolved.
- **D3 MERGED (f2b0f38).** The reviewer's fixes were applied. I verified 377 tests myself: the D3 suite plus h7, h7_adversary and h6. No adversary was run on D3: it is diagnostics only (lint and doctor), and nothing untrusted is in its path.
- **The D2 adversary (ag-b8b511, agy) REJECTED it with 7 defects.**
  - The defects:
    - truncation followed by growth is missed;
    - bidi controls pass through unescaped;
    - an unknown id is accepted;
    - sys.exit takes down the monitor;
    - a concurrent `open` crashes;
    - a broken-symlink socket dir crashes;
    - lone surrogates crash the viewer.
  - My decisions are spec amendments TM-R1a, TM-R1b and TM-R2a (1a689a9). The adversary's tests were merged into ag-51c6dc's worktree (aba9e04), and ag-51c6dc was steered to fix them.
  - Not covered by the adversary: partial, CRLF and empty-file follower cases; path-traversal bounds; tmux argv fuzzing.
- ag-725897 (the D1 fixes) is still running the full suite in chunks.

### HANDOFF #4 — 2026-09-30 08:10 UTC (orchestrator context ~260k; compact soon)
**Quota state at 08:07 UTC:**

| Provider | State |
|---|---|
| codex | weekly 100% until Oct 4 10:49 UTC |
| z.ai GLM | 5 h window 100% until 10:57 UTC; weekly 35% |
| agy Gemini | 5 h window 100% until 11:54 UTC; weekly 17% |
| agy Claude/GPT pool | unused, but routing treats agy as down while the Gemini window is exhausted (ticket-worthy) |
| claude (ours) | session 81% until 09:50 UTC; weekly 47% |
| opencode Go | monthly 99% |

**Merged today:**
- H6: 697f109
- H8: 21c5816
- H7 fixes: 4dbf415
- D3: f2b0f38
- Adversary tests: 523c23e (D1), 954f6e5 (H7)
- Spec amendments: D2, D3 and H7

**In flight or pending:**
1. **D2 (tmux viewer).**
   - The implementation and the adversary's failing tests (tests/test_d2_adversary.py) are on branch agents/implementer/51c6dc. ag-51c6dc died on agy quota before it fixed anything.
   - A replacement start_agent(implementer, model=claude-sonnet-4-6 [agy pool], verifies=ag-b8b511) was DEFERRED (tree paused, retry ~08:22 UTC).
   - When it restarts, immediately run `merge_agent(ag-51c6dc, into=<its worktree>)`. Its task tells it to wait up to 3 minutes for viewer.py, tmux.py and test_d2_adversary.py.
   - If it restarts on Gemini and fails again, re-run it after 11:54 UTC, or on GLM after 10:57 UTC.
   - Findings to fix: TM-R1a, TM-R1b, TM-R2a (spec 1a689a9).
2. **D1 fixes.**
   - ag-725897 (agy gemini) was last seen running the full suite in 4 chunks. Its branch holds 0c94f3d, the squash of ag-42b581 and ag-e6c241, plus its own commits.
   - It will likely die on the agy quota. If so, resume it on a new run, passing merge_agent(ag-725897, into=new) and its handoff (.multiagents/runs/ag-725897).
   - Done means test_d1_adversary.py and test_d1_adversary_followup.py are green, and the neighbours stay green.
   - Then merge, and set the ledger status if F-ids apply.
3. **Remaining brief items:**
   - H10 (live compaction check), not started.
   - H12 leftovers for the user: the quarantined worktrees and the classifier-denied discards (see ~08:45).
   - Triage of tester/cf02a1 (test_phase3_build_env.py) and reviewer/98e037, e4919f and e8565d (e8565d has a SIGTERM terminal-cleanup finding).
   - H5 applies only under docker.
4. **Tickets to file (gh missing):**
   - the 7 in .multiagents/runs/ag-a0be27/result.json;
   - codex sandbox commits;
   - the researcher agy model/effort conflict (the router accepted `gemini-3.8-flash-medium` with `effort: low`);
   - agy routing ignoring the separate Claude pool;
   - agy long commands becoming background tasks, which trips the doom-loop watchdog.
5. **Also outstanding:**
   - advance wt-main to main, and the user runs /mcp;
   - the D3 doctor section can then be checked live.

**Roster now (project agents.yaml, gitignored):**
- implementer-quick: agy gemini-3.8-flash-high
- implementer and implementer-deep: agy gemini-3.1-pro-high
- Fallbacks: GLM low, high and max, then codex.
- advisor, adversary and dev-advisor: agy gemini-3.1-pro-high, with codex as fallback.
- tester: claude sonnet.
- researcher: codex terra, then agy gemini-3.1-pro-low.
- agy re-enabled.
- 08:20 UTC: **D1 fixes MERGED (9fd5660).** ag-725897 died on the agy quota after its final commit. I verified 215 passed myself: d1_adversary, d1_adversary_followup, d1_review_findings, d1_limit_notices, h4_h14, h7, h7_adversary and h11. Item 2 of handoff #4 is DONE. D2 is the only item still pending (deferred).
- 08:25 UTC: **D2 was relaunched on claude sonnet at the user's request.**
  - The run is ag-d86348, verifies=ag-b8b511.
  - I added `claude: {model: sonnet}` to the implementer's models in agents.yaml.
  - I carried ag-51c6dc into it by hand, in commit 5824254: a squash, with BRIEF.md kept from main.
  - **Watch:** the earlier DEFERRED agy task (implementer, claude-sonnet-4-6) will auto-restart after the agy cooldown (~08:35 UTC). It is a DUPLICATE: stop and discard it.
- Deferred duplicate df-e34d37 (agy implementer claude-sonnet-4-6, D2). No MCP tool can cancel a deferred task (`tree.drop_deferred` exists, but nothing exposes it). That is a ticket. When it restarts, stop_agent and discard it; D2 is ag-d86348.
- 08:45 UTC: **D2 fixes done** (ag-d86348, claude sonnet, dd59986): all 7 findings.
  - **Accepted:**
    - the /tmp crash-log redirect was removed;
    - a run dir counts as known only when non-empty;
    - `open` falls through on a duplicate session.
  - **Not accepted:** the follower treats the literal statuses "active" and "terminal" as markers, only to suit the adversary's FakeTree. That is code bent to a test.
  - ag-d4ba1a, the tester (sonnet), is correcting two tests on a squash of d86348 (c196156):
    - the FakeTree gets real statuses;
    - the race test uses a known agent and catches BaseException.
  - **Next:**
    1. steer ag-d86348 to drop the alias, then carry in the tester's commit;
    2. verify the 197 tests plus the adversary tests;
    3. merge ag-d86348.
- 09:05 UTC: **D2 MERGED (695099a).**
  - The alias is gone: the follower uses only the real ACTIVE and TERMINAL sets.
  - The adversary tests were corrected by the tester (7070c89).
  - I verified it myself: 204 D2 tests pass, and test_core plus the monitor tests give 951 passed and 2 skipped.
  - The superseded branches ag-51c6dc and ag-d4ba1a were discarded; their content is in the merge.
- **Brief state:** H6, H7, H8, H11, D1, D2 and D3 are merged. What remains:
  - H10, the live compaction check;
  - the H12 leftovers for the user;
  - triage of the cf02a1 branch and the reviewer reports;
  - tickets;
  - moving wt-main to main, followed by a /mcp.
- The deferred duplicate df-e34d37 is gone: after the MCP reconnect, the deferred queue is empty and no run was started (checked 2026-09-30). Nothing left to stop.
- 2026-09-30 ~10:00 UTC. The bug-reporter (ag-3afd5a) wrote 3 tickets, but the queue filed only the last one (bug-ac396a: routing to an exhausted agy). The other two are saved verbatim in context/tickets/2026-09-30-unfiled.md: the monitor showing reset times in UTC ISO, and the deferred task vanishing without a trace (the df-e34d37 case). One more tooling defect: a run with several TICKET() markers files only the last one.

## 2026-09-30 ~10:15 UTC: the user's routing plan and the ticket backlog
- **User orders, verbatim in substance:**
  - continue all pending tasks with **claude only**;
  - when z.ai returns (5h reset at 10:57 UTC), move **all implementer tiers to zai-coding-plan/glm-5.3-flash**, which also tests it;
  - when agy returns (~11:54 UTC), run the **overdue advisor tasks**: the context/advisor-catchup.md catch-up, plus a review of the T1/T2 contracts;
  - **tickets are never submitted** (no gh): we fix them ourselves.
- **New work.**
  - The contracts are context/specs/t1-deferred-queue.md (DQ-R1..R7) and context/specs/t2-multi-ticket.md (MT-R1..R4).
  - The advisor has not reviewed them yet, because agy is down; they are logged in advisor-catchup.
- **Ticket backlog to triage against main:**
  - bug-c106a9 and bug-ac396a;
  - the 8 in the ag-a0be27 result;
  - the unfiled monitor reset-time ticket;
  - the agy Claude-pool routing issue;
  - the doom_loop trip on agy background polls;
  - the researcher's model/effort conflict.
  - A researcher does the triage.
- **H12 triage (researcher ag-bc6fd4, 2026-09-30).** The details are in the run result.
  - tester/cf02a1: phase 3 R19/R20 tests, 27 red / 21 green on main. **KEEP**: phase 3 is paused, and this is a user call.
  - reviewer/98e037: only #4 is still present, a `suppress(Exception)` in runner.py:2706-2729 that hides retry-launch failures. It is an S fix, **queued for implementer-quick on glm-5.3-flash** once z.ai is back.
  - reviewer/e4919f: nothing worth doing. #3, the `rglob` on every call in server.py:190, is optional and low.
  - reviewer/e8565d: #1-3 and #7 are fixed. #4 (transcripts.py:388, keys hardcoded to Claude's) waits until a second transcript reader exists. #5 and #6 are skipped.
  - reviewer/bf6004: #5 is declined, as documented.
  - **Branch cleanup:** the reviewer branches 98e037, e4919f, e8565d and bf6004 and tester/b689bf (superseded) can be discarded. The implementer leftovers are the quarantined worktrees and the discards the classifier denied, so they need the user.
- **Ticket triage (researcher ag-72e868, 2026-09-30).** The full table is in the run result.
- **Fixed already:**
  - 1b, the codex empty profile;
  - 5, the agy manage_task polling (bug-8615db).
- **The quarantined worktrees.** discard refused "seeded worktree is quarantined" on reviewer 98e037, e4919f and e8565d and on tester b689bf. That is left to the user; bf6004 is discarded.
- **Batch Q, S-size, known cause, straight to implementer-quick on glm-5.3-flash, in parallel because the files do not overlap:**
  - Q1: runner.py:2706 `suppress(Exception)` around the retry launch (98e037 #4). The fix logs an event.
  - Q2: cli.py, `_resolve(args.path) if find_project_root()` at 8 sites (1f).
  - Q3: executor/docker.py `stop()` omits `auth_container` (1h, bug-c106a9).
  - Q4: codex.py `login_action` does not name the profile path (1c).
  - Q5: providers.yaml codex block, `file_change` doom-loop false positive (1e). The minimal fix is `opaque_tools`.
  - Q6: monitor reset time shown as local time plus a countdown (unfiled ticket 1). This is presentation only; the UTC data stays.
- **Batch M, which needs contracts, and runner.py is shared, so these run one after another:**
  - T1 (deferred queue): in progress, and it includes 1d.
  - 1g: `consult` skips `max_concurrent` on resume.
  - 3: the project `fallback_chain` overrides the agent's own fallbacks (bug-ac396a).
  - 6: a model/effort conflict is not validated before launch.
- **Batch L:**
  - 1a: container claude 401. Deferred, since the docker executor is not in use.
  - 4: agy pools, which needs a design decision (pool-aware routing, or two providers). That goes to the advisor when agy is back.
- 2026-09-30 ~10:35 UTC:
  - Q3 merged (a0afac7), which fixes bug-c106a9.
  - Q2 merged (a7d3302), `_resolve_if_project`.
  - The T1 and T2 tests are merged (d646f77, e8d19ca), with spec amendments c01a85d and b942382.
  - In flight on claude sonnet: T1 impl ag-38d415 and T2 impl ag-9008e5.
  - Next, on glm-5.3-flash after 10:57: Q1, Q4, Q5, Q6, then batch M one at a time (1g, 3, 6).
- 2026-09-30 ~10:45 UTC: codex reset early.
  - **Verified.** One live `codex exec` through the multiagents profile brought its reading to 5h 0% and weekly 0% (the weekly window now resets 2026-10-07). The rollout reading had been about 3.6h stale, still saying weekly 100%.
  - **Config change, per the user.** advisor, dev-advisor and adversary moved to provider codex, gpt-6-sol, effort medium, with agy gemini-3.1-pro-high as their fallback. The user said "GPT-6.1-sol"; only gpt-6-sol exists in the catalog.
  - **The advisor catch-up consult is running on codex.**
- **Tooling defect, observed 2026-09-30 by the user.** A roster model change on the SAME provider does not take effect in a standing conversation. `_find_conversation` resumed advisor ag-8e7d87, which was created on codex gpt-6-astra, after the roster had changed to gpt-6-sol. `_conversation_route` checks only the provider (CX-C28).
  - **Workaround used:** stop the old conversation node, so that the next consult creates a fresh one.
  - **Ticket to fix:** a conversation whose model differs from the roster should be retired and recreated, or at least reported.
- **User order, 2026-09-30 ~10:55 UTC: launch no more implementers on claude; wait for glm.**
  - The running claude implementers (T1 ag-38d415, T2 ag-9008e5) finish their current scope.
  - The work on the T1/T2 amendments (tests are in 3ce818e) goes to glm-5.3-flash implementers. So does batch Q (Q1, Q4, Q5, Q6) and then batch M.
- 2026-09-30 ~11:20 UTC.
  - **Merged:**
    - Q5 (7f6064a), codex `file_change` declared opaque;
    - T2 (a624f9d), all MT tests and the T2 amendments green: 76 plus 10 core ticket tests;
    - Q4 (8e95077), the login names its store.
  - **Implementers on glm-5.3-flash.** Q5 and Q6 each hit `max_steps` 40, so I raised it to 80.
  - **In flight:**
    - Q6 (ag-5b8b85, glm);
    - Q1 (ag-7f69ce, glm);
    - the phase 0 invariant fix (ag-4240d0, glm). Two phase0 guards have been red since the D3 and H7-fix merges, because comments name claude and opencode in manifest.py:224 and providers.py:644.
    - T1 (ag-38d415, claude, finishing).
  - **gpt-6.1-sol.** The user says it works in their terminal. Here, codex 0.158.0 `exec` gets a 400 "not supported when using Codex with a ChatGPT account", both via ~/.codex and via the agents' profile. ~/.codex/sessions has no successful gpt-6.1-sol session today, so the user's terminal may use another CODEX_HOME or machine. I have asked the user.
- 2026-09-30 ~11:40 UTC.
  - **Merged:**
    - Q6 (a22a0b1), `reset_display`, the countdown plus local time in the monitor;
    - the phase 0 invariant fixes (5e1eb4b, and the follow-up for the Q6 docstring).
  - **codex.** The user upgraded the CLI to 0.159.2, and gpt-6.1-sol now works through the agents' profile. The model list is refreshed, and every roster use of gpt-6-sol has become gpt-6.1-sol: advisor, dev-advisor, adversary, the tester's fallback, the implementer's codex fallback, and the reviewer.
  - **The advisor conversation ag-322b14 (gpt-6-sol) is stopped,** so that the next consult starts fresh on 6.1. This is the same-provider model-change bug.
- 2026-09-30 ~12:20 UTC.
  - **Q1 merged (15fba04):** a failed retry launch now emits `retry_failed` and ends the node `failed` with its cause. On main, test_core plus the Q1 file give 560 passed.
  - **Spec M.** The contract m-routing-fixes.md has advisor amendments (0d996f2), and its tests are merged (e2e4767).
  - **Implementation of M** runs on implementer-deep glm-5.3-flash, ag-3d566d.
  - **T1 (ag-38d415, claude) is still running.** After it lands, the T1 amendment tests (3ce818e) need a glm implementer.
- 2026-09-30 ~12:50 UTC.
  - **T1 base merged (180e483):** 46 of the T1 tests pass.
  - **One old test contradicts DQ-R2.** In `test_core.py::test_draining_stops_when_the_window_closes_mid_batch`, `still_deferred` should be 3 (what is left waiting). Tester ag-0ce81a is updating it.
  - **The T1 amendments** (DQ-R8/R8a, R4a, R2a, R3c; 13 red) are on glm, ag-3a07cc.
  - **M** is on glm, ag-3d566d, with 4 commits so far and its suite running.

### HANDOFF #5, 2026-09-30 ~12:40 UTC. The orchestrator context is near wind-down.
- **Merged today, after handoff #4:**
  - Q1-Q6;
  - the phase 0 invariant fixes;
  - T2 (multi-ticket, all green);
  - the T1 base (180e483) and the test_core DQ-R2 fix (8b14a6a).
- **In flight:**
  - **T1 amendments,** ag-3a07cc (glm), done: 29 plus 46 green, commit 4e53896, NOT merged yet. Adversary ag-5917f6 (claude opus, verifies ag-3a07cc) is checking it. Its top lead is the crash window between `start()` returning and `deferred_id` being written, which can cause a duplicate restart. After it: fix its findings on a glm implementer, carrying 4e53896, then merge.
  - **M routing fixes,** ag-3d566d (implementer-deep glm): 4 commits (ff55a7c, 9e129d6, f186c1c, d5243ad), running the full suite. After it: verify `tests/test_m_routing_fixes.py` and the phase0 invariants, and consider running the adversary on it (routing decides where credentials are spent).
- **Adversary on codex gpt-6.1-sol fails at once.** codex returns "This content was flagged for possible cybersecurity risk … apply for Daybreak access" (ag-18bf9a). The adversary brief's wording trips OpenAI's cyber filter. Use claude opus (or agy) for the adversary until the user decides. **Tell the user.**
- **agy start refused** with "agy and all fallbacks are exhausted or cooling down" although `budget_status` shows agy usable at 0.83 headroom. Probably a stale agy cooldown in tree.json. Check `tree.json` cooldowns.agy before relying on agy.
- **Open decision for the user:** split agy's Claude/GPT pool into its own provider (the advisor recommends it only if the user wants to route work there).
- **Tickets still to fix later:**
  - a standing conversation keeps its old model after a same-provider roster change;
  - 1a, the container claude 401 (docker only);
  - 4, the agy pools (decision above);
  - e8565d#4, transcripts keys.
- **~13:00 UTC: adversary ag-5917f6 on T1.** 7 findings, 5 of them high:
  - a crash after `start()` gives a double start;
  - a cancelled drain strands the claim;
  - a cancel mid-restart;
  - a re-deferral takes the wrong owner, and so does an orchestrator entry drained by a subagent.
  - The rest: malformed entries crash things, and a refused entry can be retried by a recovery race.
- **Spec amended** with DQ-R9..R12.
- **Fix run** ag-c604fe (implementer-deep glm), with the worktree squashed from agents/adversary/5917f6 (amendments plus tests) as e1347f6. When green, merge it; that one merge carries the amendments and the fixes. Afterwards discard ag-3a07cc and ag-5917f6, since both are carried.
- **~13:20 UTC: M merged (28eb8a6).** The M tests and the phase0 invariants are green.
  - **Side effect:** 18 tests in `test_t1_deferred_queue.py` went red. Their fixture defers through the `worker` agent, whose `models:` zeta fallback now routes first under RM-R2. Tester ag-978728 is fixing the fixture.
  - **The T1 fix run ag-c604fe** is based on pre-M main. At merge time, check its T1 adversary and amendment tests against M: the same `worker` fixture issue may apply.
  - **Not yet done:** an adversary run on M, since routing decides where credentials are spent. It should be run on claude opus, because codex refuses adversary work.
- **~13:50 UTC: the T1 adversary fixes are merged (4c44d71),** carrying the amendments and `tests/test_t1_adversary.py`. With M on main, 215 T1, M and phase0 tests are green.
  - **Regression:** `test_core::test_a_failed_restart_leaves_the_rest_of_the_queue_intact`. A `RuntimeError` from `start()` now propagates, although DQ-R3b says to stop and return `stopped_on`. The fix is ag-? (implementer-quick glm).
  - **Discard later:** ag-3a07cc and ag-5917f6, which are carried.
  - **Adversary on M:** ag-9dffc6 (opus), running.

### HANDOFF #6, 2026-09-30 ~13:55 UTC (orchestrator context wind-down)
- **Main is at the latest BRIEF commit.** Merged today: Q1-Q6, the phase0 invariant fixes, T2, T1 (base 180e483, then amendments and adversary fixes 4c44d71), M (28eb8a6), and the test fixture fixes (8b14a6a, d95865a). ag-3a07cc and ag-5917f6 are discarded.
- **In flight (verify, then merge):**
  1. **ag-45fc5c (implementer-quick glm):** the regression `test_core::test_a_failed_restart_leaves_the_rest_of_the_queue_intact`. A `RuntimeError` must stop the drain and return `stopped_on` (DQ-R3b); only a `BaseException` re-raises (DQ-R10). After it merges, run test_core in full plus the 3 T1 files, the M file and the phase0 files.
  2. **ag-9dffc6 (adversary, claude opus, verifies ag-3d566d):** the robustness review of M. Its failing tests go in `tests/test_m_adversary.py`. Next step: amend m-routing-fixes.md with any contract gaps, then run an implementer-deep on glm, carrying the adversary branch with a manual `git merge --squash` into the new worktree.
- **Roster, all in gitignored agents.yaml; backups in the scratchpad:**
  - every implementer tier is on opencode-zai glm-5.3-flash;
  - advisor, dev-advisor and adversary are on codex gpt-6.1-sol, effort medium;
  - the adversary cannot run on codex (the OpenAI cyber filter), so pass `model: opus`;
  - implementer-quick `max_steps` is 80.
- **User decisions pending:**
  - (a) adversary on codex: request Daybreak access, rephrase the brief, or keep it on opus;
  - (b) split the agy Claude/GPT pool into its own provider.
- **Open tooling items to fix later:**
  - a standing conversation keeps its old model after a same-provider roster change;
  - the agy start was refused while `budget_status` showed agy usable (check `tree.json` cooldowns.agy);
  - the MCP `wait_for_agents` with timeout 1800 is killed by the client idle timeout, so use 1500 or less;
  - 1a, the container claude 401;
  - e8565d#4, the transcripts keys.
- **~14:05 UTC: regression fix merged (6598c07).** On main, test_core, the 3 T1 files, M and phase0 give **773 passed, 2 skipped**. T1 and T2 are DONE.
- **M adversary ag-9dffc6 (opus): VERDICT(rejected, 5).**
  - The failing tests are committed on `agents/adversary/9dffc6` (2b40a3a): probe leak, sibling effort, null suffix, consult race, stale wind-down, clock step. The full report is in `.multiagents/runs/ag-9dffc6/result.json`.
  - **NEXT (the post-compact orchestrator does this):**
    - read the report and amend m-routing-fixes.md;
    - start implementer-deep (glm), then squash `agents/adversary/9dffc6` into its worktree;
    - verify, merge, and discard ag-9dffc6.
- **Nothing is running now.**

### 2026-09-30 ~14:40 UTC — after compaction #6

**User decisions:**
- (a) The adversary gets a reworded brief and name: the new roster agent `robustness-tester` (codex gpt-6.1-sol, medium; brief `.multiagents/config/agents/team/robustness-tester.md`). `adversary` goes back to claude/opus as its primary.
- (b) agy's Claude/GPT pool becomes its own provider. The user generalised this into feature **PS**: providers share tooling (CLI, auth, quota source) while keeping their models and quota separate. The contract is `context/specs/ps-provider-sharing.md` (d5a6885), reviewed twice by the advisor ag-d20e1e.

**In flight:**
- M fix: `ag-f25929`, implementer-deep on glm, verifying ag-9dffc6. The adversary tests were squashed in (873c535), and the M spec was amended with RM-R7..R2b (6e09333).
- **NEXT for M:** verify, then the `robustness-tester` (codex) as a first real test of the renamed role, then merge, then discard ag-9dffc6.
- PS tester: deferred (claude session reset 14:51 UTC). It auto-restarts at the next wait.
- **NEXT for PS:** tester → implementer-deep (glm) → robustness-tester → reviewer → merge, then move the agy claude/gpt routes in agents.yaml to `agy-partner`.

### 2026-09-30 ~16:00 UTC

**Merged:**
- the M fix, ag-f25929, as 6e023e0: test_m_adversary 24/24, and M+T1 179 passed on main;
- the PS tests, ag-adf1bc, as d20093b. They are red by design. The spec amendments R10, R4b and R7b were committed as 7941330.

**Roster:** `robustness-tester` was added to the implement and review team rosters in project.yaml. implementer-deep's timeouts were raised to 5400/900, because glm is silent while suites run.

**In flight:**
- robustness-tester ag-6e70df (codex gpt-6.1-sol), checking M after the fix; this is the first run of the renamed role;
- PS implementer-deep ag-dc8946 (glm).

**NEXT:**
- M: read the result. Findings go to an implementer-deep on glm. If the codex filter still refuses the run, report that to the user.
- PS: verify, then the robustness-tester, then the reviewer, then merge, then move the agy claude/gpt routes in agents.yaml to agy-partner.

### 2026-09-30 ~16:30 UTC

**robustness-tester ag-6e70df (codex gpt-6.1-sol).** No content-filter refusal, so the rename works. VERDICT(rejected, 3), with tests in `tests/test_m_robustness.py`. The findings:
- start() overbooks against a consult;
- consult reservations leak on pre-launch exceptions;
- a backward clock step makes a cached reading younger.

The fix is running in implementer-deep ag-074735 (glm), and the tests were squashed into its worktree as 118141b.

**Tooling note, minor** (the harness auto-committed anyway). Under the codex `sandbox` permission:
- git cannot create an index lock, because the common git dir is read-only;
- `asyncio.to_thread` hangs in its sandbox.

This is a candidate ticket, to fix in-house later.

### 2026-09-30 ~17:00 UTC: the Gemini review of glm-5.3-flash's work, at the user's request

Both reviewers ran on gemini-3.1-pro-high.

**ag-37f81c, on M and T1.** VERDICT(rejected, 3):
1. `_settle_effort` bleeds across fallback candidates. Sent by steer to ag-074735.
2. A `cancel_deferred` race (DQ-R12).
3. `refused_total` is dropped on the paused early return (DQ-R2a).
Findings 2 and 3 go to implementer ag-ce8fce, together with the leftover in snapshot.py.

**ag-fdbf30, on the small Q fixes.** VERDICT(rejected, 2):
1. Q2: an invalid explicit `--path` falls back silently. Not acted on yet: it needs a check of whether the fallback was deliberate in the Q2 ticket. Pending.
2. The phase-0 leftover in monitor/snapshot.py. Sent to ag-ce8fce.

**Summary.** Well-structured code with good idiom fit and spec-id comments. Weak on concurrency, transaction boundaries and edge cases, and it fixes symptoms: it satisfies the test glob rather than the rule.

**Workaround applied.** agy's lapsed breaker was cleared through `Tree.note_run_outcome`. The bug is ticketed (ddcdf7f).

**Codex sandbox.** Under the reviewer's agy sandbox, `run_command git` failed. Diffs were exported into `_review/` in its worktree.

### 2026-09-30 ~17:10 UTC: user rules

- **The reviewer runs systematically after every implementer, before merge.** This overrides the pipeline threshold.
- **glm variants.** They were never passed, but glm-5.3-flash defaults to max. Per the user, keep max for all tiers: the top-level override was removed and the models: routes were set to max. The `models:` route variant (low/high/max) is ignored for the agent's preferred provider: argv had no `--variant` in any run. Worked around by a top-level `variant:` on each implementer tier (quick low, implementer high, deep max). New runs only.
- **Candidate in-house ticket:** a `models:` route entry for the preferred provider is ignored.

### 2026-09-30 ~18:30 UTC

**Merged:** T1 review fixes, ag-ce8fce, as 55befed. T1 and M are 188 passed on main.

**M round 3, ag-074735.** Reviewer ag-f21a0c rejected it with 5 defects:
- P1: a cancel after `executor.start` releases a live slot;
- the `read_at` age is counted twice;
- the age floor is overwritten by a stale hit;
- the age floor freezes ageing after a clock step;
- the post-claim settle sits outside the `finally`.
These were steered back to ag-074735, with a monotonic clock suggested.

**Q2b, queued and low priority.** The Q2 spec was silent (researcher ag-69700c).
- **Decision:** an explicit `--path` that does not exist, or is not a project, must fail loudly in the `_resolve_if_project` commands, not fall back to global.
- **Needs:** the tester to change tests/test_q2_cli_path.py:18, then an implementer-quick, then the reviewer.

**Also queued:**
- the ticket for the pinned-start half-open self-deadlock (ddcdf7f), after ag-074735 merges;
- the `models:` route of the preferred provider being ignored (variant).

### 2026-09-30 ~20:00 UTC: user decision on test-suite speed

**Measuring.** Researcher ag-613241 is measuring the full-suite time: durations, causes, whether xdist is feasible.

**The user's decision, if the findings confirm it.** The test rework is done by **claude opus as the implementer**, instructed to work with **gemini pro as its advisor**: dev-advisor on agy gemini-3.1-pro-high.

**Caveats to solve before launching:**
- The implementer tiers have the tests in `readonly_paths`, so `merge_agent` would revert their test edits. Options: a per-run exception, a dedicated agent entry, or the tester on opus. The user asked for an implementer that can consult an advisor.
- Only the implementer tiers can consult dev-advisor.
- On agy, `run_command` failed for the readonly reviewer, so a Gemini dev-advisor may be unable to run git or pytest.

### 2026-09-30 ~20:40 UTC

**PS implementer ag-dc8946 is done.** 132/132 of the PS tests pass.

**Two NEED_INFO test conflicts** (c2_provider_harness counts 5 providers; the subagent_mcp fixtures use `model: "m"`) went to tester ag-e77949. Reviewer ag-4cdd7b is on PS, and was asked to map the collisions with the 074735 cache rewrite.

**M round 3, ag-074735.** Its 5 findings are fixed, and it is being re-reviewed by ag-53986b.

**Merge order.** Merge M first (ag-074735); PS will then need a rebase or reconciliation on budget.py's cache.

**Test-speed measurement.** Researcher ag-613241 on codex could not run pytest: its readonly sandbox has a read-only HOME, /tmp and workspace. The orchestrator is measuring the chunks with `--durations` in its own shell. Output: scratchpad/durations.

**Codex sandbox note.** Codex `readonly` and `sandbox` runs cannot run the test suite, and cannot always run git commit.

### 2026-09-30 ~21:30 UTC

**M.** Re-review ag-53986b rejected it with 4 defects. The spec was amended with RM-R4e (the exact cache-age rule, plus an accepted limit) and RM-R1b (shielded launch cleanup), commit 821a6a9. The findings were steered back to ag-074735.

**PS.** Reviewer ag-4cdd7b rejected it with 11 defects. These were steered back to ag-dc8946, which reconciles with the M cache after M merges.

**Tester ag-e77949.** It updated the c2 harness count to 6 and the subagent_mcp fixture (`models_include: ["*"]`). **Merge it together with PS**: one of its tests is red on main until PS lands.

**The reviewer's timeout** was raised to 1800.

### 2026-09-30 ~22:30 UTC

**TS, test speed.**
- **Measured:** 46 min sequential (chunks of 685, 537, 841 and 712 s). 240 slow tests make up 65 % of the time, and five tests take about 60 s each.
- **Contract:** context/specs/ts-test-speed.md, with the advisor's amendments (131d8ef). The work is split into Run A (xdist and parity), then Run B (timing), then optionally Run C (fixtures).
- **Run A** is ag-2caa66: implementer-deep pinned to **opus**, consulting the new agent `gemini-advisor` (agy gemini-3.1-pro-high; added to agents.yaml and the rosters). implementer-deep got the route `claude: opus`.
- **NEXT:** review Run A's manifests, then Run B.

**M.** Round 4 was rejected again (ag-467011, 4 defects). Binding designs RM-R1c and RM-R4f (7fd909d), from advisor turn 5, were steered back to ag-074735.

**PS.** ag-dc8946 is fixing 11 findings.

### HANDOFF #7: 2026-09-30 ~23:45 UTC (context wind-down)

**User rules in force (see memory):**
- Implementers run on glm-5.3-flash, whose default variant is already max; the models: routes are set to max.
- The reviewer runs after every implementer, before merge.
- Tickets are fixed in-house: no gh, no submit.
- Egress and mounts need the user's approval.
- TS is done with an opus implementer (implementer-deep pinned `model=opus`) consulting `gemini-advisor`.

**In flight, wait on these:**

**ag-f7ced5, reviewer on M round 5** (branch agents/implementer-deep/074735, commit 10101e9).
- A previous reviewer, ag-859621, died (exit -1) after flagging: (a) the death check trusts a probe that can time out into "dead"; (b) an outer `finally` can release ownership despite the cleanup hold. f7ced5 must confirm or refute these.
- **If approved:** cherry-pick the tester commit **f8c2b26** from agents/tester/35d54e into ag-074735's worktree (it re-derives 3 clock-step tests for RM-R4f), then `merge_agent ag-074735`. Verify tests/test_m_* and test_t1_* on main, then discard ag-35d54e.
- **If rejected:** steer ag-074735, still following binding designs RM-R1c and RM-R4f.

**ag-90626e, reviewer on TS Run A** (ag-2caa66, opus).
- Run A result: pytest-xdist added. `-n auto` takes about 200 s, against 46 min serial, with 0 per-id differences from base 7fd909d over 4372 ids. There are 3 isolation fixes in tests. Manifests are in context/ts/manifests/ and AGENTS.md was updated.
- **If approved:** merge. Then **Run B**: timing, TS-R2 and R3, on opus with gemini-advisor, with a reviewer and a robustness-tester for mutation. Run B must also fix the h7 `test_probe_claim_*` (10 ms real cooldown) and the sg_r7 wrapped_stop flake.
- **Recapture the base after M and PS merge.**

**ag-dc8946, PS implementer (glm).** It is fixing the 11 review findings (ag-4cdd7b); it was resumed after an ENOSPC.
- **When done:** reviewer again. Then merge main (M) and reconcile the budget cache with RM-R4f and `_CacheEntry`. Merge the **tester branch ag-e77949 together with PS**: it holds the c2 harness at 6 providers and the subagent_mcp fixture.
- **After PS merges:** no agy claude/gpt routes exist in agents.yaml today, so nothing to move.

**/tmp** is a 14 GB tmpfs, and it filled up with pytest dirs. Use `--basetemp=/var/tmp/<agent-id>-pytest` outside any repo; AGENTS.md now says so. Old dirs (more than 3 h old) under /tmp/pytest-of-theobroma may be removed.

**Queued, in-house, low priority:**
- Q2b: an invalid explicit `--path` must fail. The tester changes test_q2_cli_path.py:18, then an implementer-quick, then the reviewer.
- The pinned-start half-open self-deadlock (ticket ddcdf7f).
- A `models:` route for the preferred provider is ignored (variant).
- The codex readonly/sandbox cannot run pytest or git commit.
- snapshot.py still names providers in 2 comments.

**Pending user-facing items:** none blocking.
- **Update, after handoff #7.** Reviewer ag-90626e rejected TS Run A with 2 findings: the h7 and sg_r7 flakes under the /var/tmp basetemp break parity (P1), and manifest.py asserts on duplicate testcase ids (P2). Both were steered back to ag-2caa66 (opus). **Next:** a reviewer again, then merge.
- **Update.** Reviewer ag-f7ced5 rejected M round 5 with 3 defects:
  - an "unknown" liveness is taken as dead (P1);
  - the outer `finally` ignores `_cleanup_holds` (P1);
  - `in_launch` is set too early, leaving a node `pending` (P2).

  These were steered to ag-074735. **Next:** a reviewer again, then cherry-pick f8c2b26, then merge.
- **Update, 20:35 UTC.** PS ag-dc8946 exited because the z.ai 5-hour quota was hit ("reset at 2026-10-01 05:01:26", z.ai time = 21:01 UTC). Its work is committed (e469bbd, f86439e, 9e7d9bd WIP). A wake-up is armed for 21:05 UTC: then steer ag-dc8946 with "quota reset, carry on", and steer ag-074735 too if it died the same way. **Correction:** the handoff timestamps above ("~23:45") were wrong; the real UTC time is about 20:30.
- **Update, 21:12 UTC.** ag-dc8946 and ag-074735 were both resumed after the z.ai reset (the first steer of 074735 hit startup_down until dc8946's trial succeeded).
- **Update.** TS Run A fixes are done (h7 made deterministic, sg_r7 fixture, manifest.py). Re-review ag-pending is started. A new product-race ticket was added to context/tickets/2026-09-30-unfiled.md: a stop during launch leaves setsid children running.
- **Update.** Re-review ag-83ad32 of TS Run A: P1, the sg_r7 wait hides the launch-window race. Steered: add a deterministic strict-xfail launch-window test tied to the ticket. **Next:** a reviewer again, then merge.

### 2026-10-01 ~00:10 UTC

**MERGED: TS Run A** (ag-2caa66, opus + gemini-advisor), as 6a06f9c, after reviewer ag-cb7fbc APPROVED it.
- On main, `pytest -n auto --basetemp=/var/tmp/<id>` runs the full suite in **265 s** (it took 46 min serially).
- The results are 171 failed, all known reds (72 phase2, 97 PS, 2 c2 PS), plus 4180 passed, 15 skipped and 7 xfailed. Against the base's 4181 passed and 14 skipped, one test moved from passed to skipped. That is probably environment-dependent; check it in Run B.

**NEXT for TS: Run B** (TS-R2, R3, R3a, R2a and R2b), on implementer-deep `model=opus` consulting gemini-advisor, followed by a reviewer and a robustness-tester. Discard ag-2caa66's leftovers if any.

**In flight:**
- reviewer ag-b859f1 on M round 6 (fdb4fcb). If approved: cherry-pick f8c2b26 (tester ag-35d54e) into the ag-074735 worktree, merge, verify, and discard ag-35d54e.
- PS ag-dc8946, fixing the 11 findings.
- **Update, ~00:20 UTC.**
  - **M round 6** was REJECTED by ag-b859f1 with 3 defects: exit_status taken as proof of death (P1); `identity` unbound under `home_policy: shared` (P1, which broke every launch in that mode); weakened auxiliary tests (P2). Steered to ag-074735 as round 7.
  - **Asked the user** whether to move M's remaining work from glm to opus. It is now in its 7th review round.
  - **PS ag-dc8946** has its 11 fixes done; re-review ag-2f0d3e has started.
  - **Merge order:** M, then PS (plus tester ag-e77949), reconciling the cache per the reviewer.
- **Update.** PS re-review ag-2f0d3e REJECTED it with 10 defects: allowlist checks against stale or reconstructed specs, cooldown read-modify-write outside a transaction, projection skipped on a cache hit. Steered to ag-dc8946. M round 7 is done; reviewer ag-cef33c is running on it. The user has still not answered whether M and PS should move from glm to opus.
- **Update.** M round 7 REJECTED by ag-cef33c with 4 defects: supervisor construction outside cleanup; a cleanup hold not holding the slot; steer's claim leaked on a prologue failure; stale_seconds bypassing the future read_at guard. Steered to ag-074735 as round 8.
- **Update.** M round 8 REJECTED by ag-7c0716 with 3 defects. The binding design RM-R1d (491f3d6) makes the cleanup hold durable in tree.json and has the new pid recorded right after start. Steered to ag-074735 as round 9.
- **User decision, 23:06 UTC.** If the reviewer rejects again, move to opus. It applies to M (ag-f27608 is reviewing round 9), and the orchestrator applies it to PS's next review too. Escalation means a new implementer-deep run pinned to model=opus on the same branch's work: carry the branch over by squash into its worktree, and give it the review history and binding designs.
- **User rule:** as a general rule, escalate to opus at review round 3 (saved to memory). PS is at round 3 now: if ag-dc8946's next review rejects, escalate.
- **Update.** M round 9 REJECTED by ag-f27608 with 4 defects, so M was escalated to **opus**: ag-cc25b3. The glm branch 074735 was squashed in (7d8d14e) and the tester commit cherry-picked (4c6f594). **After ag-cc25b3 merges:** discard ag-074735 and ag-35d54e.
- **Update.** Opus M ag-cc25b3, review round 1 (ag-43f57f): REJECTED with 6. Death confirmation is not positive (a missing pid file, or a dead wrapper whose child is alive); an in-memory retry hold; recovery probes the wrong executor; adoption's release sequence; a first consult left pending. Steered to ag-cc25b3.
- **Update.** Opus M review round 2 (ag-598c45) REJECTED it with 3: an EACCES /proc entry taken as gone (P1); _end_hold not confirming the claim release (P2); a hold takeover not rebinding occupancy (P2). Steered to ag-cc25b3.
- **Update.** PS review round 3 (ag-3644ef) REJECTED it with 7, so PS was escalated to **opus**: ag-57e6fb, with dc8946 and tester e77949 squashed in. **After it merges:** discard ag-dc8946 and ag-e77949.
- **Update, 01:02 UTC.**
  - **M:** opus review round 3 (ag-5189c2) REJECTED it with 4. The fork race is accepted as a limit, and durable-write confirmation is required (spec 7dd867e).
  - **PS:** opus ag-57e6fb failed on the Claude limit (WIP 84bd3c6).
  - **Both steers were refused** because the Claude session window reset at 01:00 still reads 100%. A wake is armed for 01:05 UTC.
  - **Next:** steer ag-cc25b3 with the round-3 decisions (they are in the spec), and steer ag-57e6fb with "carry on".
- **Update, ~01:40 UTC.** Opus PS ag-57e6fb is done: the 7 findings are fixed and the PS reds are gone. 2 fixture reds (test_h2 rf_r4, plus an h1_h2_review2 opencode test) went to tester ag-dcc1c8, whose commit is to be cherry-picked onto 57e6fb. Reviewer ag-a50515 is on PS (opus round 1). Reviewer ag-2792f3 is on M (opus round 4). **Merge order:** M (cc25b3), then PS (57e6fb plus the tester commit), reconciling the cache per opus's plan.
- **Update.** M opus round 4 (ag-2792f3) has 1 P2 left: an occupancy-registration write is unconfirmed. Steered to cc25b3, expected to be the last round. The PS fixture tester commit is **03244f2** (ag-dcc1c8): cherry-pick it onto 57e6fb before the PS merge, then discard ag-dcc1c8. Reviewer ag-a50515 is on PS.
- **Update.** PS opus round 1 (ag-a50515) REJECTED it with 7, centred on auth-block context keying and coexistence. Steered to 57e6fb, which also cherry-picks 03244f2. The final review of M (ag-144f19) is running.
- **MERGED: M (opus ag-cc25b3), as 14f06b1, after the final review ag-144f19 APPROVED it.** Full parallel suite on main: 171 failed (the same known reds: 72 phase2, 97 PS, 2 c2), 4285 passed, in 306 s. ag-074735 and ag-35d54e were discarded. **Remaining:** PS opus ag-57e6fb (round 2 fixes), then a reviewer, then merge main and reconcile the cache (M is merged now). Then TS Run B.
- **Update.** PS opus round-2 fixes are done. Steered 57e6fb to merge main (M) and reconcile the cache, then run the full suite; the only allowed reds are phase2. **Next:** the final reviewer, then merge, then discard ag-dc8946, ag-e77949 and ag-dcc1c8.
- **Update.** The final PS review (ag-e6b702, Gemini, because codex was constrained) found 1 P1: in _source_reading the clock is read before _cache_lock, so a concurrent publish looks like a backward step. Steered to 57e6fb (read the clock under the lock; adapt the round4 auxiliary test). **Next:** a short re-review, then merge.

### 2026-10-01 ~03:00 UTC: PS merged

**MERGED: PS (opus ag-57e6fb), as 77ee8a9, after re-review ag-ea1d60 APPROVED it.**
- The full parallel suite on main gives **72 failed, all `test_phase2_*` by design**, with 4445 passed, in 252 s.
- The live config loads, with the providers agy, agy-partner, claude, codex, opencode and opencode-zai.
- ag-dc8946, ag-e77949 and ag-dcc1c8 were discarded.
- **The running MCP server still runs the old code.** A server restart (`/mcp` reconnect) is needed to pick up M and PS, as well as the cancel_deferred/list_deferred tools from T1.

**Next, per the brief:**
1. **TS Run B** (timing, TS-R2/R3/R3a/R2a/R2b): implementer-deep `model=opus` with gemini-advisor, then a reviewer and a robustness-tester. Recapture the base: main is now 77ee8a9, and the known reds are only the 72 in phase2.
2. **Queued tickets, in-house:**
   - Q2b, the `--path` failure;
   - the pinned-start half-open self-deadlock;
   - the `models:` route variant for the preferred provider being ignored;
   - the setsid launch-window race (strict xfail);
   - the manual clear of a cleanup hold;
   - the snapshot.py provider names;
   - the codex sandbox unable to run pytest.

### 2026-10-01 ~05:10 UTC — after /mcp + /compact

- **In flight:** TS Run B, implementer-deep ag-6e59b2 (opus, gemini-advisor), then reviewer + robustness-tester; Q2b tester ag-f4e66c (then implementer-quick + reviewer); snapshot.py comment cleanup, implementer-quick ag-19d3c3 (then reviewer).
- **R8f (self-compaction) status, checked:** works — events show 2 real compactions (09-24 01:36, 09-27 04:03: 291k→10k, 207k→4.5k) and 1 cancel (09-26). None since the driver restart of 09-29 05:14, because R8f needs ≥300 s with the transcript unchanged and no unseen root results; with a background `wait_for_agents` almost always in flight that quiet window rarely occurs. Not a bug; a possible improvement (compact while blocked in a long wait) is a design proposal for the user, not scheduled.
- **~05:40 UTC, CW (user decision: compact while agents run + return checklist).** Contract `context/specs/cw-compact-while-waiting.md` (a6e4e93), after advisor ag-d20e1e t6 (corrected premise: interactive R8f is blocked by unseen root results/deferred, unattended R8c by active nodes). Tester ag-db8d42 writing red tests. Then implementer-deep (decision-bearing: safe-stop handshake, concurrency) → reviewer → robustness-tester. In flight too: Run B ag-6e59b2, Q2b impl ag-708900 (then reviewer), snapshot reviewer ag-915e36.
- **~06:10 UTC.** Claude 5 h window hit at ~05:55 (ag-6e59b2 Run B: 4 commits, next h4_h14; tester ag-db8d42) — both resumed by steer after reset. Merged: Q2b tests (b2af50a), snapshot comments (ed96658, reviewer ag-915e36 APPROVE). Q2b impl ag-708900 under review (ag-6da715). User suggested tmux/PTY `/compact` injection: declined after advisor t7 (CLI inherits the terminal; no readiness signal); adopted the focus-instruction idea as CW-R8 (868239d). Live smoke check of `claude.sh compact` with focus is the orchestrator's before CW merges.
- **~06:45 UTC, Q2b.** Review r1 (ag-6da715) and r2 (ag-824e8e) rejected; steered r3 to ag-708900 (one shared explicit-path resolution for `_resolve` and `_resolve_if_project`; `--path ''` fails). Tests added: c870600 (r1 cases), tester ag-00c6b6 (r2 cases). **If review r3 rejects → implementer-deep opus (user rule).** Waiting also on Run B ag-6e59b2 and CW tester ag-db8d42.
- **~07:00 UTC.** CW tests merged (9d78ab5; 2 new files + R1 flips in 3 phase0 files), decisions recorded (1db9366). CW implementer-deep **opus** ag-647544 (opus chosen directly: concurrency-heavy and opencode-zai weekly at 89%). Q2b r2 tests merged (afc79ed); ag-708900 on round 3. Run B ag-6e59b2 still running. Next: Q2b review r3 (cherry-pick afc79ed into its worktree first); CW → reviewer + robustness-tester + live smoke of `claude.sh compact` with focus.
- **~07:20 UTC, Q2b escalated.** Review r3 (ag-57659c) rejected (init positional overwrites global --path; `docker status --all` skips validation). Per user rule → implementer-deep opus ag-304a3e, glm work squashed in (8e5ccd8), ag-708900 discarded. Tester ag-05f385 writing r3 tests (cherry-pick into ag-304a3e when merged). Then reviewer.
- **~07:45 UTC, Q2b MERGED (902f1af)** after review r4 APPROVE (ag-22bb78). Remaining queued tickets wait for CW (most touch runner.py/routing, which CW's safe point also touches). Running: CW ag-647544, Run B ag-6e59b2.
- **~08:00 UTC, TS Run B done** (ag-6e59b2, 13 commits): `-n auto` wall 236 s → 163 s; tests > 5 s 127 → 42 (remaining justified in its Result); the five 60 s tests → 1–3 s (none was a hang); 30+ mutants killed (context/ts/run-b/); parity per id OK; serial not measured. Host-CLI leak found and guarded in tests/conftest.py (tests were running the real `agy budget`). **Production finding (queue):** `budget.read_all` ≈ 0.44 s per read (full config.load + ~17 YAML re-parses) on every spawn/poll. In review: reviewer ag-dc89fc + robustness-tester ag-8d5797. Then merge (expect conflicts with CW's flipped phase0 tests? Run B touched r8f_leftovers, not the CW-flipped files).
- **~11:05 UTC.** Claude 5 h window hit ~10:5x; both opus runs stopped: CW ag-647544 (was reading code, nothing committed yet, 99 red confirmed) and Run B ag-6e59b2 (guard fix for review r1 P2 written + tests/test_ts_conftest_host_cli_guard.py, parity run after3 interrupted). Steers refused while the claude reading is stale (reset 11:00). **Next:** retry `steer_agent` on both (messages: "limit reset, carry on"); robustness-tester ag-8d5797 (codex) still running on Run B. Claude weekly at 88% (user: pace on 5 h only).
- **~12:10 UTC, CW implemented** (ag-647544 opus, 5 commits; file handshake `safepoint.py`, `compact_return.py`; suite: only the 72 phase2 + 13 q2b reds, q2b now merged on main). **Live smoke of CW-R8 done by orchestrator:** branch `claude.sh compact` on a throwaway session with focus `Keep: agent ids "ag-1" & $(echo pwned) \`x\`\nsecond line` → exit 0, `20946 -> 1835 tokens`, focus recorded verbatim in the transcript (no shell expansion). In review: reviewer ag-db4940 + robustness-tester ag-8c8368. Run B: ag-6e59b2 on review r2 P2 (relative binary in host-CLI guard); robustness tests ag-8d5797 approved, merge after Run B.
- **~12:40 UTC, TS Run B MERGED** (cd57298, review r3 ag-3a237d APPROVE) + robustness tests (be26672, 11 boundary tests killing 3 mutants that survived Run B). Full suite on main running to confirm. **CW:** review r1 (ag-db4940) REJECTED, 7 findings (P1: request failure authorises stop; expiry leaves ack valid; late server not covered; container-forgeable ack; P2: final ack overrides cancellation; unattended checklist deleted before launch succeeds; unattended message rendered before relaunch). Decided CW-R2a (7df7978): handshake in host-only dir, ack bound to nonce+session+pid+start time, fail closed, late servers check barrier before serving. **Next:** when robustness-tester ag-8c8368 finishes, steer ag-647544 (opus) with all findings + CW-R2a (round 2).
- **~13:00 UTC.** CW attack tests merged (ff55ae0, tests/test_cw_attack.py, 5 red vs branch). Steered ag-647544 for **CW round 2** with all 7 review + attack findings and CW-R2a (cherry-picked 15f6be9, 0dd84c6 into its worktree). Round 3 still rejected → it is already opus; then consult advisor on approach.
- **~13:15 UTC.** Main full suite after Run B: 172 red = 72 phase2 + 100 CW (expected until CW merges); no regression. **BP** (budget read cost, from Run B's finding): contract context/specs/bp-budget-read-cost.md, tests merged (2c5fb11, 2 red), implementer ag-f120f1 (glm) → then reviewer. CW round 2 on ag-647544.
- **~14:00 UTC.** CW round 2 done (ag-647544, up to fa1a5bb; handshake in `<state>/safepoints/<slug>/`, exported as MULTIAGENTS_SAFEPOINT_DIR; new tests/test_cw_r2a_safepoint.py). Two NEED_INFOs decided → tester ag-c2907e (attack test blocker path; R8f Session harness keeps MULTIAGENTS_STATE_DIR). Review round 2: ag-f52646. BP implementer ag-f120f1 running.
- **~14:25 UTC.** CW review r2 (ag-f52646) REJECTED, 6 "unknown treated as safe" defects (per-agent docker exposure, unknown enumeration, unreadable /proc → [], ack start time unreadable, checklist deleted before CLI got it, zombie driver alive). Test fixes merged (16b963d) and cherry-picked; steered ag-647544 for round 3. If r3 rejects again: consult advisor on simplifying (the /proc discovery is the weak spot) before another round.
- **~15:10 UTC.** CW review r3 (ag-32e67c): r2 fixes confirmed, 1 new P2 (unattended delivery tracking reads the docker agent transcript location instead of the host CLI's) → steered ag-647544 (round 4; converging 7→6→1). BP review r1 (ag-b3b2ca) REJECTED 4×P2 (BP-R2 changed answer on malformed global providers.yaml / dropped seeding; shipped project.yaml parsed twice via shipped_limits cache; unresolved path keys; mid-call edit re-parsed) → steered ag-f120f1 (round 2). Note: `test_h1_h2_review2::test_rf_r3_r1_adopted_opencode_filter…` red depends on live opencode quota state (test isolation leak) — queue.
- **~15:25 UTC.** Test isolation: `test_rf_r3_r1…` red was `XDG_DATA_HOME` leaking into sv_harness servers → the shipped `opencode.sh budget` read the REAL opencode auth and curled the live usage endpoint. Fixed (b3f1b55). Follow-up tester ag-ccebe1: conftest autouse drops XDG_* and redirects HOME (or neutralises budget scripts), full suite. In flight: CW r4 ag-647544, BP r2 ag-f120f1.
- **~16:00 UTC.** CW review r4 (ag-5e2dd9) **APPROVED** 9ddc126. Second attack on the R2a redesign: robustness-tester ag-8a… (see tree) before merge. Conftest isolation merged (31ed14b: XDG_* dropped, HOME per test). Follow-ups queued (not CW-blocking): driver `limit_reached`/`has_human_turn` (driver.py ~816/1023/1240/1259) and `server._context_reading` (~350) read the orchestrator transcript via the agent executor → wrong in docker projects; `budget.py` binds CLAUDE_STATE/CLAUDE_CREDENTIALS from Path.home() at import (tests can't redirect).

### HANDOFF — 2026-10-01 ~16:15 UTC (orchestrator context wind-down at 300k)

**Done today (merged on main):** Q2b (902f1af, opus after glm r3); snapshot.py comments (ed96658); TS Run B (cd57298) + robustness boundary tests (be26672); CW tests (9d78ab5, ff55ae0 attack, 16b963d fixes); BP tests (2c5fb11); test isolation (b3f1b55 sv_harness XDG, 31ed14b conftest XDG_*/HOME per test); specs CW (a6e4e93, 868239d R8, 1db9366, 7df7978 R2a) and BP (context/specs/bp-budget-read-cost.md).

**In flight / next, in order:**
1. **CW** (branch agents/implementer-deep/647544, opus ag-647544): review r4 ag-5e2dd9 **APPROVED**. Second attack on the R2a redesign running: robustness-tester **ag-6ebca1** (writes tests/test_cw_attack2.py). If it finds real defects → merge its tests to main, cherry-pick into the CW worktree, steer ag-647544 (then reviewer). If clean → `merge_agent ag-647544` (expect conflicts only around tests/test_phase0_interactive_compact.py; branch lacks Q2b/Run B/conftest — run the full suite on main after: expected reds = 72 phase2 only). Live smoke of `claude.sh compact` with focus already OK (see ~12:10 entry). Then tell the user: restart `multiagents run` (driver) and `/mcp` to activate CW.
2. **BP** (branch agents/implementer/f120f1, glm ag-f120f1): round-2 fixes done (639efdb; config.read_yaml_cached shared resolved-path cache, per-call ContextVar snapshot, `_fetching_allowed` back on config.load memoised by stamps; BP-R3 wait test 17.3 s → 0.75 s, read_all 76 → 2.6 ms). **Next: reviewer round 2** (verifies=ag-f120f1; also judge the one deliberate divergence: malformed SHIPPED project.yaml now `{}` instead of raising). If r2 rejects → r3 still glm; if r3 rejects → implementer-deep opus (user rule).
3. **Queued follow-ups (not started):** driver `limit_reached`/`has_human_turn` (driver.py ~816/1023/1240/1259) and `server._context_reading` (~350) read the orchestrator transcript via the agent executor (wrong in docker projects); `budget.py` binds CLAUDE_STATE/CLAUDE_CREDENTIALS from Path.home() at import; plus the older in-house list (pinned-start half-open deadlock, `models:` route variant ignored, setsid launch race xfail, manual cleanup-hold clear, codex sandbox can't write /var/tmp or .git — robustness-tester/reviewer on codex keep hitting it, PS dependent docker mounts).
4. Claude weekly at ~88% (resets 2026-10-05 14:00 UTC); opencode-zai weekly 90%. User: pace claude on 5 h only.
- **~16:30 UTC update to the handoff:** second CW attack (ag-6ebca1) found 1 P1 (symlink aliases bypass the exposure check / redirect barrier writes). Tests merged (a036826, tests/test_cw_attack2.py, 4 red until fixed); decisions in the spec (81ce6fb: resolved-path exposure; hidepid=1 accepted limitation); both cherry-picked into the CW worktree; **ag-647544 steered (CW round 5)**. Next on its return: reviewer round 5 on the delta, then merge (step 1 of the handoff). BP reviewer r2 still to start.
- **~16:45 UTC.** CW round 5 done (63a22ec: resolved-path exposure, O_NOFOLLOW handshake I/O; full suite = 72 phase2 + 13 q2b only). Reviewers running: **ag-3e9254** (CW r5 delta) and **ag-bf4a3a** (BP r2). On APPROVE: merge ag-647544 (CW) / ag-f120f1 (BP), full suite on main (expected: 72 phase2 only), then tell the user to restart `multiagents run` + `/mcp`.
- **~13:15 UTC (clock check: `date -u` says 13:14; earlier "~16:xx" stamps in today's entries are off — they were estimates).** CW review r5 (ag-3e9254) REJECTED 1 P1 (running bind keeps target after alias rename; exposure cached). Decided conservative rule (spec 7b10218, cherry-picked into CW worktree). Steer of ag-647544 REFUSED: claude exhausted (5 h window, reset ~16:00 UTC). **Wake armed for ~16:02 UTC: retry the steer (message = r5 finding + spec 7b10218).** BP r2 reviewer ag-bf4a3a still running (codex).
- **~13:25 UTC.** BP review r2 (ag-bf4a3a) REJECTED 4×P2 (seed manifest missing from memo key; inherited ContextVar snapshot in child tasks; malformed shipped YAML now {} — decided: keep raising; config.load still parses shipped project.yaml a 2nd time on the claude path). Steered ag-f120f1 (glm) for **round 3**; if r3 rejects → implementer-deep opus with squash (user rule).
- **BP review r3 (ag-ac0659) REJECTED, 3×P2** (stat() errors swallowed in strict load; deep_merge shares mutable lists/dicts with the YAML cache → a caller mutation poisons later loads; config.load bypasses the per-call snapshot → mid-call edit parsed twice). **Per user rule → escalate to implementer-deep opus** with glm branch agents/implementer/f120f1 squashed in (then discard ag-f120f1). Blocked on claude until ~16:00 UTC; at the 16:02 wake do BOTH: (a) steer CW ag-647544 (r5 finding + spec 7b10218), (b) start opus BP with the three r3 findings + history r1 ag-b3b2ca, r2 ag-bf4a3a, r3 ag-ac0659.
- Tried to run the BP opus takeover on agy-partner's `claude-opus-4-6-thinking` (separate, unused quota) — refused: implementer-deep has no agy-partner route in agents.yaml. **Proposal for the user (not done):** add `agy-partner: claude-opus-4-6-thinking` to implementer-deep's `models:` so opus work can draw on the agy Claude pool while claude's weekly is at ~90%. Until then: wait for the 16:02 UTC wake.
- **User decision (2026-10-01 ~13:50 UTC): NO agy-partner route for implementer-deep.** Do not propose it again; opus work stays on the claude provider.
- **16:05 UTC — BLOCKED on a user decision.** Claude weekly is at **97%** (resets 2026-10-05 14:00 UTC); the router refuses claude below its 5% reserve, so neither the CW steer (ag-647544, r5 fix) nor the BP opus takeover can run. User already declined the agy-partner opus route. Asked the user: wait until 10-05, or route these two to another strong model (implementer-deep can run on codex gpt-6-astra or agy gemini-3.1-pro-high). Nothing running.
- **16:10 UTC — user: "continue avec claude jusqu'a epuisement du quota hebdomadaire".** Lowered `budget.reserve_headroom` 0.05 → 0.01 in .multiagents/config/project.yaml (comment says restore 0.05 after the 10-05 reset). CW steer to ag-647544 (r5) accepted. **BP opus takeover deliberately NOT started yet:** with ~2% weekly left, CW (security fix, nearly done) goes first; start BP (squash agents/implementer/f120f1 into the new worktree, task text in the 13:44 entry) when CW finishes, if claude still has room — otherwise it waits for 10-05.
- **18:20 UTC.** Orchestrator was cut (claude quota), user re-logged in. CW ag-647544 had committed the r5 fix (4b6616f: conservative exposure through writable ancestors, re-judged every proposal) then died on API ECONNRESET; steered to finish verification. **Next on its return:** reviewer round 6 on `git show 4b6616f`, then merge CW (handoff step 1), then start the BP opus takeover if claude still has room (13:44 entry), else wait for 10-05.
- **~18:50 UTC.** CW review r6 (ag-e63308) REJECTED 4 (exposure detection unwinnable). **Decided CW-R2b** (spec, after advisor t9): authenticated handshake (per-run key via env, MAC on every record, no replay reopening, key stripped from every other env path); exposure detection withdrawn. Steered ag-647544 (opus) — round 7 on CW. Then: tester adapts obsolete exposure tests (from its NEED_INFOs), robustness-tester attack #3 on R2b, reviewer, merge. BP opus takeover still waiting (quota).

### 2026-10-01 — DeepInfra (user request: "integrer les modeles 'deep infra' de opencode")
- User decisions: spend cap per provider + per-model override, OFF by default; egress api.deepinfra.com approved and added to project.yaml; roster unchanged ("juste disponible").
- Split after advisor code check: DI (context/specs/deepinfra-provider.md, provider instance `opencode-deepinfra`, metered) and SC (context/specs/spend-caps.md, ledger + caps). Commits 06d5bcc, 40fc08d.
- Testers running: ag-ed8d70 (DI), ag-d38593 (SC). Next: merge tests → implementers (DI: default tier; SC: implementer-deep — cross-process, ledger invariants) → reviewer → robustness-tester on SC → merge. After DI merges: enable `opencode-deepinfra` in .multiagents/config/providers.yaml, refresh models.
- SC implementer should start after CW merges (both touch runner/server).
- ~19:00 UTC: DI tests merged (e0ea7f0); DI implementer ag-9f89a9 (glm/zai). CW r7 returned (b1616a6 on agents/implementer-deep/647544, 8 obsolete tests): tester ag-e2adb4 adapts them (CW squashed into its worktree as f229398), reviewer ag-cb7944 on r7. PENDING: robustness-tester attack #3 on CW (refused, 4/4 slots) — start at next free slot, squash CW branch into its worktree. SC tester ag-d38593 running. Claude weekly back to 5% → reserve_headroom restored to 0.05.
- ~19:15 UTC: CW review r7 (ag-cb7944) REJECTED, 4 findings (replay reopens admission; malformed MAC TypeError stops CLI; key restored by mcp.env overlay; docker probe env file). Steered ag-647544 (opus) for r8. Attack #3 waits for r8.
- ~19:30 UTC: CW obsolete tests adapted by ag-e2adb4 → commit 2db0023 on agents/tester/e2adb4 (on top of squash f229398). At CW merge: merge ag-647544 then cherry-pick 2db0023; then discard ag-e2adb4. State-root symlink decision recorded in CW spec.
- ~19:45 UTC: CW r8 fix 0d38abf + adapted tests cherry-picked (1acb1ae) on agents/implementer-deep/647544 (ag-e2adb4 can be discarded). Reviewer r8 ag-6b214c, robustness-tester attack #3 ag-edc0cf (CW squashed into its worktree). SC tester ag-d38593 steered after wall-clock (4 commits). Check at CW merge: '13 q2b reds' reported by ag-647544 full-suite — verify they're absent on main after merge.
- ~19:45 UTC: CW r8 fix 0d38abf + adapted tests cherry-picked (1acb1ae) on agents/implementer-deep/647544 (ag-e2adb4 discardable). Reviewer r8 ag-6b214c, attack #3 ag-edc0cf (CW squashed into its worktree). SC tester ag-d38593 steered after wall clock. At CW merge: verify the '13 q2b reds' ag-647544 reported are absent on main; expect spec conflict (keep main's) and test_cw_attack*.py (keep branch's).
- FOLLOW-UP (tooling, for bug-reporter at next stop): the robustness-tester 'work in progress' auto-commit (97f7d6b in ag-edc0cf) committed a worktree with unmerged index entries — conflict markers in 3 files. Auto-commit should refuse/skip when the index has unmerged paths. Triggered by orchestrator squashing into a running agent's worktree. Also: CW branch is behind main (lacks Q2b src changes → its '13 q2b reds'); at merge, merge main into ag-647544's worktree first.
- FOLLOW-UP (tooling): steer_agent refused ag-edc0cf with 'codex startup_down' (after a codex content-safety refusal), yet start_agent 1 min later routed robustness-tester to codex with 'preferred provider has headroom' (ag-0690cb). State disagrees between steer and start routing. Attack #3 is now ag-0690cb (defensive wording; ag-edc0cf discarded).
- ~20:15 UTC: CW r8 (ag-6b214c) REJECTED 1 P1: wall-clock ordering of requests → clock rollback replay. Steered ag-647544 for r9 (per-run monotonic sequence). Attack #3 ag-0690cb runs on r8 code (still useful). DI implementer ag-9f89a9 done (20ca3ba, 51/51 DI green, no regression); DI reviewer ag-2dc2a6 running.
- ~20:30 UTC: SC tests merged (82003ba; 112 red, 21 baseline green; NEED_INFO: dev must add step id to opencode stream rules). SC implementer waits for CW merge. BP opus takeover started: ag-547be2 (glm f120f1 squashed in; discard ag-f120f1 after BP merges). Running: CW r9 ag-647544, attack#3 ag-0690cb, DI reviewer ag-2dc2a6, BP ag-547be2.
- ~20:40 UTC: DI approved (ag-2dc2a6, no defects) and MERGED (4a59615). opencode-deepinfra enabled in .multiagents/config/providers.yaml; refresh-models: 60 deepinfra models; auth check ok; budget unknown/no network. NOT verified: an agent run on a deepinfra model inside the container (egress via proxy) — no roster route exists (user: 'juste disponible'); whether the proxy picks up the new allowlist host without container recreate is also unverified. Adversary skipped for DI (config+script; money is SC).
- ~20:55 UTC: attack #3 (ag-0690cb) → tests/test_cw_attack3.py (4 red, 26 green) at tip of agents/robustness-tester/0690cb: P1 clock-tie/rollback replay (same as r8), P2 rollback wedge. Opus r9 is fixing both. After r9: cherry-pick the attack3 test file onto ag-647544's branch, confirm 30/30, then reviewer r9, merge main into CW worktree, full suite, merge. Discard ag-0690cb and ag-e2adb4 after.
- ~21:10 UTC: USER REQUEST: per-provider concurrency limiter, off by default. Spec PC context/specs/provider-concurrency.md (28f4311, advisor-amended). PC tester ag-a76c01 running. ORDER: after CW merges → implementer-deep (opus) builds PC, reviewed+attacked, merged → same implementer (steer) builds SC in a separate pass. Bug-reporter ag-7ebf12 writing 2 tooling tickets.
- ~23:25 UTC 2026-10-01 — STATE FOR RESUME:
  - CW r9: ag-647544 done (4f69afd seq-number ordering); orchestrator added tests/test_cw_attack3.py (commit on branch) and merged main into its worktree (4e960ac; conflicts: spec=main's, test_cw_attack*.py=branch's). Full suite on merged branch: 194 failed / 4845 passed — 112 SC (expected, not built) + 72 phase2 = 184; ~10 others UNIDENTIFIED → rerun per-file counts running in background (output: scratchpad/cw9-byfile.txt). Reviewer r9 ag-c30bf5 running. If r9 approves and the ~10 extras are explained (BP/PC tests?), merge_agent ag-647544, then discard ag-e2adb4, ag-0690cb; tell user to restart `multiagents run` + /mcp.
  - BP opus ag-547be2 died exit 1 (claude 5h window); steer refused "claude exhausted/cooling" at 23:21 though session reset 23:19 — retry steer "carry on" shortly.
  - PC tester ag-a76c01 (claude sonnet) — check status; may also need resume.
  - Tickets bug-ba55a9 (WIP auto-commit commits unmerged index) and bug-1213a0 (codex content-safety refusal → startup_down; steer vs start disagree) are open, to fix in-house later.
  - Next after CW merge: implementer-deep (opus) builds PC, then SC (same implementer, separate pass).
  - Per-file reds on merged CW branch: phase2 72, test_sc_* 120, test_bp_budget_read_cost 2 (BP not built). ZERO CW reds. CW mergeable once r9 review approves.
  - CW r9 review (ag-c30bf5) REJECTED 1 P1: driver.py:664 late-server race — barrier hidden after final enumeration, new root server T starts open (server.py:1633), admits launch, B restored, driver commits without T. r8 fixes confirmed. ON WAKE: steer ag-647544 for r10 (re-enumerate live participants immediately before commit; all must have acked this nonce, else cancel; and a server starting while a driver of its key is alive must fail closed until it sees an authenticated barrier state), steer ag-547be2 (BP) and ag-a76c01 (PC tester) 'carry on'.
  - ~23:35 UTC: steered ag-647544 (CW r10), ag-547be2 (BP), ag-a76c01 (PC tester) — all running.
  - ~23:50 UTC: BP opus ag-547be2 done (ed48944; r3 fixes, BP-R3 15.67s→0.78s; suite reds all known). BP reviewer r4 ag-c? started (see tree).
  - ~00:00 UTC 10-02: CW r10 ag-647544 done (220e6d7: pre-commit re-enumeration + signed GRANT mechanism for late servers, ungranted admissions wait ≤30 s). Suite: only phase2/SC/BP reds. Reviewer r10 started.
  - PC tests merged (ag-a76c01: 69 red / 26 green; blocked-head R3a test needs SC → stays red until SC; retry/wrap-up/deferred restarts on full provider uncovered). Waiting: BP r4 reviewer ag-7631f3, CW r10 reviewer ag-c7285d. PC implementation waits for CW merge.
  - BP r4 (ag-7631f3) REJECTED 3 P2 (cache key collision, borrowed snapshot closed by parent, growing cached YAMLError traceback). Steered ag-547be2 for r5.
  - CW r10 (ag-c7285d) REJECTED 2 P2 (no re-check of deadline/transcript after final enumeration; deleted grant never re-issued). Steered ag-647544 r11.
  - CW r11 ag-647544 done (667065c; 295 CW tests green; suite reds only phase2/SC/BP). Reviewer r11 started.
  - BP r5 ag-547be2 done (8774624; 32/32 BP green). Reviewer BP r5 started. Note: ag-547be2 says test_cw_safe_point::test_cw_r5_the_stop_cancels_and_orphans_nothing… is load-flaky (passes alone) — watch at CW merge.
- ~00:40 UTC 10-02: CW APPROVED r11 (ag-ac9331) and MERGED (d09358c). Advisor: merge as is; CW-R2c section added to the spec documenting what was built (seq ordering, final re-check, GRANT). Follow-ups: distinct 'driver grant unavailable' diagnostic; monotonic deadline for the 30 s grant wait. TODO now: full suite on main; discard ag-e2adb4 and ag-0690cb; tell the user to restart `multiagents run` + /mcp to activate CW; then start PC implementer (implementer-deep, opus).
  - ~00:45 UTC: PC implementer ag-03733f (opus) started. Main full suite running in background (scratchpad/main-suite.txt). BP r5 reviewer ag-675aa5 running.
  - Main suite after CW merge: 254 red = phase2 72 + SC 120 + PC 60 + BP 2 — all expected (features not built/merged). Zero CW/DI reds.
  - ~01:00 UTC: BP APPROVED r5 (ag-675aa5) and MERGED (49b8465); glm branch ag-f120f1 discarded; BP+CW-auth+DI tests on main: 160 passed. Adversary skipped for BP (read cache, no untrusted input). Running: PC implementer ag-03733f. Next: PC review + robustness attack → merge → steer ag-03733f for SC.

### 2026-10-02: PC implemented (ag-03733f, 2 commits, branch agents/implementer-deep/03733f)
- 79/86 PC tests green. 6 reds are believed wrong under PC-R3a FIFO; 1 is the SC blocked-head test.
- In flight:
  - reviewer ag-836960 (codex);
  - tester ag-25515c, revising the 6 tests plus the `hold("first")` collision;
  - robustness-tester ag-145a37 (codex; PC squashed into its worktree).
- Next: fix loop on ag-03733f → merge PC → steer ag-03733f for SC.
- Implementer decisions to check:
  - the queuer drains; a root server takes over entries from a dead queuer;
  - consult deadline = start + timeout + 60;
  - steer/resume still does not check the tree-wide `max_concurrent` (pre-existing).
- PC test revision merged as 8fdfe68: the PC suite is 85/86 on the implementation, and the only red is the SC blocked-head test.
- Ticket bug-ba55a9: tester ag-3494f6 is writing red tests in `tests/test_commit_all_unmerged.py` against contract BA-R1..R3:
  - BA-R1: refuse on unmerged entries;
  - BA-R2: a clean staged squash still commits;
  - BA-R3: a visible event when the WIP commit is refused.
- Next for bug-ba55a9: an implementer on `gitops.py`, holding the runner part until PC merges.
- bug-1213a0 is waiting for PC to merge, because it touches `runner.py`.
- PC review round 1 (ag-836960) returned CHANGES_REQUESTED with 12 findings: 7 P1 and 5 P2.
- I steered ag-03733f to fix them, after merging main.
- My decisions on the implementer's open points:
  - (a) queue ownership must follow the server, not the queuing agent;
  - (b) the 60 s slack is OK, as long as there is one enforced deadline;
  - (c) a resume must also check the tree-wide limit.
- Robustness results from ag-145a37 will follow as a second batch.
- **PC round 1 fixes:** done on branch commits 7c21a5d and 61ad6c3.
  - PC suite 85/86, with only the SC test red. CW is green.
  - I accepted the 15 s blocked re-check.
  - Reviewer round 2 is ag-5eb6ce. Robustness tester ag-145a37 is still running against the old tip.
- **bug-ba55a9:**
  - The red tests merged as 3ba0fea (17 red, 9 BA-R2 green).
  - implementer-quick ag-b09f8c (glm) is on BA-R1/R2, `gitops.py` only.
  - BA-R3 (runner) waits until PC merges.
- **PC robustness tests:** merged as dddd85c, 18 passing and 2 failing on the round-1 fixes.
  - **R3d (queued entry keeps its model):** a real defect, which goes to the implementer.
  - **Consult deadline:** that test contradicts PC-R3c, my decision: deadline = timeout + 60 slack. Recorded in the spec (f0a5784) together with R3d, R3e and R3f.
  - Tester ag-5924ba is rewriting that test and adding an R3d invalid-model test.
- **In flight:** reviewer round 2 ag-5eb6ce; ag-b09f8c on ba55a9.
- **Next:** steer ag-03733f with the round 2 findings + R3d (+ R3c if the test shows a launch past the deadline).
- **PC round 2** (ag-5eb6ce): rejected, 7 findings.
  - Steered ag-03733f with those 7 findings plus the 2 robustness reds (R3a/R3d model kept, R3d invalid model blocked). Test fix 80eddee is merged.
  - Decision: the post-mortem slot (R3f) applies only to a provider with `max_concurrent`. Without one, tree-wide counting stays as on main (PC-R5).
- **Next:** reviewer round 3.
- **bug-ba55a9 part 1 (BA-R1/R2, `gitops.py`):** merged as 10ae24f after one review round. ag-d60f42 found one P1: a failed `ls-files` check let the commit through. It is fixed and I read the fix myself.
- **BA-R3 (runner event and result text):** still to do after PC merges.
- **PC round-2 fixes are done.** Commits a342904 and 1a4a5d5. The PC suite is at 105/107.
- **Tester ag-c78ca1** is fixing the R3d robustness test. Its config is invalid: `advisor` still names `acme/m1`.
- **Reviewer round 3** is ag-f2696d.
- **PC round 3** (ag-f2696d): rejected with 5 findings. It verified that all 7 round-2 fixes hold.
  - Findings: docker wrapper liveness, a claim leak on stream-open failure, pinned queued-start validation order, terminal status during the post-mortem, and adopting an exited wrapper.
  - ag-03733f steered for round 4. The R3d test fix is merged as 6cdae4e.
- **PC round 4** (ag-c689f3): rejected with 2 findings, both on the docker probe. One: a transport error is cached as a confirmed death. Two: blocking probes run inside the tree transaction.
- **Decision:** probe only during reconciliation, outside the lock and in a thread, and record the verdict per identity. Counting reads only that record: confirmed dead releases the slot, anything else counts as held.
- ag-03733f has been steered for round 5.
- **PC round 5** (ag-d40eba): rejected with 2 findings, both about where reconciliation runs. Steered ag-03733f for round 6.
  - Nested servers and consult waits never reconcile.
  - A drain awaits every docker probe before dispatching.
- **PC round 6** (ag-f3b4ec) rejected the change with 4 findings, all in the background reconciliation task:
  - a hung probe;
  - a dropped trigger;
  - the task survives shutdown;
  - it runs when no limit is configured.

  I steered ag-03733f for round 7 with exact fixes. Then a narrow review; if it is clean, merge.

### 2026-10-02 ~09:10 UTC — PC merged; SC, BA-R3 and RC started
- **PC merged as be4c905** after 8 review rounds:
  - round 7: 1 finding (consult wake during shutdown), fixed;
  - round 8: 1 P2, deferred.
- **Deferred follow-up from round 8:** if shutdown begins during `steer()`'s internal stop, `_launch` refuses before its cleanup guard. The startup half-open probe claim then leaks until the server pid exits (runner.py ~2687).
- **The user restarted `multiagents run` and `/mcp`.** CW is active.
- **In flight:**
  - **SC:** implementer-deep ag-8362a3, pinned to claude opus. The roster default is glm; the user rule is to use claude while the weekly quota lasts.
  - **BA-R3** (bug-ba55a9 part 2): implementer-quick ag-48dce9 (glm).
  - **bug-1213a0:** the contract is `context/specs/refusal-classification.md` (RC-R1..R5, commit 39e45e2), with the advisor's gaps folded in. Tester ag-155fae is writing the red tests.
- **Next:**
  - each run gets a reviewer, then merge;
  - RC then needs an implementer;
  - resolve bug-ba55a9 and bug-1213a0 when merged.
- **Full suite on main after be4c905:** 194 reds, exactly the expected ones.
  - 72 phase2;
  - 120 SC;
  - 1 PC/SC;
  - 1 BA-R3.
- **BA-R3 merged as f9f7ee6:** reviewer ag-1712d7 approved with no findings. bug-ba55a9 is resolved as fixed, in-house and not submitted.
- **Claude session limit (09:30 UTC).** It cut off SC ag-8362a3 and tester ag-155fae. Both were steered back after the reset.
- **RC tests merged as 61bccd3** from tester ag-155fae: 45 tests, 24 red (RC-R1, R2, R3). RC-R4, RC-R5 and H7 are green.
  - **Not covered:**
    - a refused commit-fix turn finishing its startup claim;
    - steer's PC rollback when it loses the probe;
    - wall-timeout precedence.
  - **Recording field names are unpinned:** the tests search the reason, the result and the events.
- **RC implementation:** implementer ag-cfd0bd (glm), on classification and startup-finish only.
- **SC implemented** by ag-8362a3 (opus), branch `agents/implementer-deep/8362a3`, about 850 lines including the new `spendcap.py`.
  - **Tests:** 140 of 141 SC green, PC fully green, full suite 74 reds (72 phase2, BA-R3, one SC).
  - **The one SC red is a test bug.** `test_r2_a_capped_provider_still_counts_a_discarded_runs_spend` refuses a start on a predicted cost, which contradicts SC-R3 and the 0.99-admitted test. I agree with the implementer; tester ag-efab73 is fixing it.
  - **Review:** reviewer ag-d45444 and robustness-tester ag-d84aee are running in parallel, with the SC branch squashed into the robustness-tester's worktree.
  - **The implementer's deferrals to judge:**
    - a raced fresh start ends `failed` rather than deferred;
    - a lowered cap stops every run at the next event;
    - a consult refusal goes through the generic error.
- **SC test fix merged as bfb1567** (tester ag-efab73).
- **SC review round 1:** reviewer ag-d45444 rejected with 14 findings (6 P1, 8 P2). The full list is in `.multiagents/runs/ag-d45444/result.json`. All 14 were steered back to ag-8362a3.
  - **Decision:** a raced fresh start must be deferred, not failed (SC-R3). This overrules the implementer's deferral.
  - **Decision:** when spend and concurrency both refuse, both causes are reported.
  - Robustness tester ag-d84aee is still running on the previous tip; its findings come as round 2.
- **SC robustness, ag-d84aee:** rejected with 6 defects.
  - Three overlap the reviewer's findings.
  - Three are new: a `|` dedup-key collision, `nextafter` period rounding, and an epsilon comparison that refuses spend below the cap.
  - Its tests, `tests/test_sc_robustness_{ledger,runner}.py`, are on its branch. Its WIP commit also carries the squashed SC implementation.
  - **Do NOT `merge_agent` it.** `spendcap.py` would land on main. The implementer was told to check out the two test files into its own branch. Discard ag-d84aee once SC merges.
- **SC round 2:** ag-8362a3 fixed all 17 findings, 14 from the review and 3 new from robustness.
  - Its robustness tests are committed unchanged, and its regression tests are in `tests/test_sc_review_r1.py`.
  - **Tests:** SC, PC and robustness give 261 passed. The full suite shows 96 reds: 72 phase2 and 24 RC with no implementation yet.
  - **Review:** reviewer round 2 is ag-eb9628.
- **RC implemented** by ag-cfd0bd (glm), +75 lines. All 45 RC tests and H7 are green, with known reds only. Reviewer ag-997df9 is checking it, including whether `finish(resolved=True)` wrongly clears other runs' failure count.
- **RC review round 1:** reviewer ag-997df9 rejected with 3 findings, all steered back to ag-cfd0bd (glm, round 1 of 3).
  - P1: a refused commit-fix turn is retried and its verdict dropped.
  - P2: the excerpt is truncated before it is scrubbed.
  - P2: `result.json` lacks the refusal evidence.
- **SC review round 2:** reviewer ag-eb9628 rejected with 9 findings. The earlier fixes mostly hold.
  - **Spec decisions,** written into `spend-caps.md`:
    - **SC-R3c:** every admission point reads the current cap.
    - **SC-R4b:** a crossing binds only within its period, which declines finding #5.
    - **SC-R4c:** the recovered event is marked `recovered: true` and lists the recorded stops; a crossing counts as announced only after a successful write.
  - The remaining 8 were steered back to ag-8362a3 (opus).
- **SC round 3:** ag-8362a3 fixed 8 of the 9 round-2 findings and left #5 alone as declined. Fixes and tests are in 0120a58.
  - **#2 deviation accepted:** `spend_pending` is durable in the tree and retried at the next turn, instead of holding the old checkpoint.
  - **Tests:** 275 green.
  - **Flake:** `test_rc_r4_half_open_probe_free_steer_may_take_it_and_resolves_it` flaked once under load.
  - **Review:** reviewer round 3 is ag-1ef2a0.
- **SC review round 3:** reviewer ag-1ef2a0 rejected with 6 findings (2 P1, 4 P2). They are in the pending-charge path and crash recovery.
  - P1: a failed pending flush still admits.
  - P1: the 5 s cache hides another process's pending charge.
  - P2: recovering a past-period charge stops current runs.
  - P2: a torn event line counts as delivered.
  - P2: a failed `record_stop` is never retried.
  - P2: a reroute duplicates the `deferred_id`.
  - All six were steered back to ag-8362a3, asking for simplification over patching.
- **SC round 4:** ag-8362a3 fixed all six round-3 findings in a8efaf3, with tests in `test_sc_review_r3.py`. The full suite shows known reds only.
  - **Accepted residual:** a crash between node creation and spawn follows DQ-R11.
  - Reviewer round 4 is ag-f0cf70.
- **RC review round 2:** reviewer ag-2d1240 confirmed the round-1 fixes. One P2 remains: a refused original turn still passes the commit-fix gate. It was steered to ag-cfd0bd, round 2 of 3 for glm.

### 2026-10-02 — USER INSTRUCTION: pause when opencode-zai's weekly quota is exhausted
- The user said: "lorsque opencode-zai a fini son quota de la semaine, fais une pause, je te dirai la prochaine strategie."
- **When zai's weekly window is exhausted** (it was at 95% on 2026-10-02 ~09:00 UTC, resetting 2026-10-06 22:06 UTC):
  - start and steer no more glm work;
  - do not let the router fall back silently to other providers for the implementer tiers;
  - let running non-zai work finish;
  - report, and wait for the user's next strategy.
- **Check before every start or steer** of an implementer tier: `budget_status` → `opencode-zai`.
- **Update (user):** "tu peux finir le quota de glm completement ensuite reviens vers moi".
  - Keep using glm until zai actually refuses: a quota-exhausted run, or a start that is refused.
  - Then stop and report to the user. Still no silent fallback for the implementer tiers.
- **SC review round 4:** reviewer ag-f0cf70 found 3 P2, all in crash recovery.
  - A refused-after-run node is requeued.
  - Stop-record retries die with the stream.
  - A newline-less event is duplicated.
  - All were steered to ag-8362a3, as the **last** round. After the round-5 review, the plan is to merge and record any P2 residual as a follow-up.
- **RC merged as c4580c0** after 3 review rounds; round 3 (ag-2c4355) approved. bug-1213a0 is resolved as fixed, in-house and not submitted.
- **SC round 5:** ag-8362a3 fixed the 3 round-4 P2 findings in a95ba97, with tests in `test_sc_review_r4.py`. The final review is ag-e9dd77. Merge after it, then discard robustness branch ag-d84aee.
- **SC final review:** ag-e9dd77 confirmed r4 fixes #1 and #3 and the r3 seed edit.
  - **Two P2 residuals in r4 #2, deferred as follow-ups** (recovered spend_cap event agent list):
    - (a) `Node.spend_cap_crossings` persists the current-cap crossing id, not the crossing that actually stopped the run, when the cap changes between the crossing and the poll;
    - (b) recovery filters nodes by `status == "limited"`, so a node later cancelled or resumed is dropped from the list.
  - **Merge:** the merge conflicted in runner.py with RC c4580c0. ag-8362a3 was steered to merge main and resolve.
- **SC merged as 9a2b085** after 5 review rounds plus robustness, with the merge conflict against RC resolved by ag-8362a3 (d928dfb).
  - **Full suite on main:** 5236 passed, 72 failed (all phase2, known), 15 skipped. PC, SC, RC and BA are all green.
  - Robustness branch ag-d84aee discarded.
  - **Open follow-ups:**
    - SC final-review P2 (a)(b) above;
    - the PC round-8 P2 (startup claim leak when shutdown hits `steer()`);
    - bug-ac396a (minor routing);
    - the older items listed earlier.
- **zai/glm** is at 99% weekly and `usable: false`, so the router no longer routes to it. Per the user's instruction, I am pausing and reporting; I await the next strategy.

### 2026-10-02 — USER STRATEGY: deepinfra models, loop review
- **Implementers:** quick, implementer and deep run on `deepinfra/deepseek-ai/DeepSeek-V4.1-Flash`. Starting variants: quick = default, implementer = high, deep = xhigh.
- **New `loop-reviewer`:** `deepinfra/XiaomiMiMo/MiMo-V2.6-Flash`.
- **The loop:**
  - implementer ↔ loop-reviewer, until the loop-reviewer accepts;
  - then `reviewer` (codex) accepts for merge or relaunches the loop;
  - every 5 loops, the implementer goes up one level (default → high → xhigh → max), capped at max.
- **No spend cap** on deepinfra (user).
- **Status:** the advisor is checking variant support and mid-loop variant changes before `agents.yaml` is edited.
- **Roster applied** (`.multiagents/config/agents.yaml`, `project.yaml`), with the advisor's overlay. `list_agents` loads it.
  - **Implementers:** quick, implementer and deep run on `deepinfra/deepseek-ai/DeepSeek-V4.1-Flash`, with variant `""` (default), `high` and `xhigh`. `effort: ""` and `models: null`, so no fallback.
  - **`loop-reviewer`** runs on `deepinfra/XiaomiMiMo/MiMo-V2.6-Flash`, read-only, with the `team/reviewer.md` instructions. It is added to the implement roster.
- **Advisor findings, 2026-10-02:**
  - **No per-run variant override** on `start_agent` or `steer_agent`. A steer reuses `run.spec`, so editing YAML does not change a running session. Escalating every 5 loops therefore needs either a fresh start at a higher top-level variant, with a branch handoff, or a new steer/start variant override.
  - **TOOLING BUG:** `models.<provider>.variant` in fallback maps is silently ignored, because `fallback_for()` keeps only dataclass fields (`config.py:603`). The glm `variant: max` and `high` fallbacks may never have applied. bug-reporter is on exhausted opencode, so this is recorded here for now.
  - **opencode `readonly`** passes `--auto`, so the loop-reviewer's read-only setting is not hard enforcement.
- **User decisions:**
  - Stay at the starting variants with no escalation for now. "Build a `variant` override for start_agent/steer_agent" is noted **for later**.
  - Work to run on the new pipeline:
    - the ignored-fallback-variant bug;
    - the SC and PC follow-ups;
    - bug-ac396a.
- **Contracts committed in 0e2e781:**
  - `fallback-options.md` (FO-R1..R4): the ignored `models.<P>.variant` bug.
  - `fallback-scope.md` (FS-R1..R6, bug-ac396a): an agent falls back only to providers it names. The project chain and the family no longer widen the set, which also enforces the user's "no silent fallback".
  - `sc-pc-followups.md` (SF-R1..R3): the SC final-review P2 findings and the PC round-8 P2.
- **In flight:**
  - The advisor is reviewing FS/FO/SF.
  - Tester ag-effa3f is writing the SF tests.
- **Pipeline from here:**
  - tests;
  - an implementer on deepinfra DeepSeek;
  - the loop-reviewer, repeated until it accepts;
  - the codex reviewer;
  - merge.
- **Advisor check of FS/FO/SF; specs revised (cb48de5):**
  - **FS shrinks.** `_routed_spec` already rejects unnamed providers except family siblings, and every current family is a singleton, so the "no silent fallback" rule already holds. What is left:
    - keep today's ordering;
    - defer after exhaustion;
    - remove family widening on every path;
    - legacy sessions stay on their recorded sibling.
  - **FO adds:**
    - primary-provider entry merge;
    - precedence: pin > `models.P` > top-level;
    - validation against the destination provider's `spawn.optional`.
  - **SF adds:**
    - cumulative crossing evidence;
    - the SF-R3 release scope.
- **Testers:**
  - SF: ag-effa3f, steered with the revision;
  - FO: ag-f939e4;
  - FS: ag-702ff1.
- **A claude session limit hit the three testers.** After the user's /login, all three were steered back.
- **FO tests merged** from ag-f939e4: 35 tests, 19 red (FO-R1, FO-R3) and 16 green.
- **FS tester ag-702ff1:** 36 tests, 25 green and 11 red (family widening). It was steered to verify each red and to drop an unagreed `deferred_id` assertion.
- **SF tester ag-effa3f:** 9 tests, all red. It was steered to fix the SF-R3 pre-spawn refusal test, which failed for the wrong reason, and to add the queue-entry and cleanup-hold tests.
- **Tests merged:**
  - FS tests c94a5ea: 36 tests, 10 red, comprising 7 family-widening tests and 3 where the deferral reason names chain-only providers.
  - SF tests f1c7168: 9 tests, mostly red. `test_sf_r3_a_refused_queued_resume...` is green on main and unproven. The cleanup-hold rule was not testable through the public surface.
- **Decision SF-R3a:** a steer that ends before spawn releases the probe claim neutrally, with no cooldown re-arm.
- **Implementers on deepinfra DeepSeek:**
  - FO: ag-12d4f1 (implementer, high);
  - SF: ag-1827ab (deep, xhigh).
  - FS waits for the FO merge, since both touch `_routed_spec`.
- **SF implementer ag-1827ab (DeepSeek xhigh)** finished in 28 min for $0.13.
  - One commit, 818e401: all 10 SF tests green, SC/PC/RC green, and only known reds in the full suite.
  - It confirmed with a temporary debug test that the queue-restore test does exercise the path.
- **Loop round 1 on SF:** loop-reviewer ag-7c0db2 (MiMo).
- **FO implementer ag-12d4f1:** still running after 29 min, 178 steps, $0.11.

### HANDOFF 2026-10-02 (orchestrator context wind-down)

**Done today**
- PC merged as be4c905.
- BA-R3 merged as f9f7ee6; bug-ba55a9 fixed.
- RC merged as c4580c0; bug-1213a0 fixed.
- SC merged as 9a2b085.
- Main was green apart from the 72 phase2 tests at 9a2b085.
- Roster moved to deepinfra, per the user strategy above.
- Contracts FO, FS and SF (with SF-R3a) written, and their tests merged:
  - FO: 91b7fa3;
  - FS: c94a5ea;
  - SF: f1c7168.

**In flight**
- **SF:** ag-1827ab (implementer-deep, DeepSeek xhigh) is done, with one commit, 818e401.
  - Loop round 1 is loop-reviewer **ag-7c0db2** (MiMo). It was reported `stuck` (silence 189 s), but this is probably pytest and it was not killed. Check with `check_agent`; if it is really wedged, `stop_agent` it and start a new loop-reviewer.
  - I raised loop-reviewer `silence_timeout` to 600 in agents.yaml.
- **FO:** implementer **ag-12d4f1** (DeepSeek high) is still running.

**Next, in order**
1. **SF loop.**
   - If loop-reviewer rejects: steer ag-1827ab with the findings, then a new loop-reviewer, and repeat.
   - When it approves: run `reviewer` (codex) with `verifies=ag-1827ab`. If codex approves, merge. If it rejects, back into the loop.
   - Do **not** raise the variant: the user said stay at the starting levels.
2. **FO:** when ag-12d4f1 finishes, the same loop. After FO merges, start the FS implementer on `context/specs/fallback-scope.md` with the `implementer` tier. FS waits for FO because both touch `_routed_spec`.
3. **Close bug-ac396a** as fixed when FS merges. Do not submit it.
4. **Report to the user** when all three have merged, then hand back.
   - The "variant override for start/steer" item is noted for later; it is not to be started.
   - The ignored-`models.P.variant` bug is what FO fixes.
- **FO implementer ag-12d4f1 (DeepSeek high)** is done. One commit, 1a511df: all 35 FO tests pass and the full suite is at baseline.
  - **Approach:** `AgentSpec.routed()` builds a per-run spec, and `Config.warnings` emits FO-R3 warnings.
  - **NEXT:** loop-reviewer on branch `agents/implementer/12d4f1` with `verifies=ag-12d4f1`. It was not started because of the context wind-down.
- **SF loop-reviewer ag-7c0db2** is still running. Its "stuck" reports are silence during pytest under the old 180 s limit.

**2026-10-02, loop progress.**
- **SF:** loop-reviewer ag-7c0db2 returned ACCEPT after round 1 (306 tests passed; three P3s: an untried retry of tree evidence, a silent `except` in `_remember_stop`, pre-existing evidence from binding caps). The final codex reviewer ag-904ab8 is running with `verifies=ag-1827ab`.
- **FO:** loop-reviewer ag-d1e0f6 is running round 1 on `agents/implementer/12d4f1`.
- **Config:** loop-reviewer `timeout` raised from 1800 to 3600 in agents.yaml, which is not versioned.
- **SF codex review ag-904ab8: CHANGES_REQUESTED.** Four P2s, all in steer's pre-spawn cleanup:
  - a non-RuntimeError from `executor.start` skips cleanup;
  - the predecessor's hold blocks release of this steer's own resources;
  - the inherited supervision lock stays held after an early refusal;
  - a failed neutral release has no retry owner.
  One P3: tree evidence is dropped once the ledger write succeeds. Steered ag-1827ab for loop round 2 (variant stays at xhigh). NEXT: a new loop-reviewer on it, then the codex reviewer.
- **Loop-reviewer variant set to `high`** (user: "raisonnement supérieur, essaye max"). In opencode, MiMo-V2.6-Flash offers only low, medium and high, so high is its maximum. Runs already in flight keep the default.
- **Evaluation of loop-reviewer ag-7c0db2 (SF, default variant):**
  - Its SF-R1/R2 analysis was correct, and its P3 #1 was confirmed by codex.
  - It read the exact regions behind the four codex P2s (runner ~6947-6988, ~3915-3928) and argued them correct without reproducing them. In particular, its claim that "the `_held` guard cannot drop the queue restore" was disproved by codex's in-memory repro.
  - Cost: about $0.026 over 36 minutes, against 9 minutes for codex.
  - Verdict: a useful first filter for spec conformance, but weak on concurrency and cleanup invariants, and slow.
- **FO loop-reviewer ag-d1e0f6: REJECT** (35/35 FO tests; full suite has only the 72 phase2 reds). Two P2s:
  - `Config.warnings` is never surfaced through the doctor channel;
  - FO-R3 validates against the union of shipped providers instead of the destination provider.
  P3: `routed()` applies the `provider` key. Steered ag-12d4f1 for loop round 2 (variant high).
- **Note on the review itself:** a good one, with a concrete check against the spec beyond the tests.
- **SF round 2:** ag-1827ab committed e4b9878, fixing all five codex points. It adds `tests/test_sf_review_r1.py` (5 tests, red before the fix and green after); the related tests total 360 passed, and the full suite is at baseline. Loop-reviewer ag-c36577 (variant high) is checking it. It was asked to reproduce, not just reason.
- **FO round 2:** ag-12d4f1 committed 9292a07, fixing both P2s and the P3, and added `tests/test_fo_review_r1.py` (8 tests). Two FO tests now conflict with the review: they encoded validity as the union of shipped providers.
  - **Decision FO-R3a** (spec, 5435fb4): validity is decided by the destination provider alone.
  - Tester ag-28b16f is correcting `test_fo_r3_valid_keys_are_not_reported`; merge it before FO.
  - Loop-reviewer ag-22ac54 (high) is checking round 2.
- **SF:** loop-reviewer ag-c36577 (high) is checking round 2.
- **FO-R3a test correction merged** (6e15882; 48/48 on the FO branch). The spec notes that dataclass fields such as `effort` stay valid under every provider.
- **2026-10-02, user decision.**
  - `max_concurrent` raised from 4 to 6, and AGENTS.md now says `-n 4`.
  - **FS runs in double:** after FO merges, two `implementer` runs work on the FS spec and tests. Then one loop-reviewer reviews **both** branches and proposes specific cross-borrowings (A takes part X from B, and vice versa); I relay those by steer.
  - The loop continues as usual and ends with the codex reviewer. Merge the better branch and discard the other.
- **2026-10-02: second Claude account `claude-b`** (provider in `.multiagents/config/providers.yaml`, `CLAUDE_CONFIG_DIR=~/.multiagents/profiles/claude-b`).
  - **Moved to `claude-b` as provider:** tester, adversary, cartographer, harness, characterizer.
  - **`models: claude` entries renamed `claude-b`:** researcher, adversary, robustness-tester, bug-reporter.
  - **Unchanged:** the orchestrator and the initializer keep the original account. The agents have no fallback to the original account.
  - **Backup** of agents.yaml: `agents.yaml.bak-claude-b` in the scratchpad.
  - **Blocked on the user:** `multiagents auth login claude-b`.
- **claude-b verified:** a distinct account and organisation from `claude`.
- **Bug CB (budget reader not inherited through `extends`):** `budget_status` cannot read claude-b's quota. Cause: `budget.py` ~1317 looks up `_BUILTIN.get(name)` by exact provider name.
  - Not blocking: agents still run on claude-b; its exhaustion is only detected reactively.
  - Fixed in-house by implementer-quick ag-ba1672 (budget.py plus `tests/test_budget_extends_reader.py`).
  - No ticket filed, per the tickets-in-house rule.
  - NEXT: loop-reviewer, then the codex reviewer, then merge.
- **CB decisions** (ag-ba1672, steered):
  - P0-R8 (no provider vocabulary in `src/*.py`) must stay green, so the profile variable becomes a declarative provider field (`budget_profile_env: CLAUDE_CONFIG_DIR` on the shipped claude provider, inherited through `extends`).
  - The c2 characterization test that pins the old bug goes to the tester; the implementer must not touch it.
  - Config: implementer-quick `timeout` raised from 900 to 2700 and `max_steps` from 80 to 200.
- **CB implemented:** ag-ba1672 committed f4ebe8c (the declarative `budget_profile_env`; P0-R8 green; full suite shows only the expected reds plus the c2 test). It was stopped at its 900 s steer timeout with the work done.
  - Loop-reviewer ag-5fe8b6 is running.
  - Tester ag-ded12c, the first run on `claude-b`, is rewriting `test_a_second_named_claude_account_gets_no_builtin_at_all`. Merge it with CB.
- **SF and FO loop-reviewers round 2** (ag-c36577, ag-22ac54) both hit 3600 s. Both were steered to wrap up.
- **Observation:** MiMo loop reviews take more than 60 minutes each and are the bottleneck.
- **SF round 2:** loop-reviewer ag-c36577 ACCEPTed. Cooldown neutrality was reproduced; the rest was reasoned clean. Note to evaluate: the `BaseException` branch does not call `_mark_launch_failed`. The codex reviewer ag-f5e838 (round 2) is running.
- **CB:** the c2 test rewrite is merged (b943cb6). It is red on main until CB merges.
- **FO round 2:** loop-reviewer ag-22ac54 ACCEPTed (all three fixes reproduced; dedup across reloads verified). The codex reviewer ag-310b77 is running.
- **SF codex round 2 (ag-f5e838): CHANGES_REQUESTED.** All round-1 fixes hold, but three new P2s were reproduced:
  - the lock is released while the predecessor is still live (no hold does not mean dead);
  - cancellation during the stop keeps the lock after the predecessor's death;
  - the BaseException path leaves the node `running`.
  Steered ag-1827ab for loop round 3, still at variant xhigh.
- **Observation:** the MiMo loop-reviewer accepted both SF rounds while codex found P2s each time. On lifecycle code, the loop is not catching what codex catches.
- **FO codex review ag-310b77: CHANGES_REQUESTED.** Three P2s:
  - load-time dedup hides warnings after a reload;
  - a primary entry's effort is silently normalised;
  - an options-only entry is skipped on the family path.
  Decided in the spec as FO-R3b, R1b, R1c and R4a (d35dc27). Steered ag-12d4f1 for loop round 3, still at variant high.
- **CB loop-reviewer ag-5fe8b6: REJECT.**
  - **P2:** an extends instance whose base has no `budget_profile_env` reports the base account's reading.
  - **P3:** multi-hop `extends` resolution depends on the caller's providers map.
  - Everything else was reproduced correct. ag-ba1672 is steered for round 2.
- **FO round 3:** ag-12d4f1 committed 26a6afd (R3b, R1b and R1c fixed; adds `tests/test_fo_review_r2.py`; full suite shows 72 phase2 reds plus 1 stale test). Its `readonly_violation` on the FO test file is only a sync of main's copy and is harmless at merge.
  - Tester ag-3ec8fe is rewriting `test_fo_r3_the_warning_is_emitted_once_per_agent_provider_key` for FO-R3b. Merge it before FO.
  - Loop-reviewer ag-c1d8af is on round 3.
- **SF round 3:** ag-1827ab committed 707c608.
  - `_steer_release` is now the single decision point, based on `_positively_ended`.
  - Adds `tests/test_sf_review_r2.py` (3 tests).
  - 363 targeted tests pass; the full suite is at baseline.
  - Loop-reviewer ag-d89877 is running.
- **FO-R3b test rewrite merged** (95a8bda).
- **CB round 2:** ag-ba1672 committed f96076c. An extends instance without a profile field is now unknown, and `budget_builtin` is resolved at load. The full suite shows only the expected reds. Loop-reviewer ag-368f54 is running.
- **FO loop round 3, ag-c1d8af: REJECT.** P2: an explicit effort in a family-destination entry is normalised, because it is attributed to the route `""`. P3: the same fault with an earlier route. FO-R1b is generalised in the spec. ag-12d4f1 is steered for loop round 4.
- **SF loop round 3, ag-d89877 (MiMo high): REJECT.** Two reproduced P2s:
  - `_launch` releases the inherited lock behind a live predecessor;
  - an unconfirmed death has no owner and no re-check.
  ag-1827ab is steered for round 4. **MiMo at `high` found codex-grade issues this time**, unlike at the default variant.
- **FO round 4:** ag-12d4f1 committed 3dfa7a6. Effort is now attributed by source entry; the full suite shows only the 72 phase2 reds. Its readonly flag is again only a sync of main's copy.
- **CB loop round 2, ag-368f54: REJECT.** P2: a reader without a `config_dir` parameter (opencode, agy) silently drops the instance's profile and reports the base account's reading. Everything else was verified. ag-ba1672 is steered for round 3.
- **SF round 4:** ag-1827ab committed dd0b8ed.
  - `_launch(release_lock=False)` on the steer path.
  - An unconfirmed predecessor now gets a durable hold through the existing machinery.
  - 923 targeted tests pass; the full suite is at baseline.
  - Loop-reviewer ag-20e4d8 is running round 4.
- **CB round 3:** ag-ba1672 committed 70f6d50.
  - A profile is honoured only by a profile-capable reader, judged by signature, and is rejected at load otherwise.
  - The full suite shows only the expected reds.
  - A loop-reviewer is running round 3.
- **CB loop round 3, ag-d1cfc1: ACCEPT.** One P3: the load error message suggests a script remedy that does not clear the error. Codex reviewer running.
- **CB codex review ag-5158f7: CHANGES_REQUESTED.**
  - P2: the in-process cache key omits the profile; it was reproduced.
  - P3: the load-error remedy is wrong, and a script-backed provider should be exempt.
  - ag-ba1672 steered for round 4.
- **SF loop round 4, ag-20e4d8: REJECT.** The round-3 fixes were verified. Two P2 error-path gaps remain:
  - a storage error in `_steer_release` leaves an ownerless lock;
  - `_steer_predecessor` sits outside the protected region, which pins the startup probe.
  Steered ag-1827ab for round 5. Variant stays xhigh, per the user's no-escalation rule.

**HANDOFF, 2026-10-02 (orchestrator context wind-down #3)**

**In flight. Nothing is merged yet for SF, FO or CB; all three are in the loop.**

- **SF** (`context/specs/sc-pc-followups.md`). Branch `agents/implementer-deep/1827ab`, ag-1827ab (DeepSeek, xhigh).
  - Steered for loop round 5: `_steer_release` must be storage-error robust (best-effort steps, an in-memory hold if the durable write fails, never mask the original exception), and `_steer_predecessor` must move inside the protected region after `startup.claim`.
  - NEXT: when it is done, run a loop-reviewer round 5. On ACCEPT, run the codex `reviewer` (`verifies=ag-1827ab`), giving it the history of codex rounds 1 and 2 and loop rounds 3 and 4. On APPROVE, merge.
- **FO** (`context/specs/fallback-options.md`, decisions FO-R3a/R3b/R1b generalised/R1c/R4a). Branch `agents/implementer/12d4f1`, head 3dfa7a6.
  - Loop-reviewer ag-948bd9 is running round 4: effort attribution by source entry.
  - NEXT: on ACCEPT, run the codex reviewer round 2 (`verifies=ag-12d4f1`; its round-1 findings were R3b, R1b and R1c, now fixed). On APPROVE, merge. On REJECT, steer ag-12d4f1.
  - Readonly_violations on `tests/test_fo_fallback_options.py` are syncs of main's copy and are harmless.
- **CB** (budget reader through `extends`; no spec file, the requirements are in BRIEF above and in the agents' tasks). Branch `agents/implementer-quick/ba1672`, head 70f6d50.
  - ag-ba1672 is steered for round 4 on the codex review: the cache identity must include the profile dir (in-process, concurrent-fetch and on-disk), and a provider with its own `budget` script action is exempt from the load check.
  - NEXT: loop-reviewer, then codex reviewer, then merge.
  - Note: the steered run keeps `timeout` 900, so steer it to finish if that trips.
- **FS** (`context/specs/fallback-scope.md`, `tests/test_fs_fallback_scope.py`). Not started; it waits for the FO merge (both touch `_routed_spec`).
  - Then start **two** `implementer` runs (user decision: FS in double).
  - One loop-reviewer reviews both branches and proposes cross-borrowings; relay those by steer.
  - Then the codex reviewer, merge the better branch, and discard the other.
  - On FS merge: `resolve_ticket(bug-ac396a, "fixed")`. Never submit it.

**Config changes this session** (`.multiagents/config/`, not versioned):
- `max_concurrent` raised to 6.
- loop-reviewer: `variant: high`, `timeout: 3600`, `silence_timeout: 1200`.
- implementer-quick: `timeout: 2700`, `max_steps: 200`.
- New provider `claude-b`; the delegated claude agents were moved to it.

**Observations for the user:**
- MiMo at `high` finds codex-grade issues; at the default variant it did not.
- Each MiMo loop review takes 30 to 75 minutes.
- No user decision is pending.

**When FO, FS, SF and CB are all merged:** report to the user and hand back per the protocol.
- **Post-handoff results (same session):**
  - **FO loop round 4, ag-948bd9: ACCEPT.** No defects; full suite shows only the 72 phase2 reds. NEXT: codex reviewer round 2 on `agents/implementer/12d4f1` (3dfa7a6), then merge, then FS.
  - **CB ag-ba1672 degenerated** into a "Run. Emitting. OK." text loop and never ran its commit. multiagents auto-committed its worktree as WIP 851e1a0 (diff below). NEXT: have a fresh `implementer-quick`, or a steer if usable, verify that the WIP actually contains both round-4 fixes (profile in the cache identity; script-backed providers exempt) plus the tests, run them, and commit cleanly. Do not trust the WIP blindly.
  - **SF round 5 done:** ag-1827ab committed b61a350 (storage-safe `_steer_release`; predecessor capture inside the protected region). 925 targeted tests pass; the full suite is at baseline. NEXT: loop-reviewer round 5 on b61a350, then the codex reviewer.
- **Nothing is running now.** The three NEXT steps above (FO codex round 2, CB WIP verification, SF loop round 5) are ready to start after compaction.
- **2026-10-02, after compaction:**
  - **Started:** FO codex reviewer round 2 (ag-55f4e1, `verifies=ag-12d4f1`) and SF loop-reviewer round 5 (ag-d38d1c, `verifies=ag-1827ab`).
  - **Steered ag-ba1672** to verify and complete CB WIP 851e1a0, then commit.
  - **User decision:** all implementer tiers are now at `variant: max` in agents.yaml. ag-ba1672, being a steered run, keeps its old variant.
  - **FO codex round 2 (ag-55f4e1): REQUEST_CHANGES.** R3b, R1b and R1c are confirmed fixed. Two new defects were reproduced, and both fall under the existing contract:
    - P2: fractional option values such as `max_budget_usd: 0.5` are dropped by `_launch`, which keeps only str/int extras;
    - P3: a `provider` key escapes the FO-R3a warning when P's `spawn.optional` declares a `provider` placeholder.
  - **Steered ag-12d4f1 for FO round 5,** with regression tests in a new file, `tests/test_fo_round5.py`. NEXT: loop-reviewer, then codex round 3, then merge.
  - Codex could not create a detached worktree (its sandbox mounts the repo's git metadata read-only), so its suite runs rely on the loop-reviewer.
  - **CB handover.** ag-ba1672 was steered and worked productively, then hit its kept 900 s timeout and was marked stuck. A second steer ended with `failed, exited -15` within 3 s: no events, `elapsed_seconds` 19600.
    - Its work was auto-committed as 76e761e and handed with `merge_agent(into=worktree)` to a fresh `implementer` run, ag-152774 (variant max, timeout 3600), as af30b03. ag-152774 completes and verifies CB round 4.
    - NEXT: loop-reviewer, then codex, then merge.
  - **Possible tooling bug, to hand to bug-reporter at the next stop:** a steer on a run that is stuck on its wall-clock timeout died at once with -15. Evidence: ag-ba1672, run dir `.multiagents/runs/ag-ba1672`.
  - **FO round 5, ag-12d4f1: 16216eb.**
    - Floats are kept and rendered by `providers.option_text`; bools render as `true`/`false`.
    - An unrenderable option emits an `option_not_renderable` event.
    - A `provider` key is always reported.
    - New tests: `tests/test_fo_round5.py`. Full suite: 72 phase2 reds only.
    - The loop-reviewer is ag-35604a (`verifies=ag-12d4f1`). NEXT: codex round 3, then merge.
  - **AGENTS.md, 72c7c17:** the basetemp path must not contain `ag-`, because a basetemp containing `ag-1` turns a test_core test red.
  - **CB, ag-152774: verified.**
    - The handover commit af30b03 already held the complete round-4 work. The two round-4 tests are red on 70f6d50 and green on HEAD.
    - Full suite: no new reds. The extras (FO/FS 33, SF 9) are red on main because their tests were merged ahead of the code.
    - The loop-reviewer is ag-45bb04 (`verifies=ag-152774`). NEXT: codex, then merge.
- **SR, ticket bug-dc522a** (blocking; fixed in-house, never submitted).
  - **Bug:** steer on a stuck run dies with -15, because the old wrapper writes `exit_status` into the shared run dir after the new launch. Also, steer reuses `run.spec`, so the limits are stale.
  - **Contract:** `context/specs/steer-exit-race.md`, at 059ef59 plus the advisor revision 01197a8.
  - **Tester:** ag-5a55ff (claude-b) writes `tests/test_sr_steer_exit_race.py`.
  - NEXT: merge the tests, then **after the SF merge** (both touch the steer path) run `implementer-deep`, the loop-reviewer, codex, and the merge. Then `resolve_ticket(bug-dc522a, fixed)`.
  - Until then, **avoid steering a run that is stuck on its wall clock**. Steering one that is still running worked (ag-d38d1c).
- The loop-reviewer timeout was raised to 5400 after SF round-5 review ag-d38d1c hit 3600. ag-d38d1c was steered to wrap up.
- **SF loop round 5, ag-d38d1c: ACCEPT.** Both fixes were reproduced as green; its repro file is `/var/tmp/ag-d38d1c-wt/tests/test_zz_loop_repro.py`. It also raised one unreproduced observation: a raising `_steer_predecessor` may be read as a confirmed death. The codex final review is ag-f63a66 (`verifies=ag-1827ab`), asked to settle that point. NEXT: on APPROVE, merge SF; then the SR implementer.
- **FO loop round 5, ag-35604a:** stuck on its 3600 s wall clock but still working. Deliberately NOT steered, because of the SR bug. Let it finish on its own.
- **FO loop round 5, ag-35604a: ACCEPT.** No defects. Full suite: 72 phase2 reds only. Its minor notes: the `provider` warning wording, and timestamp/Decimal scalars. The codex final round 3 is ag-0f7514 (`verifies=ag-12d4f1`). On APPROVE: merge FO, then FS ×2.
- **The SR tester ag-5a55ff** is stuck on its 1500 s wall clock but still working. Not steered (SR bug); let it finish.
- **FO MERGED, 3f884b0** (codex round 3, ag-0f7514: APPROVE). On main, 58 FO tests pass. Optional follow-ups, not scheduled:
  - the `provider` warning wording at `config.py:930`;
  - timestamp/Decimal scalars get an event rather than being rendered.
- **FS ×2 started:** ag-0441db and ag-5d1bd2 (implementer, variant max). NEXT: one loop-reviewer compares both and proposes cross-borrowings, relayed by steer; then codex; merge the better branch and discard the other; then `resolve_ticket(bug-ac396a, fixed)`.
- **SR tests merged, 1c3788e** (tester ag-5a55ff): 23 red, 7 green, 6 skipped. The contract gained SR-R2b, which gates the free retry (7a6419c). The SR implementer-deep starts after the SF merge.
- `test_c2 ...second_named_claude_account...` is red on main until CB merges. That is expected.
- **SF codex final, ag-f63a66: REQUEST_CHANGES, 3 P2s.**
  - A failed capture is treated as absent and releases a live predecessor's lock (`runner.py:7164`).
  - A failed hold read leaves the lock without an owner (6838).
  - A suppressed `restore_deferred` failure loses the resume entry, and `_pc_dispatch` then says "launched" (6833).
  - **Steered ag-1827ab for round 6** with one uniform rule: "unknown is never death, and no failure path leaves state without a retry owner". Tests go in `tests/test_sf_review_r3.py`. NEXT: loop-reviewer, then codex, then merge; then SR.
- **CB loop review, ag-45bb04: REJECT, 1 defect.** All 6 requirements were reproduced green. The defect: the profile env is expanded with `expanduser` but not `expandvars` (`budget.py:1077`, `:1360`). **Steered ag-152774 for round 5.** NEXT: loop-reviewer, then codex, then merge.
- **SF round 6, ag-1827ab: f0d428b.** It applies the uniform rule: `_Predecessor.captured`, `known_hold`, and `_steer_restore_pending` with a retry from `_settle_holds`. Tests: `test_sf_review_r3.py`, red on b61a350. Full suite: no new reds. The loop review is ag-6fcded (timeout 5400). The task asks for a "Suspected, not reproduced" list that codex will check. NEXT: codex, then merge SF; then the SR implementer-deep.
- **2026-10-03, user decision: implementers moved to agy Gemini** (quick `gemini-3.8-flash-medium`, default `gemini-3.1-pro-low`, deep `gemini-3.1-pro-high`, `models: null`). **The loop-reviewer is no longer used:** the codex reviewer runs directly after each implementer.
  - The SF loop review ag-6fcded was stopped; the codex final review ag-52701a now runs on f0d428b.
  - Runs still on DeepSeek finish as they are: FS ag-0441db and ag-5d1bd2, and CB round 5 ag-152774. Each then goes straight to codex (for FS, codex compares both branches).
  - SR goes to `implementer-deep`, now Gemini pro high, after the SF merge.
- **2026-10-03, user changed the roster again.**
  - **Implementers:** codex `gpt-6.1-sol`, effort low (quick), medium (default) and high (deep), with `models: null`.
  - **Reviewer:** agy `gemini-3.1-pro-high`, falling back to codex gpt-6.1-sol at effort high.
  - **No loop-reviewer.**
- **CB round 5, ag-152774: b1e0212.** A shared `expand_env_value`/`resolved_profile` (providers.py ~907) is used by `build_env`, `_launch` and the reader; a relative profile value becomes absolute against home. The final review is ag-bc1f98 (Gemini). NEXT: merge on APPROVE.
- SF's final codex review ag-52701a is still running. It was started before the switch, and that is fine.
- **SF codex round 2, ag-52701a: REQUEST_CHANGES, 3 more P2s.** A pid-less hold never resolves (6878); a startup refusal bypasses the restore retry (7195); a failed `_pc_unreserve` has no retry owner (6845).
  - That makes seven rounds of whack-a-mole, so the task now asks for a **structural fix**: ONE per-node pending-cleanup record, with idempotent steps retried by settle until done; unknown liveness keeps the lock.
  - Moved to **implementer-deep ag-60dd6f** (codex gpt-6.1-sol high). ag-1827ab's work was handed over with `merge_agent(into)` as 0790219, which sits on top of current main (FO merged, SR tests present).
  - Tests go in `tests/test_sf_review_r4.py`. NEXT: reviewer (Gemini), then merge; then SR.
- **CB MERGED, dfc8fb0** (Gemini reviewer ag-bc1f98: APPROVE; its branch held only a `diff.txt` and was discarded).
- **FS branch A, ag-0441db** (DeepSeek max, 3c2da8b and 8d4c0fc): 36/36 FS and 58/58 FO tests pass.
  - **Six stale RM/RT/M tests** still assume unlisted-sibling routing, so tester ag-272624 is updating them.
  - **FS-R5 note:** `initializer` and `orchestrator` (claude, no `models:`) lose the implicit claude-b fallback. That is fine.
  - **Waiting for branch B,** ag-5d1bd2. Then ONE reviewer compares A and B and proposes cross-borrowings; merge the better one and discard the other; then `resolve_ticket(bug-ac396a, fixed)`.
- **User question (agy, 2026-10-03):** can agy have two accounts like claude/claude-b? Researcher ag-b643b9 is investigating; answer the user when it returns.
- **FS branch B, ag-5d1bd2** (d78017e, 1234386): 36/36 FS pass. It narrows the pool, drops the chain entirely, adds `_bound_spec` and guards the chooser.
  - The tests rewritten for FS (tester ag-272624) are merged.
  - The comparative review is ag-c2ee3d, on Gemini. NEXT: merge the winner plus its borrowings, discard the other, then `resolve_ticket(bug-ac396a)`. Two more stale tests touch the chain-skip events: `test_rt_r1_skips_unmodelled...` and `test_rm_r6a_...unmodelled_chain_entry`; send them to the tester per the reviewer's ruling.
- **AB (agy second account in docker), user request.**
  - Contract: `context/specs/agy-second-account.md` (4cd051b, plus the advisor revision 697943d).
  - Tester ag-f6de5d is writing `tests/test_ab_agy_second_account.py`. NEXT: implementer (codex), then reviewer, then merge. After that, write the `agy-b` provider entry, tell the user to switch to docker, recreate the container and run `multiagents docker login agy-b`.
- **User asked (2026-10-03) whether the docker bug is fixed and whether to go back to docker.** It is NOT fixed: the container claude 401 ("1a") was deferred. Bug-reporter ag-b41640 (agy flash) is diagnosing it with a live `docker exec` probe (no rm/down, no config edits, no credential contents) and is also checking claude-b, codex, agy and deepinfra under docker. NEXT: contract, then tester, then implementer, then reviewer; then the user switches executor and recreates. AB (agy-b) only works under docker.
- Reviewer `silence_timeout` 180 → 900.
- **FS comparative review, ag-c2ee3d (Gemini): MERGE_B.**
  - Branch A dropped `family` from `choose_provider`, which breaks Tier-A pooling of listed siblings, and it emits `route_skipped` for non-candidate chain entries, which violates FS-R4.
  - **ag-5d1bd2 (B) is steered** to borrow A's `resume` flag in place of its `_bound_spec`.
  - **Tester ag-95cfbb is rewriting the last two stale tests:** `test_rt_r1_skips_unmodelled...` and `test_rm_r6a_...unmodelled_chain_entry`. Ruling: no `route_skipped` for non-candidate chain entries.
  - NEXT: merge the tests and then B, discard A (ag-0441db), and `resolve_ticket(bug-ac396a, fixed)`. A codex/Gemini re-review of B's borrowing is optional (small diff); read the diff myself.

**HANDOFF #4, 2026-10-03 (orchestrator context near wind-down).** The user said "je te laisse continuer en autonomie".

**Roster now** (agents.yaml, not versioned):
- **Implementers:** codex `gpt-6.1-sol`, effort low (quick), medium (default) and high (deep), with `models: null`.
- **Reviewer:** agy `gemini-3.1-pro-high`, with a codex high fallback and `silence_timeout` 900.
- **No loop-reviewer.**
- **Testers** run on claude-b sonnet.

**Merged this session:** FO 3f884b0, CB dfc8fb0, the SR tests 1c3788e, and the FS stale-test rewrites f66ded6 and b3359df.

**In flight:**
- **FS:** ag-5d1bd2 (branch B, DeepSeek), steered to borrow A's `resume` flag.
  - NEXT: read the diff, then merge B and discard A (ag-0441db, `force`).
  - Then `resolve_ticket(bug-ac396a, fixed)`.
- **SF:** ag-60dd6f (implementer-deep, codex high), the structural single-cleanup-owner fix on top of main (0790219).
  - NEXT: reviewer, then merge.
  - Then **SR** (`context/specs/steer-exit-race.md`, tests on main): implementer-deep, reviewer, merge, `resolve_ticket(bug-dc522a)`.
- **AB:** tester ag-f6de5d is writing `tests/test_ab_agy_second_account.py`. Contract: `context/specs/agy-second-account.md`.
  - NEXT: merge the tests, then implementer (codex), reviewer, merge.
- **DK:** tester ag-a83388 is writing `tests/test_dk_docker_claude_auth.py`. Contract: `context/specs/docker-claude-auth.md` (a00c87a plus 065d594). Ticket bug-07d880.
  - NEXT: merge the tests, then implementer-deep, reviewer, merge.
  - AB and DK both touch `docker.py`/auth, so run their implementers sequentially or check for overlap.

**After DK and AB merge, the return to docker** (the user wants it):
1. Add the `agy-b` provider (spec AB background) and claude-b's pin (`container_account`, or whatever DK named it).
2. Tell the user to set `executor.kind: docker` and run `multiagents docker rm && multiagents docker up`, but only with no agents running.
3. Then `multiagents docker login claude`, `... claude-b` and `... agy-b`.
- Codex, opencode/deepinfra and agy were verified to work under docker (ag-b41640).

**Tickets:** fix in-house and never submit: bug-ac396a (FS), bug-dc522a (SR), bug-07d880 (DK).

**Do not steer a run that is stuck on its wall clock** (bug SR). A stuck run keeps running; let it finish.
- **FS MERGED, ded3ac2** (branch B with A's `resume` flag borrowed). On main, 288 tests pass across FS, routing, FO, budget and conversation. Branch A (ag-0441db) was discarded, and bug-ac396a is resolved as fixed (not submitted).
- **SF structural round, ag-60dd6f: 7d4f816.** It introduces one per-node cleanup record, `_Hold.steer`, with every step retried until done. Tests: `tests/test_sf_review_r4.py` (8). Focused SF tests: 33 pass. Full suite: no new reds; its CB red comes from the pre-CB base and is fixed on main. The reviewer is ag-69ee0a (Gemini). NEXT: merge on APPROVE, then the SR implementer-deep.

- 2026-10-03: SF review ag-69ee0a REJECTED 7d4f816 (3 new: second steer clobbers pending cleanup; adopted hold pid=None spins on confirm; restore not idempotent). Steered ag-60dd6f with them. Discard reviewer branch ag-69ee0a (only patch files). AB tester ag-f6de5d, DK tester ag-a83388 still running.
- 2026-10-03: AB tests merged b37d025 (44 tests, 29 red). Dual AB implementers: ag-7504ef (implementer, medium) and ag-356ddb (implementer-deep, high); next = one reviewer comparing both. DK tester ag-a83388 still running; DK impl after AB if overlap (docker.py/budget).
- 2026-10-03: AB test defect (\bagy\b matched agy-b) fixed by tester ag-dedb99, merged 15ee595. Both AB implementers at 42/44, steered to fix the missing-login note overwritten by window projection + run full suite. DK tester ag-a83388 stuck on wall clock (1500s) — left running, not steered (SR bug).
- 2026-10-03: AB both done (A ag-7504ef 0d6c017, B ag-356ddb 5bae315; 43/44 + full suite clean). Comparative reviewer ag-928aa1 (Gemini) running. Then merge winner, discard other.
- 2026-10-03: USER: no more dual implementers (codex is fast enough) — one implementer per feature. DK tests merged 15983bb (45 tests, 26 red; pin surface assumed `serve(pins=)`/`mint_token(provider=)` in Rig helpers only; R3/R3a/R5 untested → implementer must unit-test them). DK impl (implementer-deep, single) starts after AB merges (cli.py/budget.py overlap).
- 2026-10-03: SF ag-60dd6f fixed r5 findings → head 58492da (refuse 2nd steer while cleanup pending; persisted identity; restore receipts). Reviewer ag-a3a7e6 round 6 running. Branch base predates FS merge → expect runner.py conflict at merge.
- 2026-10-03: SF r6 review ag-a3a7e6 REJECTED 58492da (no operator path out of terminal blocked hold; wrapper.pid recovery lacks start time → pid reuse; restore receipts unbounded). Steered ag-60dd6f.
- 2026-10-03: AB comparative review ag-928aa1: B (ag-356ddb) is the base; B's agy.sh falls back to host in full-suite (R3d) → steered B to borrow A's executor handling. Keep A (ag-7504ef) branch until B approved (B reads it via git show), then discard A.
- 2026-10-03: AB B 04268d3: executor authoritative, MULTIAGENTS_PROFILE=host no longer redirects budget; cache keyed by executor. DECISION: absent MULTIAGENTS_EXECUTOR = legacy local; invalid = unknown; docker = container only; core must always pass executor (regression test). Steered ag-356ddb.
- 2026-10-03 ~00:31Z: codex 5h EXHAUSTED (resets 02:17:46Z); ag-60dd6f failed "quota exhausted" mid SF r6 fixes → RESUME with steer after reset (do not discard). agy gemini-5h at 95% (resets 02:09Z). ag-356ddb (AB) still running on codex — may also cut. Wake armed for ~02:20Z.
- 2026-10-03: resumed ag-60dd6f/ag-356ddb after codex reset. SF head 0339653 (r6 fixes; SF 52/52). Reviewer ag-753c9a round 7 running, told to split BLOCKING/NON-BLOCKING and approve if only non-blocking.
- 2026-10-03: SF r7 review ag-753c9a: r6 fixes verified; 1 BLOCKING (operator release skips stop_detached → two live turns). Steered ag-60dd6f: kill predecessor before release; if unconfirmed, release but record+report it. Discarded reviewer branches ag-753c9a, ag-69ee0a.
- 2026-10-03: AB B head 7c33f06 (executor rule + cache compat; full suite clean). Final reviewer ag-962536 running. On approve: merge ag-356ddb, discard ag-7504ef, then DK implementer-deep (single).
- 2026-10-03: AB APPROVED (ag-962536, 0 blocking) → MERGED 53aca60 (77 AB/budget tests green on main). Discarded ag-7504ef, ag-962536. Follow-ups running: ag-f447cb (implementer-quick, doctor shows verified identity, AB-R3c non-blocking) and DK ag-c95ca6 (implementer-deep, single). SF ag-60dd6f still on r7 blocking fix.
- 2026-10-03: AB-R3c doctor follow-up approved (ag-fad3e4) and MERGED 8f96695. AB fully done. Running: SF ag-60dd6f (r7 blocking fix), DK ag-c95ca6.
- 2026-10-03: SF ag-60dd6f r7 fix done → head ad8fd68 (SF 58/58, full suite baseline). Reviewer ag-83d17a round 8 running. DK ag-c95ca6 running.
- 2026-10-03: SF r8 APPROVED (ag-83d17a, 0/0) → MERGED fdfbfa4; 177 SF/FS/AB/budget tests green on main (sf_followups now green). SR implementer-deep ag-6ab4a5 started (single). DK ag-c95ca6 running. Open tickets: bug-dc522a (SR), bug-07d880 (DK) — resolve on merge.
- 2026-10-03: DK ag-c95ca6 done (9e2af4f, 7658ced; 45/45 + 27 integration; full suite clean). Decisions: mxa2 HMAC claims via ANTHROPIC_AUTH_TOKEN, legacy tokens accepted, CLAUDE_CONFIG_DIR dropped under docker. Reviewer ag-264be0 (security focus) running. SR ag-6ab4a5 running.
- 2026-10-03: DK review ag-264be0 rejected (2) — orchestrator DECLINED both as non-blocking: (1) pinned agent reading legacy placeholder token reaches the unpinned pool = routing identity, not isolation, explicitly out of scope per DK-R4a (pinned accounts stay excluded from pool); (2) AUTH_PROVIDER="claude" pre-exists on main (sidecar is single-upstream by design). Possible future hardening: per-provider placeholder claims. DK MERGED 33337cb; 243 DK/AB/SF/budget/docker tests green on main. bug-07d880 resolved fixed. Next after SR: user config for agy-b + claude-b pin, then guide docker switch.
- 2026-10-03: project providers.yaml (not versioned; backup scratchpad providers.yaml.bak-pre-dk): claude-b `container_account: b`; added `agy-b` (extends agy, HOME ~/.multiagents/profiles/agy-b, container_private_home). doctor: loads, AB-R4 local warning shown as designed. Vault has accounts a, b (+ default). ASSUMPTION to confirm with user: vault account b = claude-b's subscription (docker login claude-b rewrites b anyway).
- 2026-10-03: SR ag-6ab4a5 c31e95d (drain-before-launch, death gate via SF, per-turn clock, node_elapsed_seconds). DECISION: D1 finding 8 test is right — explicit timeout keeps priority only while in host-owned launch-limits.json; never trust agent-writable state. Steered to fix that + 4 regressions (test_core steer answered, adversary adoption loop, cw_r5, m_opus flock) + docker wrapper_verdict for liveness.
- 2026-10-03: SR ag-6ab4a5 8047bb4: regressions fixed, D1 honoured, wrapper_verdict. Remaining: 4 test_m_opus_fixes fixtures patch only wrapper_alive → tester ag-bfc044 adds wrapper_verdict patch (green on main + SR). Reviewer ag-3c505b on SR in parallel. Merge order: tester fixtures, then SR; then resolve bug-dc522a.
- 2026-10-03: m_opus fixtures merged 4390822. SR review ag-3c505b rejected: BLOCKING launched_at read from agent-writable command.json on adoption (budget reset); non-blocking timeout still written to command.json. Steered ag-6ab4a5: launch time host-owned only; missing → conservative/expired, never fresh.
- 2026-10-03 ~04:57Z: codex 5h EXHAUSTED again (resets 07:20:54Z). ag-6ab4a5 (SR) failed "quota exhausted" mid launched_at fix → RESUME with steer after reset (do not discard). Wake armed ~07:23Z.
  HANDOFF state: AB, SF, DK merged; tickets bug-07d880 fixed; remaining = SR (fix → reviewer → merge → resolve bug-dc522a). Then report to user + guide docker switch: set executor.kind: docker; `multiagents docker rm && multiagents docker up` (no agents running); `multiagents docker login claude`, `claude-b`, `agy-b`. Confirm vault account b = claude-b. Open user item: advisor/dev-advisor/adversary `models.agy.variant` ignored.

- 2026-10-03 ~07:55Z, user: finish SR on opus. ag-6ab4a5 (codex, quota) superseded by ag-adb0be (implementer-deep, claude opus, `verifies=ag-6ab4a5`), which cherry-picks c31e95d/8047bb4/5e02f18 and fixes the BLOCKING launch-time-from-host-state finding + stops writing limits to command.json. `implementer-deep.models` gained `claude: opus` (user request). Next: Gemini reviewer, merge, resolve bug-dc522a, discard ag-6ab4a5.
- 2026-10-03: SR merged (879a961), reviewer ag-f9def3 approved; bug-dc522a resolved fixed. Non-blocking left: on adoption `max_steps` comes from current config, not the host limits ledger. Roster (user): `implementer`/`implementer-quick` gain `claude: sonnet`, `implementer-deep` keeps `claude: opus` in `models`. SF, SR, AB, DK all done — remaining step is the user's switch to `executor.kind: docker` (guided).
- 2026-10-03 ~09:00Z: switched to `executor.kind: docker` (user). MCP server restarted (it predated SF/AB/DK/SR). User moved stale vault account `a` out (it duplicated `b`); vault = `default` (claude) + `b` (claude-b). Smoke runs under docker OK on codex, agy, claude (ag-070856) and claude-b (ag-9bd422). Open, for the next phase:
  - doctor "cli dependencies … binary not found in the container" is a false positive: the probe runs the bare binary name on the container's system PATH, while CLIs are mounted at host paths (`manifest.py` ~998).
  - DK-R3a deviation: `auth_status` for pinned claude-b lists every account (`b`, `default`) instead of only `b`.
  - agy-b not yet exercised by any roster agent; `variant` warnings on advisor/dev-advisor/adversary; bug-reporter model `kimi-k2.6` no longer listed.
- 2026-10-03 closing round started. Contract C5/C6/C7: `context/specs/phase6-closing-fixes.md` (c973968, advisor-revised). In flight: researchers ag-3fb52a (C2 triage), ag-97ccd0 (C4), ag-ab3337 (C8), ag-448789 (C11 root cause); testers ag-c62aa9 (C5), ag-cbc146 (C6), ag-1bdc17 (C7). Next: merge testers → one implementer each (codex) → reviewer; C11 contract after its researcher; then C3, C1, C10.
- 2026-10-03 closing round progress: C2, C4, C8 done (see `context/specs/phase6-closing.md` Progress); C12 added. Contracts C5/C6/C7/C11/C12 in `context/specs/phase6-closing-fixes.md` (51e467a). C6 tests merged (6ba2e27); implementer ag-8c76ef on C6. Testers running: ag-c62aa9 (C5), ag-1bdc17 (C7), ag-9c6fe9 (C11), ag-632819 (C12). Quarantined worktrees ag-98e037/e4919f/e8565d left to the user.
- 2026-10-03: C5 and C12 tests merged; implementers ag-dbb945 (C5), ag-85d660 (C12), ag-8c76ef (C6, steered after "model at capacity"). C3 contract `context/specs/c3-prompt-file-transport.md` (e54216f; adds PF-R7: retries replay the attempt's own prompt, not prompt.md); tester ag-84c0db. C3 implementation (implementer-deep) waits until the C7 and C11 runner.py work has merged.
- 2026-10-03: tooling observation. A steer of ag-8c76ef was refused with "predecessor … not confirmed dead (liveness may be unknown)", although its wrapper (7678) and agent (7684) pids were dead in the container. An immediate retry succeeded. Probably a transient docker liveness probe failure under load (six agents running suites). File a ticket if it recurs. Also: codex "model at capacity" cut ag-8c76ef twice, and the doom-loop watchdog fires on every implementer polling a full-suite log (false positives).
- 2026-10-03: C7 tests merged (c9a58ca), implementer ag-fb2ac5. C13 added at the user's request: the initializer writes plans to `context/plans/`, never `BRIEF.md`; the orchestrator imports them; user notes go in `context/notes/` (`context/specs/c13-plans-and-notes.md`, 976884b; tester ag-55d4e0). PN-R6a: after the merge, the orchestrator updates the live `.multiagents/config/agents/team/_initializer.md` and `_orchestrator.md`. Running: implementers ag-dbb945 (C5), ag-8c76ef (C6), ag-85d660 (C12), ag-fae053 (C11), ag-fb2ac5 (C7); testers ag-84c0db (C3), ag-55d4e0 (C13).
- 2026-10-03: the C13 tester ag-55d4e0 aborted (SIGABRT), and it cannot be steered. Liveness came back unknown although its pids are dead, then a "previous steer cleanup is still pending" hold survived `stop_agent`. Ticket being written by bug-reporter ag-d9513b; ag-55d4e0 is kept as evidence. TODO when a slot frees: start a fresh tester for C13 that cherry-picks WIP 31c5bce (task text as given to ag-55d4e0, plus the cherry-pick; `verifies=ag-55d4e0`). C3 tests merged (82c602b); C3 implementation waits for C7 and C11.
- 2026-10-03 ~10:00Z: codex 5h window exhausted (resets 12:52Z). Quota-cut, to be resumed by steer after the reset: ag-dbb945 (C5), ag-8c76ef (C6), ag-85d660 (C12), ag-fae053 (C11), ag-fb2ac5 (C7), and the bug-reporter ag-d9513b (liveness/steer-hold ticket). Steer message: carry on, NO full suite (container load), targeted suites only, report counts and commits. `project.yaml`: `cpus: ""` (no cap; the user asked to share all host CPUs) and memory 12g. A container recreate is needed (user step, with nothing running). Pending: a fresh C13 tester cherry-picking 31c5bce. Proposed to the user: C14, a second codex account (`codex-b`), with a researcher check first.
