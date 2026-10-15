"""Adversary 3 — IC-C11 / P5 against results the slice 2 tests never craft.

A refused result must leave the host object store and refs byte-identical
(P5), and an invalid result must be refused with an `import_*` reason (P4,
Q4). The tests here hand the import a designated result that is not a
commit, although it peels to one that descends from the input.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import ic_prov as ip  # noqa: E402

FINAL = ("done", "held", "failed", "cancelled")


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = ip.make_world(tmp_path, monkeypatch)
    world.pair()
    yield world
    world.close()


def settle(w, node, timeout=ip.WAIT):
    return w.until(lambda: w.get(node) if w.get(node)["state"] in FINAL else None, timeout,
                   what=f"{node} to settle")


def attempt_result(w, tag, fx):
    w.start_scheduler()
    node = w.simple(tag, "wk", fx={"gate": "g", **fx})
    w.wait_spawn(tag, w.fxw)
    snap = {"store": w.host_store(), "refs": w.host_refs()}
    w.gate("g", w.fxw)
    return node, snap, settle(w, node)


# The recorded branch is rewritten as a loose ref naming an annotated tag
# whose target is the attempt's own (descending) commit. Git itself refuses
# to put a tag on refs/heads, so the file is written directly.
TAG_RESULT = (
    "b = subprocess.check_output(['git','symbolic-ref','HEAD'], text=True).strip()\n"
    "sh('git tag -a -m wrapped wrapped')\n"
    "t = subprocess.check_output(['git','rev-parse','refs/tags/wrapped'], text=True).strip()\n"
    "sh('git tag -d wrapped')\n"
    "open('.git/' + b, 'w').write(t + '\\n')\n"
)


def test_ic_adv3_a_tag_object_as_the_result_is_refused_without_touching_the_host_store(w):
    node, snap, view = attempt_result(w, "TAG", {"write": {"new.txt": "via tag\n"}, "commit": "work",
                                                 "after": TAG_RESULT})
    assert w.fxw.exec_errors() == []
    assert view["state"] == "held", (view["state"], view.get("hold"))
    assert (view["hold"] or {}).get("reason") == "result_capture_failed", view["hold"]
    assert view["generations"] == []
    assert w.host_refs() == snap["refs"], "a host ref changed"
    # P5: the refusal must leave the host object store byte-for-byte unchanged.
    assert w.host_store() == snap["store"], "the host object store changed on a refused result"
    # P4/Q4: an invalid result is refused with an import_* reason code.
    assert "import_" in json.dumps(view["hold"]), view["hold"]
