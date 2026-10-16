"""QS — an infra-cut alias run with a dirty worktree resumes without a human.

Contract: `context/specs/qs-dirty-infra-resume.md` (QS-R1..R6).

Black box on the in-process scheduler fixtures (`nc_fixture.m4_world`, with the
fake clock of `nc_fixture.clock`) and fake providers, modelled on
`test_tb2_r6_alias_cleanup.py`. The reviewer `rv` of the `PAIR` loop carries
`session: B`. Its first activation is CUT (class `crash` by a signal or a bare
non-zero exit, class `quota` by a usage-limit message) after dirtying the alias
checkout. The retry backoff and the quota recheck run on the fake clock, so
nothing here sleeps through real time.

Observations are host git and the public node views only:
- the alias checkout is the `cwd` the fixture agent recorded for its first run;
  "the binding commit" is that run's `head`;
- the snapshot commit is found, not named: a commit anywhere in the project
  repository whose only parent is the binding commit and whose tree holds the
  work in progress. The spec fixes neither the ref's name nor its namespace;
- the next attempt's input is what its fixture run finds in its checkout.

Assumptions where the spec is silent (kept loose):
- the snapshot and the reset are visible as soon as the cut attempt is
  captured, i.e. by the time the node shows its pending retry or its quota
  hold (bounded by WAIT; the spec says the scheduler does this when an attempt
  ends, not at the next admission);
- the snapshot carries tracked, staged and untracked content and NOT ignored
  files (an ignored-only worktree is not dirty);
- a missing or corrupt snapshot holds the node with reason
  `wip_snapshot_unavailable` (pinned by the orchestrator), whose detail names
  the snapshot ref and says `missing` or `corrupt`; `recovery_conflict` does
  not satisfy it;
- the retry delay is `scheduler.retries.backoff_seconds` and the quota hold is
  released by advancing the clock past `quota_recheck_seconds`.

QS-R4's crash window is built through durable state, with no seam: a real cut
produces the snapshot, its recorded recovery and the reset; the test then puts
the same dirt back on the alias checkout (the state a crash between snapshot and
reset leaves) and restarts the scheduler. No internal name is used.

NOT TESTED (cannot be forced from outside): the order "ref persisted as the node's
recovery source before the worktree is touched" beyond the sampled proxy in
`test_qs_r1_...snapshot_exists_before_the_worktree_is_observed_clean`.
"""
from __future__ import annotations

import json
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nc_fixture.clock import ClockWorld  # noqa: E402
from nc_fixture.m4_world import M4World, commit_entry, finding, verdict_entry  # noqa: E402
from test_tf_support import failure_patterns, failures, pending_retry  # noqa: E402

PAIR = """\
template: qs
version: 1
params:
  rounds: {type: int, default: 3}
root:
  key: top
  kind: loop
  verdict_child: rev
  max_rounds: {param: rounds}
  children:
    - {key: wrk, kind: simple, agent: wk, task: "implement the thing"}
    - {key: rev, kind: simple, agent: rv, task: "review the thing", session: B}
"""

WAIT = 6
WAIT_ROUNDS = 15
BACKOFF = 60
QUOTA_RECHECK = 1800
GIT_BOUND = 20

GITIGNORE = ".venv/\n__pycache__/\n"
UNTRACKED = "WIP-UNTRACKED-MARKER\n"
TRACKED = "# WIP-TRACKED-MARKER\n"
FORBIDDEN = "FORBIDDEN-TEST-EDIT\n"
R1 = {".gitignore": GITIGNORE, "src/m.py": "v1\n", "tests/t.py": "original\n"}

CRASH = {"signal": signal.SIGUSR1}
EXIT = {"exit": 1}
QUOTA = {"stderr": "Error: weekly usage limit exhausted\n", "exit": 1}
INFRA = [pytest.param(CRASH, id="crash-signal"), pytest.param(EXIT, id="crash-exit"),
         pytest.param(QUOTA, id="quota")]


