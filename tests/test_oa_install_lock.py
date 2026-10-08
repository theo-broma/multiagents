"""OA-R5: the install is atomic as a whole and serialised per target profile.

Seam (spec, Decisions): when MULTIAGENTS_OA_INSTALL_PAUSE names a path, the
driver, after taking the install lock and before making the install visible,
waits until that path exists. The tests hold a driver there and look.
"""
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from support.oa_world import World  # noqa: E402

HOURS = 3600
ALICE, BOB = "alice@example.invalid", "bob@example.invalid"
SID = "11111111-2222-3333-4444-555555555555"
SID2 = "99999999-2222-3333-4444-555555555555"
LIMIT = {"do": "limit", "exhaust": {"used": 100, "reset_in": 3 * HOURS}}
SEE = 4.0           # how long a held driver is watched
BOUND = 25.0        # a released driver must finish within this


def project(tmp_path, name, home, sid):
    w = World(tmp_path, name=name, home=home)
    w.add_provider("claude-b")
    w.reserve(0.25)
    w.root_config(switch_to=["claude-b"])
    d = w.proj / ".multiagents" / "launch"
    d.mkdir(exist_ok=True)
    (d / "orchestrator.session").write_text(sid)
    w.plan({"launches": [LIMIT], "default": {"do": "ok"}})
    return w


def start(w, extra_env):
    env = {**w.env(), **extra_env}
    return subprocess.Popen(
        [sys.executable, "-c",
         "import sys; from multiagents.cli import main; sys.exit(main(sys.argv[1:]))",
         "--path", str(w.proj), "run", "--unattended", "3"],
        env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, cwd=str(w.proj))


def until(cond, bound):
    end = time.monotonic() + bound
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


def held_for(proc, seconds, check):
    """True when `check()` holds and the process stays alive for `seconds`."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if proc.poll() is not None or not check():
            return False
        time.sleep(0.05)
    return True


@pytest.fixture
def procs():
    started = []
    yield started
    for p in started:
        if p.poll() is None:
            p.kill()
        p.communicate()


def test_r5_nothing_partial_is_visible_while_the_install_is_held(tmp_path, procs):
    w = project(tmp_path, "proj", tmp_path / "home", SID)
    w.make_profile("claude", identity=ALICE, used=40)
    w.make_profile("claude-b", identity=BOB, used=10)
    gate = tmp_path / "gate"
    p = start(w, {"MULTIAGENTS_OA_INSTALL_PAUSE": str(gate)})
    procs.append(p)
    assert until(lambda: len(w.launches()) >= 1, BOUND), "the root's first launch"
    target = w.projects_dir("claude-b")

    def nothing_visible():
        return len(w.launches()) == 1 and not any(
            SID in e.name for e in (target.iterdir() if target.is_dir() else []))

    assert held_for(p, SEE, nothing_visible), \
        "the driver should be waiting at the seam with nothing of the session in the target"
    gate.write_text("")
    out, err = p.communicate(timeout=BOUND)
    assert w.cfgs()[:2] == ["claude", "claude-b"]
    second = w.launches()[1]
    assert second["mode"] == "resume" and second["existed"]
    assert second["text_at_launch"] == (w.projects_dir("claude") / f"{SID}.jsonl").read_text(), \
        "complete transcript once released"


def test_r5_a_second_install_into_the_same_profile_waits_for_the_first(tmp_path, procs):
    home = tmp_path / "home"
    a = project(tmp_path, "proja", home, SID)
    b = project(tmp_path, "projb", home, SID2)
    # B's root runs on its own host profile, so A's limit does not exhaust it
    b.add_provider("claude-c")
    b.root_config(provider="claude-c", switch_to=["claude-b"])
    b.make_profile("claude-c", identity="cara@example.invalid", used=40)
    a.make_profile("claude", identity=ALICE, used=40)
    a.make_profile("claude-b", identity=BOB, used=10)
    gate = tmp_path / "gate"
    pa = start(a, {"MULTIAGENTS_OA_INSTALL_PAUSE": str(gate)})
    procs.append(pa)
    assert until(lambda: len(a.launches()) >= 1, BOUND)
    pb = start(b, {})
    procs.append(pb)
    assert until(lambda: len(b.launches()) >= 1, BOUND), "B's root launched"
    target_b = b.projects_dir("claude-b")

    def b_is_held():
        return len(b.launches()) == 1 and len(a.launches()) == 1 and not (
            target_b.is_dir() and any(SID2 in e.name for e in target_b.iterdir()))

    assert held_for(pb, SEE, b_is_held), \
        "B must wait while A holds the install lock for the same profile"
    gate.write_text("")
    pa.communicate(timeout=BOUND)
    pb.communicate(timeout=BOUND)
    assert a.cfgs()[:2] == ["claude", "claude-b"]
    assert b.cfgs()[:2] == ["claude-c", "claude-b"]
    assert b.launches()[1]["sid"] == SID2 and b.launches()[1]["existed"]
    assert a.launches()[1]["sid"] == SID and a.launches()[1]["existed"]
