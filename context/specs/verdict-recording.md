# A reviewer's written verdict reaches the scheduler (VR)

Source: scheduler first-real-use trial, 2026-10-06. Researcher ag-cf16c3, advisor
ag-aed397. User decision 2026-10-06: "Déterministe d'abord".

Observed: four test-review loops (nd-e2cde527, nd-283b4ce4, nd-928293e3,
nd-0ef8ebbd) were held `unresolved_round`. Each reviewer ended its text with a
well-formed `VERDICT(...)` line, which the runner's parser accepts
(`runner.py:519-520`, `6202-6209`). None of them called `give_verdict`. The
scheduler only settles a round from a `pending_verdict` set by that tool
(`scheduler/rpc.py:418-443`, `engine.py:1060-1077`), so it held the rounds
(`engine.py:1131-1136`). The reviewer's brief and the `implement` template
(`defaults/node-templates/implement.yaml:16,22`) never mention `give_verdict`.
There are two verdict paths, and only one of them reaches the scheduler.

## Behaviours

**VR-R1.** If a loop's verdict-child run finishes `done` without calling
`give_verdict`, and the runner has parsed a verdict from that run's own final
text, the scheduler settles the round with that verdict. The verdict is bound to
that run's attempt and generation and to the commit the run finished on, and it
has the same effect as an equivalent `give_verdict` call.
Verified by: loop tests with a fake reviewer whose text ends in
`VERDICT(approved): …`, `VERDICT(approved, 0): …` and `VERDICT(rejected, 2): …`,
asserting the round outcome and that the loop proceeds as it would after a
`give_verdict`.

**VR-R2.** An explicit `give_verdict` always wins. When both exist and disagree,
the explicit verdict is used and the disagreement is recorded on the round.
Verified by: a test with both, in disagreement.

**VR-R3.** The text fallback never applies when:
- the run is not `done` (after RV's rule: a `done` from RV-R1 qualifies);
- no line is accepted by the parser;
- the run's text contains verdict lines that contradict each other (approved and
  rejected both present). Those stay `unresolved_round`, exactly as today.

The fallback reads only the verdict child's own run, never another run's text.
Verified by: one test per case.

**VR-R4.** The recorded round says where its verdict came from: `tool` or
`text`. That provenance is visible through `get_node` and in the round's
`verdict` transition.
Verified by: a test on both.

**VR-R5.** For a loop already held `unresolved_round` whose last verdict-child
run qualifies under VR-R1/R3, the scheduler settles the round in the same way.
It does this once, at its next tick after the upgrade, and emits the usual
transitions. This is how the four trial loops are resolved: from their own
reviewers' verdicts, not the orchestrator's.
Verified by: a test that deposits a held loop in that state, restarts the
engine and asserts the round settles; and a test that a non-qualifying held loop
stays held.

**VR-R6.** The reviewer's instructions name the tool:
- The reviewer brief and the reviewer tasks in the shipped templates tell the
  reviewer to call `give_verdict` when it runs as a node's verdict child.
- They also tell it to end with the `VERDICT(...)` line in every case.
- The template's reviewer task includes the spec path when the instance has one.
  This fixes the trial defect "implement ignores spec_path".

Verified by: review; and a test that an instantiated `implement` template's
reviewer task contains `give_verdict` and the given `spec_path`.

## Out of scope
- Any LLM in the verdict path (user decision: deterministic first).
- Changing the verdict grammar.
