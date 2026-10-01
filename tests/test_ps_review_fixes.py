"""Regression tests for the PS review (ag-4cdd7b) — one test per finding.

The findings and their PS ids:
  1  PS-R4b  recovery matches the EXECUTION CONTEXT of the block, taken from
             the executor the question is about, not from the entry
  2  PS-R5   a dependent projection inherits the shared payload's age anchor
             (RM-R4b) instead of resetting it
  3  PS-R4a  recovery judges a cooldown's cause on the LIVE entry, inside the
             transaction, not on the snapshot taken before the probe
  4  PS-R4a  a member under a quota block gains an auth block that PRESERVES
             the quota one; recovery restores it until its original expiry
  5  PS-R6   steer — and every relaunch path behind `_launch` — is admitted
             by the allowlist like a fresh start
  6  PS-R7a  a deferred restart is held to the destination provider the entry
             recorded
  7  PS-R5   the cache holds the budget SOURCE's raw payload; a selector-less
             dependent sees the raw aggregate, not the source's projection
  8  PS-R2   docker backing and vault read the credential OWNER's
             `container_private_home`, so a dependent that declares none of
             its own still resolves to the owner's paths
  9  PS-R5   refreshing the source leaves no stale dependent projections —
             projections are never cached
  10 PS-R6   catalog retention shape-checks retained entries before reading
             them
  11 PS-R10  an explicitly written `budget_windows: null` is a config error

Read with `context/specs/ps-provider-sharing.md`; the seams are the PS
suite's (see its header).
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c1_harness as c1  # noqa: E402
import c2_harness as c2  # noqa: E402
import c3_harness as h  # noqa: E402
from multiagents import budget as budget_mod  # noqa: E402
from multiagents import cli, scripts  # noqa: E402
from multiagents.config import AgentSpec  # noqa: E402
from multiagents.executor.local import LocalExecutor  # noqa: E402
from multiagents.paths import ProjectPaths  # noqa: E402
from multiagents.providers import load_providers  # noqa: E402
from multiagents.runner import Run  # noqa: E402
from multiagents.tree import now as tree_now  # noqa: E402
from test_conversation_provider_change import _calls, _fake_cli, _flag  # noqa: E402

AUTH_MARKER = "Error: invalid api key\n"
CONFIG_ERROR = ValueError


@pytest.fixture(autouse=True)
def _clean_budget_cache():
    budget_mod.invalidate_cache()
    yield
    budget_mod.invalidate_cache()


# ---------------------------------------------------------------------------
# small helpers, in the PS suite's shape
# ---------------------------------------------------------------------------

def _local():
    return LocalExecutor()


def _lines(path: Path) -> int:
    return len(path.read_text().splitlines()) if path.exists() else 0


def _run_start(runner, agent, task="work", **kwargs):
    async def go():
        try:
            result = await runner.start(agent, task, **kwargs)
        except Exception as exc:
            return {"raised": exc, "error": str(exc)}
        run = runner.runs.get(result.get("agent_id"))
        if run:
            try:
                await asyncio.wait_for(run.done.wait(), 20)
            except (asyncio.TimeoutError, TimeoutError):
                pass
        return result
    return asyncio.run(go())


def _group_runner(tmp_path, monkeypatch, *, failing="dep", threshold=1,
                  project=None):
    """`own` (owner) + `dep` (auth_from own), with a check script whose
    verdict is a file and whose runs are counted. `worker` runs on dep,
    `ownerworker` on own."""
    ok_file = tmp_path / "logged-in"
    checks = tmp_path / "checks"
    if failing == "own":
        own = h.fake_cli(tmp_path, "own", exit_code=1, stderr=AUTH_MARKER)
        dep, dep_probe = _fake_cli(tmp_path, "dep")
        own_probe = None
    else:
        own, own_probe = _fake_cli(tmp_path, "own")
        dep = h.fake_cli(tmp_path, "dep", exit_code=1, stderr=AUTH_MARKER)
        dep_probe = None
    own["script"] = "ps-own.sh"
    dep["auth_from"] = "own"
    runner = h.make_runner(
        tmp_path / "proj", monkeypatch,
        agents={"worker": AgentSpec("worker", "dep", "m"),
                "ownerworker": AgentSpec("ownerworker", "own", "m")},
        providers={"own": own, "dep": dep},
        project={"limits": {"provider_failure_threshold": threshold,
                            "provider_probe_seconds": 0},
                 **(project or {})})
    body = (f'check) echo x >> "{checks}"; '
            f'if [ -f "{ok_file}" ]; then echo ok; exit 0; fi; '
            'echo "not logged in"; exit 10 ;;')
    c2.case_script(runner.paths.config, "ps-own.sh", body)
    return SimpleNamespace(runner=runner, tree=runner.tree, ok_file=ok_file,
                           checks=checks, own_probe=own_probe,
                           dep_probe=dep_probe)


def _auth_blocked(entry) -> bool:
    return bool(entry) and entry.get("cause") == "auth" \
        and entry.get("needs_login") is True


def _docker_run(runner, node_id="ag-ctx01", agent="worker"):
    """A failed run whose spec pinned the docker executor: the breaker must
    judge it in the container context."""
    return Run(node_id=node_id, provider=runner.providers["dep"],
               spec=AgentSpec(agent, "dep", "m", executor="docker"),
               handle=SimpleNamespace(stderr_tail=""),
               supervisor=SimpleNamespace(steps=1))


# ===========================================================================
# Finding 1 — PS-R4b: recovery matches the execution context
# ===========================================================================

def test_1_a_block_from_the_container_context_survives_a_passing_host_check(
        tmp_path, monkeypatch):
    """The failure was observed through the failed run's own (docker)
    executor, and the block records that context. The host's login is then
    repaired but the container's is not: the recovery probe asks the
    container's login — the one the block is about — and must not clear the
    block on the strength of the host's. (Edited by the opus PS run: the
    probe now runs in the block's context, review ag-3644ef finding 4, so
    the check script answers per executor.)"""
    g = _group_runner(tmp_path, monkeypatch)
    runner = g.runner
    c2.case_script(runner.paths.config, "ps-own.sh",
                   f'check) echo x >> "{g.checks}"; '
                   f'if [ -f "{g.ok_file}" ] && '
                   '[ "$MULTIAGENTS_EXECUTOR" = local ]; then echo ok; exit 0; fi; '
                   'echo "not logged in"; exit 10 ;;')
    status, _ = asyncio.run(runner._provider_health_after(
        _docker_run(runner), "failed", "", AUTH_MARKER))

    entry = g.tree.cooldown("dep")
    assert _auth_blocked(entry), entry
    assert entry.get("context") == "container", entry
    member = g.tree.cooldown("own")
    assert _auth_blocked(member) and member.get("context") == "container", \
        "the group is marked in the same context"

    # The host login is repaired, and a start runs the recovery probe: it
    # asks the container, which is still signed out, so the block survives.
    g.ok_file.write_text("yes")
    result = _run_start(runner, "ownerworker")
    assert _lines(g.checks) >= 1, "control: the probe ran"
    assert _auth_blocked(g.tree.cooldown("dep")), g.tree.cooldown("dep")
    assert _auth_blocked(g.tree.cooldown("own")), g.tree.cooldown("own")
    assert result.get("deferred") or result.get("error"), result


def test_1_a_matching_context_recovers(tmp_path, monkeypatch):
    """The other half of R4b: with the project executor in the same context
    as the recorded block, the probe's pass clears the group."""
    g = _group_runner(tmp_path, monkeypatch,
                      project={"executor": {"kind": "docker"}})
    runner = g.runner
    asyncio.run(runner._provider_health_after(
        _docker_run(runner), "failed", "", AUTH_MARKER))
    assert _auth_blocked(g.tree.cooldown("dep"))

    g.ok_file.write_text("yes")
    _run_start(runner, "ownerworker")   # the recovery probe runs pre-launch
    assert g.tree.cooldown("dep") is None, "matching context recovers"
    assert g.tree.cooldown("own") is None, "the group recovers with it"


