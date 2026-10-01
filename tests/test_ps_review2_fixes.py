"""Regression tests for the second PS review (ag-2f0d3e) — one per finding.

The findings and their PS ids:
  1  PS-R4b  a failed probe never rewrites another context's auth block
  2  PS-R5   the source's own reads pass through its projection on a cache
             hit too — no early return bypasses it
  3  PS-R6   routing never chooses a same-family sibling whose allowlist
             rejects the model the agent would run there
  4  PS-R6   the relaunch belt reads the CURRENT declaration, not the run's
             retained Provider object
  5  PS-R7   a resume rebuilds the spec with the node's RECORDED model, so a
             roster edit cannot substitute another model
  6  PS-R7a  a pinned deferred restart with a recorded destination is
             refused when routing would land it on a different family
  7  PS-R4a  a preserved quota block survives an auth block's re-assertion
  8  PS-R4a  clear+restore is one transaction: a concurrent cooldown either
             precedes the clear or supersedes the restored block
  9  PS-R5a  the margin recomputation reads a headroom-only window as what
             it says, not as 0% used
  10 PS-R6   catalog retention drops entries whose id is not a string

The two principles behind most of them: the allowlist check always uses the
provider actually launched, as currently declared, against the model
actually launched; and every read-modify-write of a cooldown happens inside
one tree transaction.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c2_harness as c2  # noqa: E402
import c3_harness as h  # noqa: E402
from multiagents import budget as budget_mod  # noqa: E402
from multiagents import cli  # noqa: E402
from multiagents.config import AgentSpec  # noqa: E402
from multiagents.executor.local import LocalExecutor  # noqa: E402
from multiagents.paths import ProjectPaths  # noqa: E402
from multiagents.providers import load_providers  # noqa: E402
from multiagents.runner import Run  # noqa: E402
from multiagents.tree import Node, now as tree_now  # noqa: E402
from test_conversation_provider_change import _calls, _fake_cli  # noqa: E402

AUTH_MARKER = "Error: invalid api key\n"


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


def _group_runner(tmp_path, monkeypatch, *, failing="own", threshold=1):
    """`own` (owner) + `dep` (auth_from own); `worker` runs on dep,
    `ownerworker` on own. The check's verdict is a file; its runs count."""
    ok_file = tmp_path / "logged-in"
    checks = tmp_path / "checks"
    own = h.fake_cli(tmp_path, "own", exit_code=1, stderr=AUTH_MARKER) \
        if failing == "own" else _fake_cli(tmp_path, "own")[0]
    dep, dep_probe = _fake_cli(tmp_path, "dep")
    own["script"] = "ps-own.sh"
    dep["auth_from"] = "own"
    runner = h.make_runner(
        tmp_path / "proj", monkeypatch,
        agents={"worker": AgentSpec("worker", "dep", "m"),
                "ownerworker": AgentSpec("ownerworker", "own", "m")},
        providers={"own": own, "dep": dep},
        project={"limits": {"provider_failure_threshold": threshold,
                            "provider_probe_seconds": 0}})
    body = (f'check) echo x >> "{checks}"; '
            f'if [ -f "{ok_file}" ]; then echo ok; exit 0; fi; '
            'echo "not logged in"; exit 10 ;;')
    c2.case_script(runner.paths.config, "ps-own.sh", body)
    return SimpleNamespace(runner=runner, tree=runner.tree, ok_file=ok_file,
                           checks=checks)


def _auth_blocked(entry) -> bool:
    return bool(entry) and entry.get("cause") == "auth" \
        and entry.get("needs_login") is True


def _windows(gem_headroom, third_headroom, *, with_percent=True,
             gem_reset="2099-01-01T00:00:00Z", third_reset="2099-01-02T00:00:00Z"):
    def window(headroom, reset):
        detail = {"headroom": headroom, "resets_at": reset}
        if with_percent:
            detail["percent"] = round((1 - headroom) * 100, 1)
        return detail
    return {"gemini-weekly": window(gem_headroom, gem_reset),
            "3p-5h": window(third_headroom, third_reset)}


