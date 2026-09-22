# C4 — Defects observed at runtime, not by reading the code

These five were found on 2026-09-22 by measuring this project's own agent
runs (`.multiagents/runs/*/command.json`, `result.json`, `stream.jsonl`),
prompted by an external analysis that mostly did not survive checking. They
are recorded here because none of them is reachable by reading a file: each
one needed the log of a real run, and three of them only became visible when
two independent facts were crossed.

Ids start at F200 to stay clear of the review's F10–F171.

**F200** — The docker executor mounts a provider CLI's *resolved versioned* path, so any CLI self-update makes the container unusable until it is recreated
*Class:* bug
*Severity:* high
*Where:* `src/multiagents/executor/docker.py:340-352` (`mount_cli_from_host`: `out.append((Path(binary), True))` then `resolved = Path(binary).resolve()` and `out.append((resolved, True))`)
*Evidence:* reproduction
*Proof:* `docker inspect multiagents-multiagents-58c9de87` lists `/home/theobroma/.local/share/claude/versions/2.1.278`; the host's `readlink -f $(which claude)` is `.../versions/2.1.280`. Every `consult()` call today failed with `this container was created without /home/theobroma/.local/share/claude/versions/2.1.280`.
*What happens:* `~/.local/bin/claude` is a symlink into a per-version directory. The executor correctly mounts both the launcher and its target — the comment explains why, and it is right: mounting only the target leaves nothing named `claude` on PATH. But it resolves the symlink **at container creation**, which pins one version number into the mount list. The claude CLI updates itself. The next update changes what the symlink points at, the pinned mount no longer matches the config, and `docker up` refuses the container (correctly — that refusal is `f963ba3`, and it is the only reason this announced itself at all). The project is then dead until someone runs `multiagents docker rm && multiagents docker up`, which also kills every agent inside.
*Disposition:* fix
*Reasoning:* Mounting the versions *directory* rather than one version inside it makes an update visible to a running container immediately and removes the recurring chore entirely. The launcher symlink still has to be mounted separately, for the reason the existing comment gives. Note the failure mode is not rare or accidental: it fires on a schedule set by somebody else's release cadence, it takes the whole project down, and recovering from it costs every in-flight agent. Any provider whose launcher resolves into a versioned directory has the same exposure; today that is claude, and the fix should not special-case it by name.

**F201** — The supervisor reports only the first watchdog trip per run, so an agent that keeps looping after the first alert is never mentioned again
*Class:* bug
*Severity:* high
*Where:* `src/multiagents/supervisor.py:199-203` (`def _trip(self, trip): if self.tripped: return None  # report each run's first trip only`)
*Evidence:* observation
*Proof:* `.multiagents/runs/ag-179bc2/stream.jsonl` holds 116 `view_file` events carrying identical arguments against one file. `doom_loop_repeats` is 5. `prompt.1.md` in the same directory is the single steer that followed, sent by the orchestrator after the one alert it received.
*What happens:* Every detector in this class — doom loop, runaway steps, silence, wall-clock — funnels through `_trip`, and `_trip` latches. The first trip of a run is reported and every later one is swallowed, including trips of a *different* kind. So a run that loops, gets one alert, is steered, and resumes looping produces exactly one alert in total, and the orchestrator's next signal is the run finishing. In ag-179bc2 the detector fired correctly at 5 repeats; the run went on to 116 events (agy reports a tool twice, so ~58 real calls) in silence. The latch is not obviously wrong on its own — it exists so one condition does not spam — but it is applied per *run* rather than per *condition*, and with no re-arm.
*Disposition:* fix
*Reasoning:* The detector works; the reporting throws its output away. Two separate things need deciding and should be stated as requirements rather than assumed: whether a *different* condition tripping after the first should be reported (it should — a doom loop followed by a wall-clock timeout is two facts, not one), and how a repeat of the *same* condition re-arms (a further N repeats beyond the threshold is the obvious rule, and N should be visible in config alongside `doom_loop_repeats`). Note this fix will make runs noisier, not quieter: that is the point, and it is why it is worth doing before F202 rather than after.

