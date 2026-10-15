"""IC-C9 — the seed (IC-R1): a self-contained bundle of exactly what the
attempt may read, cached, bound to its attempt, recorded before provisioning.

Registry level (P1, P2): `multiagents.provision.seeds.SeedRegistry.build /
resolve` over a real host repository, bundles inspected with plain git.
Scheduler level (P3): the evidence a real scheduler writes.

Imports of the slice 2 modules happen inside the fixtures (support: `ic_prov`),
so a missing seed surface raises an explicit NotImplementedError stub, rather
than preventing collection. Constructors and errors follow Q1..Q3.
"""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import ic_prov as ip  # noqa: E402


@pytest.fixture
def host(tmp_path, monkeypatch):
    # hermetic git: no ambient config reaches the host repository
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    repo = ip.HostRepo(tmp_path / "host")
    ids = ip.standard_world(repo)
    return repo, ids


@pytest.fixture
def reg(host):
    return ip.seed_registry(host[0].path)


def built(reg, host, attempt="att-a", commit=None):
    repo, ids = host
    ref = reg.build(attempt, commit or ids["input"])
    return ref, Path(reg.resolve(ref, attempt))


# ------------------------------------------------------- the bundle's content

def test_ic_c9_p2_the_bundle_is_self_contained_with_exactly_one_ref(host, reg, tmp_path):
    repo, ids = host
    ref, path = built(reg, host)
    assert path.is_file()
    header = ip.bundle_header(path)
    assert header[0].startswith("# v"), header[0]
    prerequisites = [line for line in header if line.startswith("-")]
    assert prerequisites == [], "the bundle has prerequisites, it cannot be cloned alone"
    heads = ip.bundle_heads(path)
    assert len(heads) == 1, heads
    assert heads[0][0] == ids["input"]                      # one ref, pointing at checkout_commit
    assert ip.bundle_verifies(path, tmp_path)               # verifies with no repository at hand


def test_ic_c9_the_bundles_objects_equal_the_reachable_set_of_the_input_commit(host, reg, tmp_path):
    repo, ids = host
    _, path = built(reg, host)
    assert ip.bundle_objects(path, tmp_path) == repo.reachable(ids["input"])


def test_ic_c9_a_sibling_attempts_unmerged_commit_and_blob_are_absent(host, reg, tmp_path):
    repo, ids = host
    _, path = built(reg, host)
    objects = ip.bundle_objects(path, tmp_path)
    sibling_tree = ip.git(repo.path, "rev-parse", ids["sibling"] + "^{tree}")
    for secret in (ids["sibling"], ids["secret_blob"], sibling_tree, ids["unrelated"]):
        assert secret not in objects, secret
    assert repo.has(ids["sibling"])                          # the control: the host does have it


def test_ic_c9_the_seed_carries_complete_history_never_a_shallow_cut(host, reg, tmp_path):
    repo, ids = host
    _, path = built(reg, host)
    objects = ip.bundle_objects(path, tmp_path)               # fails on a shallow bundle
    assert ids["base"] in objects and ids["work"] in objects
    assert len(ip.git(repo.path, "rev-list", ids["input"]).split()) == 2


def test_ic_c9_p2_a_merge_input_authorises_both_sides_and_nothing_else(host, reg, tmp_path):
    repo, ids = host
    repo.branch("left", ids["base"])
    left = repo.commit("left", {"left.txt": "l\n"})
    repo.branch("right", ids["base"])
    right = repo.commit("right", {"right.txt": "r\n"})
    ip.git(repo.path, "checkout", "-q", "left")
    ip.git(repo.path, "merge", "-q", "--no-ff", "-m", "synthetic merge of the inputs", "right")
    merge = repo.head()
    _, path = built(reg, host, commit=merge)
    objects = ip.bundle_objects(path, tmp_path)
    assert objects == repo.reachable(merge)
    assert left in objects and right in objects and ids["work"] not in objects
    assert ip.bundle_heads(path)[0][0] == merge


