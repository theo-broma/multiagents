"""Adversary pass 4: RP13's "could supersede" flag, through commits that are
not trusted sources and past sources RP10 removed.

Both tests pass today; each kills a mutation of the ancestor flag in
`Results.readonly_decision` that the rest of tests/test_rp_*.py lets survive:
- the flag not propagated through a run's own commit (a generation reverting
  an allowed vouching ancestor two commits up is then never read, and the
  stale ancestor keeps the run's edit: RP7 step 2, RP13);
- the flag raised by a vouching ancestor RP10 already removed (a generation
  whose policy is not needed then fails capture: RP13).
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_rp_adversary4_lazy_policy_oracle import Repo  # noqa: E402
from multiagents.scheduler.results import Results  # noqa: E402

TEST, SRC = "tests/a.py", "src/m.py"


def decide(root, repo, inp, tip, gens):
    repo.git("update-ref", "refs/heads/main", inp)
    results = Results(SimpleNamespace(root=root, scheduler=root), SimpleNamespace(base_branch="main"),
                      generation_commits=set(gens), generation_policies=gens)
    return results.readonly_decision({"input_commit": inp}, tip, [TEST])


def state(test, src="s"):
    return {TEST: ("100644", test), SRC: ("100644", src)}


def test_rp7_rp13_a_revert_two_commits_below_an_allowed_vouching_generation_still_supersedes_it(tmp_path):
    """Tester generation A changes the test v0 -> v1. Another node merges A,
    commits on its own (not a source), and its recorded generation D reverts
    the test to v0. The run merges D and writes v1 back: A is superseded by
    D (RP7 step 2), D brought no change (input v0), so the path is restored
    to the input, not kept on the strength of A."""
    repo = Repo(tmp_path)
    root = repo.commit(state("v0"), [])
    inp = repo.commit(state("v0", "i"), [root])
    a = repo.commit(state("v1", "i"), [inp])
    own = repo.commit(state("v1", "o"), [a])
    d = repo.commit(state("v0", "o"), [own])
    tip = repo.commit(state("v1", "o"), [d])
    gens = {a: {"ag-00000a": [[]]}, d: {"ag-00000d": [["src/**"]]}}
    decision = decide(tmp_path, repo, inp, tip, gens)
    assert decision["paths"] == [{"path": TEST, "outcome": "reverted", "source": inp}]
    assert set(decision["producer_policies"]) == {a, d}


def test_rp13_a_descendant_of_a_source_rp10_removed_is_not_needed(tmp_path):
    """Implementer generation A carries the test at v1 but its producer could
    not write tests/: RP10 removes it for that path before supersession. Its
    descendant D, whose producer record lacks readonly_paths, reverts the
    test to v0: it cannot vouch (no change against the input) and has no
    allowed source to supersede, so its policy is not needed (RP13). The
    run's own v2 is restored to the input."""
    repo = Repo(tmp_path)
    root = repo.commit(state("v0"), [])
    inp = repo.commit(state("v0", "i"), [root])
    a = repo.commit(state("v1", "i"), [inp])
    d = repo.commit(state("v0", "d"), [a])
    tip = repo.commit(state("v2", "d"), [d])
    gens = {a: {"ag-00000a": [["tests/**"]]}, d: {"ag-00000d": [None]}}
    decision = decide(tmp_path, repo, inp, tip, gens)
    assert decision["paths"] == [{"path": TEST, "outcome": "reverted", "source": inp}]
    assert set(decision["producer_policies"]) == {a}