# ===========================================================================
# Finding 2 — PS-R5/RM-R4b: a projection inherits the payload's age
# ===========================================================================

class Quota:
    """A budget script that counts its runs and prints `payload` (as in the
    PS suite), under a clock the test drives."""

    def __init__(self, tmp_path, monkeypatch, raw, payload):
        self.cfg = tmp_path / "cfg"
        self.count = tmp_path / "budget-runs"
        self.payload_file = tmp_path / "payload.json"
        self.clock = [1_000_000.0]
        monkeypatch.setattr(budget_mod, "time",
                            types.SimpleNamespace(time=lambda: self.clock[0]))
        self.set(payload)
        c2.case_script(self.cfg, "ps-src.sh",
                       f'budget) echo x >> "{self.count}"; '
                       f'cat "{self.payload_file}"; exit 0 ;;')
        self.providers = load_providers(raw)
        self.tmp = tmp_path

    def set(self, payload):
        self.payload_file.write_text(json.dumps(payload))

    def read(self, **kwargs):
        return budget_mod.read_all(self.providers, lambda n: _local(),
                                   self.tmp / "g", self.cfg, **kwargs)

    @property
    def runs(self):
        return _lines(self.count)


def _pair_raw(own_extra=None, dep_extra=None):
    own = {"bin": "src", "script": "ps-src.sh"}
    own.update(own_extra or {})
    dep = {"bin": "dep", "budget_from": "own"}
    dep.update(dep_extra or {})
    return {"own": own, "dep": dep}


