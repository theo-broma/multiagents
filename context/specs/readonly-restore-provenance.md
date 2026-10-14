# Readonly restore by provenance (RP)

Tooling queue item 1 (BRIEF, 2026-10-13/14). Approved by the user on
2026-10-14 as work outside plan part 2.

## Problem

`Results.capture()` (`src/multiagents/scheduler/results.py`, around line 319)
restores every protected path that differs between the attempt's
`input_commit` and the captured tip, and commits the result as
"multiagents: restore readonly paths". The diff covers the whole range, so a
change the run did not make, but *merged* during the run, is reverted too:

- an implementer told to `git merge <tester commit>` has the tester's edits to
  existing tests reverted at capture; its next round and its reviewer test
  against stale files;
- a run that merges current main has every test changed on main since its
  input reverted;
- an adversary (`readonly_paths: ["**"]`) that merges the implementer's
  result has the implementer's `src/` changes reverted;
- any branch that later merges such a result carries the restore commit and
  reverts those files again.

## Terms

- **Entry**: a path's full git tree entry (mode, type and object id), or
  *absent*. Every comparison below compares entries, never bytes alone, so a
  mode or symlink change counts as a change.
- **Trusted source**: a commit reachable from the captured tip and not from
  the input commit that is either
  (a) on the first-parent chain of the base branch's tip, as resolved once at
  capture, or
  (b) a recorded generation commit of any node (`generations[].commit`,
  whatever its verdict, including nodes disposed since), **itself, not its
  ancestors**.
  Only the source's own tree counts. Its ancestors do not: a restore commit's
  parent is the agent's pre-restore tip, which carries exactly the edits the
  restore removed. A generation tree is post-restore, so every protected
  entry in it was already trusted when it was captured; that is why the
  verdict does not matter here. The base branch's first-parent chain only
  moves by the orchestrator's commits and `merge_node`'s squashes.
- **Changed path**: a protected path whose entry at the captured tip
  differs from its entry at the input commit (today's `MDT` set; additions
  excluded).

## Behaviour

**RP1 — merged trusted changes are kept.** A changed path whose entry at the
tip equals its entry in any trusted source is not restored and not listed in
`readonly_reverted`. Verified by: an implementer attempt that merges a tester
node's generation commit editing `tests/test_x.py`, with no own change to it:
the tester's version kept, no restore commit, `readonly_reverted` empty; the
same with a commit from the base branch's first-parent chain; an adversary
attempt (`["**"]`) merging an implementer generation that changed
`src/m.py`: kept.

**RP2 — own changes are still restored.** Every other changed path is
restored and listed in `readonly_reverted`, as today. It is restored to the
trusted entry when the trusted sources that differ from the input on that
path all carry the same entry; otherwise (none, or several different ones) to
its entry at the input commit. Verified by: an implementer that merges a
tester generation then edits the same test: restored to the tester's
version; an implementer editing a test with no merge: restored to the input
version, as today; a deleted protected file restored the same way; a mode
change on a test (`chmod +x`) restored; two tester generations carrying
different versions of one test, merged and then edited: restored to the
input version.

**RP3 — not a loophole.** Nothing else is trusted. In particular a commit
the run made, a pre-restore ancestor of another node's generation, a commit
from another node's live branch that is not a recorded generation, and a
commit whose message or trailer claims an origin are never trusted sources.
Verified by: a run that makes a side commit editing a test and merges it:
restored; a run that merges the pre-restore parent of another node's restore
commit: restored; a run that merges a commit with a forged "multiagents:
integrate" message: restored.

**RP4 — the decision is frozen.** Capture resolves the base branch to one
commit and the set of generation commits once, decides every changed path,
and persists the decision (base commit, per-path outcome and, for kept
paths, the source) with the retained result before the retention ref is
published. A replay after a restart reuses that decision; it never
recomputes it against records that have moved since. If a trusted source's
objects cannot be read, or the base branch is unset or cannot be resolved,
capture fails as other capture errors fail today, instead of deciding with a
smaller trusted set. Verified by: a capture interrupted after the decision
is persisted, a new generation recorded that would change the outcome, then
replayed: same result commit and same lists; an unresolvable base branch:
capture fails, nothing integrated.

**RP5 — what is unchanged.** Additions are never restored. Agents with
`readonly_paths: []` are exempt. Replay still reuses the retained result
commit and still refuses a result whose restored tree changed (strengthening
that check to the checkout tip is out of scope). `readonly_reverted` keeps its
shape and its places: the result and the generation record. `merge_agent`
and the non-scheduler path are unchanged. Verified by: the existing tests
named below, unchanged and green.

**RP6 — evidence.** Kept paths are recorded as `readonly_kept: [{"path":
..., "source": <commit>}]`, absent when empty, in the result and copied into
the generation record beside `readonly_reverted`. When several trusted
sources match, `source` is the base branch commit if it matches, else the
matching generation commit that comes first in commit order from the tip
(`git rev-list --topo-order`). Verified by: RP1's cases asserting
`readonly_kept` on the result and on the generation.

## Amendment after the adversary pass (2026-10-14, run ag-d934db)

The pass showed that a *stale* trusted source vouches for a revert: a base
commit or a sibling generation cut before the input still carries the old
entry, so a run that merges it and then writes the old entry back (or
deletes a file the input added) had its own change kept. RP1's wording
allowed it; RP3 and Decision 3 did not. RP7 settles it.

