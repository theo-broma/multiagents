"""IC-C6 — the isolation suite (IC-R13), written first, red until it passes.

Black box, through the scheduler's public entry points only: the project is
configured `executor.kind: isolated` (real backend, nothing injected), nodes
are made with `create_node`, observed with `get_node` and the run output; no
test reaches into a backend. The probe workload is `tests/support/ic_probe.py`
(a FakeProvider whose CLI runs the probes inside the launched environment);
it is delivered as the provider binary, never by mounting the host test tree.

Opt-in with SG_TEST_DOCKER=1. Without it the whole module is skipped. With it,
a missing prerequisite (no daemon, no image) FAILS (`require_docker`).
I could not run these under Docker (no daemon in the authoring sandbox): they
are verified to collect, and the probe script was exercised on the host for
syntax and for the probes that are safe to run there.

Every clause is a test with a positive control: the probe is shown to have run
and to have reached A's own equivalent of what it cannot reach in B. Each test
carries `ic_milestone(n)`: end-to-end isolation cannot pass before the
isolated launch exists (slice 3).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import ic_docker as dk  # noqa: E402
import ic_support as ic  # noqa: E402

pytestmark = pytest.mark.skipif(not dk.OPT_IN,
                                reason="real Docker is opt-in: SG_TEST_DOCKER=1")


@pytest.fixture
def world(tmp_path, monkeypatch, st_live_docker, request):
    dk.require_docker()                                  # fails, never skips, under the switch
    w = dk.IsolationWorld(tmp_path, monkeypatch, request.config.st_docker_owner)
    w.start()
    yield w
    dk.cleanup(w)


class Ns:
    pass


def pair(w, probe, **fields):
    p = Ns()
    p.w = w
    p.na, p.nb = w.nonce("A"), w.nonce("B")
    p.b = w.probe_node("plant", nonce=p.nb, hold=dk.HOLD, watch=p.na)
    p.planted = w.report(p.b, "planted")
    p.before_a = w.hold_count(p.b)
    env = p.planted.get("env_rpc") or {}
    token = next((v for k, v in env.items() if "TOKEN" in k), "none")
    endpoint = next((v for k, v in env.items() if "TOKEN" not in k), None)
    args = dict(nonce=p.na, victim=p.nb, sha=p.planted["blob"], addrs=p.planted["addrs"],
                ports=[p.planted["port"]], victim_token=token, victim_node=p.b, **fields)
    if endpoint:
        args["victim_endpoint"] = endpoint
    p.a = w.probe_node(probe, **args)
    p.report = w.final_report(p.a)
    p.after = w.last_hold(p.b, after=p.before_a)
    return p


# ---------------------------------------------------------------- filesystem

@pytest.mark.ic_milestone(3)
def test_ic_c6_a_cannot_read_modify_or_delete_bs_worktree_home_temp_or_run_directory(world):
    p = pair(world, "fs")
    try:
        r = p.report
        # positive control: the walk ran, and A reached its OWN markers
        assert r["scanned"] > 1000
        assert len(r["own_found"]) >= 3 and all(p.na in x for x in r["own_found"])
        assert all(r["own_intact"].values())
        # B's markers are nowhere A can see; nothing was touched
        assert r["hits"] == [], f"A found B's files: {r['hits']}"
        assert r["attacks"] == {}
        assert all(p.after["intact"].values()), p.after["intact"]
        assert set(p.after["intact"]) >= {"cwd", "home", "tmp"}
    finally:
        p.done()


@pytest.mark.ic_milestone(3)
def test_ic_c6_a_does_not_find_bs_secrets_by_content_either(world):
    """The name is not the only handle: B's blob content and token are looked for too."""
    p = pair(world, "fs")
    try:
        env = p.planted.get("env_rpc") or {}
        token = next((v for k, v in env.items() if "TOKEN" in k), None)
        assert token, "the victim reported no RPC token: nothing to look for"
        again = world.final_report(world.probe_node("fs", nonce=world.nonce("A2"), victim=token))
        assert again["scanned"] > 1000 and again["hits"] == []
    finally:
        p.done()


# ----------------------------------------------------------------------- git

@pytest.mark.ic_milestone(3)
def test_ic_c6_a_cannot_move_delete_or_read_bs_refs_and_objects(world):
    p = pair(world, "git")
    try:
        r = p.report
        # positive control: A has its own repository, blob and refs
        assert r["own_read"]["rc"] == 0 and p.na in r["own_read"]["out"]
        assert "refs/heads/ic-secret" in r["own_refs"]
        # B's committed blob is not readable from A's repository, and no
        # alternates expose a shared object store (IC-R1)
        assert r["victim_read_here"]["rc"] != 0 or p.nb not in r["victim_read_here"]["out"]
        assert r["victim_alternates"]["rc"] != 0
        # no foreign git directory is reachable at all; if one is, nothing worked on it
        for gitdir, tried in r["attacks"].items():
            assert tried["read_blob"]["rc"] != 0, gitdir
            assert tried["delete_ref"]["rc"] != 0 and tried["move_ref"]["rc"] != 0, gitdir
        assert r["other_gitdirs"] == [], f"foreign git directories visible: {r['other_gitdirs']}"
        # and B's own repository is intact
        assert p.after["blob_ok"] is True
        assert "refs/heads/ic-secret" in p.after["refs"]
    finally:
        p.done()


# ------------------------------------------------------------------- network

