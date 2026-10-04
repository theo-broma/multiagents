"""M4 — templates: NC-R41 (format, shipped templates, registry-only loading) and
NC-R42 (instantiation, the recorded definition).

Assumptions where the contract is silent (kept loose):
- a template that violates NC-R41 is refused at `register_template` with
  `invalid` (it may instead be refused at instantiation; both are accepted:
  the tests require "not instantiable, nothing created").
- re-registering a template of the same name with a higher `version` replaces
  it for later instantiations.
- `list_templates` mentions a template's name somewhere in its result.
- the shipped `implement` template's node tasks other than `tests_task` and
  `implement_task` are the template's own; the tests do not read them.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.m4_world import M4World, err_code, unwrap  # noqa: E402

IMPLEMENT = {"spec_path": "SPEC.md", "tests_task": "write tests", "implement_task": "implement it",
             "tester": "tester", "reviewer": "reviewer", "implementer": "implementer"}

TPL = """\
template: seq2
version: {version}
params:
  first: {{type: text, default: "{a}"}}
  second: {{type: text, default: "{b}"}}
  n: {{type: int, default: 3}}
  flag: {{type: bool, default: false}}
  who: {{type: agent, default: worker}}
root:
  key: top
  kind: sequence
  children:
    - {{key: one, kind: simple, agent: {{param: who}}, task: {{param: first}}}}
    - {{key: two, kind: simple, agent: worker, task: {{param: second}}}}