def test_2_a_dependent_projection_keeps_the_shared_payloads_age(
        tmp_path, monkeypatch):
    """The payload was 90 s old when the source read it, and the bound is
    100 s. The DEPENDENT's first read, 20 s later, must serve a 110 s-old
    reading — stale — not a freshly-birthed 90 s one: deriving a projection
    resets no clock (review finding 2)."""
    q = Quota(tmp_path, monkeypatch, _pair_raw(), _payload(stale_seconds=90))
    only_owner = {"own": q.providers["own"]}
    first = budget_mod.read_all(only_owner, lambda n: _local(),
                                q.tmp / "g", q.cfg, max_reading_age=100)
    assert first["own"].stale is False, "control: fresh at read time"
    q.clock[0] += 20
    got = q.read(max_reading_age=100)
    assert got["own"].stale is True, "the aged cache hit stays stale"
    assert got["dep"].stale is True, "the projection inherits the age"
    assert q.runs == 1, "no second fetch: the cached payload aged in place"


# ===========================================================================
# Finding 3 — PS-R4a: recovery judges the LIVE entry, not the snapshot
# ===========================================================================

def test_3_recovery_does_not_delete_a_block_written_while_the_check_ran(
        tmp_path, monkeypatch):
    """The snapshot says `dep`'s block is an auth one; while the owner's
    check runs, a quota cooldown lands on `dep`. The success clears auth
    blocks — it must leave the new quota block standing."""
    g = _group_runner(tmp_path, monkeypatch)
    runner, tree = g.runner, g.tree
    tree.note_run_outcome("own", ok=False, threshold=1, kind="failed",
                          reason="failed")
    tree.set_cooldown("own", tree_now() + 6000, "own not authenticated",
                      needs_login=True, cause="auth", context="host")
    tree.set_cooldown("dep", tree_now() + 6000, "dep not authenticated",
                      needs_login=True, cause="auth", context="host")
    g.ok_file.write_text("yes")                     # the check will pass
    cooldowns = tree.read()["cooldowns"]            # snapshot: both auth

    def probe(name, executor=None):
        # Lands between the snapshot and the recovery, as a concurrent
        # writer's would.
        tree.set_cooldown("dep", tree_now() + 3600, "out of quota",
                          cause="quota")
        return True

    monkeypatch.setattr(runner, "_auth_ok", probe)
    budgets = {"own": budget_mod.Budget("own", known=True, headroom=1.0),
               "dep": budget_mod.Budget("dep", known=True, headroom=1.0)}
    runner._half_open(budgets, cooldowns)
    live = tree.cooldown("dep")
    assert live is not None and live.get("cause") == "quota", live
    assert tree.cooldown("own") is None, "the auth block that was answered"


