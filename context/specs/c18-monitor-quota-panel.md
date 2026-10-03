# C18 — the monitor's quota panel shows every window, uniformly: the contract

**Status:** contract, orchestrator, 2026-10-03.
- **Requested by:** the user, from a live monitor screenshot.
- **Diagnosis:** researcher ag-798966.
- **Ids:** `MQ-R*`. They are never renumbered.
- **Pipeline:** tester, then implementer, then reviewer.

## Why

The MCP `budget_status` and the monitor's `providers_view()` share one reading layer, `budget.read_all()` (`server.py` ~1460, `monitor/snapshot.py` ~153). The monitor then diverges in two ways:
- it renders each provider through that provider's own `usage` action, which is free text (`snapshot.py` ~76-131, ~162);
- it checks installation on the host (`snapshot.py` ~177 → `providers.py` ~469-482).

Under the docker executor, that gives three results:
- **Every provider reads "not installed".** For agy, agy-b and agy-partner the `usage` action also fails with "binary 'agy' not found", because the binary exists only in the container. `budget_status` reads the same providers correctly.
- **claude and claude-b show only "N% of the tightest window"**, because `claude.sh` ~546-548 prints only that. Yet `read_claude()` keeps every window.
- **Each provider uses its own layout.** The glyphs differ (`#`/`.` against `█`/`░`), some show reset times and others do not, and the column layouts differ.

## Behaviours

**MQ-R1: every window of every provider is shown.**
- For each provider whose budget reading has windows, the panel shows one line per window: the window's name, a bar, the percent used and its reset time.
  - The reset time uses the existing local "Oct 05 16:00 CEST · in 1d21h" label (Q6).
  - "—" is shown when the reading has no reset time.
- This covers claude/claude-b (session and weekly_all), agy/agy-b/agy-partner (the gemini-* and 3p-* windows), codex/codex-b (5h and weekly), opencode (rolling, weekly and monthly) and opencode-zai (five_hour and weekly).
- **Uncounted windows.** A window with `counted: false` is still shown, marked as not counting toward this provider's headroom. Example: agy's 3p-* pool for agy.
- **Tightest window.** The constraining window (the one that sets `headroom`) is visibly marked.
- **Verified by:** a fixture budget reading with several windows per provider, some counted and some not, gives exactly those lines in the panel model. This is checked at the snapshot/`providers_view` seam, and the TUI and page render that model.

**MQ-R2: one format, rendered by the core.**
- The window lines are built by the monitor from the structured reading (`Budget.to_dict()` windows), not from a provider script's free text. They use one bar glyph set and one column layout for every provider, in both the TUI and the page.
- **Provider extras.** A provider's `usage` action may still contribute extra lines below the windows, such as credits, the vault account, a spend note or a metered-billing note. It must not replace the window lines.
- **No windows.** A provider with no windows, such as opencode-deepinfra (metered), shows its note and no bars.
- **Verified by:**
  - two providers whose scripts print different glyphs give identical window-line formats;
  - claude's "credits …" extra still appears;
  - deepinfra shows its note.

**MQ-R3: installation is judged where agents run.**
- Under the docker executor, "installed" means the provider's binary resolves in the container, using the same resolution `doctor` uses since C5 (the executor's `native_bin`/container check). The local executor keeps the host check.
- A provider whose binary resolves in the container is never shown as "not installed".
- **Verified by:** a docker-executor fixture where the binary is absent on the host and present in the container gives "installed". A binary absent from both gives "not installed".

**MQ-R4: a failing `usage` action does not hide the windows.**
- When a provider's `usage` action fails, for example because its binary is not on the host, its windows from the structured reading are still shown, and the failure is shown as one short line.
- For a provider whose usage action needs its native binary, that action is run where the binary is, i.e. through the executor under docker. Otherwise its extras are skipped quietly.
- **Verified by:** a provider whose `usage` action exits non-zero still has all its window lines, plus one error line.

**MQ-R5: no regression.**
- `budget_status` is unchanged.
- Q6 reset labels, the account labels from the C17/C4 isolation, the alerts and the provider health lines keep working.
- The existing monitor tests stay green, or are updated deliberately by the tester where they pinned the old script-text layout: `tests/test_core.py` ~5081-5163, ~8339-8373, ~8736-8756, and `tests/test_q6_reset_time_display.py`.

## Out of scope

- Changing how budgets are read (`budget.read_all`).
- A history or graph of usage.