"""


# Bounds on every wait: red tests fail on their own assertion within seconds.
WAIT = 8            # one launch / one transition
WAIT_ROUNDS = 20    # several activations in a row (loops, sessions)


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = M4World(tmp_path, monkeypatch)
    for role in ("tester", "reviewer", "implementer"):
        world.provider(f"fx{role}")
        world.agent(role, f"fx{role}", writes=True)
    yield world
    world.close()


def snapshot(w):
    res = w.ok("list_nodes", {})
    return res["plan_revision"], sorted(n["id"] for n in res["nodes"])


# ----------------------------------------------------------------- NC-R41

def test_nc_r41_the_shipped_templates_are_listed(w):
    w.start_scheduler()
    listing = json.dumps(w.ok("list_templates", {}))
    assert "implement" in listing and "review-loop" in listing


def test_nc_r41_implement_expands_to_two_loops_sharing_the_reviewer_alias(w):
    w.start_scheduler()
    top = w.instantiate_ok("implement", IMPLEMENT)
    root = w.get(top)
    assert root["kind"] == "sequence" and len(root["children"]) == 2
    first, second = (w.get(c) for c in root["children"])
    assert first["kind"] == second["kind"] == "loop"
    assert first["loop"]["max_rounds"] == 2 and second["loop"]["max_rounds"] == 3
    for loop, worker in ((first, "tester"), (second, "implementer")):
        kids = [w.get(c) for c in loop["children"]]
        assert [k["agent"] for k in kids] == [worker, "reviewer"]
        assert loop["loop"]["verdict_child"] == kids[-1]["id"]
    rev1, rev2 = (w.get(l["children"][-1]) for l in (first, second))
    assert rev1["session"] and rev1["session"] == rev2["session"]
    assert first["children"][0] != second["children"][0]


def test_nc_r41_implement_params_are_bound_and_the_rounds_are_overridable(w):
    w.start_scheduler()
    top = w.instantiate_ok("implement", {**IMPLEMENT, "test_rounds": 5, "impl_rounds": 7})
    first, second = (w.get(c) for c in w.children(top))
    assert first["loop"]["max_rounds"] == 5 and second["loop"]["max_rounds"] == 7
    assert "write tests" in w.get(first["children"][0])["task"]
    assert "implement it" in w.get(second["children"][0])["task"]


def test_nc_r41_review_loop_is_a_worker_and_a_reviewer(w):
    w.start_scheduler()
    reply = w.instantiate("review-loop", {"worker": "implementer", "reviewer": "reviewer",
                                          "task": "do it"})
    if not reply.get("ok"):   # the shipped param names are the developer's; only the shape is the contract
        pytest.skip(f"review-loop params unknown to this suite: {reply}")
    root = w.get(unwrap(reply["result"])["id"])
    assert root["kind"] == "loop" and len(root["children"]) == 2


def test_nc_r41_instantiating_an_unknown_template_is_refused_and_creates_nothing(w):
    w.start_scheduler()
    before = snapshot(w)
    assert w.instantiate("nope", {}).get("ok") is False
    assert snapshot(w) == before


@pytest.mark.parametrize("params,why", [
    ({k: v for k, v in IMPLEMENT.items() if k != "spec_path"}, "missing required param"),
    ({**IMPLEMENT, "tester": "no-such-agent"}, "unknown agent"),
    ({**IMPLEMENT, "test_rounds": "two"}, "int param given a string"),
    ({**IMPLEMENT, "test_rounds": 0}, "max_rounds < 1"),
    ({**IMPLEMENT, "impl_rounds": -1}, "negative rounds"),
    ({**IMPLEMENT, "bogus": 1}, "unknown param"),
    ({**IMPLEMENT, "tests_task": ""}, "empty task"),
    ({**IMPLEMENT, "tests_task": "   "}, "whitespace task"),
])
def test_nc_r42_a_bad_instantiation_is_refused_whole_and_creates_nothing(w, params, why):
    w.start_scheduler()
    before = snapshot(w)
    reply = w.instantiate("implement", params)
    assert err_code(reply) == "invalid", (why, reply)
    assert snapshot(w) == before, why


@pytest.mark.parametrize("bad_task", [
    '"{param: x} suffix"', '"prefix {param: x}"', '"{{x}}"', '"${x}"', '"$(echo hi)"',
])
def test_nc_r41_interpolation_is_not_a_thing(w, bad_task):
    w.start_scheduler()
    text = f"template: interp\nversion: 1\nparams:\n  x: {{type: string, default: a}}\nroot:\n  key: top\n  kind: simple\n  agent: worker\n  task: {bad_task}\n"
    reg = w.register(text)
    if reg.get("ok"):
        inst = w.instantiate("interp", {})
        if inst.get("ok"):
            task = w.get(unwrap(inst["result"])["id"])["task"]
            assert task == bad_task.strip('"'), "the text was interpreted, not taken literally"
            return
    assert snapshot(w)[1] == []


def test_nc_r41_an_unknown_param_reference_or_bad_type_is_refused(w):
    w.start_scheduler()
    bad = TPL.format(version=1, a="x", b="y").replace("{param: second}", "{param: nope}")
    reg = w.register(bad)
    assert reg.get("ok") is False or w.instantiate("seq2", {}).get("ok") is False
    assert snapshot(w)[1] == []
    badtype = "template: t\nversion: 1\nparams:\n  p: {type: float}\nroot: {key: a, kind: simple, agent: worker, task: x}\n"
    assert w.register(badtype).get("ok") is False


def test_nc_r41_params_are_used_as_whole_values_and_defaults_apply(w):
    w.start_scheduler()
    w.register_ok(TPL.format(version=1, a="alpha", b="beta"))
    top = w.instantiate_ok("seq2", {"first": "custom first"})
    one, two = (w.get(c) for c in w.children(top))
    assert one["task"] == "custom first" and two["task"] == "beta"
    assert one["agent"] == "worker"


def test_nc_r41_a_template_file_in_the_worktree_is_never_loaded(w):
    w.start_scheduler()
    evil = TPL.format(version=1, a="EVIL", b="EVIL").replace("seq2", "evil")
    shadow = TPL.format(version=99, a="EVIL", b="EVIL").replace("seq2", "implement")
    for rel in (".multiagents/node-templates", ".multiagents/config/node-templates",
                ".multiagents/templates", "node-templates", "defaults/node-templates"):
        d = w.root / rel
        d.mkdir(parents=True, exist_ok=True)
        (d / "evil.yaml").write_text(evil)
        (d / "implement.yaml").write_text(shadow)
    assert "evil" not in json.dumps(w.ok("list_templates", {}))
    assert w.instantiate("evil", {}).get("ok") is False
    top = w.instantiate_ok("implement", IMPLEMENT)
    assert w.get(top)["kind"] == "sequence" and "EVIL" not in json.dumps(w.get(top))


def test_nc_r41_registration_is_root_only(w):
    w.start_scheduler()
    from multiagents.scheduler import issue_run_capability
    anchor = w.simple("A", fx={"hang": True})
    w.wait_running(anchor, timeout=WAIT)
    token = issue_run_capability(w.root, "some-run", anchor, {"read", "delegate"})
    reply = w.rpc("register_template", {"yaml": TPL.format(version=1, a="a", b="b")}, token=token)
    assert err_code(reply) == "forbidden"
    assert "seq2" not in json.dumps(w.ok("list_templates", {}))


# ----------------------------------------------------------------- NC-R42

def test_nc_r42_the_top_node_records_definition_bindings_version_and_sha(w):
    w.start_scheduler()
    top = w.instantiate_ok("implement", IMPLEMENT)
    rec = w.get(top)["template"]
    assert rec["name"] == "implement" and isinstance(rec["version"], int)
    assert re.fullmatch(r"[0-9a-f]{64}", rec["sha256"]) and rec["instance"]
    assert rec["bindings"].get("spec_path") == "SPEC.md" or "SPEC.md" in json.dumps(rec["bindings"])
    other = w.instantiate_ok("implement", IMPLEMENT)
    assert w.get(other)["template"]["instance"] != rec["instance"]
    assert w.get(other)["template"]["sha256"] == rec["sha256"]
    assert all(w.get(c).get("template") is None for c in w.children(top))


def test_nc_r42_an_instance_is_created_in_one_write(w):
    w.start_scheduler()
    before_rev, before_ids = snapshot(w)
    top = w.instantiate_ok("implement", IMPLEMENT)
    rev, ids = snapshot(w)
    assert rev == before_rev + 1, "one write, one plan revision"
    assert len(ids) - len(before_ids) == 1 + 2 + 4
    assert top in ids


def test_nc_r42_a_retried_instantiation_with_one_request_id_creates_one_instance(w):
    w.start_scheduler()
    a = w.rpc("instantiate_template", {"name": "implement", "params": IMPLEMENT}, request_id="same-1")
    b = w.rpc("instantiate_template", {"name": "implement", "params": IMPLEMENT}, request_id="same-1")
    assert a == b and a.get("ok")
    assert len(snapshot(w)[1]) == 7


def test_nc_r42_editing_the_registry_after_instantiation_changes_nothing_of_the_instance(w):
    from nc_fixture.agent import task
    w.start_scheduler()
    w.register_ok(TPL.format(version=1, a="x", b="V1-SECOND"))
    top = w.instantiate_ok("seq2", {"first": task("F", gate="gF")})
    one_id, two_id = w.children(top)
    w.wait_running(one_id, timeout=WAIT)
    recorded = w.get(top)["template"]
    w.register_ok(TPL.format(version=2, a="x", b="V2-SECOND"))
    now = w.get(top)["template"]
    assert now["version"] == recorded["version"] == 1 and now["sha256"] == recorded["sha256"]
    w.gate("gF")
    call = w.wait_spawn("F", timeout=WAIT)  # noqa: F841
    w.until(lambda: any("V1-SECOND" in c["prompt"] or "V2-SECOND" in c["prompt"] for c in w.fx.calls()
                        if c["tag"] != "F"), what="the second child to launch")
    prompts = " ".join(c["prompt"] for c in w.fx.calls() if c["tag"] != "F")
    assert "V1-SECOND" in prompts and "V2-SECOND" not in prompts
    top2 = w.instantiate_ok("seq2", {})
    assert w.get(top2)["template"]["version"] == 2
    assert w.get(top2)["template"]["sha256"] != recorded["sha256"]