class QSWorld(ClockWorld, M4World):
    """The fake clock composed with the queue provider world."""


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = QSWorld(tmp_path, monkeypatch, now=datetime.now(timezone.utc), tick_seconds=0.05)
    world.fxw = world.provider("fxw")
    world.fxr = world.provider("fxr")
    failures(world.fxr)
    failure_patterns(world.fxr)
    world.agent("wk", "fxw", writes=True)
    world.agent("rv", "fxr", writes=True)
    world.project["limits"]["provider_failure_threshold"] = 100
    world.project["scheduler"]["retries"] = {"backoff_seconds": [BACKOFF]}
    yield world
    world.close()


# ---------------------------------------------------------------- helpers

def start(w, rounds=3, worker=None):
    w.fxw.queue(*(worker or [{"write": R1, "commit": "r1"}]))
    w.start_scheduler()
    w.register_ok(PAIR)
    top = w.instantiate_ok("qs", {"rounds": rounds})
    wk, rv = w.children(top)
    return top, wk, rv


def cut(kind, write=None, **more):
    """A reviewer activation that dirties the checkout and is then cut."""
    return {**({"write": write} if write is not None else {}), **kind, **more}


def dirt(**extra):
    return {"wip.txt": UNTRACKED, ".gitignore": GITIGNORE + TRACKED, **extra}


def sh(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                          timeout=GIT_BOUND)


def first_call(w):
    return w.until(lambda: (w.fxr.calls() or [None])[0], WAIT_ROUNDS, what="first reviewer run")


def cwd_of(call):
    return Path(call["cwd"])


def status(cwd):
    return sh(cwd, "status", "--porcelain", "--untracked-files=all").stdout


def is_reset(cwd, head0):
    return sh(cwd, "rev-parse", "HEAD").stdout.strip() == head0 and status(cwd) == ""


def snapshots(w, head0, path="wip.txt"):
    """Commits whose only parent is the binding commit and whose tree holds `path`."""
    out = []
    for line in w.git("rev-list", "--all", "--parents").stdout.splitlines():
        sha, *parents = line.split()
        if parents == [head0] and w.git("ls-tree", sha, "--", path).stdout.strip():
            out.append(sha)
    return out


def blob(w, sha, path):
    r = w.git("show", f"{sha}:{path}")
    return r.stdout if r.returncode == 0 else None


def refs_at(w, sha):
    return w.git("for-each-ref", "--points-at", sha, "--format=%(refname)").stdout.split()


def wait_cut_settled(w, head0, path="wip.txt"):
    """The cut attempt is over, the checkout is reset and a snapshot exists."""
    cwd = cwd_of(first_call(w))
    def probe():
        if is_reset(cwd, head0):
            found = snapshots(w, head0, path)
            return found or None
        return None
    found = w.until(probe, WAIT, what="the checkout reset to the binding commit with a snapshot")
    return found[0]


def release_retry(w):
    w.advance(seconds=BACKOFF)


def second_call(w, timeout=WAIT):
    return w.until(lambda: len(w.fxr.calls()) >= 2 and w.fxr.calls()[1] or None, timeout,
                   what="the second reviewer run")


def hold_text(node):
    return json.dumps(node.get("hold") or {}, default=str)


# ============================================================ QS-R1 / QS-R2

