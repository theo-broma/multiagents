# Phase 6 — hardening, then two features

**Status: requirements, not a contract.** Written by the initializer on
2026-09-28 and reviewed by the advisor (ag-3f9bba, turn 4). The orchestrator
writes the contracts, citing these ids. `H-*` ids are hardening items; D1 and
D2 are features, and D1 has its own file: `limit-notices.md`.

The user approved the order, and asked for the advisor to be consulted
regularly and for the work to be run as autonomously as possible.

## Order

Work the items strictly in this order, except that the D1 contract may be
written at any time. It is only *implemented* after H4.

1. **H1** — a forged `branch_pending_delete` (security).
2. **H2** — a refused or filtered run reported as `done` (false completion).
3. **H3** — host execution during a merge: base hooks and the wider audit
   (security).
4. **H4** — an empty fallback model on the steer and start paths.
5. **D1 implementation**, from its contract.
6. **H5–H7** — reliability of routing and first use.
7. **H8** — prompt transport past 128 KiB.
8. **H9–H13** — the old queue and cosmetic items.
9. **D2** — tmux step 1.

Deferred, and **not** part of this phase:
- phases 2 and 3 of the review (the 72 by-design reds);
- tmux step 2;
- nested codex spawns (latent: every codex role has `can_spawn: false`).

## Items

- **H1 — a container-forged deletion must not destroy a branch.**
  - The problem: `reap_pending_branches()` force-deletes `agents/*` branches
    on the host, trusting fields in `tree.json` (`runner.py` ~4243–4280).
    The container can write `tree.json` (`.multiagents` is writable,
    `executor/docker.py` ~935–968).
  - The attack: an agent forges a terminal node and a deletion request, and
    destroys another agent's unmerged branch.
  - The requirement: host-side deletion is authorised only by a completion
    record held on the host, somewhere the container cannot write. Nothing
    the container can write may authorise it.
  - Pipeline: full, **with an adversary**. The adversary's job is to delete
    a branch it does not own from inside the container.
  - This is SG follow-up, decision #16 in `sandbox-git.md`.

- **H2 — a refused, content-filtered or otherwise non-completing run is
  never reported as `done`.**
  - Why it matters: a refused tester, adversary or reviewer otherwise looks
    like a check that passed, and that check gates a merge.
  - The requirement: find every provider's refusal and content-filter
    signal, and make each surface as a distinct non-success status that
    `merge_agent` and `wait_for_agents` show.
  - Seen at least once in phase 0 (BRIEF, "Progress" 2026-09-27, "the
    content-filter refusal reported as done").
  - Pipeline: full, adversary optional.

- **H3 — no agent-authored code runs on the host during a merge unless the
  user opted in.**
  - SG-R5 base hooks run on the host against merged content. A base hook
    that runs the tests executes the agent's code on the host, outside the
    SG-R1 boundary.
  - **Default: base hooks are off during host merges, and a `project.yaml`
    switch turns them on.** The switch is documented next to the setting,
    and a run that skips a hook says so once.
  - This touches an earlier user decision, "hooks kept" (CI-R5, 2026-09-27),
    but that decision was about agent commits made in the container, not
    host merges. So it is raised as `NEED_DECISION(merge-hooks)` with this
    default.
  - **Audit** everything else Git may run on the host during `gitops.merge()`
    (`gitops.py` ~1083–1125):
    - filters and drivers from `.gitattributes` or config (`filter.*`,
      `merge.*.driver`, `diff.*.textconv`);
    - `core.fsmonitor` and `core.hooksPath`;
    - any other config key that names a command;
    - LFS.

    Record what you find. Anything agent-controllable that executes on the
    host is closed or put behind the same opt-in.
  - Running hooks inside the container is **later work**, not this phase:
    the base checkout is read-only there and it needs its own design.
  - Pipeline: full, **with an adversary**.

- **H4 — an empty fallback model is never a valid route.**
  - CX-C28 refuses it on the consult path only.
  - `_spec_of()` (steer, `runner.py` ~1571–1587) and the start fallback
    path (~2826–2835) still accept it when routing picks a family sibling.
  - The requirement: refuse it there too, with the same report.

- **H5 — the claude budget under Docker.**
  - The reader looks only at the host account.
  - It must read the account that the agents in the container actually
    spend against.

- **H6 — `refresh-models`.**
  - It fails before the first codex use.
  - A refresh dropped the `opencode-go/*` models. A provider that fails to
    list must never erase another provider's entries.

- **H7 — opencode startup failures.**
  - They have lasted about 11 days, and the router keeps picking opencode.
  - The router must stop routing to a provider that keeps failing at
    startup until it recovers.
  - An explicit `model:` override that names a provider must pin that
    provider. It is not a hint the router may ignore.

- **H8 — prompt transport.**
  - Adapters receive the prompt as one argv element (`providers.py`
    ~291–320). Above the kernel's per-argument limit (128 KiB) the process
    fails to start. It is not truncated silently.
  - Generic fix: a bounded run-file transport in the provider contract.
  - Until it exists, an oversize prompt fails with a clear message naming
    the limit.

- **H9 — AGENTS.md names `scripts/test-chunk.sh`** as the way to run the
  full suite.
- **H10 — a live compaction check** (queue item 7 of phase 0).
- **H11 — the config-drift warning** (phase 0 queue).
- **H12 — cleanup.**
  - The 5 unmerged `agents/*` branches and the idle nodes.
  - Before deleting anything, read each branch and confirm that its work is
    merged or no longer needed. Record the result.
- **H13 — cosmetic: `packed-refs.lock` on container commits.**
  - Git's auto-maintenance tries to take the lock, and the commit still
    lands.
  - Silence it, for example with `gc.auto=0` or
    `maintenance.auto=false` on container-side commits.

- **D2 — tmux, step 1 only.**
  - Read-only viewer windows that follow `runs/<id>/stream.jsonl`.
  - The runner keeps owning the processes.
  - The design notes are in BRIEF, "tmux to watch the tasks". Settle their
    "to decide" list in the contract. Suggested defaults:
    - one tmux session per project;
    - the socket under the project's `.multiagents/`, with permissions 0700;
    - the viewer only, no command windows;
    - no tmux installed: the monitor hides the button.

## Pipelines and the roster

- The roster is unchanged. Codex now carries the adversary, reviewer and
  advisor, so H1 and H3 get a real adversary from a family other than the
  implementers'.
- Live checks and any container recreate happen **only with no agent
  running**.
