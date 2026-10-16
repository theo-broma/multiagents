"""Adversary, slice 4A: session continuity and conversation storage.

Each test here failed on the branch it was written against (nd-905bbdf0); it
states the contract clause it holds the implementation to.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / 'support'))
import ic_s3 as s3  # noqa: E402
import ic_s4a as a  # noqa: E402

# The guest runs `ins["code"]` before the ordinary session probe: this is how
# an attempt writes whatever it likes under its own HOME.
GUEST = r'''
elif probe == "adv_guest":
    exec(ins["code"])
    say(dict(common(), done=True))
elif probe == "plant":
'''


class AdvProvider(a.Provider):
    def __init__(self, tmp, name, *, mode='none', rpc=False):
        super().__init__(tmp, name, mode=mode, rpc=rpc)
        script = Path(self.entry['bin'])
        source = script.read_text()
        marker = 'elif probe == "plant":\n'
        assert marker in source
        script.write_text(source.replace(marker, GUEST.lstrip('\n'), 1))


class World(a.World):
    def configure(self, mode='none', rpc=False):
        provider = AdvProvider(self.tmp, 'acme', mode=mode, rpc=rpc)
        self.p.fakes['acme'] = provider
        self.p.providers['acme'] = provider.entry
        self.fake = provider
        self.p.agents['worker'].update(can_spawn=True, max_children=8)
        self.p.write()
        self._reset_server()


@pytest.fixture
def world(tmp_path, monkeypatch):
    s = World(tmp_path, monkeypatch, backend='reference')
    try:
        yield s
    finally:
        s.close()


# --- what the guest can put in the export ------------------------------------

DEEP = r'''
d = os.path.join(HOME, "sessions")
os.makedirs(d, exist_ok=True)
json.dump({"session": SES, "nonce": "payload"}, open(os.path.join(d, "payload.json"), "w"))
p = d
for i in range(1200):
    p = os.path.join(p, "d")
    os.mkdir(p)
'''


def test_guest_deep_tree_in_session_payload_is_settled_not_misread_as_a_crash(world):
    """IC-C22 / D-S4A.4 Ordering: "An `export` copy completes, and is recorded,
    before that attempt's resources may be removed." The guest writes a
    payload and a 1200-deep directory chain under the declared path, then
    exits 0. The host export recurses once per level and raises
    RecursionError: no `session_export` is recorded, the clean exit is
    recorded as a `crash` (exit -1, signal 1), and the TF retry then fails
    with "missing conversation payload"."""
    world.configure('export'); world.start()
    node = world.launch('adv_guest', code=DEEP)
    world.report(node)
    s3.wait_done(world, node, a.WAIT)
    first_run = world.runs(node)[0]
    assert first_run.get('session_export'), first_run
    assert (first_run.get('cause') or {}).get('class') != 'crash', first_run
    assert len(world.runs(node)) == 1, world.runs(node)


# The export runs inside the scheduler process, over a file the guest wrote.
SPARSE = r'''
import os, resource, sys, types, uuid
from pathlib import Path
root = Path(sys.argv[1]); os.environ["MULTIAGENTS_STATE_DIR"] = str(root / "state")
from multiagents.paths import ProjectPaths
from multiagents.scheduler import conversations
(root / "proj").mkdir(parents=True)
paths = ProjectPaths(root / "proj")
provider = types.SimpleNamespace(name="acme", container_account="", family="", session_store=None,
    session={"mode": "export", "path": "sessions", "resumed_event": "e", "resume_deadline_seconds": 1})
attempt = {"node_id": "nd-" + uuid.uuid4().hex[:8], "attempt_id": uuid.uuid4().hex, "binding": {"model": "m"}}
home = root / "home"; (home / "sessions").mkdir(parents=True)
conversations.prepare(paths, attempt, provider, home=home)
with open(home / "sessions" / "payload.json", "wb") as f:
    f.truncate(3 * 1024 ** 3)            # sparse: no disk, 3 GiB of reads
resource.setrlimit(resource.RLIMIT_AS, (1024 ** 3, 1024 ** 3))
try:
    print("SETTLED", conversations.finish(paths, attempt, provider, "ses_x", home=home)["status"])
except conversations.ConversationError as exc:
    print("SETTLED refused", exc)
'''


def test_guest_sized_payload_does_not_exhaust_host_memory(tmp_path):
    """D-S4A.3 / IC-C22: the export copies what the guest left under `path`.
    `_payload` reads each file whole into memory (conversations.py:164), so a
    sparse 3 GiB file - free for the guest to create - costs the scheduler
    3 GiB. Under a 1 GiB address-space limit the export dies with MemoryError
    instead of settling (recorded or refused); without one the scheduler
    process that serves every attempt is the one that grows."""
    script = tmp_path / 'sparse.py'
    script.write_text(SPARSE)
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'))
    out = subprocess.run([sys.executable, str(script), str(tmp_path / 'w')], capture_output=True,
                         text=True, timeout=120, env=env)
    assert 'MemoryError' not in out.stderr, out.stderr[-600:]
    assert 'SETTLED' in out.stdout, (out.stdout, out.stderr[-600:])
