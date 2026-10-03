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

## Revision after the advisor's check (2026-10-03, ag-aed397, before tests)

These override any earlier wording they contradict. The decisions are the orchestrator's.

**MQ-R1a: reading the windows.**
- The window name is its key in `Budget.to_dict()` windows.
- The percent is `percent`, else the legacy `used_percent`, else `100 × (1 − headroom)`. If none of these is present, the line shows "—" with no bar.
- A missing `counted` means counted. `account`, when present, is shown with the window.
- **The constraining window** is the counted window with the largest used percent, the same rule as `budget._window_used()` (`budget.py` ~292/1296/1359). Ties go to the earliest reset, then to the name. Windows with no percent are never constraining.
- **No windows.** A known reading without windows but with a top-level `used_percent` shows one line named `overall`.
- **No truncation.** The TUI's four-line limit (`tui.py` ~161) goes, so every window line is shown.
- **`with_scripts=False`.** The window lines are still produced, because they come from the reading, not from scripts.

**MQ-R2a: the extras-only script contract.**
- A provider's `usage` action now prints extras only: credits, account, notes, spend. It never prints window bars. The shipped `claude.sh`, `agy.sh`, `opencode.sh` (including opencode-zai's `used/limit credits`, which exists only there) and any other shipped usage formatters are converted to this.
- `defaults/providers/README.md` documents the contract.
- **Exit codes.** Exit 64 means "no extras", and is quiet. Any other non-zero exit becomes one short diagnostic line under the windows.
- **Credits survive.** ZAI's `9963/10000 credits` and claude's `credits 0.00 of 85.00` must still appear, as extras.

**MQ-R3a: installation is judged by the C5 probe.**
- Under docker, the status comes from `manifest.probe(name, paths, context="docker")` (`manifest.py` ~1106-1129, ~1194). That probe never starts a container.
- The panel distinguishes these states:
  - `installed`;
  - `not installed` (the probe ran and the binary is absent);
  - `container not running`;
  - `probe failed` or timed out;
  - `unverified (no manifest)`.
- Only a real absence is shown as "not installed".

**MQ-R4a: usage actions stay on the host, and the windows never wait.**
- `usage` actions keep running on the host (`scripts.run_action`). They are not moved into the container, because they may read host backing and vault paths.
- A binary-dependent extra whose binary is not on the host exits 64 and is skipped quietly.
- **Nothing blocks the window lines.** A cold refresh must not hold the window lines behind usage actions, which today run serially with a 10 s timeout and a 30 s cache (`snapshot.py` ~76-110, ~162). Extras come from the cache, or are fetched in the background, and may lag by one refresh.
- **Verified by:** a usage action that sleeps past its timeout does not delay the window lines of any provider in that refresh.