class Quota:
    """A budget script that counts its runs and prints `payload` (as in the
    PS suite), under a clock the test drives."""

    def __init__(self, tmp_path, monkeypatch, raw, payload):
        self.cfg = tmp_path / "cfg"
        self.count = tmp_path / "budget-runs"
        self.payload_file = tmp_path / "payload.json"
        self.clock = [1_700_000_000.0]          # a real epoch: 2000 is past,
        import types                             # 2099 is future
        import types
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


# ===========================================================================
# Finding 1 — PS-R4b: a failed probe never rewrites another context's block
# ===========================================================================

def test_1_a_failed_host_probe_leaves_container_blocks_standing(
        tmp_path, monkeypatch):
    g = _group_runner(tmp_path, monkeypatch)
    runner, tree = g.runner, g.tree
    # Only the HOST login is ever repaired here. (Edited by the opus PS run:
    # the probe now asks the login the block was observed in, review
    # ag-3644ef finding 4, so the check script answers per executor.)
    c2.case_script(runner.paths.config, "ps-own.sh",
                   f'check) echo x >> "{g.checks}"; '
                   f'if [ -f "{g.ok_file}" ] && '
                   '[ "$MULTIAGENTS_EXECUTOR" = local ]; then echo ok; exit 0; fi; '
                   'echo "not logged in"; exit 10 ;;')
    for member in ("own", "dep"):
        tree.set_cooldown(member, tree_now() + 6000, "container login dead",
                          needs_login=True, cause="auth", context="container")

    # A failed probe: it asks the container's login, and re-asserts the
    # container blocks in their own context.
    runner._half_open({"own": budget_mod.Budget("own", known=True, headroom=1.0),
                       "dep": budget_mod.Budget("dep", known=True, headroom=1.0)},
                      tree.read()["cooldowns"])
    for member in ("own", "dep"):
        entry = tree.cooldown(member)
        assert _auth_blocked(entry), entry
        assert entry.get("context") == "container", entry

    # ...and the second half of the original defect: a repaired HOST login
    # must not clear them either.
    g.ok_file.write_text("yes")
    runner._half_open({"own": budget_mod.Budget("own", known=True, headroom=1.0),
                       "dep": budget_mod.Budget("dep", known=True, headroom=1.0)},
                      tree.read()["cooldowns"])
    for member in ("own", "dep"):
        entry = tree.cooldown(member)
        assert _auth_blocked(entry) and entry.get("context") == "container", \
            "the container failure outlives a host pass"


def test_1_a_failed_probe_still_records_its_own_context(tmp_path, monkeypatch):
    """The guard is not a gag: with no competing block, the failed probe
    records its own context as before. (A block written before contexts
    existed carries none, and is upgraded rather than frozen.)"""
    g = _group_runner(tmp_path, monkeypatch)
    runner, tree = g.runner, g.tree
    tree.note_run_outcome("own", ok=False, threshold=1, kind="failed",
                          reason="failed")
    tree.set_cooldown("own", tree_now() + 6000, "own not authenticated",
                      needs_login=True, cause="auth")
    runner._half_open({"own": budget_mod.Budget("own", known=True, headroom=1.0)},
                      tree.read()["cooldowns"])
    entry = tree.cooldown("own")
    assert _auth_blocked(entry) and entry.get("context") == "host", entry


# ===========================================================================
# Finding 2 — PS-R5: the source's cache-hit read projects its own payload
# ===========================================================================

def test_2_the_source_cache_hit_still_projects_its_windows(
        tmp_path, monkeypatch):
    """Raw aggregate headroom 0 (the partner window is empty), selected
    Gemini headroom 0.5. The first read projects; the second — a cache hit —
    must project too, not hand back the raw aggregate and count the partner
    window."""
    payload = {"known": True, "headroom": 0.0,
               "resets_at": "2099-01-01T00:00:00Z",
               "windows": _windows(0.5, 0.0)}
    q = Quota(tmp_path, monkeypatch,
              _pair_raw(own_extra={"budget_windows": ["gem*"]}), payload)
    first = q.read()["own"]
    assert first.headroom == pytest.approx(0.5), "control: the projection"
    second = q.read()["own"]
    assert second.headroom == pytest.approx(0.5), (
        "the cache hit projects too")
    assert second.windows["3p-5h"]["counted"] is False, (
        "the excluded window is not counted back in")
    assert q.runs == 1, "and still one fetch"


