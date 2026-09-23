# A conversation reads the current code (ticket bug-7f6ba7)

**Status:** contract, 2026-09-23. Requested by the user after bug-7f6ba7.

## The defect

`Runner.consult()` creates a conversational agent's worktree on turn 1, from
the base branch as it is at that moment (`runner.py` ~2277). Every later turn
reuses that directory as it is. Work merged into the base branch between turns
stays invisible. The advisor then answers about code that no longer exists,
and nothing in the result says so. On 2026-09-23, advisor ag-25c350 reviewed
52105e7, d4ae4ec and 29218c6 against pre-merge code, and several of its claims
were false.

## Terms

- **base**: the branch the node's worktree was created from. That is
  `config.base_branch`, else the branch checked out in the project root,
  resolved **at the time of each turn**, not remembered from turn 1.
- **own work**: commits on the node's branch that are not on base, plus any
  uncommitted change (tracked or untracked) in its worktree.

## Behaviours

**CF-R1 — a turn starts on the current base when nothing would be lost.**
Before running turn N ≥ 2 of a conversation, if the worktree has **no own
work** and base has advanced past the worktree's HEAD, the node's branch and
worktree are moved to base's current HEAD. The agent's session (its
conversation memory) is kept; only the files change.
*Verified by:* a test that consults (turn 1), commits a change to a file on
base, consults again (turn 2), and asserts the worktree file has the new
content before the agent runs.

**CF-R2 — own work is never destroyed.** If the worktree has own work
(commits not on base, or uncommitted changes), nothing in it is reset,
discarded or rewritten. The turn runs on the worktree as it is.
*Verified by:* tests with (a) an uncommitted change and (b) a commit of the
node's own, each followed by base advancing and a second consult. Both
assert that the own work is intact and that the branch was not moved.

**CF-R3 — the agent is told when its view moved or is stale.** When CF-R1
moved the worktree, the turn's prompt starts with one line saying the
worktree was updated from `<old short sha>` to `<new short sha>`, and that
anything it read on earlier turns may have changed. When CF-R2 prevented a
move, the line says that the worktree is `<n>` commits behind base and was
not updated because it holds own work. When neither applies, no line is
added: the message is passed through unchanged.
*Verified by:* asserting on the prompt handed to the provider in each of the
three cases.

**CF-R4 — the caller can see what was read.** `consult`'s result gains
`commit` (the short sha of the worktree HEAD the turn ran on), `base_commit`
(base's short sha at the start of the turn), and `behind` (the number of base
commits not in the worktree HEAD; 0 after a CF-R1 move). The fields exist on
turn 1 as well. The existing fields are unchanged.
*Verified by:* asserting the three fields on turn 1, after a CF-R1 move, and
in the CF-R2 case (`behind` > 0).

**CF-R5 — a refresh that fails does not lose the turn.** If moving the
worktree fails (a git error, or a missing or renamed base), the turn still
runs on the worktree as it was, with the CF-R2 line. It does not raise. The
failure is recorded as an event on the node.
*Verified by:* a test with base deleted or renamed between turns.

**CF-R6 — turn 1 and non-conversational runs are unchanged.** `start_agent`
and the first `consult` of a conversation behave exactly as before, apart
from CF-R4's added fields.
*Verified by:* the existing suite staying green.

## Out of scope, recorded

- Letting read-only agents run `git show` under agy's `--sandbox` is a
  provider-permission question, and is not decided here. With CF-R1 in
  place, the advisor usually no longer needs it.
- Refreshing between turns of `steer_agent` on non-conversational agents.
