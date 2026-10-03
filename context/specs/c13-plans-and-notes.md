# C13 — the initializer plans in its own files; the user has a notes directory: the contract

**Status:** contract, orchestrator, 2026-10-03.
- **Requested by:** the user, who validated the orchestrator's proposal and asked for a notes directory.
- **Ids:** `PN-R*`. They are never renumbered.
- **Why:** the initializer and the orchestrator both wrote `BRIEF.md` and committed on main in the same repository. Planning the next phase therefore had to wait for the orchestrator to finish. With this change the user can run `multiagents init-agent` in parallel with `multiagents run`.

## Behaviours

**PN-R1: the initializer never writes `BRIEF.md`.**
- **Where it writes.**
  - The initializer writes plans to `context/plans/<YYYY-MM-DD>-<slug>.md`.
  - It may create **new** files under `context/specs/`.
  - It does not modify `BRIEF.md`, and it does not modify an existing spec file. An amendment to an existing spec goes in the plan, and the orchestrator applies it.
- **What it commits.** It commits only the paths it wrote. If git's index lock is held, because the orchestrator is committing, it retries with a bounded wait. It never commits anyone else's changes.
- **Bootstrap exception.** A project with no `BRIEF.md` yet keeps today's behaviour: the first `init-agent` creates `BRIEF.md`, because nothing else exists to hold the brief. The implementer documents this exception.
- **Verified by:** the shipped and project initializer instructions say so. A test asserts that the shipped `_initializer.md` names `context/plans/` and forbids editing `BRIEF.md` outside the bootstrap.

**PN-R2: a plan has a fixed shape.**
- **The header** carries a `status:` of `draft`, `ready` or `imported`, plus `imported_in:` (a commit) once imported.
- **The sections:**
  - **Apply now:** decisions that affect work in progress, such as a standing rule, a roster change or a cancelled item;
  - **Next phase:** work for the phase after the current one;
  - **Config changes:** every change to an unversioned config file (`agents.yaml`, `providers.yaml`, `project.yaml`), each marked as already applied by the initializer or as proposed;
  - **Notes considered:** see PN-R5.
- **Template.** A template ships with multiagents, and `multiagents init` copies it to `context/plans/TEMPLATE.md`.
- **Verified by:** the parser of PN-R4 reads the template and a filled example. A plan with a missing or unknown `status` is reported as malformed, not ignored.

**PN-R3: the orchestrator imports plans.**
- **When.** At every stop (where it reads tickets) and before handing back, the orchestrator looks for `ready` plans.
- **Apply now.** It applies "Apply now" at its next stop. Each decision is recorded in `BRIEF.md`, or in the spec concerned, citing the plan file.
- **Next phase.** It imports "Next phase" into `BRIEF.md` when the current phase's work is done, or earlier if the plan says so.
- **Marking.** It then sets `status: imported` and `imported_in: <commit>`.
- **What it ignores.** It never acts on a `draft`. It never acts on a user note directly (PN-R5).
- **Verified by:** the shipped and project orchestrator protocol say so (review). PN-R4's tool makes the state visible.

**PN-R4: plans and notes are visible.**
- **A read-only MCP tool `list_plans`** returns each plan's path, status, title (its first heading), `imported_in` and section presence, and flags malformed plans. It also returns the notes summary of PN-R5.
- **`multiagents doctor`** prints one line per `ready`, unimported plan, plus the count of unprocessed notes.
- **Both stay quiet** when `context/plans/` and `context/notes/` are absent.
- **Untrusted content.** Plan and note files are user- or agent-written. They are parsed as data, with bounded reads, and symlinks are refused.
- **Verified by:** tests over a fixture `context/` with `draft`, `ready`, `imported` and malformed plans, and notes.

**PN-R5: the user's notes directory.**
- **What it is.** `context/notes/` belongs to the user: free-form Markdown, in any number of files. Agents never edit, move or delete anything there.
- **The initializer reads it.**
  - It reads every note at the start of each session.
  - It takes notes into account when planning.
  - It records in the plan's "Notes considered" section each note it used or deliberately set aside, as path, content hash and one line on what it did with it.
- **Unprocessed notes.** A note is unprocessed when no plan records its current content hash. That count is what PN-R4 reports, so editing a note makes it unprocessed again.
- **The orchestrator** does not act on notes. Notes reach the work only through a plan.
- **Scaffolding.** `multiagents init` creates `context/notes/README.md`, explaining the above in a few lines. The README itself is never counted as a note.
- **Verified by:**
  - a note whose hash appears in a plan is processed;
  - an edited note becomes unprocessed;
  - the README is excluded;
  - the initializer instructions say all of the above (review).