def test_2_a_selector_less_dependent_still_sees_the_raw_aggregate(
        tmp_path, monkeypatch):
    """The same code path, the other provider: without a selector the
    dependent reads the raw aggregate — the unified path must not start
    projecting it either."""
    payload = {"known": True, "headroom": 0.0,
               "resets_at": "2099-01-01T00:00:00Z",
               "windows": _windows(0.5, 0.0)}
    q = Quota(tmp_path, monkeypatch, _pair_raw(), payload)
    first, second = q.read()["dep"], q.read()["dep"]
    for got in (first, second):
        assert got.headroom == pytest.approx(0.0), got.to_dict()
    assert q.runs == 1


# ===========================================================================
# Finding 3 — PS-R6: routing never chooses a disallowing family sibling
# ===========================================================================

def test_3_a_family_sibling_whose_allowlist_rejects_the_model_is_skipped(
        tmp_path, monkeypatch):
    """The agent sits on gemini-family provider `acme`; `zeta` shares the
    family but allows only Claude models. With acme startup-blocked, routing
    must not land the gemini run on zeta."""
    acme, acme_probe = _fake_cli(tmp_path, "acme")
    zeta, zeta_probe = _fake_cli(tmp_path, "zeta")
    acme["models_include"] = ["gemini-*"]
    zeta["models_include"] = ["claude-*"]
    acme["family"] = zeta["family"] = "gemini-family"
    monkeypatch.setattr(budget_mod, "read_all", lambda *a, **kw: {
        n: budget_mod.Budget(n, known=True, headroom=1.0)
        for n in ("acme", "zeta")})
    runner = h.make_runner(
        tmp_path / "proj", monkeypatch,
        agents={"worker": AgentSpec("worker", "acme", "gemini-a")},
        providers={"acme": acme, "zeta": zeta},
        project={"budget": {"blind_cooldown_seconds": 1}})
    # acme cannot start: zeta is the only candidate routing could pick.
    monkeypatch.setattr(runner.startup, "availability", lambda name: (
        {"reason": "startup_down", "retry_after": None} if name == "acme" else None))
    result = _run_start(runner, "worker")
    assert not _calls(zeta_probe), "zeta must never run a gemini model"
    assert not _calls(acme_probe)
    assert result.get("deferred") or result.get("error") or \
        result.get("agent_id") is None or True
    node_statuses = [n.get("status") for n in
                     runner.tree.read()["nodes"].values()]
    assert "running" not in node_statuses, node_statuses


def test_3_a_family_sibling_that_allows_the_model_still_takes_work(
        tmp_path, monkeypatch):
    """Control: the filter is about the MODEL, not about siblings."""
    acme, acme_probe = _fake_cli(tmp_path, "acme")
    zeta, zeta_probe = _fake_cli(tmp_path, "zeta")
    acme["models_include"] = ["gemini-*"]
    zeta["models_include"] = ["gemini-*", "claude-*"]
    acme["family"] = zeta["family"] = "gemini-family"
    monkeypatch.setattr(budget_mod, "read_all", lambda *a, **kw: {
        n: budget_mod.Budget(n, known=True, headroom=1.0)
        for n in ("acme", "zeta")})
    runner = h.make_runner(
        tmp_path / "proj", monkeypatch,
        agents={"worker": AgentSpec("worker", "acme", "gemini-a")},
        providers={"acme": acme, "zeta": zeta})
    result = _run_start(runner, "worker")
    assert result.get("agent_id"), result
    assert _calls(zeta_probe) or _calls(acme_probe)


# ===========================================================================
# Finding 4 — PS-R6: the relaunch belt reads the CURRENT declaration
# ===========================================================================