def test_ic_c9_the_bundle_is_not_built_from_the_current_branch_but_from_the_given_commit(host, reg, tmp_path):
    """A later host commit on main must not enter the seed of an earlier input."""
    repo, ids = host
    repo.checkout("main")
    later = repo.commit("later", {"later.txt": "after the input\n"})
    _, path = built(reg, host)
    objects = ip.bundle_objects(path, tmp_path)
    assert later not in objects and objects == repo.reachable(ids["input"])


# ---------------------------------------------------------------------- cache

def test_ic_c9_a_second_attempt_with_the_same_inputs_reuses_the_entry(host, reg):
    _, first = built(reg, host, "att-a")
    before = first.stat()
    _, second = built(reg, host, "att-b")
    assert second == first
    after = second.stat()
    assert (after.st_ino, after.st_mtime_ns, after.st_size) == (before.st_ino, before.st_mtime_ns, before.st_size)


def test_ic_c9_different_inputs_get_different_entries(host, reg):
    repo, ids = host
    _, a = built(reg, host, "att-a", ids["input"])
    _, b = built(reg, host, "att-b", ids["base"])
    assert a != b


def test_ic_c9_a_cache_hit_still_hands_out_a_verified_bundle(host, reg, tmp_path):
    _, first = built(reg, host, "att-a")
    _, second = built(reg, host, "att-b")
    assert ip.bundle_verifies(second, tmp_path)


