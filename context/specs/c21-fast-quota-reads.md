# C21 — the monitor never waits for quota reads, and reads run in parallel: the contract

**Status:** contract, orchestrator, 2026-10-04.
- **Found by:** the user ("the monitor page takes more than 10 seconds to show").
- **Ids:** `FQ-R*`. They are never renumbered.
- **Pipeline:** tester, then implementer, then reviewer.

## Measured (orchestrator, main at de27ee3, fresh monitor)

| Request | Time |
|---|---|
| `GET /` | 2 ms |
| first `GET /api/state` | **13.7 s** |
| following `GET /api/state` | 0.2 s, while the cache is warm |

`budget.read_all()` (`budget.py` ~1604-1650) reads every provider **serially**:
- some readers spawn a CLI (agy's `/usage`, the codex app-server);
- others make HTTP calls (claude oauth for each vault account, opencode, z.ai).

Its cache lasts `_CACHE_TTL = 60 s` (~1153). So every monitor start blocks for about 13 s, and once a minute a poll can block for that long again. `providers_view` (`monitor/snapshot.py`) calls `read_all` on the request path.

## Behaviours

**FQ-R1: provider reads run concurrently.**
- `read_all` reads independent providers concurrently, through a bounded worker pool. A cold read then costs about the slowest single provider, not the sum.
- **Kept as they are:**
  - PS-R5: a shared source is fetched once per call, for all its dependents (`budget_from`, `auth_from`);
  - BP-R1: one parse per config file for the whole call;
  - per-provider timeouts, the cache, and the output, which is identical to the serial result.
- **Verified by:**
  - with fake readers that each sleep 1 s, five providers return in well under 5 s;
  - a shared source with two dependents is fetched exactly once;
  - the results equal those of a serial run.

**FQ-R2: the monitor never blocks a poll on a quota read.**
- `/api/state`, the details page data (`/api/quota`) and the TUI poll serve the last known reading immediately, and refresh it in the background (stale-while-revalidate).
- **Cold start:** with no reading yet, they return at once. The quota part is marked as loading: each provider row shows "loading…", and the agents, tree and alerts views are complete.
- **One refresh at a time:** at most one background refresh runs, and concurrent polls never start a second one.
- **A failed refresh** keeps the last good reading, marked with its age. It never blanks the panel.
- **Verified by:**
  - with a reader that sleeps 5 s, the first `/api/state` answers in under 1 s, with quota rows marked loading;
  - a later poll, after the refresh completes, carries the reading;
  - concurrent polls trigger exactly one refresh.

**FQ-R3: the MCP `budget_status` gains from FQ-R1 and keeps its contract.**
- `budget_status` keeps its blocking semantics: it returns a complete reading, but now in parallel time.
- Its output is unchanged.
- **Verified by:** the existing `budget_status` and budget tests stay green.

**FQ-R4: no regression.**
- The C18 and C19 panel and details page, the alerts, `doctor`, and the router's use of budgets behave as before.
- Readers are never run more often than today's cache allows.
- The existing suites stay green.

## Out of scope

- Changing any provider's reading method, or the TTL values.