@pytest.mark.parametrize("kind", INFRA)
def test_qs_r1_r2_an_infra_cut_with_a_dirty_alias_is_snapshotted_reset_and_resumed(w, kind):
    w.fxr.queue(cut(kind, dirt()), verdict_entry("approved"))
    top, wk, rv = start(w)
    first = first_call(w)
    head0, cwd = first["head"], cwd_of(first)
    snap = wait_cut_settled(w, head0)
    # QS-R1: the snapshot is a commit on the binding commit holding all the dirt
    assert blob(w, snap, "wip.txt") == UNTRACKED
    assert blob(w, snap, ".gitignore") == GITIGNORE + TRACKED
    assert blob(w, snap, "src/m.py") == "v1\n"
    assert refs_at(w, snap), "the snapshot is not on any ref"
    # QS-R2: not held dirty, and no cleanup run was spent on it
    node = w.get(rv)
    assert (node.get("hold") or {}).get("reason") != "dirty_worktree", node
    assert not [e for e in w.events() if w.kind_of(e) == "node.cleanup_requested"]
    if kind is QUOTA:
        w.wait_held(rv, "quota_exhausted", timeout=WAIT)
        w.advance(seconds=QUOTA_RECHECK)
    else:
        pending_retry(w, rv)
        release_retry(w)
    second = second_call(w)
    files = second["files"]
    assert "wip.txt" in files
    assert (cwd_of(second) / "wip.txt").read_text() == UNTRACKED
    assert (cwd_of(second) / ".gitignore").read_text() == GITIGNORE + TRACKED
    assert (cwd_of(second) / "src" / "m.py").read_text() == "v1\n"
    done = w.wait_state(top, "done", timeout=WAIT_ROUNDS)
    assert done["outcome"] == "approved"
    assert w.fxr.spawns() == 2, "the node was launched more than once for the cut"
    assert cwd_of(second) == cwd, "the alias path moved"


def test_qs_r1_the_snapshot_exists_before_the_worktree_is_observed_clean(w):
    w.fxr.queue(cut(CRASH, dirt()), verdict_entry("approved"))
    top, wk, rv = start(w)
    first = first_call(w)
    head0, cwd = first["head"], cwd_of(first)
    deadline = time.monotonic() + WAIT
    dirty_seen = False
    while time.monotonic() < deadline:
        state = status(cwd)
        if state:
            dirty_seen = True
        elif dirty_seen or (w.fxr.dir / "done.jsonl").exists():
            # read the refs AFTER the clean reading: a correct scheduler had
            # the snapshot before it reset, so it is still there now
            if not snapshots(w, head0):
                raise AssertionError("the worktree was reset before any snapshot existed (WIP lost)")
            return
        time.sleep(0.005)
    raise AssertionError("the dirty alias was never reset after the cut")


def test_qs_r1_the_tar_archive_is_still_written(w):
    w.fxr.queue(cut(CRASH, dirt()), verdict_entry("approved"))
    start(w)
    head0 = first_call(w)["head"]
    wait_cut_settled(w, head0)
    texts = []
    for p in w.scheduler_files():
        try:
            texts.append(p.read_bytes().decode("utf-8", "replace"))
        except OSError:
            pass
    assert any(UNTRACKED.strip() in t for t in texts), "the untracked content is not archived"
    assert any(TRACKED.strip() in t for t in texts), "the tracked diff is not archived"


def test_qs_r1_staged_changes_deletions_and_new_staged_files_are_carried(w):
    w.fxr.queue(cut(CRASH, {"wip.txt": UNTRACKED}, gate="g"), verdict_entry("approved"))
    top, wk, rv = start(w, worker=[{"write": {**R1, "gone.txt": "to delete\n"}, "commit": "r1"}])
    first = first_call(w)
    head0, cwd = first["head"], cwd_of(first)
    sh(cwd, "rm", "-q", "gone.txt")                                  # staged deletion
    (cwd / "staged.txt").write_text("staged v1\n")
    assert sh(cwd, "add", "staged.txt").returncode == 0               # staged addition
    (cwd / "staged.txt").write_text("staged v2\n")                    # + unstaged edit on top
    w.gate("g", w.fxr)
    snap = wait_cut_settled(w, head0)
    assert blob(w, snap, "staged.txt") == "staged v2\n"
    assert blob(w, snap, "gone.txt") is None, "a staged deletion was lost"
    assert blob(w, snap, "wip.txt") == UNTRACKED
    pending_retry(w, rv)
    release_retry(w)
    second = second_call(w)
    assert "gone.txt" not in second["files"] and "staged.txt" in second["files"]