# ===========================================================================
# Finding 4 — PS-R4a: a quota block and an auth block coexist
# ===========================================================================

def test_4_an_auth_block_survives_a_members_expiring_quota_block(
        tmp_path, monkeypatch):
    """The dependent is quota-cooling when the group's login dies. It must
    gain an auth block that OUTLIVES its quota timer — with the quota block
    preserved under it — or it looks healthy to `_half_open` the moment the
    quota timer runs out."""
    g = _group_runner(tmp_path, monkeypatch, failing="own")
    runner, tree = g.runner, g.tree
    tree.set_cooldown("dep", time.time() + 2.0, "out of quota", cause="quota")
    _run_start(runner, "ownerworker")    # the owner's login is rejected
    entry = tree.cooldown("dep")
    assert entry is not None, "the dependent must not look healthy"
    assert _auth_blocked(entry), entry
    assert (entry.get("also_quota") or {}).get("cause") == "quota", entry

    # The preserved quota block's own expiry passes: the auth block holds.
    time.sleep(2.0)
    assert _auth_blocked(tree.cooldown("dep")), (
        "the dependent must still be blocked after the quota timer ran out")

    g.ok_file.write_text("yes")
    _run_start(runner, "worker")         # a start runs the recovery probe
    assert tree.cooldown("own") is None, "the authentication block cleared"
    assert tree.cooldown("dep") is None, (
        "recovery clears the auth half; the preserved quota block had "
        "already expired, so nothing is restored")


def test_4_recovery_restores_the_preserved_quota_block_until_its_expiry(
        tmp_path, monkeypatch):
    g = _group_runner(tmp_path, monkeypatch, failing="own")
    runner, tree = g.runner, g.tree
    tree.set_cooldown("dep", time.time() + 3600, "out of quota", cause="quota")
    _run_start(runner, "ownerworker")
    assert _auth_blocked(tree.cooldown("dep"))
    g.ok_file.write_text("yes")
    _run_start(runner, "worker")
    live = tree.cooldown("dep")
    assert live is not None and live.get("cause") == "quota", live
    assert live["until"] > time.time() + 3000, "until its ORIGINAL expiry"


# ===========================================================================
# Finding 5 — PS-R6: steer and the relaunch paths are admitted
# ===========================================================================

def _wait_for(predicate, seconds=10.0):
    end = time.time() + seconds
    while time.time() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    return predicate()


def _long_running_cli(tmp_path, name):
    """A CLI that reports its session at once and then stays alive, so a
    steer finds a live, resumable run."""
    script = tmp_path / f"{name}-cli"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys, time\n"
        "print(json.dumps({'type': 'text', 'text': 'working', "
        "'session': 'sess-live'}), flush=True)\n"
        "time.sleep(30)\n")
    script.chmod(script.stat().st_mode | 0o111)
    return {"bin": str(script),
            "spawn": {"args": ["--provider", name, "--model", "{model}"],
                      "resume": ["--resume", "{session_id}"]},
            "stream": {"format": "ndjson", "session_id_paths": ["session"],
                       "rules": [{"match": {"type": "text"}, "as": "text",
                                  "fields": {"text": "text"}}]}}