@pytest.mark.ic_milestone(4)
def test_ic_c6_a_cannot_reach_bs_network_services(world):
    p = pair(world, "net")
    try:
        r = p.report
        assert any(v is True for v in r["control"].values()), r["control"]     # A reaches its own
        assert r["tried"], "the probe tried nothing"
        assert not any(v is True for v in r["tried"].values()), r["tried"]
        assert p.after["listening"] is True
    finally:
        p.done()


@pytest.mark.ic_milestone(4)
def test_ic_c6_a_cannot_reach_bs_rpc_endpoint_or_use_its_credentials(world):
    p = pair(world, "rpc")
    try:
        r = p.report
        assert r["have_token"] and r["endpoint"], "A has no RPC token/endpoint of its own"
        assert r["own"].get("ok") is True, r["own"]                                # control
        victim_ep = r["victim_endpoint_own_token"]
        assert victim_ep.get("ok") is not True, victim_ep                         # B's endpoint
        assert r["forged_token"].get("ok") is not True, r["forged_token"]         # B's credential
        assert p.nb not in ic.text_of(r["forged_token"])
    finally:
        p.done()


@pytest.mark.ic_milestone(4)
def test_ic_c6_an_rpc_identity_cannot_be_forged(world):
    p = pair(world, "rpc")
    try:
        r = p.report
        assert r["own"].get("ok") is True                                         # control
        # B's token presented on A's endpoint is refused, even for A's own node
        assert r["forged_token_as_own"].get("ok") is not True, r["forged_token_as_own"]
        # A's token asking for B's node is out of scope: refused, nothing of B returned
        assert r["own_token_victim_node"].get("ok") is not True, r["own_token_victim_node"]
        assert p.nb not in ic.text_of(r["own_token_victim_node"].get("result"))
    finally:
        p.done()


# --------------------------------------------------------------------- cache

@pytest.mark.ic_milestone(5)
def test_ic_c6_a_cannot_poison_a_shared_cache_that_b_will_read(world):
    p = pair(world, "cache", poison=world.nonce("A"))
    try:
        r = p.report
        assert r["own_private_ok"] is True                                       # control: A writes its own
        landed = r["landed"]
        if landed:                                                               # a writable cache: A sees its own write
            assert any(p.na in x for x in r["readable"])
        # B scans its caches (and every cache-named directory) for A's marker
        assert p.after["seen_rival"] == [], p.after["seen_rival"]
        later = world.last_hold(p.b, after=world.hold_count(p.b))
        assert later["seen_rival"] == []
    finally:
        p.done()


# ------------------------------------------------------------------ readonly

@pytest.mark.ic_milestone(3)
def test_ic_c6_a_readonly_attempt_cannot_write_its_worktree_or_git_metadata(world):
    node = world.probe_node("ro", agent="reader")
    r = world.final_report(node)
    # positive control: it can read, git works, and the allowed scratch space is writable
    assert r["read_ok"]["ok"] is True and r["status"]["rc"] == 0
    assert r["tmp_write"]["ok"] is True
    writes = r["writes"]
    for name in ("write_worktree", "write_gitdir", "write_hooks", "write_config"):
        assert writes[name]["ok"] is False, f"{name} succeeded in a readonly attempt"
    assert writes["commit"]["rc"] != 0
    assert writes["update_ref"]["rc"] != 0
    assert r["status"]["out"].strip() == ""


# ------------------------------------------------------------------ handover

@pytest.mark.ic_milestone(3)
def test_ic_c6_a_dependencys_approved_work_reaches_its_dependent_and_nothing_unrelated_does(world):
    unrelated = world.nonce("U")
    stranger = world.probe_node("plant", nonce=unrelated, hold=dk.HOLD)
    world.report(stranger, "planted")
    try:
        nd = world.nonce("D")
        dep = world.probe_node("write", path="dep/out.txt", nonce=nd)
        world.final_report(dep)
        world.wait(lambda: world.get(dep).get("outcome") in ("approved", "success", "done", None)
                   and world.get(dep)["state"] == "done", what="the dependency to settle")
        dependent = world.probe_node("handover", expect="dep/out.txt", nonce=nd, unrelated=unrelated,
                                     depends_on=[{"node": dep}], inputs=[{"node": dep}])
        r = world.final_report(dependent)
        assert r["expected_content"] == nd                       # the deliberate handover works
        assert r["scanned"] > 1000 and r["unrelated_hits"] == []  # and nothing else does
    finally:
        world.stop_victim(stranger)


# ------------------------------------------------------- housekeeping (IC-C6)

@pytest.mark.ic_milestone(3)
def test_ic_c6_everything_a_test_creates_carries_the_session_label(world):
    node = world.probe_node("plant", nonce=world.nonce("L"), hold=dk.HOLD)
    try:
        world.report(node, "planted")
        mine = dk.referencing(world.tmp_marker)
        assert mine, "no container references this test's directory: where does the attempt run?"
        labelled = set(dk.owned(world.owner))
        assert set(mine) <= labelled, f"unlabelled containers: {sorted(set(mine) - labelled)}"
    finally:
        world.stop_victim(node)


def test_ic_c6_a_missing_prerequisite_fails_rather_than_skips(tmp_path, monkeypatch):
    """With the switch set, the prerequisite check itself must be an assertion."""
    monkeypatch.setattr(dk, "docker", lambda *a, **k: type(
        "R", (), {"returncode": 1, "stderr": "no daemon", "stdout": ""})())
    with pytest.raises(AssertionError, match="no Docker daemon"):
        dk.require_docker()