def test_qs_r1_ignored_only_dirt_makes_no_snapshot_and_resumes_on_the_plain_input(w):
    w.fxr.queue(cut(CRASH, {".venv/pyvenv.cfg": "x\n", "__pycache__/m.pyc": "y\n"}),
                verdict_entry("approved"))
    top, wk, rv = start(w)
    first = first_call(w)
    head0 = first["head"]
    pending_retry(w, rv)
    w.until(lambda: is_reset(cwd_of(first), head0) or not (cwd_of(first) / ".venv").exists(),
            WAIT, what="the ignored leftovers to be cleared")
    assert snapshots(w, head0, ".gitignore") == [] and snapshots(w, head0, "src/m.py") == []
    release_retry(w)
    second = second_call(w)
    assert second["head"] == head0
    assert ".venv" not in second["files"] and "__pycache__" not in second["files"]
    assert not [e for e in w.events() if w.kind_of(e) == "node.cleanup_requested"]


def test_qs_r1_a_clean_worktree_after_an_infra_cut_makes_no_snapshot(w):
    w.fxr.queue(cut(CRASH), verdict_entry("approved"))
    top, wk, rv = start(w)
    first = first_call(w)
    pending_retry(w, rv)
    w.quiet(1)
    assert not snapshots(w, first["head"], ".gitignore")
    release_retry(w)
    second = second_call(w)
    assert second["head"] == first["head"] and second["files"] == first["files"]
    assert w.wait_state(top, "done", timeout=WAIT_ROUNDS)["outcome"] == "approved"


# ================================================================= QS-R2

def test_qs_r2_the_work_in_progress_is_input_to_one_attempt_only(w):
    """Consumed after the confirmed launch: the next round does not carry it."""
    w.fxr.queue(cut(CRASH, dirt()),
                verdict_entry("rejected", [finding("redo")]),
                verdict_entry("approved"))
    top, wk, rv = start(w, worker=[{"write": R1, "commit": "r1"}, commit_entry("b.txt", "two\n", "r2")])
    head0 = first_call(w)["head"]
    wait_cut_settled(w, head0)
    pending_retry(w, rv)
    release_retry(w)
    second_call(w)
    assert w.wait_state(top, "done", timeout=WAIT_ROUNDS)["outcome"] == "approved"
    calls = w.fxr.calls()
    assert len(calls) == 3
    assert "wip.txt" in calls[1]["files"]
    assert "b.txt" in calls[2]["files"]
    assert "wip.txt" not in calls[2]["files"], "the recovery was carried into the next round"
    assert (cwd_of(calls[2]) / ".gitignore").read_text() == GITIGNORE


def test_qs_r2_a_failed_launch_does_not_consume_the_recovery(w):
    w.fxr.queue(cut(CRASH, dirt()), verdict_entry("approved"))
    top, wk, rv = start(w)
    first = first_call(w)
    wait_cut_settled(w, first["head"])
    pending_retry(w, rv)
    script = w.fxr.dir / "agent.py"
    original = script.read_text()
    # an interpreter that vanished: exec fails before any provider process
    script.write_text("#!/qs-missing-interpreter\n" + original.split("\n", 1)[1])
    try:
        release_retry(w)
        w.quiet(1)
        assert w.fxr.spawns() == 1, "the broken launch started a provider"
    finally:
        script.write_text(original)
    for _ in range(3):
        if w.fxr.spawns() >= 2 or w.get(rv)["state"] == "held":
            break
        release_retry(w)
        w.quiet(0.3)
    if w.get(rv)["state"] == "held":
        # budget exhausted by the broken launch: a manual relaunch must still recover
        reply = w.root_op("relaunch_node", rv)
        assert reply.get("ok"), reply
    second = second_call(w)
    assert "wip.txt" in second["files"], "the recovery was lost with the failed launch"
    assert (cwd_of(second) / "wip.txt").read_text() == UNTRACKED


# ================================================================= QS-R3

