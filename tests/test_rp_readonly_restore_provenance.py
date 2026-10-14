"""Readonly restore by provenance: RP1-RP6.

Contract: `context/specs/readonly-restore-provenance.md` (Decisions 1-6).

Every test drives the scheduler's completion boundary the way
`tests/test_nc_m3_result_replay.py` does: a node record and its launched
attempt are saved in the store, the run's checkout is a real managed clone,
the "agent" is git run in that checkout, and `Engine.finished(attempt, run)`
captures and integrates. Other nodes' generations are produced the same way,
so the generation records capture must trust are the engine's own. Every
observation is host git on the project repository or the stored node and
attempt records (`generations[]`, `result`).

Seams assumed (named, not invented):
- capture reads the generation records and the base branch itself, from the
  engine's store and the project configuration (`git.base_branch`, else the
  current branch, as `Results.prepare` resolves it); nothing extra is passed
  to `Engine.finished`.
- the base branch is moved with host plumbing (`update-ref`), as the
  orchestrator's own commits and `merge_node` move it.
- a disposed node keeps its record with `disposed` set (effects.py), and a
  rejected generation keeps `verdict: "rejected"`; RP1's "whatever its
  verdict, including nodes disposed since" is exercised by setting those two
  fields on a stored record.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.gitworld import GitWorld  # noqa: E402
from multiagents.scheduler.engine import Engine, attempts, save_attempt  # noqa: E402
from multiagents.scheduler.model import create_record  # noqa: E402
from multiagents.scheduler.results import Results  # noqa: E402
from multiagents.scheduler.rpc import Service  # noqa: E402
from multiagents.tree import Node, now  # noqa: E402

TEST = "tests/test_x.py"
SRC = "src/m.py"
# Bound on any single git command a test runs; a hung git fails fast.
GIT_BOUND = 20.0

GIT_ENV = {"GIT_AUTHOR_NAME": "agent", "GIT_AUTHOR_EMAIL": "agent@example.invalid",
           "GIT_COMMITTER_NAME": "agent", "GIT_COMMITTER_EMAIL": "agent@example.invalid",
           "GIT_EDITOR": "true", "GIT_MERGE_AUTOEDIT": "no"}


class Harness:
    def __init__(self, world, engine):
        self.world, self.engine = world, engine
        self.results = Results(world.paths, engine.service.configuration())

    # ---------------------------------------------------------------- host
    def git(self, *args, env=None):
        res = subprocess.run(["git", "-C", str(self.world.root), *args], capture_output=True, text=True,
                             env={**os.environ, **GIT_ENV, **(env or {})}, timeout=GIT_BOUND)
        assert res.returncode == 0, f"git {args}: {res.stderr}"
        return res.stdout.strip()

    def entry(self, ref, path):
        """`mode type object` of a path's tree entry, or None when absent."""
        out = self.git("ls-tree", ref, "--", path)
        return out.split("\t")[0] if out else None

    def blob(self, ref, path):
        return self.world.blob(ref, path)

    def base_commit(self, files, message="main change", parents=None):
        """A commit on the base branch's first-parent chain, made with
        plumbing so the project's worktree and config stay untouched."""
        base = "refs/heads/" + self.world.base
        before = self.git("rev-parse", base)
        commit = self.plumb(before, files, message, parents or [before])
        self.git("update-ref", base, commit, before)
        return commit

    def plumb(self, start, files, message, parents):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
            self.git("read-tree", start, env=env)
            for path, text in files.items():
                blob = subprocess.run(["git", "-C", str(self.world.root), "hash-object", "-w", "--stdin"],
                                      input=text, capture_output=True, text=True, check=True,
                                      timeout=GIT_BOUND).stdout.strip()
                self.git("update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", env=env)
            tree = self.git("write-tree", env=env)
        args = ["commit-tree", tree]
        for parent in parents:
            args += ["-p", parent]
        return self.git(*args, "-m", message)

    def nodes(self):
        with self.engine.store.transaction(write=False) as db:
            return json.loads(json.dumps(dict(self.engine.store.node_records(db))))

    def node(self, node_id):
        return self.nodes()[node_id]

    def result(self, launch):
        with self.engine.store.transaction(write=False) as db:
            return json.loads(json.dumps(attempts(db)[launch.attempt["attempt_id"]].get("result")))

    def update_node(self, node_id, **fields):
        with self.engine.store.transaction() as db:
            node = json.loads(json.dumps(self.engine.store.node_records(db)[node_id]))
            node.update(fields)
            self.engine.store.save_node(db, node)

    # ------------------------------------------------------------- launches
    def launch(self, readonly, inputs=()):
        """One top-level simple node with a launched attempt and its checkout."""
        node = create_record({"kind": "simple", "agent": "guard", "task": "work",
                              "inputs": [{"node": n} for n in inputs]}, "root")
        nodes = {**self.nodes(), node["id"]: node}
        prepared = self.results.prepare(node, nodes)
        run_id = "ag-" + uuid.uuid4().hex[:6]
        attempt = {"attempt_id": uuid.uuid4().hex, "activation_id": uuid.uuid4().hex,
                   "run_id": run_id, "node_id": node["id"], "state": "launched",
                   "locks": [], "at": now(), "readonly_paths": list(readonly), **prepared}
        node.update(state="running", runs=[{"run_id": run_id, "attempt_id": attempt["attempt_id"],
                                           "input_commit": prepared["input_commit"]}])
        with self.engine.store.transaction() as db:
            self.engine.store.save_node(db, node)
            save_attempt(db, attempt)
        self.results.ensure_branch(node)
        checkout = self.world.paths.worktree(run_id)
        branch = "agents/guard/" + run_id[3:]
        self.results.create_checkout(checkout, branch, prepared["input_commit"])
        run = Node(id=run_id, agent="guard", provider="fx", model="fx/m1", parent=None, depth=1,
                   branch=branch, worktree=str(checkout), status="done", node_id=node["id"],
                   attempt_id=attempt["attempt_id"])
        self.engine.runner.tree.add(run)
        self.engine.runner.authority.add(run)
        return SimpleNamespace(id=node["id"], attempt=attempt, run=run, checkout=checkout,
                               input=prepared["input_commit"])

    def sh(self, launch, script):
        """The agent's own work: a shell script in its checkout."""
        res = subprocess.run(["sh", "-ec", script], cwd=launch.checkout, capture_output=True, text=True,
                             env={**os.environ, **GIT_ENV}, timeout=GIT_BOUND)
        assert res.returncode == 0, f"{script}: {res.stdout}{res.stderr}"
        return res.stdout.strip()

    def write(self, launch, files, message="work"):
        for path, text in files.items():
            target = launch.checkout / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        self.sh(launch, f"git add -A && git commit -q -m '{message}'")
        return self.tip(launch)

    def tip(self, launch):
        return self.sh(launch, "git rev-parse HEAD")

    def finish(self, launch):
        self.engine.finished(launch.attempt, launch.run)
        return self.node(launch.id)

    def generation(self, files, readonly=()):
        """Another node's recorded generation that writes `files`."""
        launch = self.launch(readonly)
        self.write(launch, files)
        node = self.finish(launch)
        assert node["state"] == "done" and len(node["generations"]) == 1, node.get("hold")
        return node["generations"][0]["commit"]


