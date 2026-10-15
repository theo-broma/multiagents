---
status: ready
---
# Monitor: two display defects in the scheduler panel

Raised by the user on 2026-10-15, reading the monitor page during phase 7
part 2. Small, no spec: the contract can cite this plan.

## Apply now

**Order.** Independent of phase 7 part 2. It touches
`src/multiagents/monitor/page.html` and `src/multiagents/scheduler/overview.py`.
The user decisions plan (`2026-10-15-user-decisions-queue.md`) also changes
the monitor page, so the orchestrator orders the two by their locks.

**1. "window closes ?" on a node that declares no window.**
- **The cause.** `windows.evaluate` returns `open: true` with
  `next_close: null` when a node declares no run window (NC-R38).
  `page.html` (around line 403) then prints "window closes ?", which reads
  like an unknown deadline.
- **The fix.** A node with no declared window shows nothing about windows.
  A declared window with no next change in range says so in words, never
  "?".
- **The text panel.** `overview.py` (around line 177) already omits empty
  keys; keep it consistent.

**2. The `sessions:` line lists every alias of the whole plan.**
- **The cause.** `_aliases` (`overview.py`, around line 88) returns every
  alias of every node, the 512 done and 60 cancelled included, so the line
  holds dozens of `A@<uuid>` / `B@<uuid>` entries. The bound ones are not
  necessarily those of live nodes.
- **The fix.** By default, show only the aliases used by nodes that are
  `open`, `running`, `suspended` or `held`.
  - Put each one on its node's row, or group them by loop, with the node ids
    and the short instance id.
  - Say in words what bound means: bound means a real provider session
    exists; unbound means none was created yet.
  - The full list stays reachable, behind a toggle or a count such as "+48
    finished".
- **Keep the API complete.** `/api/state` keeps the full alias list, or adds
  a filter, so the future GUI can still read everything.

**Done when** a test pins each behaviour, a reviewer has checked it, and
the page has been looked at once with live nodes.

## Next phase

None.

## Config changes

None.

## Notes considered

`context/notes/` contains only README.md: there are no user notes.