**PN-R6: this project is migrated.**
- The project copies of the instructions are updated together with the shipped ones. These are `.multiagents/config/agents/team/_initializer.md`, `_orchestrator.md`, and any other project copy that the loader actually uses. The implementer finds out which are live and says so.
- `context/plans/TEMPLATE.md` and `context/notes/README.md` are created in this repository.
- `BRIEF.md` is not restructured.
- **Verified by:** review.

**PN-R7: no regression.**
- `init-agent` and `run` launch as before, and the existing init/driver/doctor suites stay green.

## Out of scope

- Any lock between the two sessions beyond git's own index lock.
- Turning notes into tickets automatically.

## Revision after the advisor's check (2026-10-03, ag-894250, before tests)

These override any earlier wording they contradict. Decisions by the orchestrator.

**PN-R1a: commits go through a helper.**
- The initializer commits with a new CLI command, `multiagents plan commit <path>...`.
- **What it does:**
  - it refuses paths outside `context/plans/` and `context/specs/`, and it refuses an existing spec file that is modified;
  - it refuses while the root is in a merge, rebase or conflict state;
  - it commits with `git commit --only -- <exact paths>`, so the orchestrator's staged changes are never included;
  - it retries **only** on index-lock contention, once a second for at most 30 s;
  - it never deletes a lock, resets, stashes, or retries any other failure.
- The instructions tell the initializer to use it.
- **Verified by:** tests on a scratch repo with:
  - a staged unrelated change that stays out of the commit;
  - a held `index.lock` released after 2 s, which succeeds;
  - a held lock never released, which fails after the bound;
  - a merge in progress, which is refused;
  - a path outside the allowed roots, which is refused.
- The helper reduces the risk of concurrent root transactions but does not make them fully safe. That is accepted.

**PN-R2a: exact format.**
- **Header.** A YAML front-matter block (`---` … `---`) with `status` (`draft`, `ready`, `applied` or `imported`), and optional `title`, `applied_in` and `imported_in`.
- **Sections.** Level-2 headings, matched exactly: `## Apply now`, `## Next phase`, `## Config changes`, `## Notes considered`. A missing section means empty.
- **Notes considered.** One line per note: `- <path relative to repo root> sha256:<64 hex> — <what was done>`. The hash is SHA-256 over the note's raw bytes.
- **Discovery.**
  - Only direct children `*.md` of `context/plans/` and `context/notes/`, not recursive.
  - Excluded: `TEMPLATE.md` in plans and `README.md` in notes.
- **Bounds.** At most 1 MiB per file and 500 files per directory. Anything over a bound is reported, not read.
- **Symlinks.** A symlinked file, or a symlinked `plans/` or `notes/` directory, is refused and reported.
- **Malformed plans.** A plan with no front matter, an unknown status or an over-bound size is reported as malformed, with its reason.
- **Which plans count for notes.** Only `ready`, `applied` and `imported` plans mark notes as processed. A `draft` or malformed plan does not.

**PN-R3a: import in two steps.**
1. After applying "Apply now", the orchestrator commits the decisions, then marks the plan `status: applied` and `applied_in: <that commit>` in a following commit.
2. When "Next phase" is imported into `BRIEF.md`, the orchestrator commits the import, then marks the plan `status: imported` and `imported_in: <that commit>` in a following commit.

A plan with an empty "Next phase" goes straight to `imported` at step 1.

**PN-R4a: what is tested.**
- **Automated:** parsing, discovery, bounds and symlinks; the `list_plans` and doctor output; the commit helper; and scaffolding.
- **Instructions:** they are tested on the **assembled launch prompt** of the `initializer` and `orchestrator` roles, not on the shipped file alone. Instruction precedence is `team/_initializer.md` resolved project → global → shipped (`config.py` ~865/990), and the orchestrator uses the active team's list (`driver.py` ~65-82).
- **Agent behaviour** (obeying the write rules, importing plans) cannot be proven by tests. It is checked by review, then by one recorded live exercise that the orchestrator carries out after the merge.

**PN-R5a: scaffolding when `context/` already exists.**
- `multiagents init` adds `context/plans/TEMPLATE.md` and `context/notes/README.md` when they are missing, even if `context/` exists (`cli.py` ~901-914 guards on its absence today).
- It never overwrites them, even with `--force`.

**PN-R6a: this project's live copies.**
- `.multiagents/config/` is not versioned, so the implementer's worktree does not contain it.
- The orchestrator updates the live project copies after the merge, from the merged shipped text plus this project's existing customisations, and records that in BRIEF.
