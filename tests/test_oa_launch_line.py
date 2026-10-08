"""OA-R3: every root launch prints one line: provider, account identity, headroom.

The identity is read by the `identity` action of the provider script from the
fake profile's .claude.json. Identities are fictional (*.example.invalid).
"""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from support.oa_world import World  # noqa: E402

ALICE = "alice@example.invalid"
BOB = "bob@example.invalid"


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    w.add_provider("claude-b")
    w.make_profile("claude", identity=ALICE, used=40)
    w.make_profile("claude-b", identity=BOB, used=25)
    return w


def line_for(text, *needles):
    return [l for l in text.splitlines() if all(n in l for n in needles)]


def test_r3_line_names_provider_identity_and_headroom(world):
    r = world.run(2)
    lines = line_for(r.err, "claude", ALICE)
    assert lines, f"no launch line on stderr naming provider and identity:\n{r.err}"
    assert re.search(r"60\s?%|\b0\.6\b", lines[0]), f"headroom (60%) missing: {lines[0]}"
    assert len(lines) <= len(world.launches()), "one line per launch, not more"


def test_r3_the_line_goes_to_stderr_when_stdout_is_not_a_terminal(world):
    r = world.run(2)
    assert ALICE in r.err
    assert ALICE not in r.out


def test_r3_line_for_a_second_profile_names_that_profile(world):
    world.root_config(provider="claude-b")
    r = world.run(2)
    lines = line_for(r.err, "claude-b", BOB)
    assert lines, r.err
    assert ALICE not in r.err
    assert re.search(r"75\s?%|\b0\.75\b", lines[0])


def test_r3_failing_identity_says_unknown_and_the_launch_goes_on(world):
    world.make_profile("claude", identity=None, used=40)   # no oauthAccount: identity exits 64
    r = world.run(2)
    assert "identity unknown" in r.err, r.err
    assert world.launches(), "the launch must go on"


def test_r3_a_failing_identity_line_still_names_provider_and_headroom(world):
    world.make_profile("claude", identity=None, used=40)
    r = world.run(2)
    lines = line_for(r.err, "identity unknown")
    assert lines and "claude" in lines[0]
    assert re.search(r"60\s?%|\b0\.6\b", lines[0])


def test_r3_initializer_launch_prints_the_line_too(world):
    r = world.cli("init-agent")
    assert ALICE in r.err, r.err


def test_r3_identity_is_never_written_to_a_file(world, tmp_path):
    def stamp():
        out = {}
        for root in (world.home, world.proj, world.base):
            for p in root.rglob("*"):
                if p.is_file() and not p.is_symlink() and ".git/" not in str(p) \
                        and "/.git" not in str(p.parent) and world.fake not in p.parents:
                    st = p.stat()
                    out[p] = (st.st_mtime_ns, st.st_size)
        return out

    before = stamp()
    world.run(2)
    world.root_config(provider="claude-b")
    world.run(2)
    leaked = []
    for p, s in stamp().items():
        if before.get(p) == s:
            continue
        data = p.read_bytes()
        if ALICE.encode() in data or BOB.encode() in data or b"example.invalid" in data:
            leaked.append(str(p))
    assert leaked == [], f"identity written to: {leaked}"