**RP7 — a source vouches only for a change it brought.** For each changed
path, the decision runs in this order:
1. *Eligible sources*: the trusted sources of the Terms section.
2. *Supersession*: an eligible source that is an ancestor of another
   eligible source is dropped, so a later trusted snapshot on the same line
   of history (including one that reverts an earlier one) is the one that
   counts.
3. *Vouching*: a remaining source vouches for the path only if its entry
   differs from the path's entry at every merge base of that source and the
   input commit (`git merge-base --all`). A source with no merge base with
   the input vouches for nothing. A failure of git is a capture failure, not
   "no merge base".
4. *Decision*: RP1 keeps the path when its tip entry equals the entry of a
   vouching source; RP2 restores it to the vouching sources' entry when they
   all carry the same one, else to the input's entry.
Entries are compared whole (mode, type, oid, absent), as before. RP7 applies
only when a capture decision is first made; a decision already persisted
before RP7 is replayed as recorded (RP4). Verified by: the adversary's
tests in tests/test_rp_adversary_stale_source.py (revert to a moved base's
version, deletion of a file the input added, revert through an unrelated
sibling generation: each restored to the input); a stale source steering the
restore target: restored to the input, not the stale version; a trusted
revert v1→v0 descending from a trusted v0→v1 with input v0, then an own edit
to v2: restored to v0; a criss-cross history with two merge bases; an
earlier generation of a node with several, merged on its own: vouches.

**RP8 — protected-path matching covers every file name.** `readonly_paths`
matching treats a newline, a leading or trailing space and any other
character in a file name like any other character: `*`, `?` and `**` match
them, and the path is never trimmed before matching. Trimming of the
configured patterns themselves is unchanged. Verified by:
tests/test_rp_adversary_path_names.py; a path with an embedded newline, a
terminal newline and a trailing space, under `*`, `?`, `**`, a directory
pattern and an ordered negation.

**RP9 — coverage the first suite lacked.** Verified by: an unresolvable
base with no protected path changed is captured as today (Decision 8); a
replay whose restored tree changed is refused (RP5).

## Existing tests that must stay green unchanged

- `tests/test_nc_m3_branches.py::test_nc_r65_readonly_paths_are_restored_from_the_input_commit_and_recorded`
- `tests/test_nc_m3_branches.py::test_nc_r65_a_result_that_touches_no_protected_path_records_no_reverted_path`
- `tests/test_nc_m3_result_replay.py::test_nc_r61_r65_capture_replay_keeps_the_first_readonly_restoration_commit`
- `tests/test_nc_m3_adv_live.py::test_adv_a_deleted_readonly_path_is_restored_before_integration`
- `tests/test_core.py::test_editing_a_protected_file_is_reverted_and_the_rest_still_merges`
- `tests/test_core.py::test_deleting_a_protected_file_is_caught_too`
- `tests/test_core.py::test_an_agent_that_owns_the_files_is_exempt`
- `tests/test_core.py::test_a_forgotten_mutation_is_reverted_rather_than_merged`

If one of them asserts the old whole-range behaviour in a case RP1 now
covers, the tester reverses it deliberately and says which.

## Decisions

1. **Provenance from scheduler records, not from commit metadata.** Messages
   and trailers are written by the agent and prove nothing (RP3). The base
   branch and the generation records are host state the agent cannot write.
2. **Trees of trusted commits, never their ancestry** (advisor, 2026-10-14):
   a restore commit's parent is the forbidden version, so trusting ancestors
   would hand a run the very edits capture removed.
3. **Entry equality, not hunk attribution, decides.** A path is kept when
   its whole entry matches a trusted version; this tells "merged unchanged"
   from "merged then edited" without attributing changes to commits.
4. **The scheduler path only.** `merge_agent` reverts at merge time from a
   different reference; it has not caused the failures above and is left as
   it is.
5. **Tasks merge recorded generations.** Only the commit recorded in a
   node's `generations` (what `get_node` shows, usually the node's branch
   tip) is trusted, not an earlier commit inside that branch. Tasks that tell
   an agent to merge another node's work name that recorded commit.
6. **Restore commits already on branches are not rewritten.** Branches made
   before this lands keep their restore commits; the BRIEF workarounds
   (explicit restore lists, copying tests at merge) stay until those branches
   are gone.
7. **"Unset" base branch means unresolvable after today's fallback**
   (RP tester, 2026-10-14): capture resolves the base branch as
   `Results.prepare` does (`git.base_branch`, else the current branch), and
   fails only when that still resolves to no commit.
8. **An unresolvable base fails capture only when it matters:** when at
   least one protected path changed. A result touching no protected path is
   captured as today.
9. **RP6's base source is one commit:** when several commits of the base
   branch's first-parent chain match, `source` is the one nearest the tip in
   `git rev-list --topo-order` from the tip, as for generations.
10. **Provenance means a change, not a matching snapshot** (adversary pass,
    2026-10-14; advisor-checked): RP7's supersession and merge-base rule is
    git's own notion of what a merged side brought. A node whose own
    `readonly_paths` do not cover a path can still change it and have its
    generation trusted; that is Decision 1 working as designed, not a gap.
