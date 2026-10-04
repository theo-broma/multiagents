"""M3 — dependencies and inputs bound to a generation: the `dependency`,
`input` and `input_conflict` outcomes of NC-R22/R52, NC-R65 (combining several
inputs), and the dependent side of NC-R59/R33's reopening rule.

Assumptions where the contract is silent (kept loose):
- a blocked reason is `{code, detail}` and `get_node` reports `ready: false`
  for a node blocked by `dependency` or `input` (NC-R52: both are structural).
- a node whose inputs cannot be combined is `held` with `hold.reason ==
  "input_conflict"` (NC-R65) and is never launched.
- `test_nc_r47_a_rejected_generation_*` needs a loop with a verdict child
  (NC-R5/R35/R34, M4) and scrapes NC-R66's prompt for the generation under
  review; it is M4-dependent and its give_verdict argument names are a guess.
- `test_nc_r59_*` needs `relaunch_node` (NC-R36, M4).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.gitworld import GitWorld  # noqa: E402
from nc_fixture.world import blocked_codes, err_code  # noqa: E402


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = GitWorld(tmp_path, monkeypatch)
    yield world
    world.close()


def dep(node: str, require: str | None = None) -> dict:
    return {"node": node, **({"require": require} if require else {})}


def never_launched(w: GitWorld, tag: str, seconds: float = 3) -> None:
    w.quiet(seconds)
    assert w.fx.by_tag(tag) == [], f"{tag} launched although it must stay blocked"


# --------------------------------------------------------------- dependency

def test_nc_r22_a_node_waiting_on_a_dependency_is_blocked_with_the_code_dependency(w):
    w.start_scheduler()
    a = w.coder("A", {"a.txt": "a"}, fx={"gate": "ga"})
    b = w.coder("B", {"b.txt": "b"}, depends_on=[dep(a)])
    w.wait_running(a)
    node = w.until(lambda: "dependency" in blocked_codes(w.get(b)) and w.get(b),
                   what="B blocked by dependency")
    assert node["ready"] is False and node["eligible"] is False and node["state"] == "open"
    detail = [x for x in node["blocked"] if x["code"] == "dependency"][0]["detail"]
    assert a in str(detail)
    never_launched(w, "B")
    w.gate("ga")


def test_nc_r22_a_satisfied_dependency_launches_the_node_exactly_once_after_the_predecessor(w):
    w.start_scheduler()
    a = w.coder("A", {"a.txt": "a"}, fx={"gate": "ga"})
    b = w.coder("B", {"b.txt": "b"}, depends_on=[dep(a)])
    w.wait_running(a)
    w.gate("ga")
    w.done(b)
    assert len(w.fx.by_tag("B")) == 1
    assert w.index_of("done", a) < w.index_of("launched", b)
    assert "dependency" not in blocked_codes(w.get(b))


def test_nc_r22_a_failed_predecessor_keeps_a_success_dependent_blocked_for_good(w):
    w.start_scheduler()
    a = w.coder("A", fx={"crash": True})
    b = w.coder("B", {"b.txt": "b"}, depends_on=[dep(a, "success")])
    assert w.done(a)["outcome"] == "failed"
    never_launched(w, "B")
    node = w.get(b)
    assert node["state"] == "open" and "dependency" in blocked_codes(node)


def test_nc_r22_a_finished_dependency_is_satisfied_by_a_failed_predecessor(w):
    w.start_scheduler()
    a = w.coder("A", fx={"crash": True})
    b = w.coder("B", {"b.txt": "b"}, depends_on=[dep(a, "finished")])
    w.done(a)
    assert w.done(b)["outcome"] == "completed"


def test_nc_r22_a_cancelled_predecessor_never_satisfies_a_success_dependency(w):
    w.start_scheduler()
    a = w.coder("A", fx={"gate": "ga"})
    b = w.coder("B", {"b.txt": "b"}, depends_on=[dep(a)])
    w.wait_running(a)
    assert w.cancel(a)["ok"] is True
    never_launched(w, "B")
    assert "dependency" in blocked_codes(w.get(b))


def test_nc_r22_two_dependencies_are_both_required(w):
    w.start_scheduler()
    a = w.coder("A", {"a.txt": "a"}, fx={"gate": "ga"})
    b = w.coder("B", {"b.txt": "b"}, fx={"gate": "gb"})
    c = w.coder("C", {"c.txt": "c"}, depends_on=[dep(a), dep(b)])
    w.wait_running(a)
    w.wait_running(b)
    w.gate("ga")
    w.done(a)
    never_launched(w, "C", 2)
    assert "dependency" in blocked_codes(w.get(c))
    w.gate("gb")
    w.done(c)


def test_nc_r22_blocked_and_ready_are_derived_never_persisted(w):
    w.start_scheduler()
    a = w.coder("A", fx={"gate": "ga"})
    b = w.coder("B", depends_on=[dep(a)])
    w.wait_running(a)
    w.until(lambda: "dependency" in blocked_codes(w.get(b)), what="B blocked")
    for path in w.scheduler_files():
        text = path.read_bytes()
        for key in (b'"eligible"', b'"blocked"', b'"ready"'):
            assert key not in text, f"{key!r} persisted in {path}"
    w.gate("ga")


# -------------------------------------------------------------------- input

def test_nc_r22_a_node_whose_input_has_no_generation_yet_is_blocked_with_the_code_input(w):
    w.start_scheduler()
    a = w.coder("A", {"a.txt": "a"}, fx={"gate": "ga"})
    b = w.coder("B", {"b.txt": "b"}, inputs=[{"node": a}])
    w.wait_running(a)
    node = w.until(lambda: "input" in blocked_codes(w.get(b)) and w.get(b),
                   what="B blocked by input")
    assert node["ready"] is False and node["state"] == "open"
    never_launched(w, "B")
    w.gate("ga")


def test_nc_r33_a_dependent_node_builds_on_the_generation_of_its_input(w):
    w.start_scheduler()
    a = w.coder("A", {"a.txt": "from A\n"}, fx={"gate": "ga"})
    b = w.coder("B", {"b.txt": "from B\n"}, inputs=[{"node": a}],
                fx={"shell": ["cat a.txt > $FX_DIR/seen.B", "git rev-parse HEAD > $FX_DIR/head.B"]})
    w.wait_running(a)
    w.gate("ga")
    gen_a = w.done(a)["generations"][0]["commit"]
    w.done(b)
    assert w.fx_file("seen.B") == "from A\n"
    assert w.fx_file("head.B").strip() != w.base
    assert w.is_ancestor(gen_a, w.fx_file("head.B").strip())
    assert {"a.txt", "b.txt"} <= w.files(w.tip(b))
    assert "b.txt" not in w.files(w.tip(a)), "B's work leaked into its input's branch"


def test_nc_r33_the_input_is_bound_at_launch_later_generations_do_not_move_a_running_node(w):
    w.start_scheduler()
    a = w.coder("A", {"a.txt": "a"})
    w.done(a)
    gen = w.generations(a)[0]["commit"]
    b = w.coder("B", {"b.txt": "b"}, inputs=[{"node": a}],
                fx={"shell": ["git rev-parse HEAD > $FX_DIR/head.B"]})
    w.done(b)
    assert w.is_ancestor(gen, w.fx_file("head.B").strip())


def test_nc_r22_a_failed_input_leaves_the_dependent_blocked_with_input(w):
    w.start_scheduler()
    a = w.coder("A", fx={"crash": True})
    b = w.coder("B", {"b.txt": "b"}, inputs=[{"node": a}])
    w.done(a)
    never_launched(w, "B")
    assert "input" in blocked_codes(w.get(b))


def test_nc_r22_an_input_that_names_a_generation_that_does_not_exist_is_blocked_input(w):
    w.start_scheduler()
    a = w.coder("A", {"a.txt": "a"})
    w.done(a)
    b = w.coder("B", {"b.txt": "b"}, inputs=[{"node": a, "generation": 7}])
    never_launched(w, "B")
    assert "input" in blocked_codes(w.get(b))


def test_nc_r22_an_input_that_pins_an_existing_generation_is_usable(w):
    w.start_scheduler()
    a = w.coder("A", {"a.txt": "a"})
    w.done(a)
    b = w.coder("B", {"b.txt": "b"}, inputs=[{"node": a, "generation": 1}])
    w.done(b)
    assert "a.txt" in w.files(w.tip(b))


def test_nc_r33_a_non_verdict_node_that_committed_nothing_still_satisfies_an_input(w):
    w.start_scheduler()
    a = w.simple("A")                          # read-only agent, no commit
    b = w.coder("B", {"b.txt": "b"}, inputs=[{"node": a}])
    w.done(a)
    assert w.done(b)["outcome"] == "completed"


def test_nc_r47_a_rejected_generation_passed_as_input_is_an_input_block(w):
    """M4-dependent: a loop [writer, reviewer(verdict child)] whose reviewer
    rejects generation 1. See the module docstring for the guesses."""
    w.start_scheduler()
    writer = w.coder("W", {"w.txt": "w"})
    reviewer = w.coder("R", commit=False, agent="coder",
                       fx={"verdict_rpc": {"verdict": "rejected",
                                           "findings": [{"summary": "no", "severity": "high"}]}})
    reply = w.create({"kind": "loop", "children": [writer, reviewer],
                      "loop": {"verdict_child": reviewer, "max_rounds": 1, "rounds_rejected": 0}})
    assert reply.get("ok"), reply
    from nc_fixture.world import unwrap
    loop = unwrap(reply["result"])["id"]
    w.until(lambda: w.get(loop)["state"] == "held" and
            (w.get(loop).get("hold") or {}).get("reason") == "loop_max",
            timeout=60, what="the loop to stop at loop_max")
    gens = w.generations(loop)
    assert gens and gens[-1]["verdict"] == "rejected"
    pinned = w.coder("X1", {"x.txt": "x"}, inputs=[{"node": loop, "generation": gens[-1]["seq"]}])
    latest = w.coder("X2", {"x.txt": "x"}, inputs=[{"node": loop}])
    never_launched(w, "X1")
    never_launched(w, "X2", 0)
    assert "input" in blocked_codes(w.get(pinned))
    assert "input" in blocked_codes(w.get(latest))


# ------------------------------------------------------- several inputs (R65)

def test_nc_r65_several_inputs_are_combined_into_the_launch_checkout(w):
    w.start_scheduler()
    a = w.coder("A", {"a.txt": "a\n"})
    b = w.coder("B", {"b.txt": "b\n"})
    w.done(a)
    w.done(b)
    c = w.coder("C", {"c.txt": "c\n"}, inputs=[{"node": a}, {"node": b}],
                fx={"shell": ["ls > $FX_DIR/ls.C"]})
    w.done(c)
    seen = set(w.fx_file("ls.C").split())
    assert {"a.txt", "b.txt"} <= seen
    assert {"a.txt", "b.txt", "c.txt"} <= w.files(w.tip(c))
    assert "c.txt" not in w.files(w.tip(a)) and "c.txt" not in w.files(w.tip(b))


def test_nc_r65_inputs_that_cannot_be_combined_hold_the_node_input_conflict(w):
    w.start_scheduler()
    a = w.coder("A", {"shared.txt": "from A\n"})
    b = w.coder("B", {"shared.txt": "from B\n"})
    w.done(a)
    w.done(b)
    tips = (w.tip(a), w.tip(b))
    c = w.coder("C", {"c.txt": "c"}, inputs=[{"node": a}, {"node": b}])
    held = w.until(lambda: (n := w.get(c))["state"] == "held" and n, what="C held")
    assert held["hold"]["reason"] == "input_conflict"
    assert w.fx.by_tag("C") == []
    assert (w.tip(a), w.tip(b)) == tips, "combining inputs moved an input's branch"
    assert "held" in w.transitions(c)


def test_nc_r65_a_conflict_among_inputs_holds_only_that_node(w):
    w.start_scheduler()
    a = w.coder("A", {"shared.txt": "from A\n"})
    b = w.coder("B", {"shared.txt": "from B\n"})
    w.done(a)
    w.done(b)
    c = w.coder("C", {"c.txt": "c"}, inputs=[{"node": a}, {"node": b}])
    d = w.coder("D", {"d.txt": "d"}, inputs=[{"node": a}])
    w.until(lambda: w.get(c)["state"] == "held", what="C held")
    assert w.done(d)["outcome"] == "completed"


# ---------------------------------------------------------- reopening (R33)

def test_nc_r59_reopening_a_done_node_blocks_a_dependent_that_has_not_launched(w):
    """Needs `relaunch_node` (M4) for the reopening itself."""
    w.start_scheduler()
    a = w.coder("A", {"a.txt": "a"}, fx={"sleep": 3})
    gate_node = w.simple("G", fx={"gate": "gg"})
    b = w.coder("B", {"b.txt": "b"}, depends_on=[dep(a), dep(gate_node)], inputs=[{"node": a}])
    w.done(a)
    w.wait_running(gate_node)
    rev = w.get(a)["revision"]
    reply = w.rpc("relaunch_node", {"id": a, "revision": rev})
    assert reply.get("ok"), reply
    w.gate("gg")
    w.done(gate_node)
    w.quiet(1)
    assert w.fx.by_tag("B") == [], "a dependent launched on a reopened predecessor"
    assert w.get(b)["state"] == "open"
    w.done(a)
    w.done(b)
