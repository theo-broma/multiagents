"""IC-C10 — the fresh repository (IC-R1): the backend clones the seed into the
guest repository path. Conformance suite, through the protocol only, parametrised
by backend (extends `test_ic_c5_conformance.py`; a Docker backend joins by
appending to `BACKENDS`). The repository is inspected from INSIDE the guest by
running `ic_report.py` as an invocation.

Plus P6 (completion, retries, serialisation) and P9 (no config, hooks, filters).
Uses the public constructor and refusal spellings from Q1..Q3.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import ic_prov as ip  # noqa: E402
import ic_support as ic  # noqa: E402
from ic_support import data_of, identity_of, invocation, kind_of, launch_spec, py, run  # noqa: E402

BACKENDS = ["reference"]


class Env:
    def __init__(self, tmp_path, backend_name):
        self.tmp = tmp_path
        self.repo = ip.HostRepo(tmp_path / "host")
        self.ids = ip.standard_world(self.repo)
        self.reg = ip.seed_registry(self.repo.path)
        self.root = tmp_path / "backend-root"
        self.backend = ip.wired_backend(self.root, self.reg)
        self.name = backend_name
        self.n = 0

    def spec(self, owner="att-c10", commit=None, **over):
        ref = self.reg.build(owner, commit or self.ids["input"])
        return launch_spec(self.root, owner=owner, lifetime="conversation", seed=ref, **over)

    def provision(self, owner="att-c10", key="c10-key", commit=None, **over):
        spec = self.spec(owner, commit, **over)
        outcome = run(self.backend.provision(spec, key))
        return spec, outcome

    def ident(self, **kw):
        spec, outcome = self.provision(**kw)
        assert kind_of(outcome) not in ("invalid_spec", "unsupported", "identity_conflict", "unknown"), \
            ip.reason_of(outcome)
        return identity_of(outcome)

    def turn(self, ident, argv, *, cwd=ic.GUEST_REPO, env=None, wait=True):
        self.n += 1
        key = f"turn-{self.n}"
        reply = run(self.backend.run(ident, invocation(argv, kind="turn", cwd=cwd, env=env), key))
        assert kind_of(reply) not in ("invalid_spec", "unknown", "exited"), ip.reason_of(reply)
        if wait:
            ip.wait_until(lambda: kind_of(run(self.backend.inspect_invocation(ident, key))) == "exited",
                          f"{key} to exit", timeout=ic.OP)
        return key

    def out(self, ident, key, stream="stdout"):
        return data_of(run(self.backend.output(ident, key, stream, 0)))

    def report(self, ident) -> dict:
        key = self.turn(ident, [sys.executable, str(ip.REPORT_FILE)])
        text = self.out(ident, key).decode()
        assert "IC-REPORT " in text, (text, self.out(ident, key, "stderr")[-500:])
        return json.loads(text.split("IC-REPORT ", 1)[1])

    def sh(self, ident, code) -> str:
        key = self.turn(ident, py(code))
        return self.out(ident, key).decode()


@pytest.fixture(params=BACKENDS)
def env(request, tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    e = Env(tmp_path, request.param)
    yield e

    try:
        async def reap():
            for ident in await e.backend.list_owned("ic-test"):
                try:
                    await e.backend.stop(identity_of(ident), 0.1)
                    await e.backend.remove(identity_of(ident), False)
                except Exception:
                    pass
        run(reap())
    except Exception:
        pass


# ----------------------------------------------------------- the clone itself

def test_ic_c10_the_guest_repository_is_a_sound_independent_clone_of_the_seed(env):
    ident = env.ident()
    assert ip.isolated_clone_problems(env.report(ident), env.repo.path, env.ids["input"]) == []


def test_ic_c10_the_checkout_is_checkout_commit_and_the_files_are_its_tree(env):
    ident = env.ident()
    rep = env.report(ident)
    assert rep["head"] == env.ids["input"]
    tree = set(ip.git(env.repo.path, "ls-tree", "-r", "--name-only", env.ids["input"]).splitlines())
    assert set(rep["tracked"]) == tree and not rep["status"]
    assert env.sh(ident, "print(open('c.txt').read(), end='')") == "gamma\n"


def test_ic_c10_checkout_commit_is_whatever_the_seed_was_built_for(env):
    ident = env.ident(owner="att-base", key="k-base", commit=env.ids["base"])
    rep = env.report(ident)
    assert rep["head"] == env.ids["base"]
    assert "c.txt" not in rep["tracked"]                       # the later commit's file
    assert ip.isolated_clone_problems(rep, env.repo.path, env.ids["base"]) == []


def test_ic_c10_no_alternates_no_shared_git_no_linked_worktree_no_host_remote(env):
    rep = env.report(env.ident())
    assert not rep["alternates"]
    assert rep["git_is_dir"] and not rep["commondir"]
    assert [x for x in rep["worktrees"] if x.startswith("worktree ")].__len__() == 1
    assert str(env.repo.path) not in rep["config"]
    assert not any(str(env.repo.path) in line for line in rep["remotes"])
    assert rep["local_object_count"] == len(rep["objects"])    # every object is in its own store


def test_ic_c10_a_siblings_refs_objects_and_reflogs_are_absent(env):
    ids = env.ids
    rep = env.report(env.ident())
    assert ids["sibling"] not in rep["objects"] and ids["secret_blob"] not in rep["objects"]
    assert ids["unrelated"] not in rep["objects"]
    assert all("sibling" not in name and "unrelated" not in name for name in rep["refs"])
    assert ids["sibling"] not in rep["reflog_oids"] and ids["unrelated"] not in rep["reflog_oids"]
    probe = env.sh(env.ident(), f"import subprocess; print(subprocess.run(['git','cat-file','-e','{ids['sibling']}']).returncode)")
    assert probe.strip() != "0"                                # the sibling commit cannot be read by id


def test_ic_c10_the_guest_cannot_see_the_host_repository_or_the_seed_cache(env):
    spec, outcome = env.provision()
    ident = identity_of(outcome)
    rep = env.report(ident)
    cache = Path(env.reg.resolve(spec.seed, "att-c10")).parent
    text = json.dumps([rep["config"], rep["remotes"], list(rep["env"].values())])
    assert str(env.repo.path) not in text
    assert str(cache) not in text and str(env.tmp / "host") not in text


# ------------------------------------------------- idempotence, retries (P6)

def test_ic_c10_provision_is_idempotent_by_key_and_returns_the_completed_clone(env):
    spec, first = env.provision()
    ident = identity_of(first)
    env.sh(ident, "open('mine.txt','w').write('kept')")
    second = run(env.backend.provision(spec, "c10-key"))
    assert identity_of(second) == ident
    assert len(run(env.backend.list_owned("ic-test"))) == 1
    assert env.sh(ident, "print(open('mine.txt').read(), end='')") == "kept"      # not reset
    assert env.report(ident)["head"] == env.ids["input"]


def test_ic_c10_a_retry_never_resets_a_running_checkout(env):
    spec, first = env.provision()
    ident = identity_of(first)
    env.turn(ident, py("open('work.txt','w').write('in progress')\n"
                       "import subprocess; subprocess.run(['git','add','work.txt'])\n"
                       "print('ready', flush=True)\nimport time; time.sleep(30)"), wait=False)
    ip.wait_until(lambda: "work.txt" in env.sh(ident, "import os; print(os.listdir('.'))"), "the file")
    again = run(env.backend.provision(spec, "c10-key"))
    assert identity_of(again) == ident
    assert "work.txt" in env.sh(ident, "import os; print(os.listdir('.'))")
    assert env.sh(ident, "import subprocess; print(subprocess.run(['git','status','--porcelain'],"
                         "capture_output=True,text=True).stdout)").strip() == "A  work.txt"
    run(env.backend.stop(ident, 0.2))


def test_ic_c10_p6_the_same_key_with_a_different_seed_is_an_identity_conflict(env):
    env.provision()
    other = env.spec("att-c10", env.ids["base"])
    assert kind_of(run(env.backend.provision(other, "c10-key"))) == "identity_conflict"


def test_ic_c10_p6_two_provisionings_under_one_key_are_serialised_and_yield_one_clone(env):
    spec = env.spec()

    async def both():
        return await asyncio.gather(env.backend.provision(spec, "c10-key"),
                                    env.backend.provision(spec, "c10-key"),
                                    env.backend.provision(spec, "c10-key"))
    outcomes = run(both(), timeout=ip.OP * 2)
    idents = {identity_of(o) for o in outcomes}
    assert len(idents) == 1 and all(kind_of(o) not in ("invalid_spec", "unknown") for o in outcomes), \
        [ip.reason_of(o) for o in outcomes]
    assert len(run(env.backend.list_owned("ic-test"))) == 1
    assert ip.isolated_clone_problems(env.report(idents.pop()), env.repo.path, env.ids["input"]) == []


def test_ic_c10_p6_a_provisioning_cut_at_any_step_is_rebuilt_by_the_retry(env, tmp_path, monkeypatch):
    """The N-th state-changing git call fails after it ran, leaving whatever a
    crash at that point leaves; the retry under the same key must end in a
    sound, complete clone, with no leftovers of the first attempt. N runs from
    1 up to the number of calls a provisioning makes."""
    shim = ip.GitShim(tmp_path, monkeypatch)
    cut = 0
    for fail_at in range(1, 25):
        owner, key = f"att-cut{fail_at}", f"cut-key-{fail_at}"
        spec = env.spec(owner)
        shim.arm(fail_at)
        try:
            run(env.backend.provision(spec, key))
        except BaseException:       # noqa: BLE001  (an operational failure may be an exception, K7)
            pass
        if shim.seen() < fail_at:   # the provisioning completed before reaching the N-th call: all steps were cut
            break
        cut += 1
        shim.arm(0)                                             # the retry runs unhindered
        again = run(env.backend.provision(spec, key))
        assert kind_of(again) not in ("invalid_spec", "unknown", "identity_conflict"), (fail_at, ip.reason_of(again))
        problems = ip.isolated_clone_problems(env.report(identity_of(again)), env.repo.path, env.ids["input"])
        assert problems == [], (fail_at, problems)
    assert cut >= 1, "the provisioning ran no git command from PATH: the shim cannot cut it"
    assert len(run(env.backend.list_owned("ic-test"))) == cut + 1   # one resource per key (the last was not cut), none duplicated


# ------------------------------------------------------------ the seed handle

def test_ic_c10_a_handle_of_another_attempt_is_invalid_spec_and_creates_nothing(env):
    foreign = env.reg.build("att-other", env.ids["input"])
    spec = launch_spec(env.root, owner="att-c10", lifetime="conversation", seed=foreign)
    outcome = run(env.backend.provision(spec, "c10-key"))
    assert isinstance(outcome, ip.lifecycle().InvalidSpec), ip.reason_of(outcome)
    assert outcome.reason.startswith("unauthorised: ")
    assert kind_of(run(env.backend.find("c10-key"))) == "absent"
    assert run(env.backend.list_owned("ic-test")) == []


@pytest.mark.parametrize("bogus", ["bundle-path", "forged", "none-existent"])
def test_ic_c10_a_bundle_path_or_forged_handle_supplied_by_a_run_is_invalid_spec(env, bogus):
    real = env.reg.build("att-c10", env.ids["input"])
    path = Path(env.reg.resolve(real, "att-c10"))
    if bogus == "bundle-path":
        seed = str(path)
    elif bogus == "forged":
        seed = _forge(real)
    else:
        seed = "/nonexistent/seed.bundle"
    spec = launch_spec(env.root, owner="att-c10", lifetime="conversation", seed=seed)
    outcome = run(env.backend.provision(spec, "c10-key"))
    assert isinstance(outcome, ip.lifecycle().InvalidSpec), ip.reason_of(outcome)
    code, separator, reason = outcome.reason.partition(": ")
    assert separator and reason and code in {"unauthorised", "seed_unavailable", "corrupt"}
    assert kind_of(run(env.backend.find("c10-key"))) == "absent"


def _forge(ref):
    import copy
    forged = copy.copy(ref)
    object.__setattr__(forged, "id", "f" * 64)
    return forged


def test_ic_c10_a_corrupt_cache_entry_is_rebuilt_and_the_provisioning_succeeds(env):
    spec = env.spec()
    Path(env.reg.resolve(spec.seed, "att-c10")).write_bytes(b"corrupt")
    outcome = run(env.backend.provision(spec, "c10-key"))
    assert kind_of(outcome) not in ("invalid_spec", "unknown"), ip.reason_of(outcome)
    assert ip.isolated_clone_problems(env.report(identity_of(outcome)), env.repo.path, env.ids["input"]) == []


def test_ic_c10_a_corrupt_entry_with_its_pinned_objects_gone_is_seed_unavailable(env):
    repo = env.repo
    repo.branch("throwaway", env.ids["input"])
    only = repo.commit("only via throwaway", {"only.txt": "x\n"})
    spec = env.spec(commit=only)
    path = Path(env.reg.resolve(spec.seed, "att-c10"))
    for line in ip.git(repo.path, "for-each-ref", "--format=%(refname)", "refs/multiagents/seeds/").splitlines():
        ip.git(repo.path, "update-ref", "-d", line)
    repo.checkout("main")
    ip.git(repo.path, "branch", "-D", "throwaway")
    ip.git(repo.path, "reflog", "expire", "--expire=now", "--all")
    ip.git(repo.path, "gc", "-q", "--prune=now")
    path.write_bytes(b"corrupt")
    outcome = run(env.backend.provision(spec, "c10-key"))
    assert isinstance(outcome, ip.lifecycle().InvalidSpec), ip.reason_of(outcome)
    assert outcome.reason.startswith("seed_unavailable: ")
    assert kind_of(run(env.backend.find("c10-key"))) == "absent"


# -------------------------------------------------------------------- undo

def test_ic_c10_remove_without_artifacts_discards_the_clone_and_a_new_key_gets_a_fresh_one(env):
    spec, first = env.provision()
    ident = identity_of(first)
    env.sh(ident, "open('old-activation.txt','w').write('x')\n"
                  "import subprocess; subprocess.run(['git','config','user.name','x'])\n"
                  "subprocess.run(['git','commit','-q','--allow-empty','-m','scratch'],"
                  "env=dict(__import__('os').environ,GIT_AUTHOR_NAME='a',GIT_AUTHOR_EMAIL='a@b',"
                  "GIT_COMMITTER_NAME='a',GIT_COMMITTER_EMAIL='a@b'))")
    run(env.backend.stop(ident, 0.2))
    run(env.backend.remove(ident, False))
    assert kind_of(run(env.backend.inspect(ident))) == "absent"
    fresh = env.ident(key="c10-key-2")
    rep = env.report(fresh)
    assert rep["head"] == env.ids["input"] and "old-activation.txt" not in rep["status"] + rep["tracked"]
    assert ip.isolated_clone_problems(rep, env.repo.path, env.ids["input"]) == []


# ------------------------------------------------------ P9: config, hooks, filters

@pytest.fixture
def hostile_git_config(tmp_path, monkeypatch):
    """A global and a system git config that run code if they are ever honoured.
    Every execution appends `<name> <cwd> <git dir>` to one log, so the test can
    tell a run in the clone from the host's own scratch repositories."""
    marks = tmp_path / "marks"
    marks.mkdir()
    hook_dir = tmp_path / "hooks"
    hook_dir.mkdir()
    template = tmp_path / "template"
    (template / "hooks").mkdir(parents=True)
    log = marks / "ran.log"
    script = tmp_path / "mark.sh"
    script.write_text(f"#!/bin/sh\necho \"filter-or-fsmonitor $PWD ${{GIT_DIR:-}}\" >> {log}\ncat\n")
    script.chmod(0o755)
    for name in ("post-checkout", "post-merge", "reference-transaction", "pre-auto-gc", "post-rewrite"):
        for d in (hook_dir, template / "hooks"):
            (d / name).write_text(f"#!/bin/sh\necho \"hook-{name} $PWD ${{GIT_DIR:-}}\" >> {log}\n")
            (d / name).chmod(0o755)
    cfg = tmp_path / "hostile.gitconfig"
    cfg.write_text(
        f"[core]\n\thooksPath = {hook_dir}\n\tfsmonitor = {script}\n"
        f"[init]\n\ttemplateDir = {template}\n"
        f"[filter \"evil\"]\n\tsmudge = {script} smudge\n\tclean = {script} clean\n\trequired = false\n"
        f"[filter \"lfs\"]\n\tsmudge = {script} lfs-smudge %f\n\tclean = {script} lfs-clean %f\n\trequired = false\n"
        f"[protocol \"file\"]\n\tallow = always\n"
        f"[user]\n\tname = hostile\n\temail = h@example.invalid\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(cfg))
    monkeypatch.delenv("GIT_CONFIG_NOSYSTEM", raising=False)
    monkeypatch.setenv("GIT_TEMPLATE_DIR", str(template))
    return log


def test_ic_c10_p9_system_and_global_config_hooks_and_filters_do_not_run_in_the_clone(
        tmp_path, monkeypatch, hostile_git_config):
    log = hostile_git_config
    # host side and seed build run under a quiet config; only the clone meets the hostile one
    with monkeypatch.context() as quiet:
        quiet.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
        quiet.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
        quiet.delenv("GIT_TEMPLATE_DIR", raising=False)
        repo = ip.HostRepo(tmp_path / "host")
        repo.commit("filters", {".gitattributes": "*.dat filter=evil\n*.bin filter=lfs diff=lfs merge=lfs -text\n",
                                "x.dat": "raw content\n",
                                "big.bin": "version https://git-lfs.github.com/spec/v1\noid sha256:" + "ab" * 32 + "\nsize 99999999\n"})
        commit = repo.head()
        reg = ip.seed_registry(repo.path)
        ref = reg.build("att-p9", commit)
    root = tmp_path / "backend-root"
    backend = ip.wired_backend(root, reg)
    spec = launch_spec(root, owner="att-p9", lifetime="conversation", seed=ref)
    outcome = run(backend.provision(spec, "p9-key"), timeout=ip.OP * 2)
    assert kind_of(outcome) not in ("invalid_spec", "unsupported", "unknown"), ip.reason_of(outcome)
    ident = identity_of(outcome)
    key = "p9-read"
    run(backend.run(ident, invocation(py(
        "print(open('x.dat').read(), end='|'); print(open('big.bin').read(), end='')"),
        kind="turn"), key))
    ip.wait_until(lambda: kind_of(run(backend.inspect_invocation(ident, key))) == "exited", "read")
    shown = data_of(run(backend.output(ident, key, "stdout", 0))).decode()
    assert shown.startswith("raw content\n|"), shown                  # no smudge ran
    assert "oid sha256:" + "ab" * 32 in shown                          # LFS pointers stay pointer files
    ran = log.read_text().splitlines() if log.exists() else []
    in_clone = [line for line in ran if str(root) in line or str(root.resolve()) in line]
    assert in_clone == [], in_clone                                  # nothing ran in the guest repository


def test_ic_c10_p9_a_submodule_stays_an_empty_directory_with_its_gitlink_recorded(env):
    repo = env.repo
    gitlink = "1" * 40
    repo.write(".gitmodules", '[submodule "vendor/lib"]\n\tpath = vendor/lib\n\turl = https://example.invalid/lib.git\n')
    ip.git(repo.path, "add", ".gitmodules")
    ip.git(repo.path, "update-index", "--add", "--cacheinfo", f"160000,{gitlink},vendor/lib")
    ip.git(repo.path, "commit", "-q", "-m", "with a gitlink")
    commit = repo.head()
    ident = env.ident(owner="att-sub", key="k-sub", commit=commit)
    listing = env.sh(ident, "import subprocess,os;"
                            "print(subprocess.run(['git','ls-tree','HEAD','vendor/lib'],capture_output=True,text=True).stdout.strip());"
                            "print(os.path.isdir('vendor/lib'), os.listdir('vendor/lib') if os.path.isdir('vendor/lib') else None)")
    assert listing.splitlines()[0].startswith("160000 commit " + gitlink), listing
    assert listing.splitlines()[1] == "True []", listing


# ------------------------------------------------- through the scheduler (IC-C10 e2e)

@pytest.fixture
def world(tmp_path, monkeypatch):
    w = ip.make_world(tmp_path, monkeypatch)
    w.pair()
    yield w
    w.close()


def test_ic_c10_a_scheduled_isolated_attempt_starts_in_a_sound_fresh_clone(world):
    w = world
    ip.git(w.root, "checkout", "-q", "-b", "nodes/sibling-secret")
    ip.git(w.root, "commit", "-q", "--allow-empty", "-m", "sibling's unmerged work")
    sibling = ip.git(w.root, "rev-parse", "HEAD")
    ip.git(w.root, "checkout", "-q", "-")
    w.start_scheduler()
    node = w.simple("E1", "wk", fx={"report": True})
    w.wait_state(node, "done", timeout=ip.WAIT)
    run = w.runs(node)[0]
    rep = w.report(w.fxw, "E1")
    assert ip.isolated_clone_problems(rep, w.root, run["input_commit"]) == []
    assert sibling not in rep["objects"] and sibling not in rep["reflog_oids"]


def test_ic_c10_the_run_is_not_given_the_seed_cache_or_the_host_repository(world):
    w = world
    w.start_scheduler()
    node = w.simple("E2", "wk", fx={"report": True})
    w.wait_state(node, "done", timeout=ip.WAIT)
    run = w.runs(node)[0]
    rep = w.report(w.fxw, "E2")
    reg = ip.seed_registry(w.root)
    cache = Path(reg.resolve(reg.build("probe-attempt", run["input_commit"]), "probe-attempt")).parent
    text = json.dumps([rep["config"], rep["remotes"], list(rep["env"].values()), rep["cwd"]])
    assert str(cache) not in text
    assert str(w.root / ".git") not in text
    assert str(w.root) not in json.dumps([rep["config"], rep["remotes"]])


def test_ic_c10_the_first_attempt_of_a_node_and_a_retry_each_get_their_own_complete_clone(world):
    w = world
    w.plan({"fail_nth_provision": 1, "mode": "raise_after"})   # the clone is made, then the call fails (K7)
    w.start_scheduler()
    node = w.simple("E3", "wk", fx={"report": True})
    w.wait_state(node, "done", timeout=ip.WAIT)
    run = w.runs(node)[-1]
    assert ip.isolated_clone_problems(w.report(w.fxw, "E3"), w.root, run["input_commit"]) == []


def test_ic_c10_q2_a_seed_without_a_registry_is_refused(tmp_path):
    backend = ip.lifecycle().ReferenceBackend(tmp_path / "backend")
    spec = launch_spec(tmp_path, seed="nonempty-host-handle")
    outcome = run(backend.provision(spec, "no-registry"))
    assert isinstance(outcome, ip.lifecycle().InvalidSpec)
    assert outcome.reason == "seed_unavailable: no registry"
    assert kind_of(run(backend.find("no-registry"))) == "absent"


def test_ic_c10_q7_an_empty_seed_creates_no_repository(tmp_path):
    backend = ip.lifecycle().ReferenceBackend(tmp_path / "backend")
    ident = identity_of(run(backend.provision(launch_spec(tmp_path), "empty-seed")))
    try:
        key = "inspect-empty"
        reply = run(backend.run(ident, invocation(py(
            "import os; print(os.listdir('.')); print(os.path.exists('.git'))")), key))
        assert kind_of(reply) not in {"invalid_spec", "unknown"}, ip.reason_of(reply)
        ip.wait_until(lambda: kind_of(run(backend.inspect_invocation(ident, key))) == "exited",
                      "empty-seed invocation to exit", timeout=ic.OP)
        assert data_of(run(backend.output(ident, key, "stdout", 0))).decode().splitlines() == ["[]", "False"]
    finally:
        run(backend.stop(ident, 0.1))
        run(backend.remove(ident, False))


def test_ic_c10_k9_attempt_resources_have_a_private_home_per_invocation(tmp_path):
    backend = ip.lifecycle().ReferenceBackend(tmp_path / "backend")
    homes, identities = [], []
    try:
        for n in range(2):
            ident = identity_of(run(backend.provision(launch_spec(tmp_path, owner=f"att-{n}"), f"home-{n}")))
            identities.append(ident)
            key = f"agent-{n}"
            run(backend.run(ident, invocation(py(
                "import os, pathlib; h=pathlib.Path(os.environ['HOME']); "
                "print(str(h), (h/'prior').exists()); (h/'prior').write_text('private')")), key))
            ip.wait_until(lambda: kind_of(run(backend.inspect_invocation(ident, key))) == "exited",
                          "attempt invocation to exit", timeout=ic.OP)
            out = data_of(run(backend.output(ident, key, "stdout", 0))).decode().strip()
            home, prior = out.rsplit(" ", 1)
            assert prior == "False", out
            homes.append(home)
        assert homes[0] != homes[1]
    finally:
        for ident in identities:
            run(backend.stop(ident, 0.1))
            run(backend.remove(ident, False))


@pytest.mark.ic_milestone(3)
@pytest.mark.skipif(os.environ.get("SG_TEST_DOCKER") != "1", reason="SG_TEST_DOCKER=1 required")
def test_ic_c10_the_docker_guest_repository_home_and_temp_have_stable_paths(
        tmp_path, monkeypatch, st_live_docker, request):
    import ic_docker as dk
    dk.require_docker()
    w = dk.IsolationWorld(tmp_path, monkeypatch, request.config.st_docker_owner)
    try:
        w.start()
        node = w.probe_node("plant", nonce=w.nonce("stable"), hold=0)
        rep = w.report(node, "planted")
        assert rep["cwd"] == ic.GUEST_REPO
        assert rep["home"] == ic.GUEST_HOME
        assert rep["tmp"] == ic.GUEST_TEMP
    finally:
        dk.cleanup(w)


def test_ic_c10_q7_seed_required_is_a_nonretryable_scheduler_refusal(world):
    world.plan({"seed_required": True})
    world.start_scheduler()
    node = world.simple("NO-SEED", "wk", fx={"report": True})
    view = world.until(lambda: (v := world.get(node)) and v["state"] in {"held", "failed", "done"}
                       and v, timeout=ip.WAIT, what="the seed-required refusal")
    causes = [c for c in ic.causes(view) if "seed_required" in ic.text_of(c)]
    assert causes, view
    assert causes[0]["class"] == "other"
    assert causes[0].get("retryable", False) is False
    assert len(world.runs(node)) == 1
    assert world.fxw.spawns() == 0