def test_qs_r3_read_only_paths_are_restored_in_the_snapshot_not_carried(w):
    w.agents["rv"]["readonly_paths"] = ["tests/**"]
    w.fxr.queue(cut(CRASH, dirt(**{"tests/t.py": FORBIDDEN, "tests/new.py": FORBIDDEN})),
                verdict_entry("approved"))
    top, wk, rv = start(w)
    first = first_call(w)
    head0 = first["head"]
    snap = wait_cut_settled(w, head0)
    assert blob(w, snap, "tests/t.py") == "original\n", "a forbidden edit became the baseline"
    assert blob(w, snap, "tests/new.py") is None, "a new file under a read-only path was carried"
    assert blob(w, snap, "wip.txt") == UNTRACKED, "the allowed work was not carried"
    assert blob(w, snap, ".gitignore") == GITIGNORE + TRACKED
    # the raw edits survive in the archive
    texts = []
    for p in w.scheduler_files():
        try:
            texts.append(p.read_bytes().decode("utf-8", "replace"))
        except OSError:
            pass
    assert any(FORBIDDEN.strip() in t for t in texts), "the raw forbidden edit is not archived"
    pending_retry(w, rv)
    release_retry(w)
    second = second_call(w)
    cwd = cwd_of(second)
    assert (cwd / "tests" / "t.py").read_text() == "original\n"
    assert not (cwd / "tests" / "new.py").exists()
    assert (cwd / "wip.txt").read_text() == UNTRACKED


def test_qs_r3_a_forbidden_edit_alone_leaves_the_retry_on_the_binding_content(w):
    w.agents["rv"]["readonly_paths"] = ["tests/**"]
    w.fxr.queue(cut(CRASH, {"tests/t.py": FORBIDDEN}), verdict_entry("approved"))
    top, wk, rv = start(w)
    first = first_call(w)
    head0 = first["head"]
    pending_retry(w, rv)
    w.until(lambda: is_reset(cwd_of(first), head0), WAIT, what="the checkout reset")
    # only a forbidden edit: nothing allowed to carry, the retry sees the original
    release_retry(w)
    second = second_call(w)
    assert (cwd_of(second) / "tests" / "t.py").read_text() == "original\n"
    assert FORBIDDEN.strip() not in "".join(
        (p.read_text() for p in cwd_of(second).rglob("*") if p.is_file() and ".git" not in p.parts))


# ================================================================= QS-R4

def test_qs_r4_a_restart_after_the_cut_makes_no_second_snapshot_and_loses_nothing(w):
    w.fxr.queue(cut(CRASH, dirt()), verdict_entry("approved"))
    top, wk, rv = start(w)
    first = first_call(w)
    head0 = first["head"]
    snap = wait_cut_settled(w, head0)
    pending_retry(w, rv)
    w.restart_scheduler()
    w.until(lambda: w.status().get("last_tick"), WAIT, what="first tick")
    # last_tick is taken from the fixed fake clock, so move the clock (far inside
    # the backoff) twice and wait until each new instant has been evaluated.
    for _ in range(2):
        w.advance(seconds=1)
        w.until(lambda: w._names(w._last_tick() or "", w.now), WAIT, what="a tick at the new instant")
    assert snapshots(w, head0) == [snap], "a restart made another snapshot"
    assert is_reset(cwd_of(first), head0)
    assert w.fxr.spawns() == 1
    release_retry(w)
    second = second_call(w)
    assert (cwd_of(second) / "wip.txt").read_text() == UNTRACKED
    assert (cwd_of(second) / ".gitignore").read_text() == GITIGNORE + TRACKED
    assert w.wait_state(top, "done", timeout=WAIT_ROUNDS)["outcome"] == "approved"
    assert snapshots(w, head0) == [snap]