@pytest.fixture
def h(tmp_path, monkeypatch):
    world = GitWorld(tmp_path, monkeypatch)
    world.commit_on_main(TEST, "v0\n", "test base")
    world.commit_on_main(SRC, "m0\n", "src base")
    world.commit_on_main("tests/test_other.py", "other\n", "other test")
    world.write_config()
    service = Service(world.root, now())
    service.store.initialize()
    engine = Engine(service)
    service.engine = engine
    yield Harness(world, engine)
    engine.loop.close()
    world.close()


def done(h, node):
    assert node["state"] == "done", f"{node['state']} {node.get('hold')}"
    assert len(node["generations"]) == 1
    return node["generations"][0]


def merged(h, launch, *commits):
    for commit in commits:
        h.sh(launch, f"git merge -q --no-ff --no-edit {commit}")


# ------------------------------------------------------------------- RP1/RP6

def test_rp1_rp6_a_merged_tester_generation_is_kept_without_a_restore_commit(h):
    tester = h.generation({TEST: "from tester\n"}, readonly=["src/**"])
    impl = h.launch(["tests/**"])
    merged(h, impl, tester)
    h.write(impl, {SRC: "impl\n"})
    checkout_tip = h.tip(impl)
    generation = done(h, h.finish(impl))
    result = h.result(impl)
    assert h.blob(generation["commit"], TEST) == "from tester"
    assert h.blob(generation["commit"], SRC) == "impl"
    assert result["commit"] == checkout_tip, "a restore commit was made for a kept path"
    assert not result.get("readonly_reverted")
    assert "readonly_reverted" not in generation
    # RP6: the evidence, on the result and copied into the generation.
    assert result["readonly_kept"] == [{"path": TEST, "source": tester}]
    assert generation["readonly_kept"] == [{"path": TEST, "source": tester}]


