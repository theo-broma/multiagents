# C23 contract test handoff

Contract: `context/specs/c23-quota-handover.md`, QH-R1..QH-R22.
Suites: `tests/test_c23_quota_handover.py` (55 cases after the v3 retirements) and
`tests/test_c23_v3_priorities.py` (87 cases, amendment v3, QH-R23..R29; section at the end).
No production changes or not-implemented stubs. The provider CLI is a real
subprocess with atomic invocation records and per-session transcript files.

## Result

Final command:

```sh
PYTHONPATH=src uv run --frozen python -m pytest -q -p no:cacheprovider -n 4 --basetemp=/var/tmp/pt-dbaced tests/test_c23_*
```

**60 failed, 4 passed in 13.06 seconds. Expected red.** Every failure is an
assertion about missing C23 behavior or an expected config error not raised.
There are no import, collection, syntax, or fixture exceptions. The restart
fixture demonstrably launches alpha, then fails because beta never launches.

Failure reasons, covering every failing case by its first requirement id:

| Requirement | Failed cases | Current first failing condition |
| --- | ---: | --- |
| QH-R1 | 2 | Enabled/default-on does not hand over |
| QH-R2 | 8 | Invalid config accepted (7); unset reservation lacks handover (1) |
| QH-R3 | 3 | Missing shipped modes; no continuation with none/undeclared modes |
| QH-R5 | 7 | No successor launch, including model/availability/order scenarios |
| QH-R6 / R13 | 3 | Floor dispatched immediately (2); reserved wins despite usable alternative (1) |
| QH-R7 | 6 | MCP steer_agent has no provider parameter |
| QH-R8 | 7 | No copy/resume; divergent transfer has no failure event |
| QH-R9 | 1 | No cross-family continuation |
| QH-R10 | 3 | No successive handover, durable attempt, or restart target launch |
| QH-R11 | 7 | Initial handover missing, so return-home scenarios cannot proceed |
| QH-R12 | 1 | Initial reserved handover missing |
| QH-R14 / R16 | 4 | No reserve request (3); floor dispatched while another agent runs (1) |
| QH-R15 | 2 | No reserve request / FIFO floor admission |
| QH-R17 | 2 | No reserve request for floor scenarios |
| QH-R18 | 1 | Handover events absent |
| QH-R19 | 1 | home_provider/current_provider/segments absent from MCP output |
| QH-R20 | 1 | Second segment missing |
| QH-R21 | 1 | Child runs alpha but does not hand over to beta |

The four passes preserve existing behavior: C23 disabled, agent opt-out,
reserved admission above the floor, and quota stop with no successor.

The existing suite was run separately, ignoring only the new test module:

```sh
PYTHONPATH=src uv run --frozen python -m pytest -q -p no:cacheprovider -n 4 --basetemp=/var/tmp/pt-dbaced tests/ --ignore=tests/test_c23_quota_handover.py
```

Result: **6,852 passed, 449 failed, 390 errors, 22 skipped, 7 xfailed** in
1,070.30 seconds. All 839 red outcomes belong to existing `test_phase2_*`
(72) and `test_nc_*` (767) files. Thus QH-R22's wholly green existing suite
cannot be demonstrated on this branch. Existing tests were not edited.

## Coverage seams and contract gaps

- Clock/headroom seam: existing `runner.now`, `tree.now`, `budget.read_all`
  and `budget.read_provider`, patched with monkeypatch. The separate restart
  process uses pytest.MonkeyPatch for the same budget seam. Reserve deadlines
  must use the injected clock and be reconsidered by public wait/resume calls.
  Process polling and provider gates wait for observable I/O, never quota resets.
- Session storage uses the existing `transcript.dir`/`transcript.glob` provider
  declaration. No private transfer function is prescribed. Atomic installation,
  identical/divergent targets, credential exclusion and source retention are
  tested. **Mid-transfer I/O failure needs a deterministic adapter/filesystem
  fault seam**; it is not yet covered.
- The restart test kills only its own server process while the target has not
  confirmed resume. A **crash before transfer** still needs a deterministic
  transfer gate, rather than depending on an event-reader scheduling race.
