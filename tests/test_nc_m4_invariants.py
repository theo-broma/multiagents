"""Additional M4 checks for frozen bindings and persistent composite locks."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.m3_adv import Harness
from multiagents.scheduler import sessions
from multiagents.scheduler.model import Refused


@pytest.fixture
def h(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    yield harness
    harness.close()


def aliased(h):
    root, children = h.tree("group", 2)
    root["template"] = {"instance": "test-instance"}
    for child in children:
        child["session"] = "B"
    h.save(root, *children)
    return root, children


def test_nc_r30_retained_agent_without_provider_still_checks_the_frozen_binding(h, monkeypatch):
    _, children = aliased(h)
    config = h.service.configuration()
    with h.service.store.transaction() as db:
        sessions.freeze(db, children[0], h.service.store.nodes(db), {"provider": "fx", "model": "fx/m1"}, h.world.paths, config)
    monkeypatch.setitem(config.agents, "coder", config.agents["coder"].replace(provider=""))
    with h.service.store.transaction() as db:
        with pytest.raises(Refused) as refused:
            sessions.freeze(db, children[1], h.service.store.nodes(db),
                            {"provider": "other-family", "model": "other/m1"}, h.world.paths, config)
        assert refused.value.result["error"] == "session_unavailable"


def test_nc_r30_an_account_change_cannot_silently_rebind_an_alias(h, monkeypatch):
    _, children = aliased(h)
    config = h.service.configuration()
    with h.service.store.transaction() as db:
        sessions.freeze(db, children[0], h.service.store.nodes(db), {"provider": "fx", "model": "fx/m1"}, h.world.paths, config)
        binding = sessions.aliases(db)
    monkeypatch.setattr(h.engine.runner.providers["fx"], "container_account", "different")
    blocked = sessions.blockers(children[1], h.nodes(), {}, binding, h.engine.runner)
    assert [reason["code"] for reason in blocked] == ["session_unavailable"]


def test_nc_r68_an_unlaunched_composite_does_not_keep_an_abandoned_claims_locks(h):
    root, children = h.tree("group", 1)
    root["locks"] = ["shared"]
    outside = h.record(locks=["shared"])
    h.save(root, outside)
    claim = {"state": "abandoned", "node_id": children[0]["id"], "locks": ["shared"],
             "lock_owners": {"shared": root["id"]}}
    assert h.engine.lock_blockers(outside, h.nodes(), {"claim": claim}) == []


def test_nc_r68_a_launched_composite_keeps_its_lock_with_no_active_attempt(h):
    root, children = h.tree("group", 1)
    root.update(locks=["shared"], lock_claimed=True, state="running")
    children[0].update(state="done", outcome="completed")
    outside = h.record(locks=["shared"])
    h.save(root, children[0], outside)
    blocked = h.engine.lock_blockers(outside, h.nodes(), {})
    assert [reason["code"] for reason in blocked] == ["lock"]
    root.update(state="cancelled")
    h.save(root)
    assert h.engine.lock_blockers(outside, h.nodes(), {}) == []


@pytest.mark.parametrize("leftover", ["ignored", "assume_unchanged"])
def test_nc_r62_ignored_files_and_index_flags_cannot_hide_dirty_alias_work(h, leftover):
    node = h.record()
    h.save(node)
    attempt, run = h.launch(node["id"])
    h.commit(run, {"owned.txt": "captured\n"})
    h.engine.finished(attempt, run)
    captured = h.journal()[attempt["attempt_id"]]["result"]["commit"]
    checkout = Path(run.worktree)
    if leftover == "ignored":
        (checkout / ".git" / "info" / "exclude").write_text("scratch.txt\n")
        path = checkout / "scratch.txt"
    else:
        from multiagents import gitops
        gitops.run(checkout, "update-index", "--assume-unchanged", "owned.txt", check=True)
        path = checkout / "owned.txt"
    path.write_text("keep this work\n")
    binding = {"id": "dirty-check", "worktree": run.worktree, "commit": captured}
    with pytest.raises(Refused) as refused:
        sessions.check_clean(h.world.paths, binding, h.engine.runner.authority)
    assert refused.value.result["error"] == "dirty_worktree"
    assert Path(refused.value.result["detail"]).is_file()
    assert path.read_text() == "keep this work\n"


def test_nc_r5_a_completed_composite_has_a_generation_consumable_as_an_input(h):
    from multiagents.scheduler.results import input_generation
    root, children = h.tree("group", 1)
    attempt, run = h.launch(children[0]["id"])
    h.commit(run, {"output.txt": "work\n"})
    h.engine.finished(attempt, run)
    h.engine.composites()
    nodes = h.nodes()
    generation = input_generation(nodes[root["id"]], {"node": root["id"]}, nodes)
    assert generation and generation["commit"] == nodes[root["id"]]["branch_tip"]


def test_nc_r62_a_committed_readonly_change_does_not_make_the_alias_dirty(h):
    from multiagents import gitops
    (h.world.root / "protected.txt").write_text("original\n")
    gitops.run(h.world.root, "add", "protected.txt", check=True)
    gitops.run(h.world.root, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
               "commit", "-qm", "protected baseline", check=True)
    node = h.record()
    h.save(node)
    attempt, run = h.launch(node["id"], readonly=["protected.txt"])
    raw_commit = h.commit(run, {"protected.txt": "committed but excluded\n"})
    h.engine.finished(attempt, run)
    result = h.journal()[attempt["attempt_id"]]["result"]
    assert result["commit"] != raw_commit and result["checkout_commit"] == raw_commit
    binding = {"id": "readonly-alias", "worktree": run.worktree, "commit": result["checkout_commit"]}
    sessions.check_clean(h.world.paths, binding, h.engine.runner.authority)


def test_nc_r59_a_delegate_cannot_attach_a_new_child_to_a_launched_alias(h):
    from multiagents.scheduler.store import issue_run_capability
    root, children = aliased(h)
    children[0]["runs"] = [{"run_id": "previous", "attempt_id": "previous"}]
    children[1]["session"] = None
    h.save(*children)
    token = issue_run_capability(h.world.root, "delegating-run", children[1]["id"], {"delegate"})
    reply = h.service.request({"op": "create_node", "request_id": "delegate-late-alias", "token": token,
                               "args": {"kind": "simple", "agent": "coder", "task": "delegated work",
                                        "session": "B", "parent": children[1]["id"]}})
    assert reply["error"]["error"] == "invalid"
    assert len(h.nodes()) == 3


@pytest.mark.parametrize("status", ["done", "failed"])
def test_nc_r34_r69_a_verdict_is_applied_only_after_successful_completion(h, status):
    from dataclasses import replace
    from multiagents.scheduler.engine import save_attempt
    from multiagents.scheduler.store import issue_run_capability
    from multiagents.scheduler.results import input_generation
    loop, children = h.tree("group", 2)
    loop.update(kind="loop", loop={"verdict_child": children[-1]["id"], "max_rounds": 3, "rounds_rejected": 0})
    h.save(loop)
    attempt, run = h.launch(children[0]["id"])
    h.commit(run, {"output.txt": "candidate\n"})
    h.engine.finished(attempt, run)
    attempt, run = h.launch(children[1]["id"])
    generation = h.nodes()[loop["id"]]["generations"][-1]
    review = {"node_id": loop["id"], "generation_seq": generation["seq"], "commit": generation["commit"]}
    attempt["review"] = review
    with h.service.store.transaction() as db:
        save_attempt(db, attempt)
    token = issue_run_capability(h.world.root, run.id, children[1]["id"], {"verdict"})
    reply = h.service.request({"op": "give_verdict", "request_id": "propose", "token": token,
                               "args": {**review, "verdict": "approved"}})
    assert reply["ok"]
    nodes = h.nodes()
    assert nodes[loop["id"]]["generations"][-1]["verdict"] is None
    assert input_generation(nodes[children[0]["id"]], {}, nodes) is None
    h.engine.finished(attempt, replace(run, status=status))
    nodes = h.nodes()
    expected = "approved" if status == "done" else None
    assert nodes[loop["id"]]["generations"][-1]["verdict"] == expected
    assert nodes[children[0]["id"]]["generations"][-1]["verdict"] == expected
    assert bool(input_generation(nodes[children[0]["id"]], {}, nodes)) == (status == "done")


def test_nc_r37_r96_root_approval_is_separate_and_allows_the_last_generation(h):
    from multiagents.scheduler.results import input_generation
    loop, children = h.tree("group", 1)
    loop.update(kind="loop", loop={"verdict_child": children[0]["id"], "max_rounds": 1, "rounds_rejected": 1},
                state="held", hold={"reason": "loop_max"})
    gen = {"seq": 1, "run_id": "rejected-work", "commit": "a" * 40, "verdict": "rejected"}
    loop["generations"] = [dict(gen)]
    children[0].update(state="done", outcome="completed", generations=[dict(gen)])
    h.save(loop, *children)
    with h.service.store.transaction(write=False) as db:
        token = h.service.store.meta(db, "root_token")
    reply = h.service.request({"op": "close_node", "request_id": "override", "token": token,
                               "args": {"id": loop["id"], "revision": loop["revision"], "outcome": "approved"}})
    assert reply["ok"]
    nodes = h.nodes()
    assert nodes[loop["id"]]["closure"]["outcome"] == "approved"
    for node in nodes.values():
        assert node["generations"][-1]["verdict"] == "rejected"
        assert input_generation(node, {}, nodes) == gen


@pytest.mark.parametrize("change", ["dirty", "holder", "node", "attempt"])
def test_nc_r62_dirty_check_releases_the_store_and_stale_checks_never_claim(h, monkeypatch, change):
    import asyncio
    from unittest.mock import AsyncMock, Mock
    from multiagents.scheduler.engine import save_attempt
    root, children = aliased(h)
    children[0].update(state="done", outcome="completed")
    h.save(children[0])
    with h.service.store.transaction() as db:
        sessions.freeze(db, children[0], h.nodes(), {"provider": "fx", "model": "fx/m1"},
                        h.world.paths, h.service.configuration())
    monkeypatch.setattr(h.engine.runner, "adopt", AsyncMock())
    monkeypatch.setattr(h.engine, "reconcile", AsyncMock())
    monkeypatch.setattr(h.engine.runner, "start", AsyncMock(return_value={"admitted": True, "provider": "fx", "model": "fx/m1"}))
    spawn = Mock()
    monkeypatch.setattr(h.engine, "spawn", spawn)
    checked = []
    def clean(paths, binding, authority):
        # An independent writer succeeds during the filesystem check; a
        # scheduler-held write transaction would lock this connection out.
        with h.service.store.transaction() as db:
            checked.append(binding["id"])
            if change == "holder":
                sessions.save_alias(db, {**binding, "last_run": "new-holder"})
            elif change == "node":
                node = h.service.store.nodes(db)[children[1]["id"]]
                node["revision"] += 1
                h.service.store.save_node(db, node)
            elif change == "attempt":
                save_attempt(db, {"attempt_id": "intervening", "node_id": children[0]["id"], "run_id": "other",
                                  "alias_id": binding["id"], "state": "recorded"})
        if change == "dirty":
            raise Refused("dirty_worktree", detail="kept.diff")
    monkeypatch.setattr(sessions, "check_clean", clean)
    asyncio.run(h.engine.tick())
    assert checked
    assert not spawn.called
    assert not any(a["node_id"] == children[1]["id"] for a in h.journal().values())
    node = h.nodes()[children[1]["id"]]
    assert node["state"] == ("held" if change == "dirty" else "open")


def test_nc_r67_a_terminal_run_keeps_waking_evaluation_until_death_is_confirmed(h, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    node = h.record()
    h.save(node)
    attempt, run = h.launch(node["id"])
    monkeypatch.setattr(h.engine, "spawn", lambda _: None)
    monkeypatch.setattr(h.engine.runner, "_steer_predecessor_dead", AsyncMock(return_value=False))
    assert h.engine.completion_changed()
    asyncio.run(h.engine.reconcile())
    assert h.journal()[attempt["attempt_id"]]["state"] == "launched"
    assert h.engine.completion_changed()
    h.engine.finished(attempt, run)
    assert not h.engine.completion_changed()


@pytest.mark.parametrize("change", ["dirty", "holder", "node", "attempt"])
def test_nc_r62_worker_preflight_releases_the_store_and_drops_stale_claims(h, monkeypatch, change):
    import asyncio
    from unittest.mock import AsyncMock
    from multiagents.scheduler import worker
    from multiagents.scheduler.engine import save_attempt
    from multiagents.tree import now
    _, children = aliased(h)
    node = children[1]
    with h.service.store.transaction() as db:
        alias = sessions.freeze(db, node, h.nodes(), {"provider": "fx", "model": "fx/m1"},
                                h.world.paths, h.service.configuration())
        attempt = {"attempt_id": "preflight", "node_id": node["id"], "run_id": "ag-preflight",
                   "state": "claimed", "at": now(), **alias}
        save_attempt(db, attempt)
    start = AsyncMock()
    monkeypatch.setattr(worker, "Runner", lambda *args: h.engine.runner)
    monkeypatch.setattr(h.engine.runner, "start", start)
    def clean(paths, binding, authority):
        with h.service.store.transaction() as db:
            if change == "holder":
                sessions.save_alias(db, {**binding, "last_run": "other"})
            elif change == "node":
                current = h.service.store.nodes(db)[node["id"]]
                current["revision"] += 1
                h.service.store.save_node(db, current)
            elif change == "attempt":
                current = h.journal()[attempt["attempt_id"]]
                current["cancel_requested"] = True
                save_attempt(db, current)
        if change == "dirty":
            raise Refused("dirty_worktree", detail="kept.diff")
    monkeypatch.setattr(sessions, "check_clean", clean)
    asyncio.run(worker.supervise(h.world.root, attempt["attempt_id"]))
    start.assert_not_called()
    assert h.journal()[attempt["attempt_id"]]["state"] == "abandoned"
    assert h.nodes()[node["id"]]["state"] == ("held" if change == "dirty" else "open")


def test_nc_r59_a_peer_cannot_detach_after_the_alias_launches(h):
    root, children = aliased(h)
    children[0]["runs"] = [{"run_id": "previous", "attempt_id": "previous"}]
    h.save(children[0])
    nodes = h.nodes()
    original = h.nodes()
    nodes[children[1]["id"]]["session"] = None
    with pytest.raises(Refused) as refused:
        sessions.validate_attachments(nodes, original, {}, {})
    assert refused.value.result["error"] == "invalid"


def test_nc_r59_an_unrelated_run_does_not_seal_a_new_alias(h):
    root, children = aliased(h)
    outside = h.record()
    outside["runs"] = [{"run_id": "unrelated", "attempt_id": "unrelated"}]
    h.save(outside)
    original = h.nodes()
    original[children[1]["id"]]["session"] = None
    sessions.validate_attachments(h.nodes(), original, {}, {})