**F202** — There is no seam for provider-specific prompt guidance: `notes:` is parsed off every provider and never used anywhere
*Class:* design
*Severity:* medium
*Where:* `src/multiagents/providers.py:153` (`notes: str = ""` on the `Provider` dataclass), `src/multiagents/providers.py:196` (`notes=data.get("notes", "")`), and no other reference in `src/multiagents/`. Contrast `src/multiagents/runner.py:668-672` (`compose_prompt`'s `parts = [preamble]` … which has no provider-dependent branch at all).
*Evidence:* reproduction
*Proof:* `grep -rn "notes" src/multiagents/*.py` returns the two lines above and nothing that reads the field.
*What happens:* `notes:` in `providers.yaml` is documentation for whoever opens the file; it reaches the `Provider` object and stops there. Nothing built from a provider's configuration can influence what its agents are told. The consequence is not abstract: agy's `view_file` returns, on truncation, the literal text *"The above content does NOT show the entire file contents. If you need to view any lines of the file which were not shown to complete your task, call this tool again to view those lines."* (verified in `~/.gemini/antigravity-cli/brain/*/\.system_generated/logs/transcript.jsonl`, 840 occurrences). It says "call this tool again" and names no pagination argument, so a model that follows it literally re-reads the same head forever. Compensating for that today would mean writing agy's tool name and its English error string into the shared `PREAMBLE` in `runner.py`, where every agent on every provider would read it — which is exactly the hardcode the providers-are-plugins invariant exists to prevent (see `context/review/BRIEF-review-phase.md`).
*Disposition:* fix
*Reasoning:* The invariant already says where this belongs: a provider's quirks live in `providers.yaml`. What is missing is a key the runner concatenates for that provider's agents only. The defect being compensated is third-party and will not be fixed on our schedule, and it is not the last one of its kind — every CLI this project drives has tooling we do not control. Worth stating explicitly as a requirement: the fragment must reach only agents on that provider, must be absent from the prompt entirely when the key is absent, and must not be `notes:` itself — configuration commentary addressed to a human reader should not start being sent to models because the two happened to share a field.

**F203** — Four of the six opencode-pinned agents fall back onto agy, the one provider with a known looping defect, and opencode is at 99% of its monthly cap
*Class:* risk
*Severity:* medium
*Where:* `.multiagents/config/agents.yaml` (`researcher`, `implementer-quick` → `agy: gemini-3.8-flash-medium`; `adversary`, `reporter` → `agy: gemini-3.8-flash-high`), `src/multiagents/runner.py:499` (the pause refuses only when every option is out)
*Evidence:* observation
*Proof:* `budget_status` on 2026-09-22: opencode `headroom: 0.01`, `used_percent: 99.0` on the monthly window, resetting 2026-10-05. The looping runs measured the same day are `ag-179bc2`, `ag-d49a8e`, `ag-b2c933`, `ag-f958f0`, `ag-d538bd`, `ag-3c7e97`, `ag-3987e3` — all agy.
*What happens:* The failover works as designed, and that is the problem. When opencode's monthly cap binds, `_wind_down` and the configured `models:` blocks move four agents onto agy at once — onto the CLI whose `view_file` induces the loop described in F202. So the budget wall does not stop the phase, it routes the phase into the loop. F201 then means each affected run announces the loop once and goes quiet. The three findings are one failure, arriving in three steps, and none of them is visible from the others.
*Disposition:* accept
*Reasoning:* Recorded so the exposure is a decision rather than a surprise. Fixing F201 and F202 removes the damaging part of it: the loops become both survivable and visible, and agy remains a legitimate free fallback. Repinning is explicitly off the table — the user's instruction on 2026-09-22 is that agents keep their current models. `critic`, which has no fallback at all and stops dead when opencode is exhausted, was raised and the user's answer was that it is not used on this project, so its absence is accepted and not work.

**F204** — `compose_prompt` puts per-run unique text ahead of the stable per-role instructions
*Class:* efficiency
*Severity:* low
*Where:* `src/multiagents/runner.py:649-672` (`preamble = PREAMBLE.format(agent_id=node.id, …, workdir=workdir)` then `parts = [preamble]`, with `instructions` appended after it)
*Evidence:* observation
*Proof:* Measured over the 42 claude runs that carry a `result.json`: `cache_creation_input_tokens` 1,800,480, `cache_read_input_tokens` 48,050,603, `input_tokens` 1,253. For 2026-09-22 alone: 7 claude runs, creation 254,175, read 4,610,921, fresh 126.
*What happens:* `agent_id` and `workdir` are unique per run and sit at the very top of every prompt, ahead of the role instructions, which are identical for every agent of that role. Any cross-run prefix reuse is therefore impossible by construction. The structural observation is correct and the ordering is worth inverting on cleanliness grounds.
*Disposition:* accept
*Reasoning:* Deliberately **not** scheduled, and the measurement is the reason. Read beats creation 26.7:1, and the addressable surface is the creation figure alone — 1.8M of ~51M claude-side tokens, under 3.5%. Most of that is incremental extension of the prefix within a single multi-turn run, which no reordering recovers: per-run creation tracks run size (ag-19f7be 222,450 creation against 5,145,978 read; ag-188fa7 370 against 65,030). What could in principle be shared is the ~40k stable head between two runs of the same role, and runs of one role are hours apart, so the prefix is dead by TTL long before the second starts. The realistic saving is a fraction of 2% of the agent-side spend, on a day when the agent side was a small minority of Claude usage. Reopen it if agents of one role ever start running concurrently or back to back, which is the only condition under which it pays.