def test_qs_r4_a_crash_between_snapshot_and_reset_resets_without_a_second_snapshot(w):
    w.fxr.queue(cut(CRASH, dirt()), verdict_entry("approved"))
    top, wk, rv = start(w)
    first = first_call(w)
    head0 = first["head"]
    snap = wait_cut_settled(w, head0)
    pending_retry(w, rv)
    cwd = cwd_of(first)
    refs_before = sorted(w.git("for-each-ref", "--format=%(refname) %(objectname)").stdout.splitlines())
    # The state a crash leaves: snapshot ref + recorded recovery exist, the alias is dirty again.
    w.stop_scheduler()
    (cwd / "wip.txt").write_text(UNTRACKED)
    (cwd / ".gitignore").write_text(GITIGNORE + TRACKED)
    assert status(cwd) != "" and sh(cwd, "rev-parse", "HEAD").stdout.strip() == head0
    w.start_scheduler()
    w.until(lambda: is_reset(cwd, head0), WAIT, what="the restarted scheduler to reset the alias")
    w.quiet(1)
    assert snapshots(w, head0) == [snap], "a second snapshot was made"
    refs_after = sorted(w.git("for-each-ref", "--format=%(refname) %(objectname)").stdout.splitlines())
    assert refs_after == refs_before, "the restart changed or added refs"
    assert w.fxr.spawns() == 1
    release_retry(w)
    second = second_call(w)
    assert (cwd_of(second) / "wip.txt").read_text() == UNTRACKED
    assert (cwd_of(second) / ".gitignore").read_text() == GITIGNORE + TRACKED
    assert w.wait_state(top, "done", timeout=WAIT_ROUNDS)["outcome"] == "approved"


def test_qs_r4_a_scheduler_killed_right_after_the_cut_ends_clean_with_one_snapshot(w):
    w.fxr.queue(cut(CRASH, dirt()), verdict_entry("approved"))
    top, wk, rv = start(w)
    first = first_call(w)
    head0 = first["head"]
    w.until(lambda: (w.fxr.dir / "done.jsonl").exists(), WAIT, what="the cut run to end")
    w.kill9()
    w.start_scheduler()
    cwd = cwd_of(first)
    w.until(lambda: is_reset(cwd, head0) and snapshots(w, head0), WAIT * 2,
            what="the restarted scheduler to reset the checkout with a snapshot")
    w.quiet(1)
    assert len(snapshots(w, head0)) == 1, "the interrupted cut produced several snapshots"
    for _ in range(4):
        if w.fxr.spawns() >= 2:
            break
        release_retry(w)
        w.quiet(0.3)
    second = second_call(w)
    assert (cwd_of(second) / "wip.txt").read_text() == UNTRACKED, "the work in progress was lost"
    assert len(snapshots(w, head0)) == 1


# ================================================================= QS-R5

def tamper(w, snap, how):
    refs = refs_at(w, snap)
    assert refs, "the snapshot is not on any ref"
    tamper_refs(w, snap, how, refs)
    return refs


def tamper_refs(w, snap, how, refs):
    if how == "missing":
        for ref in refs:
            assert w.git("update-ref", "-d", ref).returncode == 0
    elif how == "unrelated":
        tree = w.git("rev-parse", snap + "^{tree}").stdout.strip()
        orphan = w.git("-c", "user.name=t", "-c", "user.email=t@example.invalid",
                       "commit-tree", tree, "-m", "unrelated").stdout.strip()
        assert orphan
        for ref in refs:
            assert w.git("update-ref", ref, orphan).returncode == 0
    else:
        stray = subprocess.run(["git", "-C", str(w.root), "hash-object", "-w", "--stdin"], input="x",
                               capture_output=True, text=True, timeout=GIT_BOUND).stdout.strip()
        for ref in refs:
            assert w.git("update-ref", ref, stray).returncode == 0


@pytest.mark.parametrize("how", ["missing", "unrelated", "blob"])
def test_qs_r5_a_missing_or_corrupt_snapshot_holds_and_never_launches_on_a_partial_tree(w, how):
    w.fxr.queue(cut(CRASH, dirt()), verdict_entry("approved"))
    top, wk, rv = start(w)
    head0 = first_call(w)["head"]
    snap = wait_cut_settled(w, head0)
    pending_retry(w, rv)
    refs = tamper(w, snap, how)
    release_retry(w)
    def probe():
        node = w.get(rv)
        return node if node["state"] == "held" else None
    held = w.until(probe, WAIT, what="the node to be held for the bad snapshot")
    assert (held["hold"] or {}).get("reason") == "wip_snapshot_unavailable", held["hold"]
    text = hold_text(held)
    assert any(ref in text for ref in refs), f"the hold detail does not name the snapshot ref {refs}: {text}"
    assert any(word in text.lower() for word in ("missing", "corrupt")), text
    w.quiet(1)
    assert w.fxr.spawns() == 1, "the node launched on a partial tree"
    assert w.get(rv)["state"] == "held"