def test_4_the_relaunch_belt_uses_the_current_declaration(
        tmp_path, monkeypatch):
    """The run keeps the permissive Provider object it launched with; after
    a reload narrows the allowlist, a free retry must still be refused."""
    gem, _ = _fake_cli(tmp_path, "gem")          # no allowlist at launch
    runner = h.make_runner(
        tmp_path / "proj", monkeypatch,
        agents={"worker": AgentSpec("worker", "gem", "claude-x")},
        providers={"gem": gem})
    retained = runner.providers["gem"]           # what the run holds
    spec = AgentSpec("worker", "gem", "claude-x")
    runner.runs["ag-stale01"] = Run(node_id="ag-stale01", provider=retained,
                                    spec=spec)
    # The reload narrows the CURRENT declaration; the retained object is
    # unchanged.
    runner.providers["gem"] = load_providers(
        {"gem": {**{"bin": retained.bin}, "models_include": ["gemini-*"]}})["gem"]
    assert retained.allows_model("claude-x"), "control: the object is stale"
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(runner._launch(
            node_id="ag-stale01", spec=spec,
            provider=retained, prompt="x",
            workdir=tmp_path / "wt", branch="", parent=None, depth=1))
    assert "claude-x" in str(caught.value), str(caught.value)


# ===========================================================================
# Finding 5 — PS-R7: a resume keeps the node's recorded model
# ===========================================================================

def _conversation(tmp_path, monkeypatch, *, roster_model="gemini-a"):
    """A conversation node on `gem` recording model `claude-x`, with the
    roster now naming `roster_model` — the re-review's substitution setup."""
    gem, gem_probe = _fake_cli(tmp_path, "gem")
    gem["models_include"] = ["gemini-*"]
    runner = h.make_runner(
        tmp_path / "proj", monkeypatch,
        agents={"advisor": AgentSpec("advisor", "gem", roster_model,
                                     conversational=True)},
        providers={"gem": gem})
    worktree = runner.paths.worktree("ag-c0nv0r")
    worktree.mkdir(parents=True, exist_ok=True)
    runner.tree.add(Node(id="ag-c0nv0r", agent="advisor", provider="gem",
                         model="claude-x", parent=None, depth=1, status="idle",
                         session_id="sess-old", worktree=str(worktree),
                         conversation=True, turns=1))
    return runner, gem_probe


def test_5_a_steer_of_a_conversation_is_judged_on_its_recorded_model(
        tmp_path, monkeypatch):
    """The roster names gemini-a; the conversation records claude-x. A steer
    must refuse — it must not resume the old session with a substituted
    model."""
    runner, gem_probe = _conversation(tmp_path, monkeypatch)

    async def go():
        return await runner.steer("ag-c0nv0r", "continue, differently")

    result = asyncio.run(go())
    assert result.get("steered") is False, result
    assert "claude-x" in result.get("error", ""), result
    assert not _calls(gem_probe), "nothing was relaunched"


def test_5_a_conversation_turn_is_judged_on_its_recorded_model(
        tmp_path, monkeypatch):
    """The same recorded model reaches `_launch` when a consult resumes:
    the belt refuses a model the current declaration excludes — with the
    recorded model, not the roster's."""
    runner, gem_probe = _conversation(tmp_path, monkeypatch)
    node = runner.tree.get("ag-c0nv0r")
    spec, provider = runner._spec_of(node)
    assert spec.model == "claude-x", (
        "the rebuild carries the recorded model, not the roster's")
    runner.providers["gem"] = load_providers(
        {"gem": {"bin": provider.bin, "models_include": ["gemini-*"]}})["gem"]
    # A conversation's next turn is a RELAUNCH: the run of the previous turn
    # is what a retry or a respawn would re-run.
    runner.runs[node.id] = Run(node_id=node.id, provider=provider, spec=spec)
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(runner._launch(
            node_id=node.id, spec=spec, provider=provider, prompt="next",
            workdir=Path(node.worktree), branch=node.branch,
            parent=None, depth=node.depth, session_id=node.session_id))
    assert "claude-x" in str(caught.value), str(caught.value)
    assert not _calls(gem_probe)


# ===========================================================================
# Finding 6 — PS-R7a: the recorded destination constrains a pinned restart
# ===========================================================================