def test_rp1_rp6_a_merged_base_branch_first_parent_commit_is_kept(h):
    impl = h.launch(["tests/**"])
    main = h.base_commit({TEST: "from main\n"})
    merged(h, impl, main)
    h.write(impl, {SRC: "impl\n"})
    checkout_tip = h.tip(impl)
    generation = done(h, h.finish(impl))
    result = h.result(impl)
    assert h.blob(generation["commit"], TEST) == "from main"
    assert result["commit"] == checkout_tip
    assert not result.get("readonly_reverted")
    assert result["readonly_kept"] == [{"path": TEST, "source": main}]
    assert generation["readonly_kept"] == [{"path": TEST, "source": main}]


def test_rp1_rp6_an_adversary_protecting_everything_keeps_a_merged_implementer_generation(h):
    implementer = h.generation({SRC: "impl\n"}, readonly=["tests/**"])
    adversary = h.launch(["**"])
    merged(h, adversary, implementer)
    h.write(adversary, {"tests/test_adv.py": "adv\n"})
    generation = done(h, h.finish(adversary))
    result = h.result(adversary)
    assert h.blob(generation["commit"], SRC) == "impl"
    assert h.blob(generation["commit"], "tests/test_adv.py") == "adv"
    assert not result.get("readonly_reverted")
    assert result["readonly_kept"] == [{"path": SRC, "source": implementer}]
    assert generation["readonly_kept"] == [{"path": SRC, "source": implementer}]


def test_rp1_a_fast_forward_onto_a_tester_generation_is_kept(h):
    """The run only merges, and git fast-forwards: the tip is the generation."""
    tester = h.generation({TEST: "from tester\n"})
    impl = h.launch(["tests/**"])
    h.sh(impl, f"git merge -q --ff-only {tester}")
    generation = done(h, h.finish(impl))
    result = h.result(impl)
    assert h.blob(generation["commit"], TEST) == "from tester"
    assert result["commit"] == tester
    assert not result.get("readonly_reverted")
    assert result["readonly_kept"] == [{"path": TEST, "source": tester}]


def test_rp1_a_rejected_generation_of_a_since_disposed_node_is_still_trusted(h):
    tester = h.generation({TEST: "from tester\n"})
    owner = next(n for n in h.nodes().values() if any(g["commit"] == tester for g in n["generations"]))
    owner["generations"][0]["verdict"] = "rejected"
    h.update_node(owner["id"], generations=owner["generations"], disposed=now())
    impl = h.launch(["tests/**"])
    merged(h, impl, tester)
    generation = done(h, h.finish(impl))
    result = h.result(impl)
    assert h.blob(generation["commit"], TEST) == "from tester"
    assert not result.get("readonly_reverted")
    assert result["readonly_kept"] == [{"path": TEST, "source": tester}]


# ----------------------------------------------------------------------- RP2