def test_5_steering_refuses_to_relaunch_a_model_the_provider_now_refuses(
        tmp_path, monkeypatch):
    gem = _long_running_cli(tmp_path, "gem")
    runner = h.make_runner(
        tmp_path / "proj", monkeypatch,
        agents={"worker": AgentSpec("worker", "gem", "claude-x")},
        providers={"gem": gem})

    async def go():
        started = await runner.start("worker", "work")
        agent_id = started.get("agent_id")
        assert agent_id, started
        node = runner.tree.get(agent_id)
        assert node.status == "running", started
        deadline = time.time() + 5
        while time.time() < deadline and not runner.tree.get(agent_id).session_id:
            await asyncio.sleep(0.05)      # the stream reports its session
        node = runner.tree.get(agent_id)
        assert node.session_id, "control: the run is resumable"
        live_run = runner.runs.get(agent_id)
        # The roster narrows under the live run.
        runner.providers["gem"].models_include = ["gemini-*"]
        result = await runner.steer(agent_id, "new instructions")
        await runner.stop(agent_id)
        return agent_id, live_run, result

    agent_id, live_run, result = asyncio.run(go())
    assert result.get("steered") is False, result
    assert "claude-x" in result["error"], result
    assert "allow" in result["error"], result
    assert runner.runs.get(agent_id) is live_run, (
        "the live run was left untouched — not stopped and relaunched")


def test_5_no_launch_path_runs_a_model_the_provider_does_not_allow(
        tmp_path, monkeypatch):
    """`_launch` is the belt under every admission check: the free retry
    and the commit-fix turn pass through it, and so does a first launch.
    (Edited by the opus PS run: the glm version exempted first launches, the
    gap review ag-2f0d3e finding 3 and ag-3644ef finding 1 walked through.)"""
    gem, _ = _fake_cli(tmp_path, "gem")
    runner = h.make_runner(
        tmp_path / "proj", monkeypatch,
        agents={"worker": AgentSpec("worker", "gem", "claude-x")},
        providers={"gem": gem})
    runner.providers["gem"].models_include = ["gemini-*"]
    spec = AgentSpec("worker", "gem", "claude-x")
    # A run already admitted onto this node: what a retry or a fix turn
    # relaunches.
    runner.runs["ag-belt01"] = Run(node_id="ag-belt01",
                                   provider=runner.providers["gem"],
                                   spec=spec)
    for _ in ("relaunch", "first launch"):
        with pytest.raises(RuntimeError) as caught:
            asyncio.run(runner._launch(
                node_id="ag-belt01", spec=spec,
                provider=runner.providers["gem"], prompt="x",
                workdir=tmp_path / "wt", branch="", parent=None, depth=1))
        assert "claude-x" in str(caught.value), str(caught.value)
        assert "allow" in str(caught.value), str(caught.value)
        runner.runs.clear()


# ===========================================================================
# Finding 6 — PS-R7a: the recorded destination is honoured at restart
# ===========================================================================

class Queue:
    """gem (gemini-*) and partner (claude-*), roomy budgets, a `routed`
    agent with a partner route — the PS suite's deferred-queue shape."""

    def __init__(self, tmp_path, monkeypatch):
        gem, self.gem_probe = _fake_cli(tmp_path, "gem")
        partner, self.partner_probe = _fake_cli(tmp_path, "partner")
        gem["models_include"] = ["gemini-*"]
        partner["models_include"] = ["claude-*", "gpt-*"]
        partner["family"] = "partner"
        monkeypatch.setattr(budget_mod, "read_all", lambda *a, **kw: {
            n: budget_mod.Budget(n, known=True, headroom=1.0)
            for n in ("gem", "partner")})
        self.runner = h.make_runner(
            tmp_path / "proj", monkeypatch,
            agents={"routed": AgentSpec.from_dict("routed", {
                "provider": "gem", "model": "gemini-a",
                "models": {"partner": "claude-x"}})},
            providers={"gem": gem, "partner": partner},
            project={"budget": {"blind_cooldown_seconds": 1}})
        self.tree = self.runner.tree

    def queue(self, spec_extra):
        spec = {"agent": "routed", "task": "work", "timeout": None,
                "model": None, "workdir": None}
        spec.update(spec_extra)
        return self.tree.defer(spec, time.time() - 1.0, "quota")["id"]

    def drain(self):
        async def go():
            result = await self.runner.resume_deferred()
            for entry in result.get("restarted") or []:
                run = self.runner.runs.get(entry.get("agent_id"))
                if run:
                    await asyncio.wait_for(run.done.wait(), 20)
            return result
        return asyncio.run(go())