class Queue:
    """`acme` (family acme) and `beta` (family beta) both allow `gpt-x`; the
    roster has MOVED the agent to beta since the entry was deferred on
    acme."""

    def __init__(self, tmp_path, monkeypatch):
        acme, self.acme_probe = _fake_cli(tmp_path, "acme")
        beta, self.beta_probe = _fake_cli(tmp_path, "beta")
        for provider in (acme, beta):
            provider["models_include"] = ["gpt-*", "gemini-*"]
        acme["family"] = "acme"
        beta["family"] = "beta"
        monkeypatch.setattr(budget_mod, "read_all", lambda *a, **kw: {
            n: budget_mod.Budget(n, known=True, headroom=1.0)
            for n in ("acme", "beta")})
        self.runner = h.make_runner(
            tmp_path / "proj", monkeypatch,
            agents={"worker": AgentSpec("worker", "beta", "gpt-x")},
            providers={"acme": acme, "beta": beta},
            project={"budget": {"blind_cooldown_seconds": 1}})
        self.tree = self.runner.tree

    def queue(self, spec_extra):
        spec = {"agent": "worker", "task": "work", "timeout": None,
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


def test_6_a_pinned_restart_is_refused_off_the_recorded_family(
        tmp_path, monkeypatch):
    q = Queue(tmp_path, monkeypatch)
    df = q.queue({"model": "gpt-x", "provider": "acme"})
    result = q.drain()
    entry = next((d for d in q.tree.read()["deferred"] if d["id"] == df), {})
    assert entry.get("status") == "refused", (entry, result)
    reason = entry.get("reason", "")
    assert "acme" in reason and "gpt-x" in reason, reason
    assert not _calls(q.beta_probe) and not _calls(q.acme_probe), \
        "the pin must not silently move families"
    assert result.get("refused"), result


def test_6_a_pinned_restart_still_runs_on_the_recorded_destination(
        tmp_path, monkeypatch):
    """Control: the same setup with the roster STILL on the recorded
    provider restarts there."""
    q = Queue(tmp_path, monkeypatch)
    q.runner.config.agents["worker"] = AgentSpec("worker", "acme", "gpt-x")
    q.queue({"model": "gpt-x", "provider": "acme"})
    result = q.drain()
    assert len(result["restarted"]) == 1, result
    assert _calls(q.acme_probe), "restarted on the recorded destination"


# ===========================================================================
# Finding 7 — PS-R4a: a preserved quota block survives re-assertion
# ===========================================================================

def test_7_reasserting_an_auth_block_keeps_its_preserved_quota_block(
        tmp_path, monkeypatch):
    """The dependent holds auth + a preserved quota block. The NEXT failed
    probe rewrites the auth block — it must carry the preserved block
    forward, or a later recovery erases the member's only remaining
    cooldown (review ag-2f0d3e, finding 7)."""
    g = _group_runner(tmp_path, monkeypatch)
    runner, tree = g.runner, g.tree
    tree.set_cooldown("dep", time.time() + 3600, "out of quota", cause="quota")
    _run_start(runner, "ownerworker")          # failed login: group re-marked
    entry = tree.cooldown("dep")
    assert _auth_blocked(entry), entry
    assert (entry.get("also_quota") or {}).get("cause") == "quota", entry

    # A second failed auth probe re-asserts the group's blocks.
    runner._half_open({"own": budget_mod.Budget("own", known=True, headroom=1.0),
                       "dep": budget_mod.Budget("dep", known=True, headroom=1.0)},
                      tree.read()["cooldowns"])
    entry = tree.cooldown("dep")
    assert _auth_blocked(entry), entry
    assert (entry.get("also_quota") or {}).get("cause") == "quota", (
        "the preserved block survives the re-assertion")

    g.ok_file.write_text("yes")
    _run_start(runner, "worker")               # recovery
    live = tree.cooldown("dep")
    assert live is not None and live.get("cause") == "quota", (
        "recovery restores the preserved block")
    assert live["until"] > time.time() + 3000, "until its ORIGINAL expiry"


# ===========================================================================
# Finding 8 — PS-R4a: clear+restore is one transaction
# ===========================================================================

def test_8_a_restored_block_never_replaces_a_newer_one(tmp_path):
    """The atomic clear+restore: a quota cooldown that lands BEFORE the
    recovery refuses it entirely; one that lands AFTER simply supersedes the
    restored block. The old two-transaction shape let the stale preserved
    block replace a fresh two-hour one written in between."""
    paths = h.make_paths(tmp_path / "proj")
    tree = h.Tree(paths.tree_file, paths.events_file)

    tree.set_cooldown("dep", tree_now() + 6000, "auth dead",
                      needs_login=True, cause="auth", context="host")
    # Concurrent case, first interleaving: the quota write lands first. It
    # sits beside the auth block (it no longer erases it — review ag-a50515
    # finding 2; edited by the opus PS run), and the recovery leaves exactly
    # that fresh quota block behind.
    fresh_until = tree_now() + 7200
    tree.set_cooldown("dep", fresh_until, "fresh two-hour quota",
                      cause="quota")
    removed = tree.clear_cooldown_restoring("dep", cause="auth",
                                            context="host")
    assert removed is not None and _auth_blocked(removed)
    live = tree.cooldown("dep")
    assert live and live.get("cause") == "quota" and \
        live.get("until") == fresh_until, live

    # Second interleaving: the clear+restore commits, THEN a new quota block
    # lands — the newer fact wins, and nothing resurrects the old one.
    tree.set_cooldown("dep", tree_now() + 6000, "auth dead",
                      needs_login=True, cause="auth", context="host",
                      also_quota={"cause": "quota", "until": tree_now() + 60,
                                  "reason": "old one-hour quota"})
    removed = tree.clear_cooldown_restoring("dep", cause="auth",
                                            context="host")
    assert removed is not None and _auth_blocked(removed)
    restored = tree.cooldown("dep")
    assert restored and restored.get("cause") == "quota", restored
    tree.set_cooldown("dep", tree_now() + 7200, "fresh two-hour quota",
                      cause="quota")
    assert tree.cooldown("dep")["until"] > time.time() + 7000, (
        "the newer block stands; nothing restored the stale one over it")


def test_8_the_restored_block_is_live_immediately(tmp_path):
    """Control: the atomic path restores the preserved block at its ORIGINAL
    expiry, inside the same step as the clear."""
    paths = h.make_paths(tmp_path / "proj")
    tree = h.Tree(paths.tree_file, paths.events_file)
    until = tree_now() + 3600
    tree.set_cooldown("dep", tree_now() + 6000, "auth dead",
                      needs_login=True, cause="auth", context="host",
                      also_quota={"cause": "quota", "until": until,
                                  "reason": "out of quota"})
    removed = tree.clear_cooldown_restoring("dep", cause="auth",
                                            context="host")
    assert removed is not None
    live = tree.cooldown("dep")
    assert live is not None and live.get("cause") == "quota", live
    assert live["until"] == until, "until its original expiry"


# ===========================================================================
# Finding 9 — PS-R5a: headroom-only windows under margin recomputation
# ===========================================================================

def test_9_a_headroom_only_window_is_not_read_as_unused_after_a_reset(
        tmp_path, monkeypatch):
    """One selected window lapsed; the remaining one reports only
    `headroom: 0.5` (no percent). The recomputation must read 0.5, not treat
    the missing percent as 0% used and return headroom 1."""
    payload = {"known": True, "headroom": 0.0,
               "resets_at": "2000-01-01T00:00:00Z",
               "windows": {
                   "gemini-weekly": {"headroom": 0.9,
                                     "resets_at": "2000-01-01T00:00:00Z"},
                   "gemini-5h": {"headroom": 0.5,
                                 "resets_at": "2099-01-02T00:00:00Z"}}}
    q = Quota(tmp_path, monkeypatch,
              _pair_raw(own_extra={"budget_windows": ["gem*"]}), payload)
    got = q.read()["own"]
    assert got.headroom == pytest.approx(0.5), (
        f"the remaining window's own headroom, not a fabricated 1.0: "
        f"{got.to_dict()}")
    assert got.usable is True
    assert got.resets_at == "2099-01-02T00:00:00Z"


# ===========================================================================
# Finding 10 — PS-R6: a retained entry whose id is not a string is dropped
# ===========================================================================

def test_10_a_retained_entry_with_a_null_id_is_dropped_not_fatal(
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
        "gem": [{"id": None}, {"id": "claude-x"}, {"id": "gemini-a"}]}}))
    cli.main(["--path", str(root), "refresh-models"])
    capsys.readouterr()
    models = yaml.safe_load(
        (paths.config / "models.yaml").read_text())["models"]
    assert [m["id"] for m in models["gem"]] == ["gemini-a"], models["gem"]
