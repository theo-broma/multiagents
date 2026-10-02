# Provider refusals are not startup outages — the contract

**Status:** contract, written by the orchestrator on 2026-10-02 for ticket
bug-1213a0. It is fixed in-house and not submitted.
- **Ids:** `RC-R*`, never renumbered. A behaviour is retired by marking it
  withdrawn.

## The defect

A codex run hit the provider's content-safety filter. It printed "This content
was flagged for possible cybersecurity risk…" on stderr, exited non-zero
before any assistant output, and the run was classified `failed`. That set the
provider `startup_down` for the whole account, and `steer_agent` on that run
was then refused for `startup_down`.

A refusal tells us something about the prompt, not about the provider: the
binary started, authenticated and got an answer.

## Behaviours

**RC-R1: codex refusals are recognised.**
- The shipped codex provider declares `refusal_markers` that match the
  content-safety message ("This content was flagged for possible … risk").
  Matching is case-insensitive.
- Verified by: a codex-shaped fake run that prints that line on stderr and
  exits non-zero ends with status `refused`, not `failed`.

**RC-R2: markers are checked on stderr as well as the final message.**
- **The final message, as today.** It is matched with
  `re.fullmatch(pattern, final_message.strip(), re.IGNORECASE)`, unchanged.
- **Stderr, new.** The bounded stderr tail the executor keeps (`stderr_tail`)
  is matched with `re.search(pattern, tail, re.IGNORECASE)`.
  - This applies only to a run that exited non-zero. A successful run whose
    stderr contains a marker is not reclassified.
  - For handles that drain stderr asynchronously, stderr is drained to EOF
    before classification, so the verdict does not depend on timing.
- **Precedence.** Run the existing classification first: stop requested,
  timeout, truncation, and structured refusal. Stderr markers are consulted
  only where the run would otherwise be `failed`.
- **What is recorded.** On a match, the result and events record:
  - the source (`assistant` or `stderr`);
  - the pattern that matched;
  - a bounded excerpt of the matched text, at most 200 chars, with the
    existing redaction applied.
- **Verified by:**
  - a non-zero exit with the marker only on stderr gives `refused`;
  - a zero exit with the marker on stderr keeps its normal status;
  - a non-zero exit with no marker anywhere stays `failed`, unchanged;
  - the recorded signal names the source and the pattern.

**RC-R3: a refusal never counts against startup health.**
- A run that ends `refused` does not increment the provider's startup
  failure count and never sets the provider `startup_down`. This holds even
  when it produced no progress before exiting.
- The finalizer decides startup failure today from "no progress". A
  `refused` outcome must instead finish the startup claim as a success. This
  includes a half-open probe and commit-fix turns.
- A refusal triggers no automatic retry and no failure-breaker increment.
- Verified by:
  - after N refused runs, N being at least `startup_failure_threshold`,
    `startup.availability(provider)` is still `None`;
  - a refused run that was the half-open probe leaves the provider available.

**RC-R4: steer and start agree on startup health.**
- **Advisor finding.** The half-open path largely exists already:
  `StartupHealth._blocked` reports no problem after the cooldown expires while
  no probe is held, and `claim()` takes the probe atomically. This
  requirement therefore pins the behaviour down rather than changing it.
- **What must hold.** For startup health alone, a pinned `steer_agent` and a
  pinned `start_agent` on the same provider give the same verdict in each
  state:
  - **Cooldown active:** both are refused with `startup_down` and the same
    `retry_after`.
  - **Half-open, probe free:** both may compete for the probe claim. Whoever
    wins runs as the probe, and its outcome resolves it.
  - **Half-open, probe held:** both are refused with the same reason.
- **Other checks still apply.** Auth, quota, model, the cleanup hold and PC
  (PC-R*) checks are unchanged. Steer keeps its current order: it reserves PC
  capacity before `startup.claim`, and rolls the reservation back when it loses
  the claim. A queued PC resume holds no probe.
- **Unpinned starts are not compared.** They may fall back to another
  provider, so their overall verdict need not match.
- **Verified by:** tests driving `StartupHealth` into each of the three states
  and comparing a pinned start with a pinned steer.

**RC-R5: no regression.**
- A genuine startup failure still behaves exactly as today:
  - non-zero exit;
  - no progress;
  - no refusal marker.
- `tests/test_h7_provider_startup.py` stays green.
- Verified by: the existing startup-health suite, plus one test that a
  marker-free early exit still counts as a startup failure.

## Out of scope

- Retrying a refused run automatically, or rewording its prompt.
- Changing how routing ranks providers (bug-ac396a is separate).