def test_6_a_deferred_restart_is_refused_when_the_recorded_destination_rejects_the_pin(
        tmp_path, monkeypatch):
    q = Queue(tmp_path, monkeypatch)
    df = q.queue({"model": "claude-x", "provider": "gem"})
    result = q.drain()
    entry = next((d for d in q.tree.read()["deferred"] if d["id"] == df), {})
    assert entry.get("status") == "refused", (entry, result)
    for needle in ("gem", "claude-x", "partner"):
        assert needle in entry.get("reason", ""), (needle, entry)
    assert not _calls(q.gem_probe) and not _calls(q.partner_probe), \
        "no silent re-route to the provider that accepts it"
    assert result.get("refused"), result


def test_6_a_deferred_restart_on_a_destination_that_still_allows_restarts(
        tmp_path, monkeypatch):
    q = Queue(tmp_path, monkeypatch)
    q.queue({"model": "gemini-b", "provider": "gem"})
    result = q.drain()
    assert len(result["restarted"]) == 1, result
    assert _wait_for(lambda: _calls(q.gem_probe)), "the restart launched on gem"
    assert not _calls(q.partner_probe)
    assert _flag(_calls(q.gem_probe)[0], "--model") == "gemini-b"


# ===========================================================================
# Finding 7 — PS-R5: a selector-less dependent sees the RAW aggregate
# ===========================================================================

def _windows(gem_headroom, third_headroom):
    return {"gemini-weekly": {"headroom": gem_headroom,
                              "percent": round((1 - gem_headroom) * 100, 1),
                              "resets_at": "2099-01-01T00:00:00Z"},
            "3p-5h": {"headroom": third_headroom,
                      "percent": round((1 - third_headroom) * 100, 1),
                      "resets_at": "2099-01-02T00:00:00Z"}}


def _payload(*, stale_seconds=None):
    out = {"known": True, "headroom": 0.5, "resets_at": "2099-01-01T00:00:00Z",
           "windows": _windows(0.5, 0.5)}
    if stale_seconds is not None:
        out["stale_seconds"] = stale_seconds
    return out


def test_7_a_selector_less_dependent_reads_the_raw_aggregate(
        tmp_path, monkeypatch):
    """The source projects itself through its own selector; the dependent
    without a selector must still see the RAW payload — its headroom, its
    windows, exactly as the script printed them."""
    payload = {"known": True, "headroom": 0.0,
               "resets_at": "2099-01-01T00:00:00Z",
               "windows": _windows(0.1, 0.0)}
    q = Quota(tmp_path, monkeypatch,
              _pair_raw(own_extra={"budget_windows": ["gem*"]}), payload)
    got = q.read()
    assert got["own"].headroom == pytest.approx(0.1), "the source's projection"
    assert got["dep"].headroom == pytest.approx(0.0), "the raw aggregate"
    assert got["dep"].resets_at == "2099-01-01T00:00:00Z", "the raw reset"
    for key, detail in got["dep"].windows.items():
        assert "counted" not in detail, (
            f"{key}: a selector-less dependent's windows are the payload's "
            f"own, not the source's projection: {detail}")


# ===========================================================================
# Finding 8 — PS-R2: the owner's declaration decides the backing paths
# ===========================================================================