# ================================================================= QS-R6

def test_qs_r6_the_snapshot_is_not_published_integrated_or_a_verdict(w):
    w.fxr.queue(cut(CRASH, dirt()), verdict_entry("approved"))
    top, wk, rv = start(w)
    first = first_call(w)
    head0 = first["head"]
    generations = w.until(lambda: len(w.get(top)["generations"]) or None, WAIT,
                          what="the worker generation")
    base_tip = w.git("rev-parse", "HEAD").stdout.strip()
    snap = wait_cut_settled(w, head0)
    pending_retry(w, rv)
    assert len(w.get(top)["generations"]) == generations, "the snapshot recorded a generation"
    assert w.get(rv)["generations"] == [] and w.get(rv)["outcome"] is None
    assert w.fxr.verdicts() == []
    assert w.git("rev-parse", "HEAD").stdout.strip() == base_tip
    heads = w.git("for-each-ref", "--format=%(refname)", "refs/heads/nodes").stdout.split()
    for ref in heads + ["HEAD"]:
        assert w.git("merge-base", "--is-ancestor", snap, ref).returncode != 0, \
            f"the snapshot is reachable from {ref}"
    assert not [t for t in w.transitions(rv) if t in ("done", "published", "integrated")]


def test_qs_r6_a_dirty_worktree_after_a_done_end_keeps_the_cleanup_run(w):
    w.fxr.queue(verdict_entry("rejected", [finding("redo")], leave=dirt(), v={}), {"delete": ["wip.txt"],
                "write": {".gitignore": GITIGNORE}, "gate": "c1"}, verdict_entry("approved"))
    top, wk, rv = start(w, worker=[{"write": R1, "commit": "r1"}, commit_entry("b.txt", "two\n", "r2")])
    first = first_call(w)
    head0 = first["head"]
    cleanup = w.until(lambda: len(w.fxr.calls()) >= 2 and w.fxr.calls()[1] or None, WAIT_ROUNDS,
                      what="the cleanup run")
    assert cleanup["resume"] == first["session"] and "wip.txt" in cleanup["files"]
    assert snapshots(w, head0) == [], "a done end was snapshotted"
    assert (cwd_of(first) / "wip.txt").exists(), "a done end's leftovers were reset"
    w.gate("c1", w.fxr)
    assert w.wait_state(top, "done", timeout=WAIT_ROUNDS)["outcome"] == "approved"
    assert snapshots(w, head0) == []


def test_qs_r6_a_dirty_worktree_after_a_done_end_still_holds_when_the_cleanup_fails(w):
    w.fxr.queue(verdict_entry("rejected", [finding("redo")], leave=dirt()), {}, verdict_entry("approved"))
    top, wk, rv = start(w, worker=[{"write": R1, "commit": "r1"}, commit_entry("b.txt", "two\n", "r2")])
    first = first_call(w)
    w.wait_held(rv, "dirty_worktree", timeout=WAIT_ROUNDS)
    assert snapshots(w, first["head"]) == []
    assert (cwd_of(first) / "wip.txt").read_text() == UNTRACKED, "the leftover was reset"
    w.quiet(1)
    assert w.fxr.spawns() == 2


def test_qs_r6_a_question_end_keeps_its_dirty_worktree_untouched(w):
    w.fxr.queue(cut({"text": "NEED_INFO(qs): which one?"}, dirt()), verdict_entry("approved"))
    top, wk, rv = start(w)
    first = first_call(w)
    w.wait_held(rv, "needs_info", timeout=WAIT_ROUNDS)
    w.quiet(1)
    assert snapshots(w, first["head"]) == [], "a question end was snapshotted"
    assert (cwd_of(first) / "wip.txt").read_text() == UNTRACKED
    assert status(cwd_of(first)) != ""
