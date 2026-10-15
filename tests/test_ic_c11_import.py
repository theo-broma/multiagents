"""IC-C11 — import with validation before promotion (IC-R2), through a real
scheduler: an isolated attempt (injected reference backend) crafts its result
inside its own repository; the host imports it or refuses it.

Observed: the node (`done`, or `held` with `result_capture_failed` and the
reason code in the hold's detail: P4), the host object store file by file
(P5's "byte-for-byte unchanged"), the host refs, the generations.

The result is crafted by `exec` / `after` directives of the fixture provider
(`ic_prov` docstring): `exec` runs before the provider's own commit, `after`
once the commit is made. The provider is gated while the test snapshots the
host store, so that nothing the scheduler wrote before launching is mistaken
for something the import wrote.

"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import ic_prov as ip  # noqa: E402

SMALL = {"max_bytes": 60_000, "max_inflated_bytes": 50_000_000, "timeout_seconds": 60}
TINY_INFLATED = {"max_bytes": 50_000_000, "max_inflated_bytes": 500_000, "timeout_seconds": 60}
FINAL = ("done", "held", "failed", "cancelled")


def make(tmp_path, monkeypatch, limits=None):
    w = ip.make_world(tmp_path, monkeypatch, import_limits=limits)
    w.pair()
    return w


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = make(tmp_path, monkeypatch)
    yield world
    world.close()


@pytest.fixture
def wsmall(tmp_path, monkeypatch):
    world = make(tmp_path, monkeypatch, SMALL)
    yield world
    world.close()


@pytest.fixture
def winflate(tmp_path, monkeypatch):
    world = make(tmp_path, monkeypatch, TINY_INFLATED)
    yield world
    world.close()


def settle(w, node, timeout=ip.WAIT):
    return w.until(lambda: w.get(node) if w.get(node)["state"] in FINAL else None, timeout,
                   what=f"{node} to settle")


def attempt_result(w, tag, fx, *, before=None, agent="wk", extra_commits=0):
    """Run one gated node whose result is crafted by `fx`; return (node,
    host snapshots before the release, the settled node)."""
    for i in range(extra_commits):
        ip.git(w.root, "commit", "-q", "--allow-empty", "-m", f"host {i}")
    w.start_scheduler()
    node = w.simple(tag, agent, fx={"gate": "g", **fx})
    w.wait_spawn(tag, w.fxw)
    snap = {"store": w.host_store(), "refs": w.host_refs()}
    if before:
        before(w)
    w.gate("g", w.fxw)
    return node, snap, settle(w, node)


def hold_text(view):
    return json.dumps(view.get("hold") or {}, default=str)


def refused_with(w, view, snap, code):
    assert view["state"] == "held", (view["state"], view.get("outcome"))
    assert (view["hold"] or {}).get("reason") == "result_capture_failed", view["hold"]
    assert code in hold_text(view), view["hold"]
    assert view["generations"] == []
    assert w.host_store() == snap["store"], "the host object store changed"
    assert w.host_refs() == snap["refs"], "a host ref changed"


# ------------------------------------------------------------------ accepted

def test_ic_c11_a_result_descending_from_the_input_is_imported_with_its_new_objects(w):
    node, snap, view = attempt_result(w, "OK1", {"write": {"new.txt": "brand new\n"}, "commit": "work"})
    assert view["state"] == "done", (view["state"], view.get("hold"))
    commit = view["generations"][0]["commit"]
    assert ip.git(w.root, "show", f"{commit}:new.txt") == "brand new"
    assert w.host_has(commit)
    blob = ip.git(w.root, "rev-parse", f"{commit}:new.txt")
    assert blob not in snap["store"] and w.host_has(blob)       # a new object, accepted: not limited to seed objects


def test_ic_c11_a_result_equal_to_the_input_commit_is_valid(w):
    node, snap, view = attempt_result(w, "OK2", {})
    assert view["state"] == "done", (view["state"], view.get("hold"))


def test_ic_c11_a_result_of_several_commits_and_a_merge_is_imported(w):
    code = ("sh('git checkout -q -b side && echo s > side.txt && git add -A && git commit -qm side && "
            "git checkout -q - && echo m > main.txt && git add -A && git commit -qm main && "
            "git merge -q --no-ff -m merged side')")
    node, snap, view = attempt_result(w, "OK3", {"exec": code}, extra_commits=1)
    assert view["state"] == "done", (view["state"], view.get("hold"), w.fxw.exec_errors())
    commit = view["generations"][0]["commit"]
    assert set(ip.git(w.root, "ls-tree", "-r", "--name-only", commit).split()) >= {"side.txt", "main.txt"}


# --------------------------------------------------------------------- ancestry

ORPHAN = ("b = subprocess.check_output(['git','symbolic-ref','HEAD'], text=True).strip()\n"
          "sh('git checkout -q --orphan elsewhere && git rm -rqf --ignore-unmatch . && echo x > x.txt && git add x.txt && "
          "git commit -qm orphan')\n"
          "sh('git update-ref %s elsewhere && git symbolic-ref HEAD %s' % (b, b))\n")
ANCESTOR = "sh('git reset -q --hard HEAD~1')\n"


@pytest.mark.parametrize("how", ["orphan", "ancestor-of-input"])
def test_ic_c11_a_result_not_descending_from_the_input_is_refused_before_the_host_store_is_touched(w, how):
    node, snap, view = attempt_result(w, "AN-" + how, {"after": ORPHAN if how == "orphan" else ANCESTOR},
                                      extra_commits=2)
    assert w.fxw.exec_errors() == []
    refused_with(w, view, snap, "import_ancestry")


def test_ic_c11_a_refused_result_is_not_retried_and_no_retention_ref_is_published(w):
    node, snap, view = attempt_result(w, "AN3", {"after": ORPHAN})
    refused_with(w, view, snap, "import_ancestry")
    w.checkpoint()
    assert len(w.runs(node)) == 1 and w.fxw.spawns() == 1
    attempt_id = w.runs(node)[0]["attempt_id"]                       # the checkpoint node's own (successful) attempt may keep its retention ref
    assert not [r for r in w.host_refs() if "node-results" in r and attempt_id in r]


def test_ic_c11_a_refused_result_stays_in_the_attempts_own_repository_for_inspection(w):
    node, snap, view = attempt_result(w, "AN4", {"after": ORPHAN})
    refused_with(w, view, snap, "import_ancestry")
    cwd = Path(w.fxw.calls()[0]["cwd"])
    assert (cwd / ".git").is_dir()
    assert ip.git(cwd, "log", "-1", "--format=%s", "elsewhere") == "orphan"       # kept, there, not on the host


# ----------------------------------------------------------------------- bounds

def test_ic_c11_an_oversized_result_is_refused_within_bounds(wsmall):
    w = wsmall
    ok_node, _, ok = attempt_result(w, "B-OK", {"write": {"small.txt": "tiny\n"}, "commit": "small"})
    assert ok["state"] == "done", (ok["state"], ok.get("hold"))       # the control: the limits do not refuse everything
    node = w.simple("B-BIG", "wk", fx={"gate": "g2", "exec": "open('big.bin','wb').write(os.urandom(300000))",
                                       "commit": "big"})
    w.wait_spawn("B-BIG", w.fxw)
    snap = {"store": w.host_store(), "refs": w.host_refs()}
    w.gate("g2", w.fxw)
    refused_with(w, settle(w, node), snap, "import_bounds")


def test_ic_c11_a_result_that_inflates_beyond_the_limit_is_refused_though_it_compresses_small(winflate):
    w = winflate
    ok_node, _, ok = attempt_result(w, "I-OK", {"write": {"small.txt": "tiny\n"}, "commit": "small"})
    assert ok["state"] == "done", (ok["state"], ok.get("hold"))
    node = w.simple("I-BOMB", "wk", fx={"gate": "g2", "exec": "open('zeros.bin','wb').write(bytes(6000000))",
                                        "commit": "bomb"})
    w.wait_spawn("I-BOMB", w.fxw)
    snap = {"store": w.host_store(), "refs": w.host_refs()}
    w.gate("g2", w.fxw)
    refused_with(w, settle(w, node), snap, "import_bounds")


# -------------------------------------------------------------------- malformed

MISSING = ("sha = subprocess.check_output(['git','rev-parse','HEAD:new.txt'], text=True).strip()\n"
           "p = '.git/objects/%s/%s' % (sha[:2], sha[2:]); os.chmod(p, 0o644); os.remove(p)\n")
GARBAGE = ("sha = subprocess.check_output(['git','rev-parse','HEAD:new.txt'], text=True).strip()\n"
           "p = '.git/objects/%s/%s' % (sha[:2], sha[2:]); os.chmod(p, 0o644); open(p,'wb').write(b'garbage' * 9)\n")
WRONG = ("import zlib\n"
         "sha = subprocess.check_output(['git','rev-parse','HEAD:new.txt'], text=True).strip()\n"
         "p = '.git/objects/%s/%s' % (sha[:2], sha[2:]); os.chmod(p, 0o644)\n"
         "open(p,'wb').write(zlib.compress(b'blob 5\\0other'))\n")
BAD_COMMIT = ("b = subprocess.check_output(['git','symbolic-ref','HEAD'], text=True).strip()\n"
              "tree = subprocess.check_output(['git','rev-parse','HEAD^{tree}'], text=True).strip()\n"
              "parent = subprocess.check_output(['git','rev-parse','HEAD'], text=True).strip()\n"
              "body = 'tree %s\\nparent %s\\nauthor x <x> notatime\\n\\nbroken\\n' % (tree, parent)\n"
              "sha = subprocess.run(['git','hash-object','-t','commit','-w','--stdin','--literally'],"
              " input=body, capture_output=True, text=True).stdout.strip()\n"
              "subprocess.run(['git','update-ref', b, sha])\n")


@pytest.mark.parametrize("damage", ["missing-blob", "garbage-blob", "wrong-content-blob", "malformed-commit"])
def test_ic_c11_a_malformed_result_is_refused_before_the_host_store_is_touched(w, damage):
    code = {"missing-blob": MISSING, "garbage-blob": GARBAGE, "wrong-content-blob": WRONG,
            "malformed-commit": BAD_COMMIT}[damage]
    node, snap, view = attempt_result(w, "M-" + damage,
                                      {"write": {"new.txt": "content\n"}, "commit": "work", "after": code})
    assert w.fxw.exec_errors() == []
    refused_with(w, view, snap, "import_malformed")


# ------------------------------------------------------------------- alternates

@pytest.mark.parametrize("target", ["host-objects", "empty-dir"])
def test_ic_c11_a_source_with_alternates_is_refused(w, target):
    path = (str(w.root / ".git" / "objects") if target == "host-objects" else "/nonexistent-objects")
    code = f"open('.git/objects/info/alternates', 'w').write({path!r} + '\\n')\n"
    node, snap, view = attempt_result(w, "AL-" + target,
                                      {"write": {"new.txt": "content\n"}, "commit": "work", "after": code})
    assert w.fxw.exec_errors() == []
    refused_with(w, view, snap, "import_alternates")


# ------------------------------------------------------------ unreachable objects

def test_ic_c11_unreachable_extra_objects_are_not_promoted(w):
    code = ("blob = subprocess.run(['git','hash-object','-w','--stdin'], input='dangling-blob-%s\\n' % os.getpid(),"
            " capture_output=True, text=True).stdout.strip()\n"
            "tree = subprocess.check_output(['git','rev-parse','HEAD^{tree}'], text=True).strip()\n"
            "env = dict(os.environ, GIT_AUTHOR_NAME='a', GIT_AUTHOR_EMAIL='a@b', GIT_COMMITTER_NAME='a', GIT_COMMITTER_EMAIL='a@b')\n"
            "ghost = subprocess.run(['git','commit-tree', tree, '-m', 'never referenced'], capture_output=True,"
            " text=True, env=env).stdout.strip()\n"
            "open(base + '/extras', 'w').write(blob + ' ' + ghost)\n")
    node, snap, view = attempt_result(w, "UN1", {"write": {"new.txt": "kept\n"}, "commit": "work", "after": code})
    assert view["state"] == "done", (view["state"], view.get("hold"), w.fxw.exec_errors())
    blob, ghost = (w.fxw.dir / "extras").read_text().split()
    assert not w.host_has(blob) and not w.host_has(ghost)
    kept = ip.git(w.root, "rev-parse", view["generations"][0]["commit"] + ":new.txt")
    assert w.host_has(kept)                                     # the reachable ones are


# -------------------------------------------------- hooks, config, filters (P9)

POISON = r'''
mark = base + "/mark.sh"
open(mark, "w").write("#!/bin/sh\ntouch %s/marker-$(basename $0)-$1\ncat\n" % base)
os.chmod(mark, 0o755)
hooks = os.path.abspath(".git/hooks"); os.makedirs(hooks, exist_ok=True)
alt = base + "/hooks-elsewhere"; os.makedirs(alt, exist_ok=True)
for d in (hooks, alt):
    for name in ("pre-commit", "post-checkout", "post-merge", "reference-transaction", "pre-auto-gc",
                 "post-rewrite", "pre-push", "post-update", "update", "pre-receive", "post-receive",
                 "applypatch-msg", "fsmonitor-watchman", "push-to-checkout"):
        p = os.path.join(d, name)
        open(p, "w").write("#!/bin/sh\ntouch %s/marker-hook-%s\n" % (base, name))
        os.chmod(p, 0o755)
cfg = open(".git/config", "a")
cfg.write("[core]\n\thooksPath = %s\n\tfsmonitor = %s fsmonitor\n\tsshCommand = %s ssh\n\tpager = %s pager\n\teditor = %s editor\n"
          % (alt, mark, mark, mark, mark))
cfg.write('[filter "x"]\n\tclean = %s clean\n\tsmudge = %s smudge\n\tprocess = %s process\n\trequired = true\n' % (mark, mark, mark))
cfg.write('[diff]\n\texternal = %s diff\n[uploadpack]\n\tpackObjectsHook = %s pack\n' % (mark, mark))
cfg.write('[gc]\n\tauto = 1\n[receive]\n\tfsckObjects = false\n')
cfg.close()
os.makedirs(".git/info", exist_ok=True)
open(".git/info/attributes", "w").write("* filter=x diff=x\n")
'''


def test_ic_c11_p9_hooks_config_and_filters_in_the_source_never_run(w):
    node, snap, view = attempt_result(w, "HK1",
                                      {"write": {"new.txt": "content\n"}, "commit": "work", "after": POISON})
    assert w.fxw.exec_errors() == []
    assert view["state"] in ("done", "held")                  # the host looked at the result
    assert w.fxw.markers() == [], w.fxw.markers()


def test_ic_c11_p9_hooks_and_filters_never_run_on_a_refused_result_either(w):
    node, snap, view = attempt_result(w, "HK2", {"after": ORPHAN + "\n" + POISON})
    assert w.fxw.exec_errors() == []
    assert view["state"] == "held"
    assert w.fxw.markers() == [], w.fxw.markers()


# -------------------------------------------------------- interruption (P5)

@pytest.mark.skipif(os.geteuid() == 0, reason="permission bits do not bind root")
def test_ic_c11_p5_a_failure_during_promotion_leaves_no_ref_and_no_generation(w):
    objects = w.root / ".git" / "objects"

    def freeze(world):
        for d in [objects, *[p for p in objects.rglob("*") if p.is_dir()]]:
            d.chmod(0o555)

    def thaw():
        for d in [objects, *[p for p in objects.rglob("*") if p.is_dir()]]:
            d.chmod(0o755)

    try:
        node, snap, view = attempt_result(w, "PR1", {"write": {"new.txt": "promoted?\n"}, "commit": "work"},
                                          before=freeze)
        assert view["state"] == "held" and (view["hold"] or {}).get("reason") == "result_capture_failed"
        assert view["generations"] == []
        assert w.host_refs() == snap["refs"], "a ref was published before promotion completed"
    finally:
        thaw()


def test_ic_c11_q4_import_uses_the_recorded_kind_after_a_config_change(w):
    node, snap, view = attempt_result(
        w, "RECORDED-ISOLATED", {"after": ORPHAN},
        before=lambda world: world.set_executor("local"))
    assert w.runs(node)[0]["executor"]["kind"] == "isolated"
    refused_with(w, view, snap, "import_ancestry")
