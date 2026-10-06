"""A template node with `spec_path` and no task of its own (VR-R6).

The reviewer finding: `_with_spec` raised `KeyError` when the node had no `task`
field, and rendered a literal "None" when `task: null`. Both are one defect -- a
node that names a specification but says nothing about the thing under review
still needs a task to run, and the only text it has is the reference.

Assumptions where the contract is silent (kept loose):
- `task: null` is not rejected by `register_template`: the node's required
  fields are checked at expansion, where the reference turns it into a task.
- the node's task is read back with `get_node`, as VR-R6's own test reads the
  shipped reviewer tasks.
- a node that has a task is unchanged (its own text first, then the reference).
  Asserted on the shipped `implement` template, so the regression test does not
  fix a wording in its own fixture.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.m4_world import M4World  # noqa: E402

SPEC = "context/specs/vr-taskless-spec-path.md"
REFERENCE = f"The specification is at `{SPEC}`."

MISSING_TASK = """\
template: missing-task
version: 1
params:
  spec_path: {type: string, default: "%s"}
root:
  key: top
  kind: sequence
  children:
    - {key: review, kind: simple, agent: worker, spec_path: {param: spec_path}}
""" % SPEC

NULL_TASK = """\
template: null-task
version: 1
params:
  spec_path: {type: string, default: "%s"}
root:
  key: top
  kind: sequence
  children:
    - {key: review, kind: simple, agent: worker, task: null, spec_path: {param: spec_path}}
""" % SPEC

WITH_TASK = """\
template: with-task
version: 1
params:
  spec_path: {type: string, default: "%s"}
root:
  key: top
  kind: sequence
  children:
    - {key: review, kind: simple, agent: worker, task: do the work, spec_path: {param: spec_path}}
""" % SPEC


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = M4World(tmp_path, monkeypatch, tick_seconds=0.1)
    world.start_scheduler()
    for text in (MISSING_TASK, NULL_TASK, WITH_TASK):
        world.register_ok(text)
    yield world
    world.close()


def task_of(w, template: str) -> str:
    """The one child's task of an instantiation of `template`."""
    top = w.instantiate_ok(template, {})
    (child,) = w.get(top)["children"]
    return w.get(child).get("task") or ""


def test_a_node_with_a_spec_path_and_no_task_gets_the_reference_as_its_task(w):
    task = task_of(w, "missing-task")
    assert task == REFERENCE, task


def test_a_node_with_a_null_task_gets_the_reference_as_its_task(w):
    task = task_of(w, "null-task")
    assert task == REFERENCE, task


def test_no_taskless_node_renders_the_word_none(w):
    for template in ("missing-task", "null-task"):
        task = task_of(w, template)
        assert "None" not in task, task
        assert task.strip(), task


def test_a_node_with_a_task_keeps_the_composition_it_had(w):
    task = task_of(w, "with-task")
    assert task == f"do the work\n\n{REFERENCE}", task


def test_the_shipped_implement_reviewers_keep_task_then_reference(w):
    for role in ("tester", "reviewer", "implementer"):
        w.provider(f"fx{role}")
        w.agent(role, f"fx{role}", writes=True)
    top = w.instantiate_ok(
        "implement",
        {"spec_path": SPEC, "tests_task": "write tests", "implement_task": "implement it",
         "tester": "tester", "reviewer": "reviewer", "implementer": "implementer"})

    def walk(node_id):
        node = w.get(node_id)
        if node["kind"] == "simple" and node.get("agent") == "reviewer":
            yield node["task"]
        for child in node["children"]:
            yield from walk(child)

    reviewer_tasks = list(walk(top))
    assert len(reviewer_tasks) == 2, reviewer_tasks
    for task in reviewer_tasks:
        assert task.rstrip().endswith(REFERENCE), task
        assert not task.startswith("None"), task