def test_rp2_merged_then_edited_is_restored_to_the_trusted_version(h):
    tester = h.generation({TEST: "from tester\n"})
    impl = h.launch(["tests/**"])
    merged(h, impl, tester)
    h.write(impl, {TEST: "edited by impl\n", SRC: "impl\n"})
    checkout_tip = h.tip(impl)
    generation = done(h, h.finish(impl))
    result = h.result(impl)
    assert h.blob(generation["commit"], TEST) == "from tester"
    assert h.entry(generation["commit"], TEST) == h.entry(tester, TEST)
    assert h.blob(generation["commit"], SRC) == "impl"
    assert result["commit"] != checkout_tip
    assert result["readonly_reverted"] == [TEST]
    assert generation["readonly_reverted"] == [TEST]
    assert "readonly_kept" not in result and "readonly_kept" not in generation


def test_rp2_a_plain_edit_is_restored_to_the_input_and_records_nothing_kept(h):
    impl = h.launch(["tests/**"])
    h.write(impl, {TEST: "edited\n", SRC: "impl\n"})
    generation = done(h, h.finish(impl))
    result = h.result(impl)
    assert h.entry(generation["commit"], TEST) == h.entry(impl.input, TEST)
    assert h.blob(generation["commit"], SRC) == "impl"
    assert result["readonly_reverted"] == [TEST]
    assert generation["readonly_reverted"] == [TEST]
    assert "readonly_kept" not in result and "readonly_kept" not in generation


@pytest.mark.parametrize("merge_first", [False, True], ids=["plain", "after_merging_a_tester_generation"])
def test_rp2_a_deleted_protected_file_is_restored(h, merge_first):
    tester = h.generation({TEST: "from tester\n"}) if merge_first else None
    impl = h.launch(["tests/**"])
    if merge_first:
        merged(h, impl, tester)
    h.sh(impl, f"git rm -q {TEST} && echo ok > ok.txt && git add -A && git commit -q -m del")
    generation = done(h, h.finish(impl))
    expected = tester if merge_first else impl.input
    assert h.entry(generation["commit"], TEST) == h.entry(expected, TEST)
    assert h.blob(generation["commit"], "ok.txt") == "ok"
    assert h.result(impl)["readonly_reverted"] == [TEST]
    assert "readonly_kept" not in generation


@pytest.mark.parametrize("merge_first", [False, True], ids=["plain", "after_merging_a_tester_generation"])
def test_rp2_a_mode_change_on_a_protected_file_is_restored(h, merge_first):
    tester = h.generation({TEST: "from tester\n"}) if merge_first else None
    impl = h.launch(["tests/**"])
    if merge_first:
        merged(h, impl, tester)
    h.sh(impl, f"chmod +x {TEST} && git add -A && git commit -q -m chmod")
    assert h.sh(impl, f"git ls-tree HEAD -- {TEST}").startswith("100755")
    generation = done(h, h.finish(impl))
    expected = tester if merge_first else impl.input
    assert h.entry(generation["commit"], TEST) == h.entry(expected, TEST)
    assert h.entry(generation["commit"], TEST).startswith("100644")
    assert h.result(impl)["readonly_reverted"] == [TEST]


def test_rp2_two_diverging_trusted_versions_merged_then_edited_restore_the_input(h):
    first = h.generation({TEST: "tester one\n"})
    second = h.generation({TEST: "tester two\n"})
    impl = h.launch(["tests/**"])
    merged(h, impl, first)
    h.sh(impl, f"git merge -q --no-ff --no-edit -X ours {second}")
    h.write(impl, {TEST: "edited by impl\n"})
    generation = done(h, h.finish(impl))
    result = h.result(impl)
    assert h.entry(generation["commit"], TEST) == h.entry(impl.input, TEST)
    assert result["readonly_reverted"] == [TEST]
    assert "readonly_kept" not in result


def test_rp2_rp1_paths_are_decided_one_by_one(h):
    """One merged trusted path is kept while an own edit in the same result
    is restored: the decision is per path, not per result."""
    tester = h.generation({TEST: "from tester\n"})
    impl = h.launch(["tests/**"])
    merged(h, impl, tester)
    h.write(impl, {"tests/test_other.py": "tampered\n"})
    generation = done(h, h.finish(impl))
    result = h.result(impl)
    assert h.blob(generation["commit"], TEST) == "from tester"
    assert h.blob(generation["commit"], "tests/test_other.py") == "other"
    assert result["readonly_reverted"] == ["tests/test_other.py"]
    assert result["readonly_kept"] == [{"path": TEST, "source": tester}]
    assert generation["readonly_reverted"] == ["tests/test_other.py"]
    assert generation["readonly_kept"] == [{"path": TEST, "source": tester}]