def test_ic_c9_concurrent_builds_of_one_key_yield_one_complete_entry(host, tmp_path):
    repo, ids = host
    for i in range(30):                                     # a bundle worth racing for
        repo.commit(f"bulk {i}", {f"bulk/{i}.bin": os.urandom(4000)})
    commit = repo.head()
    go = tmp_path / "go"
    ready = tmp_path / "ready"
    ready.mkdir()
    code = (
        "import json, sys, time\n"
        f"sys.path.insert(0, {str(ip.SUPPORT)!r}); sys.path.insert(0, {str(ip.SRC)!r})\n"
        "import ic_prov\nfrom pathlib import Path\n"
        f"reg = ic_prov.seed_registry(Path({str(repo.path)!r}))\n"
        "attempt = sys.argv[1]\n"
        f"Path({str(ready)!r}, attempt).touch()\n"
        f"while not Path({str(go)!r}).exists(): time.sleep(0.005)\n"
        f"ref = reg.build(attempt, {commit!r})\n"
        "print(json.dumps(str(reg.resolve(ref, attempt))))\n")
    procs = [subprocess.Popen([sys.executable, "-c", code, f"att-{i}"], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True,
                              env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null"})
             for i in range(5)]
    try:
        ip.wait_until(lambda: len(list(ready.iterdir())) == 5 or any(p.poll() is not None for p in procs),
                      "all builders at the barrier")
        assert len(list(ready.iterdir())) == 5, "a builder failed before the barrier"
        go.write_text("go")
        results = []
        for p in procs:
            out, err = p.communicate(timeout=ip.OP * 2)
            assert p.returncode == 0, err[-1500:]
            results.append(json.loads(out.strip().splitlines()[-1]))
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
    assert len(set(results)) == 1, results
    entry = Path(results[0])
    assert ip.bundle_verifies(entry, tmp_path)
    assert ip.bundle_objects(entry, tmp_path) == repo.reachable(commit)
    # nothing half-built is left beside the entry (a sibling file that is a bundle but not this one)
    others = [p for p in entry.parent.iterdir() if p != entry and p.is_file() and p.stat().st_size
              and ip.bundle_header(p)[0:1] and ip.bundle_header(p)[0].startswith("# v")]
    assert others == [], others


def test_ic_c9_concurrent_builds_in_threads_agree_too(host, reg):
    repo, ids = host
    out, errs = [], []

    def work(i):
        try:
            ref = reg.build(f"att-t{i}", ids["input"])
            out.append(str(reg.resolve(ref, f"att-t{i}")))
        except BaseException as exc:      # noqa: BLE001
            errs.append(repr(exc))

    threads = [threading.Thread(target=work, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(ip.OP)
    assert not errs, errs
    assert len(set(out)) == 1 and len(out) == 6


# ----------------------------------------------------------------- corruption

@pytest.mark.parametrize("damage", ["garbage", "truncated", "empty", "deleted"])
def test_ic_c9_a_corrupt_entry_is_rebuilt_from_its_pinned_ids(host, reg, tmp_path, damage):
    repo, ids = host
    ref, path = built(reg, host)
    data = path.read_bytes()
    if damage == "garbage":
        path.write_bytes(b"not a bundle at all" * 20)
    elif damage == "truncated":
        path.write_bytes(data[: len(data) // 2])
    elif damage == "empty":
        path.write_bytes(b"")
    else:
        path.unlink()
    again = Path(reg.resolve(ref, "att-a"))
    assert again.is_file() and ip.bundle_verifies(again, tmp_path)
    assert ip.bundle_objects(again, tmp_path) == repo.reachable(ids["input"])


def test_ic_c9_a_corrupt_entry_whose_pinned_objects_are_gone_is_seed_unavailable(host, tmp_path):
    repo, ids = host
    repo.branch("throwaway", ids["input"])
    only = repo.commit("only reachable through the throwaway branch", {"only.txt": "x\n"})
    reg = ip.seed_registry(repo.path)
    ref = reg.build("att-a", only)
    path = Path(reg.resolve(ref, "att-a"))
    # the pin lapses (an attempt-less entry may be evicted) and the host forgets the commit
    for line in ip.git(repo.path, "for-each-ref", "--format=%(refname)", "refs/multiagents/seeds/").splitlines():
        ip.git(repo.path, "update-ref", "-d", line)
    repo.checkout("main")
    ip.git(repo.path, "branch", "-D", "throwaway")
    ip.git(repo.path, "reflog", "expire", "--expire=now", "--all")
    ip.git(repo.path, "gc", "-q", "--prune=now")
    assert not repo.has(only)
    path.write_bytes(b"corrupt")
    exc = ip.refused(reg.resolve, ref, "att-a", code="seed_unavailable")
    assert exc.code == "seed_unavailable"
    # and it never falls back to something else: the corrupt file is not handed out later either
    ip.refused(reg.resolve, ref, "att-a", code="seed_unavailable")


def test_ic_c9_a_failed_build_leaves_no_usable_handle_and_does_not_poison_the_next(host, reg, tmp_path):
    repo, ids = host
    missing = "0123456789abcdef0123456789abcdef01234567"
    ip.refused(reg.build, "att-x", missing)
    ref, path = built(reg, host, "att-ok")
    assert ip.bundle_verifies(path, tmp_path)


# ------------------------------------------------------------ authority (P1)

def test_ic_c9_p1_a_handle_resolves_for_its_own_attempt(host, reg):
    ref, path = built(reg, host, "att-a")
    assert Path(reg.resolve(ref, "att-a")) == path


def test_ic_c9_p1_a_handle_is_refused_for_another_attempt_even_with_a_shared_entry(host, reg):
    ref_a, path_a = built(reg, host, "att-a")
    ref_b, path_b = built(reg, host, "att-b")               # same inputs: one shared entry
    assert path_a == path_b
    ip.refused(reg.resolve, ref_a, "att-b", code="unauthorised")
    ip.refused(reg.resolve, ref_b, "att-a", code="unauthorised")
    assert Path(reg.resolve(ref_a, "att-a")) == path_a      # the owner is not locked out


@pytest.mark.parametrize("other", ["", "att-A", "att-a ", "att-a/../att-b", "att"])
def test_ic_c9_p1_only_the_exact_attempt_id_resolves(host, reg, other):
    ref, _ = built(reg, host, "att-a")
    ip.refused(reg.resolve, ref, other, code="unauthorised")


def test_ic_c9_p1_a_forged_or_unknown_handle_is_refused(host, reg):
    ref, path = built(reg, host, "att-a")
    for bogus in ("0" * 64, "../../etc/passwd", str(path), ""):
        forged = _forge(ref, bogus)
        ip.refused(reg.resolve, forged, "att-a")


def _forge(ref, new_id):
    import copy
    forged = copy.copy(ref)
    object.__setattr__(forged, "id", new_id)
    return forged


def test_ic_c9_p1_the_handle_is_frozen_opaque_and_hashable(host, reg):
    ref, _ = built(reg, host)
    assert isinstance(ref.id, str) and ref.id
    with pytest.raises((AttributeError, TypeError, dataclasses.FrozenInstanceError)):
        ref.id = "other"
    hash(ref)
    assert str(ref.id) not in str(host[0].path)
    assert not Path(ref.id).is_absolute()                   # opaque: not a bundle path


def test_ic_c9_p1_two_builds_for_one_attempt_give_the_same_handle(host, reg):
    a, _ = built(reg, host, "att-a")
    b, _ = built(reg, host, "att-a")
    assert a == b


# ---------------------------------------------------------------- P3 (record)

@pytest.fixture
def world(tmp_path, monkeypatch):
    w = ip.make_world(tmp_path, monkeypatch)
    w.pair()
    yield w
    w.close()


SEED_KEYS = {"ref", "checkout_commit", "roots", "object_format", "seed_version", "cache_key"}


def test_ic_c9_p3_the_seed_record_is_written_before_provision(world):
    """The injected backend fails the first provisioning: the record is
    already there, next to the creation key, and no identity exists."""
    world.plan({"fail_from_nth_provision": 1})
    world.start_scheduler()
    node = world.simple("S1", "wk", fx={})
    run = world.until(lambda: (world.runs(node) or [None])[0], what="the first attempt", timeout=ip.SPAWN)
    ex = ip.wait_until(lambda: world.evidence(run["attempt_id"]).get("executor", {}).get("seed")
                       and world.evidence(run["attempt_id"])["executor"], "the seed record", timeout=ip.SPAWN)
    assert set(ex["seed"]) >= SEED_KEYS, ex["seed"]
    assert ex.get("creation_key") and not ex.get("identity")
    assert world.fxw.spawns() == 0


def test_ic_c9_p3_the_seed_record_names_the_attempts_input(world):
    world.start_scheduler()
    node = world.simple("S2", "wk", fx={"gate": "g"})
    world.wait_spawn("S2", world.fxw)
    run = world.runs(node)[0]
    seed = world.evidence(run["attempt_id"])["executor"]["seed"]
    assert set(seed) >= SEED_KEYS
    assert seed["checkout_commit"] == run["input_commit"]
    assert seed["roots"] == [run["input_commit"]]            # P2: exactly one root
    assert seed["object_format"] == "sha1"
    assert isinstance(seed["seed_version"], int) and seed["seed_version"] >= 1
    assert isinstance(seed["ref"], str) and seed["ref"] and isinstance(seed["cache_key"], str) and seed["cache_key"]
    world.gate("g", world.fxw)


def test_ic_c9_p3_the_cache_entry_is_pinned_by_a_host_ref_while_the_attempt_is_unfinished(world):
    world.start_scheduler()
    node = world.simple("S3", "wk", fx={"gate": "g"})
    world.wait_spawn("S3", world.fxw)
    run = world.runs(node)[0]
    seed = world.evidence(run["attempt_id"])["executor"]["seed"]
    ref = "refs/multiagents/seeds/" + seed["cache_key"]
    assert ref in world.host_refs(), sorted(world.host_refs())
    pinned = world.host_refs()[ref]
    assert ip.git_ok(world.root, "merge-base", "--is-ancestor", run["input_commit"], pinned) or \
        pinned == run["input_commit"]
    world.gate("g", world.fxw)


def test_ic_c9_p3_every_attempt_records_the_seed_of_its_own_preparation(world):
    """The first provisioning fails; the host branch moves on; whichever
    attempt launches, its seed is the input of ITS recorded preparation."""
    world.plan({"fail_nth_provision": 1})
    world.start_scheduler()
    node = world.simple("S4", "wk", fx={"gate": "g"})
    first = world.until(lambda: (world.runs(node) or [None])[0], what="the first attempt", timeout=ip.SPAWN)
    ip.git(world.root, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
           "commit", "-q", "--allow-empty", "-m", "main moves while the node waits")
    world.wait_spawn("S4", world.fxw)
    runs = world.runs(node)
    seeds = [world.evidence(r["attempt_id"])["executor"]["seed"] for r in runs]
    assert len(seeds) >= 2
    for run, seed in zip(runs, seeds):
        assert seed["checkout_commit"] == run["input_commit"]
    world.gate("g", world.fxw)
