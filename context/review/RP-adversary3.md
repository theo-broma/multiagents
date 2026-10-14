# RP — readonly restore by provenance, adversary pass 3 (2026-10-14, run ag-0ed5ef)

**F206** — capture fails on a missing producer policy the decision does not need
*Class:* correctness
*Severity:* low
*Where:* `src/multiagents/scheduler/results.py:302-311`
*Evidence:* reproduction
*Proof:* `tests/test_rp_adversary3_unneeded_policy.py` (branch nodes/nd-e8e39590, commit 12cf46a6), 2 tests failing at d64ba44d
*What happens:* every eligible generation in the run's range has its producer policy checked before any path is considered; one missing policy raises `result_capture_failed`, even when that generation changed no protected path and can neither vouch nor supersede. RP10 limits the failure to a generation "needed for a decision". Fails safe (the node is held, nothing is kept wrongly).
*Disposition:* fix
*Reasoning:* contract deviation with a cheap fix: check the policy lazily, per path, only for sources that reach the decision.

**F207** — run ids are 6 hex digits with no uniqueness check
*Class:* correctness
*Severity:* low
*Where:* `src/multiagents/scheduler/engine.py:753`, `src/multiagents/sessions.py:304`
*Evidence:* trace
*Proof:* —
*What happens:* `ag-` plus 6 random hex digits, never checked against existing runs. Collisions become likely at a few thousand attempts. RP10 joins producer policies through `run_id`: a collision with a cleanup attempt (no `readonly_paths`) makes every capture that merges that producer fail (F206), and a collision of two producing attempts applies the stricter policy, which can restore a path RP1 would keep. Other run-id consumers (run directories, logs) may also mix.
*Disposition:* fix
*Reasoning:* make run ids unique by construction (check on allocation or widen them); fails safe for RP today but the id space is small for a long-lived project.