# ----------------------------------------------------------------------- RP3

def restored_to_input(h, impl):
    generation = done(h, h.finish(impl))
    result = h.result(impl)
    assert h.entry(generation["commit"], TEST) == h.entry(impl.input, TEST)
    assert result["readonly_reverted"] == [TEST]
    assert "readonly_kept" not in result and "readonly_kept" not in generation


def test_rp3_a_side_commit_the_run_made_and_merged_is_restored(h):
    impl = h.launch(["tests/**"])
    branch = impl.run.branch
    h.sh(impl, f"git checkout -q -b side && echo side > {TEST} && git commit -qam side "
               f"&& git checkout -q {branch} && echo w > w.txt && git add -A && git commit -qm w "
               f"&& git merge -q --no-ff --no-edit side")
    restored_to_input(h, impl)


def test_rp3_the_pre_restore_parent_of_another_nodes_restore_commit_is_restored(h):
    other = h.launch([TEST])
    h.write(other, {TEST: "forbidden\n", "o.txt": "o\n"})
    restore = done(h, h.finish(other))["commit"]
    pre_restore = h.git("rev-parse", restore + "^")
    assert h.blob(restore, TEST) == "v0" and h.blob(pre_restore, TEST) == "forbidden"
    impl = h.launch(["tests/**"])
    merged(h, impl, pre_restore)
    restored_to_input(h, impl)


def test_rp3_a_commit_with_a_forged_integrate_message_and_trailer_is_restored(h):
    tester = h.generation({"t.txt": "t\n"})
    impl = h.launch(["tests/**"])
    h.sh(impl, f"echo forged > {TEST} && git add -A && "
               f"GIT_AUTHOR_NAME=multiagents GIT_AUTHOR_EMAIL=scheduler@multiagents.invalid "
               f"GIT_COMMITTER_NAME=multiagents GIT_COMMITTER_EMAIL=scheduler@multiagents.invalid "
               f"git commit -q -m 'multiagents: integrate {impl.id}' "
               f"-m 'Multiagents-Generation: {tester}' -m 'Multiagents-Source: {tester}'")
    restored_to_input(h, impl)


def test_rp3_an_earlier_commit_inside_another_nodes_generation_is_not_trusted(h):
    """Decision 5: only the recorded commit is trusted. The earlier commit
    carries the very entry the generation carries, but the generation itself
    is not merged."""
    other = h.launch([])
    earlier = h.write(other, {TEST: "from other\n"}, "first")
    h.write(other, {"o.txt": "o\n"}, "second")
    recorded = done(h, h.finish(other))["commit"]
    assert recorded != earlier and h.entry(recorded, TEST) == h.entry(earlier, TEST)
    impl = h.launch(["tests/**"])
    merged(h, impl, earlier)
    restored_to_input(h, impl)


def test_rp3_a_commit_from_another_nodes_live_branch_is_not_trusted(h):
    live = h.launch([])
    h.write(live, {TEST: "live\n"})
    impl = h.launch(["tests/**"])
    h.sh(impl, f"git fetch -q {live.checkout} {live.run.branch} && git merge -q --no-ff --no-edit FETCH_HEAD")
    assert h.sh(impl, f"cat {TEST}") == "live"
    restored_to_input(h, impl)


def test_rp3_writing_a_trusted_version_without_merging_its_source_is_restored(h):
    """Entry equality alone is not provenance: the source must be reachable
    from the captured tip."""
    tester = h.generation({TEST: "from tester\n"})
    impl = h.launch(["tests/**"])
    # Its own message: with the generation's tree, parent, identity and
    # second, a default message would rebuild the generation byte for byte.
    own = h.write(impl, {TEST: "from tester\n"}, "impl rewrites the test")
    assert own != tester
    assert h.sh(impl, f"git merge-base --is-ancestor {tester} HEAD || echo absent") == "absent"
    assert h.sh(impl, f"git ls-tree HEAD -- {TEST}").split("\t")[0] == h.entry(tester, TEST)
    restored_to_input(h, impl)