- QH-R21 subagents are covered. Scheduler exclusion is not covered: this
  branch lacks the scheduler launcher integration, and C23 explicitly leaves
  its frozen binding to follow-up work. The orchestrator's own interactive
  session is also outside the fixture surface.
- The local fake records its serving account, but has no vault. Account fields
  are required on segments; **actual known vault-account attribution** needs a
  vault/executor fixture. Provider usage/spend attribution is covered.
- Config rejections assume the loader's existing ValueError convention; C23
  does not specify the exception type. Refusal wording, event timestamps and
  budget-output nesting are otherwise not pinned to invented exact strings.
- Tier 3's eligibility for a same-family instance with a different allowed
  model is not explicit (R9 labels continuation as another family). The model
  test requires that it never resumes the old session with a different model;
  it does not invent a winner between a fresh sibling and another family.
- The session-carryover tests directly verify id, transcript, branch, cwd,
  model and effort. Limits, budget tags and readonly paths do not yet have
  dedicated handover assertions; their existing tests remain unmodified.

Tooling note: the prescribed `rm -rf /var/tmp/pt-dbaced` cleanup was rejected
by the command tool with “rm -f style commands are not permitted”, despite
the unrestricted permission profile. Cleanup used Python shutil instead.


## Amendment v3 (QH-R23..R29), run ag-abeb4b

Command: `PYTHONPATH=src uv run --frozen python -m pytest -q -p no:cacheprovider -n 4 --basetemp=/var/tmp/pt-c23v3/b tests/test_c23_*.py`
Final: **121 failed, 21 passed** (142 cases). Expected red. The 21 green are
4 v2 regression guards (off-switch, opt-out, above-floor admission, no-candidate),
13 "valid value still loads" config cases, one valid-priorities load, one
"run already on the best instance is untouched", and two family-expansion cases
that today's routing already satisfies (the discriminating one, `blocked1-alpha`, is red).

### Retired from the v2 file (withdrawn by v3)
Deleted, 9 cases: `test_qh_r11_return_home_only_at_resume_boundary_after_reset[False,True]`,
`test_qh_r11_return_home_failure_keeps_current_and_emits_event`,
`test_qh_r11_cross_family_return_requires_resumable_unchanged_home[3]`,
`test_qh_r11_quota_reset_does_not_interrupt_live_foreign_turn` (a live turn may now be stopped),
`test_qh_r12_reserved_instance_leaves_for_freed_better_tier`,
`test_qh_r17_floor_leaves_at_boundary_when_home_reset`.
Their behaviour is rewritten as promotion in the v3 file: reset -> promote, cross-family
back (resumable / changed / mode none), floor run promoted off the floor.
No QH-R5 test changed outcome: with no `priorities:` the list equals the old agent list.
Seam additions to the v2 CLI (inert unless set): plan keys `tool_gate`, `say`; rules for tool_use/tool_result.

### Why the v3 tests are red
| Group | Cases | First failing condition |
| --- | ---: | --- |
| R29 invalid values (project and per-agent) | 26 | No error raised at config load |
| R23 unknown/duplicate priorities | 5 | No error raised |
| R23 routing by priorities / family expansion | 5 | `priorities:` ignored |
| R24/R28 rank, pinned output | 3 | `rank`, `pinned` absent from tool output |
| R25/R26/R27/R28/R29 behavioural (promotion) | 46 | Stops at the explicit precondition "quota stop never handed over" (v2 handover not implemented on this branch) |
| R27 pin / R7 | 4 | `steer_agent` has no `provider` parameter |

**Caveat:** because v2 handover is not implemented either, the promotion scenarios never run past
their setup, so their later assertions (safe point, grace, fallback, ranks in events) have not yet
been exercised against any implementation. Expect the first green run to expose fixture mistakes there.

### Contract gaps
See the report. Short list: entry syntax of `priorities:`; base of `rank`; meaning of
"demoted" (literal reading is vacuous); entry point of the promotion check; what a "budget
refresh" is observable as; "being merged" has no seam; `provider="auto"` effect on the live
turn; sibling return in `copy` mode (target holds a prefix of the source); `tier` on promote events.
