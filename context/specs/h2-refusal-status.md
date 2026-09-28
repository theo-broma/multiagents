# H2: refused or filtered runs are never `done`

**Status:** contract, written by the orchestrator on 2026-09-28. It covers
H2 in `phase6-hardening.md`.
- Ids are `RF-R*`. They are never renumbered. A behaviour is retired by
  marking it withdrawn.
- Research: ag-94c9f0 mapped how each provider's end of stream becomes a
  node status.

## Why

`_classify()` (`runner.py` ~2581) returns `done` for any clean exit with
non-empty text. A provider that blocks or refuses a request usually exits
cleanly and says so in prose or in a field nobody parses, so the run is
reported as a success.

The recorded case is agy run `ag-da2c22`. It answered "This request was
blocked by Gemini's filters…", made zero commits, and ended `done` with an
empty reason.

A refused tester, adversary or reviewer then looks like a check that passed,
and those checks gate merges.

What each provider parses today:
- **claude:** only `result.subtype`. `stop_reason` and `terminal_reason`
  are dropped.
- **codex:** `turn.failed` and a non-zero exit already become `failed`.
- **agy:** only `result.status` is parsed. The time limit is caught by a
  stderr marker, and a filter message in the response prose is dropped.
- **opencode:** there is no result rule at all, and
  `step_finish.part.reason` is dropped.

## Behaviours

**RF-R1: a `refused` terminal status.**
- `refused` is added to the tree's terminal statuses. It is a non-success
  status, like `failed`. Examples:
  - a wait loop or check that treats `done` as success does not treat
    `refused` as success;
  - an auto-merge into the parent never happens for it, since it happens
    only after `done`.
- The node's `reason` names the provider signal that caused it. That is the
  field and value, or the marker that matched, cut to 200 characters. An
  example: `claude stop_reason=refusal` or `agy response matched refusal
  marker "blocked by Gemini's filters"`.
- `refused` is **not** a quota or authentication failure. It never cools
  down the provider, never counts toward exhaustion, and never triggers
  fallback routing.
- A `refused` node can be resumed with `steer_agent`, exactly as a `failed`
  one can.
- `wait_for_agents`, `check_agent`, `collect_agent`, `agent_tree` and the
  monitor show `refused` as it is. It is never folded into `done` or
  `failed`.
- Verified by:
  - a run classified `refused` is terminal, is not auto-merged into its
    parent, and appears with that status and reason in `wait_for_agents`
    and `collect_agent`;
  - a `refused` run leaves the provider's budget and cooldown state
    unchanged;
  - `steer_agent` on a `refused` node resumes it.

**RF-R2: the refusal signals are declared by providers, not coded in the
core.** No provider name appears in core code (the phase-5 invariant). A
provider declares its refusal signals in `providers.yaml` by two routes:

1. **Structured fields.** An event rule may map a native field or value to
   the normalised final status `REFUSED`. That uses the same field-mapping
   mechanism that already produces `status`. `_classify` turns a final
   status of `REFUSED` into `refused`.
2. **`refusal_markers:`** is a provider-level list of case-insensitive
   substrings, a sibling of the existing `truncation_markers`. They are
   matched against:
   - the run's **final assistant message**, not the whole transcript;
   - the final status text.

   They are meant for fixed wording that the provider or its safety filter
   generates, never for a model's own prose refusals, which cannot be told
   apart from legitimate text.

Precedence inside `_classify`:
- the existing truncation check comes first;
- refusal is checked next, before the clean-exit shortcut to `done`.

A run can therefore exit 0 with text and still be `refused`.

Verified by: a test provider block declaring each route drives
`_classify` to `refused`. Removing the declaration makes the same stream
`done`, which shows that the core holds no provider-specific knowledge.

**RF-R3: the shipped declarations.** The shipped `providers.yaml` declares,
at minimum:
- **claude:** a result carrying `stop_reason: "refusal"` maps to `REFUSED`.
  A result subtype `error_max_turns` maps to the existing `truncated`
  outcome, not to `failed`, because the work may be partial and resumable.
- **agy:** `refusal_markers` includes the wording from `ag-da2c22`
  (`blocked by Gemini's filters`). Take the exact text from that run's
  stream under `.multiagents/runs/ag-da2c22/` if it still exists, otherwise
  from `BRIEF.md` ~1586.
- **opencode:**
  - a result or terminal rule exists;
  - `step_finish.part.reason` values meaning content filtering map to
    `REFUSED`;
  - values meaning a length or turn limit map to `truncated`.

  Where the exact reason strings are unknown, name the ones found in the
  opencode source or docs available, and state what was inferred.
- **codex:** no change is required, because `turn.failed` is already
  `failed`. If the codex event schema in
  `context/codex-proposal/app-server-schema/` names a refusal or
  content-filter reason, map it to `REFUSED`.

Verified by: recorded stream fixtures, one per declared signal. Each
classifies as declared. Where no real sample exists, a synthetic fixture
is marked as such in the test.

**RF-R4: the ag-da2c22 regression.** Replaying that run's final message
through the agy rules classifies it `refused`, not `done`.
- Verified by: a fixture built from that run's text.

**RF-R5: a writing agent with no commits is flagged, not re-statused.** A
`writes: true` agent can end `done` with zero commits on its branch and no
`NEED_INFO` or `NEED_DECISION`. That is how a prose refusal the markers
cannot catch presents itself.
- Such a run stays `done`. Zero commits is legitimate for an adversary
  that found nothing.
- Its result carries `no_commits: true` in `wait_for_agents`,
  `check_agent` and `collect_agent`, together with one line of
  explanation.
- Verified by:
  - a writing agent ending clean with text and zero commits shows
    `no_commits: true`;
  - one commit shows no flag;
  - a non-writing agent never shows the flag.

**RF-R6: `merge_agent` on a node that is not `done`.**
- `merge_agent` still works on any node that has a branch. That does not
  change.
- When the node's status is `refused`, `failed` or `truncated`, the result
  carries `status_before_merge`. It also carries a one-line warning that
  the work was not reported complete.
- Verified by: merging a `refused` node with a commit succeeds and returns
  the warning; merging a `done` node returns no warning.

**RF-R7: nothing else regresses.**
- Quota and auth classification, truncation, and the "did work" rescue for
  silent runs keep their current behaviour.
- The existing suite stays green, apart from the 72 known phase2 reds.
- Verified by: the existing tests.