def test_rp3_a_base_branch_commit_off_its_first_parent_chain_is_not_trusted(h):
    impl = h.launch(["tests/**"])
    main = h.git("rev-parse", h.world.base)
    side = h.plumb(main, {TEST: "side of main\n"}, "side", [main])
    h.base_commit({TEST: "side of main\n"}, "merge side into main", parents=[main, side])
    merged(h, impl, side)
    restored_to_input(h, impl)


def test_rp3_a_trusted_source_already_in_the_input_does_not_vouch_for_reverting_it(h):
    """Sources reachable from the input are not trusted: a run that puts back
    the pre-input version of a test is restored to its input."""
    tester = h.generation({TEST: "from tester\n"})
    owner = next(n["id"] for n in h.nodes().values() if any(g["commit"] == tester for g in n["generations"]))
    impl = h.launch(["tests/**"], inputs=[owner])
    assert h.blob(impl.input, TEST) == "from tester"
    h.write(impl, {TEST: "v0\n"})
    generation = done(h, h.finish(impl))
    assert h.blob(generation["commit"], TEST) == "from tester"
    assert h.result(impl)["readonly_reverted"] == [TEST]
    assert "readonly_kept" not in generation


# ----------------------------------------------------------------------- RP4

def crash_after_retention(monkeypatch, launch):
    original = Results.move
    ref = "refs/heads/node-results/" + launch.attempt["attempt_id"]
    state = {"crashed": False}

    def move(self, target, sha, expected):
        original(self, target, sha, expected)
        if target == ref and not state["crashed"]:
            state["crashed"] = True
            raise RuntimeError("crash after capture ref")

    monkeypatch.setattr(Results, "move", move)
    return ref


def test_rp4_a_replay_keeps_its_decision_after_a_new_generation_is_recorded(h, monkeypatch):
    live = h.launch([])
    candidate = h.write(live, {TEST: "from live\n"})
    impl = h.launch(["tests/**"])
    h.sh(impl, f"git fetch -q {live.checkout} {live.run.branch} && git merge -q --no-ff --no-edit FETCH_HEAD")
    ref = crash_after_retention(monkeypatch, impl)
    with pytest.raises(RuntimeError, match="crash after capture ref"):
        h.engine.finished(impl.attempt, impl.run)
    retained = h.git("rev-parse", ref)
    # The merged commit becomes a recorded generation after the decision.
    assert done(h, h.finish(live))["commit"] == candidate
    generation = done(h, h.finish(impl))
    result = h.result(impl)
    assert result["commit"] == retained == generation["commit"]
    assert h.entry(generation["commit"], TEST) == h.entry(impl.input, TEST)
    assert result["readonly_reverted"] == [TEST]
    assert "readonly_kept" not in result and "readonly_kept" not in generation


def test_rp4_a_replay_keeps_its_decision_after_the_base_branch_moves_back(h, monkeypatch):
    impl = h.launch(["tests/**"])
    before = h.git("rev-parse", h.world.base)
    main = h.base_commit({TEST: "from main\n"})
    merged(h, impl, main)
    checkout_tip = h.tip(impl)
    ref = crash_after_retention(monkeypatch, impl)
    with pytest.raises(RuntimeError, match="crash after capture ref"):
        h.engine.finished(impl.attempt, impl.run)
    retained = h.git("rev-parse", ref)
    h.git("update-ref", "refs/heads/" + h.world.base, before, main)
    generation = done(h, h.finish(impl))
    result = h.result(impl)
    assert retained == checkout_tip, "the first capture restored a path RP1 keeps"
    assert result["commit"] == retained == generation["commit"]
    assert h.blob(generation["commit"], TEST) == "from main"
    assert not result.get("readonly_reverted")
    assert result["readonly_kept"] == [{"path": TEST, "source": main}]
    assert generation["readonly_kept"] == [{"path": TEST, "source": main}]