def test_8_a_dependent_without_its_own_private_home_resolves_to_the_owners(
        tmp_path):
    raw = {"own": {"bin": "ownbin", "container_private_home": [".acct"]},
           "dep": {"bin": "depbin", "auth_from": "own"}}
    providers = load_providers(raw)
    ex = c1.make_docker_executor(tmp_path, providers)
    owner_backing = ex.private_state("own")
    assert owner_backing, "control: the owner has a backing"
    assert ex.private_state("dep") == owner_backing, "PS-R2: the owner's paths"
    assert ex.vault_state("dep"), "the dependent resolves to the owner's vault"
    assert set(ex.vault_state("dep").values()) == \
        set(ex.vault_state("own").values())
    assert len(set(ex.private_state().values())) == 1

    env = scripts.build_env("dep", providers["dep"], ex)
    assert env.get("MULTIAGENTS_PRIVATE_BACKING") == \
        str(next(iter(owner_backing.values()))), \
        "the dependent's actions are handed the owner's backing"


# ===========================================================================
# Finding 9 — PS-R5: refreshing the source refreshes the dependents
# ===========================================================================

def test_9_refreshing_the_source_leaves_no_stale_dependent_projection(
        tmp_path, monkeypatch):
    q = Quota(tmp_path, monkeypatch,
              _pair_raw(own_extra={"budget_windows": ["gem*"]},
                        dep_extra={"budget_windows": ["3p-*"]}),
              {"known": True, "headroom": 0.0,
               "resets_at": "2099-01-01T00:00:00Z",
               "windows": _windows(0.5, 0.0)})
    assert q.read()["dep"].headroom == pytest.approx(0.0), "control: exhausted"
    q.set({"known": True, "headroom": 0.9, "resets_at": "2099-01-01T00:00:00Z",
           "windows": _windows(0.9, 0.9)})
    got = budget_mod.read_all({"own": q.providers["own"]}, lambda n: _local(),
                              q.tmp / "g", q.cfg, use_cache=False, force=True)
    assert got["own"].headroom == pytest.approx(0.9), "the source refreshed"
    dep = q.read()["dep"]
    assert dep.headroom == pytest.approx(0.9), (
        "the dependent reads the refreshed payload, not a stale projection")
    assert q.runs == 2, "and the dependent's read fetched nothing"


# ===========================================================================
# Finding 10 — PS-R6: catalog retention shape-checks entries
# ===========================================================================

def test_10_a_malformed_retained_entry_is_dropped_not_fatal(
        tmp_path, monkeypatch, capsys):
    root = tmp_path / "proj"
    paths = ProjectPaths(root)
    paths.ensure()
    (paths.config / "providers").mkdir(parents=True, exist_ok=True)
    (paths.config / "providers.yaml").write_text(yaml.safe_dump({"providers": {
        "gem": {"bin": "sh", "script": "ps-models.sh", "models_parse": "tsv",
                "models_include": ["gemini-*"], "spawn": {"args": ["x"]}}}}))
    c2.case_script(paths.config, "ps-models.sh",
                   "models) echo 'listing broke' >&2; exit 1 ;;")
    (paths.config / "models.yaml").write_text(yaml.safe_dump({"models": {
        "gem": ["garbage-not-a-model", {"id": "claude-x"}, {"id": "gemini-a"}]}}))
    cli.main(["--path", str(root), "refresh-models"])
    capsys.readouterr()
    models = yaml.safe_load(
        (paths.config / "models.yaml").read_text())["models"]
    assert [m["id"] for m in models["gem"]] == ["gemini-a"], models["gem"]


# ===========================================================================
# Finding 11 — PS-R10: an explicit `budget_windows: null` is an error
# ===========================================================================

def test_11_an_explicit_budget_windows_null_is_a_config_error():
    with pytest.raises(CONFIG_ERROR) as caught:
        load_providers({"own": {"bin": "o", "budget_windows": None}})
    message = str(caught.value)
    assert "own" in message and "budget_windows" in message, message


def test_11_an_absent_or_empty_budget_windows_still_loads():
    assert "a" in load_providers({"a": {"bin": "a"}})
    assert "b" in load_providers({"b": {"bin": "b", "budget_windows": []}})
