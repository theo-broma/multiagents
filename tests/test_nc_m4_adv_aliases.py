"""M4 adversary: joining a session alias after its first launch (NC-R59,
NC-R30/R31). `session` is editable only before the alias's first launch; the
implementation checks only whether the edited node itself has runs, so a
fresh node can be pointed at an alias that has already run and is then
activated in that alias's provider session and stable checkout.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.m4_world import M4World, commit_entry, err_code, verdict_entry  # noqa: E402

WAIT_ROUNDS = 20

SEQ = """\
template: m4advjoin
version: 1
params: {}
root:
  key: top
  kind: sequence
  children:
    - key: lp
      kind: loop
      verdict_child: rev
      max_rounds: 2
      children:
        - {key: wrk, kind: simple, agent: wk, task: "implement the thing"}
        - {key: rev, kind: simple, agent: rv, task: "review the thing", session: B}
    - {key: late, kind: simple, agent: rv, task: "FX {\\"tag\\": \\"LATE\\"}"}
"""


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = M4World(tmp_path, monkeypatch)
    world.fxw = world.provider("fxw")
    world.fxr = world.provider("fxr")
    world.agent("wk", "fxw", writes=True)
    world.agent("rv", "fxr", writes=True)
    yield world
    world.close()


def test_adv_session_cannot_be_set_to_an_alias_that_has_already_launched(w):
    w.fxw.queue(commit_entry("a.txt", "one\n", "r1"))
    w.fxr.queue({"gate": "gr", **verdict_entry("approved")})
    w.start_scheduler()
    w.register_ok(SEQ)
    top = w.instantiate_ok("m4advjoin", {})
    lp, late = w.children(top)
    wk, rv = w.children(lp)
    w.wait_running(rv, timeout=WAIT_ROUNDS)          # alias B has had its first launch
    reply = w.root_op("update_node", late, session="B")
    w.gate("gr", w.fxr)
    assert err_code(reply) == "invalid", (
        f"`session` was set to an alias after that alias's first launch: {reply}")