def test_rp4_an_unresolvable_base_branch_fails_capture_and_integrates_nothing(h):
    impl = h.launch(["tests/**"])
    h.write(impl, {TEST: "edited\n", SRC: "impl\n"})
    h.world.project["git"] = {"base_branch": "no-such-branch"}
    h.world.write_config()
    node = h.finish(impl)
    result = h.result(impl)
    assert node["generations"] == []
    assert node["state"] == "held", node["state"]
    assert result.get("capture_error")
    assert h.world.tip(impl.id) == impl.input
    assert not any(ref.startswith("refs/heads/node-generations/" + impl.id) for ref in h.world.all_refs())


# ----------------------------------------------------------------------- RP5

def test_rp5_a_protected_addition_is_never_restored_nor_recorded(h):
    impl = h.launch(["tests/**"])
    h.write(impl, {"tests/test_new.py": "new\n"})
    generation = done(h, h.finish(impl))
    result = h.result(impl)
    assert h.blob(generation["commit"], "tests/test_new.py") == "new"
    assert not result.get("readonly_reverted")
    assert "readonly_kept" not in result and "readonly_kept" not in generation


def test_rp5_an_agent_without_protected_paths_is_exempt(h):
    impl = h.launch([])
    h.write(impl, {TEST: "edited\n"})
    generation = done(h, h.finish(impl))
    result = h.result(impl)
    assert h.blob(generation["commit"], TEST) == "edited"
    assert not result.get("readonly_reverted")
    assert "readonly_kept" not in result and "readonly_kept" not in generation


# ----------------------------------------------------------------------- RP6

def test_rp6_rp7_a_base_commit_superseded_by_a_generation_cut_from_it_is_not_the_source(h):
    """Reversed by RP7 (was: the base commit is the source). The generation
    is cut from the moved base, so the base commit is its ancestor and is
    dropped by supersession before RP6 picks a source."""
    impl = h.launch(["tests/**"])
    main = h.base_commit({TEST: "shared\n"})
    tester = h.generation({"t.txt": "t\n"})
    assert h.entry(tester, TEST) == h.entry(main, TEST)
    merged(h, impl, tester)
    generation = done(h, h.finish(impl))
    result = h.result(impl)
    assert h.blob(generation["commit"], TEST) == "shared"
    assert result["readonly_kept"] == [{"path": TEST, "source": tester}]
    assert generation["readonly_kept"] == [{"path": TEST, "source": tester}]


def test_rp6_the_base_branch_commit_is_the_source_when_an_unrelated_generation_also_matches(h):
    """Neither source is an ancestor of the other and both brought the same
    entry: RP6 prefers the base commit."""
    impl = h.launch(["tests/**"])
    tester = h.generation({TEST: "shared\n"})
    main = h.base_commit({TEST: "shared\n"})
    merged(h, impl, tester, main)
    generation = done(h, h.finish(impl))
    result = h.result(impl)
    assert h.blob(generation["commit"], TEST) == "shared"
    assert result["readonly_kept"] == [{"path": TEST, "source": main}]
    assert generation["readonly_kept"] == [{"path": TEST, "source": main}]


def test_rp6_the_first_matching_generation_in_topo_order_is_the_source(h):
    first = h.generation({TEST: "from tester\n"})
    relay = h.launch([])
    merged(h, relay, first)
    h.write(relay, {"c.txt": "c\n"})
    second = done(h, h.finish(relay))["commit"]
    assert h.entry(second, TEST) == h.entry(first, TEST)
    impl = h.launch(["tests/**"])
    merged(h, impl, second)
    tip = h.tip(impl)
    order = h.sh(impl, f"git rev-list --topo-order {tip}").split()
    expected = next(c for c in order if c in {first, second})
    assert expected == second
    generation = done(h, h.finish(impl))
    result = h.result(impl)
    assert result["readonly_kept"] == [{"path": TEST, "source": expected}]
    assert generation["readonly_kept"] == [{"path": TEST, "source": expected}]
