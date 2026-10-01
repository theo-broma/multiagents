"""Regression tests for the third PS review (ag-3644ef), opus round.

The findings and their PS ids:
  1  PS-R7   a consult resumed with no surviving Run carries the node's
             RECORDED model, and no launch ever runs a model its provider
             excludes
  2  PS-R4b  the initiating provider's own auth write leaves another
             context's live block, and its preserved quota, standing
  3  PS-R4a  re-assertion keeps a live preserved quota even when the outer
             auth record has expired; the write judges the live entry
  4  PS-R4b  a block made under an agent's executor override is probed
             through that executor, and so can recover
  5  PS-R3   interactive setup logs in through the credential OWNER
  6  PS-R5   one fetch per shared source: concurrent misses, and a
             non-forced uncached pass
  7  PS-R1/R5 borrowing one facet from a provider that borrows the other is
             not a chain
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c2_harness as c2  # noqa: E402
import c3_harness as h  # noqa: E402
from multiagents import budget as budget_mod  # noqa: E402
from multiagents import cli  # noqa: E402
from multiagents.config import AgentSpec, Config  # noqa: E402
from multiagents.executor.local import LocalExecutor  # noqa: E402
from multiagents.paths import ProjectPaths  # noqa: E402
from multiagents.providers import load_providers  # noqa: E402
from multiagents.runner import Run  # noqa: E402
from multiagents.tree import Node, now as tree_now  # noqa: E402
from test_conversation_provider_change import _calls, _fake_cli, _flag  # noqa: E402

AUTH_MARKER = "Error: invalid api key\n"


@pytest.fixture(autouse=True)
def _clean_budget_cache():
    budget_mod.invalidate_cache()
    yield
    budget_mod.invalidate_cache()


def _lines(path: Path) -> int:
    return len(path.read_text().splitlines()) if path.exists() else 0


def _auth_blocked(entry) -> bool:
    return bool(entry) and entry.get("cause") == "auth" \
        and entry.get("needs_login") is True


def _group_runner(tmp_path, monkeypatch, *, project=None, check_body=None):
    """`own` (owner) + `dep` (auth_from own). The check passes when the
    `ok_file` exists, unless `check_body` says otherwise."""
    ok_file = tmp_path / "logged-in"
    checks = tmp_path / "checks"
    own, _ = _fake_cli(tmp_path, "own")
    dep = h.fake_cli(tmp_path, "dep", exit_code=1, stderr=AUTH_MARKER)
    own["script"] = "ps-own.sh"
    dep["auth_from"] = "own"
    runner = h.make_runner(
        tmp_path / "proj", monkeypatch,
        agents={"worker": AgentSpec("worker", "dep", "m"),
                "ownerworker": AgentSpec("ownerworker", "own", "m")},
        providers={"own": own, "dep": dep},
        project={"limits": {"provider_failure_threshold": 1,
                            "provider_probe_seconds": 0},
                 **(project or {})})
    body = check_body or (
        f'check) echo "$MULTIAGENTS_EXECUTOR" >> "{checks}"; '
        f'if [ -f "{ok_file}" ]; then echo ok; exit 0; fi; '
        'echo "not logged in"; exit 10 ;;')
    c2.case_script(runner.paths.config, "ps-own.sh", body)
    return SimpleNamespace(runner=runner, tree=runner.tree, ok_file=ok_file,
                           checks=checks)


def _failed_run(runner, executor="", provider="dep"):
    return Run(node_id="ag-fail01", provider=runner.providers[provider],
               spec=AgentSpec("worker", provider, "m", executor=executor),
               handle=SimpleNamespace(stderr_tail=""),
               supervisor=SimpleNamespace(steps=1))


def _budgets(*names):
    return {n: budget_mod.Budget(n, known=True, headroom=1.0) for n in names}


# ===========================================================================
# Finding 1 — PS-R7: a consult resume keeps the recorded model
# ===========================================================================

def test_1_a_consult_resume_without_a_run_launches_the_recorded_model(
        tmp_path, monkeypatch):
    """The conversation records `sib/gemini-old`; the roster now names
    `own/gemini-new`, and `sib` (same family) allows only `gemini-old`.
    The resumed turn must run `gemini-old` — never the roster's model on a
    provider that excludes it."""
    own, own_probe = _fake_cli(tmp_path, "own")
    sib, sib_probe = _fake_cli(tmp_path, "sib")
    own["family"] = sib["family"] = "gem"
    sib["models_include"] = ["gemini-old"]
    runner = h.make_runner(
        tmp_path / "proj", monkeypatch,
        agents={"advisor": AgentSpec("advisor", "own", "gemini-new",
                                     conversational=True)},
        providers={"own": own, "sib": sib})
    worktree = runner.paths.worktree("ag-c0nv01")
    worktree.mkdir(parents=True, exist_ok=True)
    runner.tree.add(Node(id="ag-c0nv01", agent="advisor", provider="sib",
                         model="gemini-old", parent=None, depth=1,
                         status="idle", session_id="sess-old",
                         worktree=str(worktree), conversation=True, turns=1))

    async def go():
        try:
            return await runner.consult("advisor", "next question")
        except Exception as exc:              # a refusal is acceptable too
            return {"error": str(exc)}

    asyncio.run(go())
    for argv in _calls(sib_probe):
        assert _flag(argv, "--model") == "gemini-old", argv
    assert not any(_flag(argv, "--model") == "gemini-new"
                   for argv in _calls(sib_probe))


def test_1_every_launch_is_checked_against_the_current_allowlist(
        tmp_path, monkeypatch):
    """The belt under every admission: even a first launch that reached
    `_launch` with an excluded model is refused before anything runs."""
    gem, gem_probe = _fake_cli(tmp_path, "gem")
    gem["models_include"] = ["gemini-*"]
    runner = h.make_runner(
        tmp_path / "proj", monkeypatch,
        agents={"worker": AgentSpec("worker", "gem", "gemini-a")},
        providers={"gem": gem})
    spec = AgentSpec("worker", "gem", "claude-x")
    with pytest.raises(RuntimeError, match="claude-x"):
        asyncio.run(runner._launch(
            node_id="ag-first1", spec=spec, provider=runner.providers["gem"],
            prompt="x", workdir=tmp_path / "wt", branch="", parent=None,
            depth=1))
    assert not _calls(gem_probe)


# ===========================================================================
# Finding 2 — PS-R4b: the initiating write respects another context
# ===========================================================================

def test_2_a_host_failure_leaves_a_container_block_and_its_quota(
        tmp_path, monkeypatch):
    """`dep` holds a container auth block preserving a live quota block; a
    host run on the owner then fails authentication. The group write must
    leave dep's container block (and its quota) standing — a later host
    recovery would otherwise erase both. Only the host login is repaired."""
    host_only = ('check) if [ -f "$OK" ] && [ "$MULTIAGENTS_EXECUTOR" = local ]; '
                 'then echo ok; exit 0; fi; echo "not logged in"; exit 10 ;;')
    g = _group_runner(tmp_path, monkeypatch,
                      check_body=host_only.replace("$OK", str(tmp_path / "logged-in")))
    quota_until = tree_now() + 7200
    g.tree.set_cooldown("dep", tree_now() + 6000, "container login dead",
                        needs_login=True, cause="auth", context="container",
                        also_quota={"cause": "quota", "until": quota_until,
                                    "reason": "out of quota"})
    asyncio.run(g.runner._provider_health_after(
        _failed_run(g.runner, executor="local", provider="own"),
        "failed", "", AUTH_MARKER))
    entry = g.tree.cooldown("dep")
    assert _auth_blocked(entry), entry
    assert set(entry["auth"]) == {"container", "host"}, \
        "both failures are kept, one block per context"
    assert (entry.get("also_quota") or {}).get("until") == quota_until, entry
    g.ok_file.write_text("yes")
    g.runner._half_open(_budgets("own", "dep"), g.tree.read()["cooldowns"])
    # The host block recovered across the group; the container one stands.
    assert g.tree.cooldown("own") is None, g.tree.cooldown("own")
    entry = g.tree.cooldown("dep")
    assert _auth_blocked(entry) and set(entry["auth"]) == {"container"}, entry
    assert (entry.get("also_quota") or {}).get("until") == quota_until, entry


def test_2_the_login_hint_names_the_credential_owner(tmp_path, monkeypatch):
    """PS-R10: a dependent's block says to log in as its owner."""
    g = _group_runner(tmp_path, monkeypatch)
    asyncio.run(g.runner._provider_health_after(
        _failed_run(g.runner), "failed", "", AUTH_MARKER))
    entry = g.tree.cooldown("dep")
    assert _auth_blocked(entry), entry
    assert "multiagents auth login own" in entry["reason"], entry
    assert "auth login dep" not in entry["reason"], entry


# ===========================================================================
# Finding 3 — PS-R4a: re-assertion keeps a live preserved quota block
# ===========================================================================

def test_3_reassertion_over_an_expired_auth_record_keeps_its_live_quota(
        tmp_path):
    paths = h.make_paths(tmp_path / "proj")
    tree = h.Tree(paths.tree_file, paths.events_file)
    quota_until = tree_now() + 3600
    tree.set_cooldown("dep", tree_now() - 1, "auth dead (expired)",
                      needs_login=True, cause="auth", context="host",
                      also_quota={"cause": "quota", "until": quota_until,
                                  "reason": "out of quota"})
    tree.block_auth({"dep": "auth dead again"}, tree_now() + 600, "host")
    entry = tree.cooldown("dep")
    assert _auth_blocked(entry), entry
    assert (entry.get("also_quota") or {}).get("until") == quota_until, entry

    lifted = tree.clear_auth(["dep"], {"host"})
    assert "dep" in lifted
    live = tree.cooldown("dep")
    assert live and live.get("cause") == "quota" and \
        live["until"] == quota_until, live


def test_3_reassertion_preserves_the_live_quota_not_an_older_snapshot(
        tmp_path):
    """The write judges the entry it replaces inside its own transaction: a
    fresh two-hour quota block written just before it is the one preserved."""
    paths = h.make_paths(tmp_path / "proj")
    tree = h.Tree(paths.tree_file, paths.events_file)
    tree.set_cooldown("dep", tree_now() + 3600, "old quota", cause="quota")
    tree.set_cooldown("dep", tree_now() + 7200, "fresh quota", cause="quota")
    tree.block_auth({"dep": "auth dead"}, tree_now() + 600, "host")
    preserved = tree.cooldown("dep")["also_quota"]
    assert preserved["reason"] == "fresh quota", preserved
    assert preserved["until"] > tree_now() + 7000, preserved


def test_3_a_preserved_block_is_restored_with_its_own_cause(tmp_path):
    """No cause means not quota: a restored block keeps the cause it had."""
    paths = h.make_paths(tmp_path / "proj")
    tree = h.Tree(paths.tree_file, paths.events_file)
    tree.set_cooldown("dep", tree_now() + 3600, "provider down",
                      cause="provider_down")
    tree.block_auth({"dep": "auth dead"}, tree_now() + 600, "host")
    tree.clear_auth(["dep"], {"host"})
    assert tree.cooldown("dep").get("cause") == "provider_down"


# ===========================================================================
# Finding 4 — PS-R4b: a block from an executor override can recover
# ===========================================================================

def test_4_a_host_block_in_a_docker_project_is_probed_on_the_host(
        tmp_path, monkeypatch):
    """Docker project; the failing run was pinned to `executor: local`, so
    its group is blocked in the host context. Once the host login works,
    the probe asks the host's login — not the container's — and recovers."""
    g = _group_runner(tmp_path, monkeypatch,
                      project={"executor": {"kind": "docker"}})
    g.runner.config.agents["worker"] = AgentSpec("worker", "dep", "m",
                                                 executor="local")
    asyncio.run(g.runner._provider_health_after(
        _failed_run(g.runner, executor="local"), "failed", "", AUTH_MARKER))
    assert g.tree.cooldown("dep").get("context") == "host"
    g.ok_file.write_text("yes")
    g.runner._half_open(_budgets("own", "dep"), g.tree.read()["cooldowns"])
    assert g.tree.cooldown("own") is None, g.tree.cooldown("own")
    assert g.tree.cooldown("dep") is None, g.tree.cooldown("dep")
    assert "local" in g.checks.read_text().split(), \
        "the recovery check ran through the local executor"


# ===========================================================================
# Finding 5 — PS-R3: interactive setup logs in as the owner
# ===========================================================================

def test_5_setup_runs_the_owners_login_for_a_dependent(
        tmp_path, monkeypatch, capsys):
    marker = tmp_path / "logins"
    c2.case_script(tmp_path, "owner.sh",
                   f'check) [ -f "{marker}" ] && exit 0; exit 10 ;;\n'
                   f'login) echo owner >> "{marker}"; exit 0 ;;')
    c2.case_script(tmp_path, "dependent.sh",
                   f'check) exit 10 ;;\n'
                   f'login) echo dependent >> "{marker}"; exit 0 ;;')
    monkeypatch.setattr(cli, "global_config_dir", lambda: tmp_path)
    monkeypatch.setattr("builtins.input", lambda *_: "y")
    providers = load_providers({
        "owner": {"bin": "sh", "script": "owner.sh", "enabled": False},
        "dep": {"bin": "sh", "script": "dependent.sh", "auth_from": "owner"},
        "dep2": {"bin": "sh", "script": "dependent.sh", "auth_from": "owner"},
    })
    config = Config(project={}, providers={}, agents={}, models={},
                    instruction_dirs=[])
    paths = ProjectPaths(tmp_path)
    paths.ensure()
    remaining = cli._ensure_authenticated(paths, config, providers,
                                          interactive=True)
    assert remaining == 0, capsys.readouterr().out
    assert marker.read_text().split() == ["owner"], \
        "one login, the owner's, for both dependents"


# ===========================================================================
# Finding 6 — PS-R5: one fetch per shared source
# ===========================================================================

def _shared_source(tmp_path, *, delay=0.0):
    cfg = tmp_path / "cfg"
    count = tmp_path / "budget-runs"
    payload = {"known": True, "headroom": 0.5,
               "windows": {"gemini-weekly": {"percent": 50.0},
                           "3p-5h": {"percent": 10.0}}}
    c2.case_script(cfg, "ps-src.sh",
                   f'budget) echo x >> "{count}"; sleep {delay}; '
                   f"echo '{json.dumps(payload)}'; exit 0 ;;")
    providers = load_providers({
        "own": {"bin": "src", "script": "ps-src.sh",
                "budget_windows": ["gemini*"]},
        "dep": {"bin": "dep", "budget_from": "own",
                "budget_windows": ["3p-*"]}})
    return cfg, count, providers


def test_6_concurrent_misses_fetch_the_shared_source_once(tmp_path):
    cfg, count, providers = _shared_source(tmp_path, delay=0.5)
    results = {}
    barrier = threading.Barrier(2)

    def read(name):
        barrier.wait()
        results[name] = budget_mod.read_provider(
            name, providers[name], LocalExecutor(), tmp_path / "g", cfg,
            providers=providers)

    threads = [threading.Thread(target=read, args=(n,)) for n in ("own", "dep")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert _lines(count) == 1, "one script run for two concurrent misses"
    assert results["own"].headroom == pytest.approx(0.5)
    assert results["dep"].headroom == pytest.approx(0.9)


def test_6_an_uncached_unforced_pass_fetches_the_shared_source_once(tmp_path):
    cfg, count, providers = _shared_source(tmp_path)
    got = budget_mod.read_all(providers, lambda n: LocalExecutor(),
                              tmp_path / "g", cfg, use_cache=False, force=False)
    assert _lines(count) == 1
    assert got["own"].headroom == pytest.approx(0.5)
    assert got["dep"].headroom == pytest.approx(0.9)
    budget_mod.read_all(providers, lambda n: LocalExecutor(),
                        tmp_path / "g", cfg, use_cache=False, force=False)
    assert _lines(count) == 2, "a new pass bypassing the cache fetches again"


def test_6_an_owner_selector_keeps_the_owners_own_spend(tmp_path):
    """Projection replaces only what it recomputes: an owner's spend is its
    own, a dependent's never includes the source's."""
    raw = budget_mod.Budget("own", known=True, headroom=0.5,
                            spent={"own": 7},
                            windows={"gemini-w": {"percent": 20.0}})
    providers = load_providers({
        "own": {"bin": "o", "budget_windows": ["gemini*"]},
        "dep": {"bin": "d", "budget_from": "own",
                "budget_windows": ["gemini*"]}})
    assert budget_mod._project_reading("own", raw, providers["own"]).spent \
        == {"own": 7}
    assert budget_mod._project_reading("dep", raw, providers["dep"]).spent == {}


# ===========================================================================
# Finding 7 — PS-R1/R5: chains are judged per facet
# ===========================================================================

@pytest.mark.parametrize("raw", [
    {"login": {"bin": "l"},
     "quota": {"bin": "q", "auth_from": "login"},
     "dep": {"bin": "d", "budget_from": "quota"}},
    {"quota": {"bin": "q"},
     "login": {"bin": "l", "budget_from": "quota"},
     "dep": {"bin": "d", "auth_from": "login"}},
])
def test_7_borrowing_the_other_facet_is_not_a_chain(raw):
    providers = load_providers(raw)
    assert set(providers) == set(raw)


@pytest.mark.parametrize("key", ["auth_from", "budget_from"])
def test_7_a_chain_in_the_same_facet_is_still_rejected(key):
    with pytest.raises(ValueError, match=key):
        load_providers({"a": {"bin": "a"},
                        "b": {"bin": "b", key: "a"},
                        "c": {"bin": "c", key: "b"}})


# ===========================================================================
# Review ag-a50515 (round 2 of the opus run)
#   a1 PS-R4b  a successful run lifts only its own context's auth block
#   a2 PS-R4a  quota / provider_down writes coexist with a live auth block
#   a3 PS-R1a  credential keys come from the owner, never an inherited base
#   a4 PS-R4b  auth blocks are kept per context; recovery lifts its own
#   a5 PS-R7   a conversation whose route moved still gets the refusal
#   a6 PS-R2   a dependent's private HOME carries the owner's home_links
#   a7 PS-R5   a payload published before the generation snapshot is used
# ===========================================================================

def _tree(tmp_path):
    paths = h.make_paths(tmp_path / "proj")
    return h.Tree(paths.tree_file, paths.events_file)


def test_a1_a_success_lifts_only_its_own_contexts_auth_block(tmp_path):
    tree = _tree(tmp_path)
    quota_until = tree_now() + 3600
    tree.set_cooldown("dep", quota_until, "out of quota", cause="quota")
    tree.block_auth({"dep": "container login dead"}, tree_now() + 600,
                    "container")
    tree.note_run_outcome("dep", ok=True, context="host")
    entry = tree.cooldown("dep")
    assert _auth_blocked(entry) and set(entry["auth"]) == {"container"}, entry
    assert entry["also_quota"]["until"] == quota_until, entry

    tree.note_run_outcome("dep", ok=True, context="container")
    live = tree.cooldown("dep")
    assert live and live.get("cause") == "quota" and \
        live["until"] == quota_until, "the preserved quota block remains"


def test_a1_a_plain_cooldown_still_clears_on_success(tmp_path):
    """Control: no auth block, today's behaviour — a success lifts it."""
    tree = _tree(tmp_path)
    tree.set_cooldown("p", tree_now() + 600, "down", cause="provider_down")
    tree.note_run_outcome("p", ok=True, context="host")
    assert tree.cooldown("p") is None


@pytest.mark.parametrize("cause", ["quota", "provider_down", "family"])
def test_a2_a_non_auth_write_never_erases_a_live_auth_block(tmp_path, cause):
    tree = _tree(tmp_path)
    auth_until = tree_now() + 6000
    tree.block_auth({"dep": "auth dead"}, auth_until, "container")
    tree.set_cooldown("dep", tree_now() + 60, "short", cause=cause)
    entry = tree.cooldown("dep")
    assert _auth_blocked(entry) and entry["until"] == auth_until, entry
    assert entry["also_quota"]["cause"] == cause, entry


def test_a2_clear_quota_lifts_the_quota_block_and_leaves_the_auth_one(tmp_path):
    tree = _tree(tmp_path)
    tree.block_auth({"dep": "auth dead"}, tree_now() + 6000, "host")
    tree.set_cooldown("dep", tree_now() + 3600, "out of quota", cause="quota")
    tree.clear_quota({"dep"})
    entry = tree.cooldown("dep")
    assert _auth_blocked(entry) and "also_quota" not in entry, entry


def test_a2_an_auth_block_lapsing_under_a_live_quota_reads_as_the_quota(
        tmp_path):
    """The quota cooldown keeps the provider out; once it ends, the lapsed
    auth block is back on top, owed a probe."""
    tree = _tree(tmp_path)
    quota_until = tree_now() + 3600
    tree.set_cooldown("dep", quota_until, "out of quota", cause="quota")
    tree.block_auth({"dep": "auth dead"}, tree_now() - 1, "host")
    live = tree.cooldown("dep")
    assert live and live.get("cause") == "quota" and \
        live["until"] == quota_until, live
    record = tree.read()["cooldowns"]["dep"]
    assert set(record["auth"]) == {"host"}, "the lapsed auth block is kept"


def test_a3_an_inherited_env_value_never_replaces_the_owners(tmp_path):
    providers = load_providers({
        "owner": {"bin": "o", "env": {"PROFILE": "/owner"}},
        "base": {"bin": "b", "env": {"PROFILE": "/other", "TOOL": "x"}},
        "dep": {"extends": "base", "auth_from": "owner"}})
    env = providers["dep"].credential_env
    assert env["PROFILE"] == "/owner", env
    assert env["TOOL"] == "x", "the dependent's own non-credential env stays"


def test_a4_a_second_context_failure_is_kept_and_recovers_on_its_own(tmp_path):
    tree = _tree(tmp_path)
    tree.block_auth({"own": "r", "dep": "r"}, tree_now() + 600, "container")
    tree.block_auth({"own": "r", "dep": "r"}, tree_now() + 300, "host")
    for member in ("own", "dep"):
        assert set(tree.cooldown(member)["auth"]) == {"container", "host"}
    lifted = tree.clear_auth(["own", "dep"], {"container"})
    assert set(lifted) == {"own", "dep"}
    for member in ("own", "dep"):
        entry = tree.cooldown(member)
        assert _auth_blocked(entry) and entry["context"] == "host", entry
    assert tree.clear_auth(["own", "dep"], {"container"}) == {}, \
        "nothing left in that context"


def test_a5_a_moved_conversation_with_a_disallowed_model_is_refused(
        tmp_path, monkeypatch):
    """The conversation records `gem/claude-old`; gem now allows only Gemini
    models AND the roster moved the advisor to `partner`. The PS-R7 refusal
    comes first — no replacement conversation is started."""
    gem, gem_probe = _fake_cli(tmp_path, "gem")
    partner, partner_probe = _fake_cli(tmp_path, "partner")
    gem["models_include"] = ["gemini-*"]
    partner["models_include"] = ["claude-*"]
    partner["family"] = "partner"
    runner = h.make_runner(
        tmp_path / "proj", monkeypatch,
        agents={"advisor": AgentSpec("advisor", "partner", "claude-new",
                                     conversational=True)},
        providers={"gem": gem, "partner": partner})
    worktree = runner.paths.worktree("ag-c0nv02")
    worktree.mkdir(parents=True, exist_ok=True)
    runner.tree.add(Node(id="ag-c0nv02", agent="advisor", provider="gem",
                         model="claude-old", parent=None, depth=1,
                         status="idle", session_id="sess-old",
                         worktree=str(worktree), conversation=True, turns=1))

    async def go():
        return await runner.consult("advisor", "next question")

    with pytest.raises(ValueError, match="claude-old"):
        asyncio.run(go())
    assert set(runner.tree.read()["nodes"]) == {"ag-c0nv02"}, \
        "no replacement conversation"
    node = runner.tree.get("ag-c0nv02")
    assert (node.status, node.session_id, node.turns) == ("idle", "sess-old", 1)
    assert not _calls(gem_probe) and not _calls(partner_probe)


def test_a6_a_dependents_private_home_links_the_owners_credentials(
        tmp_path, monkeypatch):
    from multiagents import runner as runner_mod
    own, _ = _fake_cli(tmp_path, "own")
    dep, _ = _fake_cli(tmp_path, "dep")
    own["home_links"] = [".owner/token"]
    own["home_copy"] = [".owner.json"]
    dep["home_links"] = [".dep/state"]
    dep["auth_from"] = "own"
    runner = h.make_runner(
        tmp_path / "proj", monkeypatch,
        agents={"worker": AgentSpec("worker", "dep", "m")},
        providers={"own": own, "dep": dep})
    seen = {}

    class Stop(Exception):
        pass

    def capture(home, links, policy="per-agent", agent="agent", copies=None):
        seen.update(links=list(links), copies=list(copies or []))
        raise Stop

    monkeypatch.setattr(runner_mod, "prepare_home", capture)
    with pytest.raises(Stop):
        asyncio.run(runner._launch(
            node_id="ag-home01", spec=AgentSpec("worker", "dep", "m"),
            provider=runner.providers["dep"], prompt="x",
            workdir=tmp_path / "wt", branch="", parent=None, depth=1))
    assert seen["links"] == [".owner/token", ".dep/state"], seen
    assert seen["copies"] == [".owner.json"], seen


def test_a7_a_payload_published_before_the_generation_snapshot_is_used(
        tmp_path, monkeypatch):
    """The owner publishes between the dependent's cache miss and the moment
    it would have sampled the generation. The dependent must use that
    payload, not fetch again."""
    cfg, count, providers = _shared_source(tmp_path)
    budget_mod.read_provider("own", providers["own"], LocalExecutor(),
                             tmp_path / "g", cfg, providers=providers)
    assert _lines(count) == 1
    published = budget_mod._cache["own"]

    class Racing(dict):
        """Empty on the dependent's first look; the owner's publication
        lands right after it."""
        looked = False

        def get(self, key, default=None):
            if not Racing.looked:
                Racing.looked = True
                self[key] = published
                budget_mod._fetch_generation[key] = \
                    budget_mod._fetch_generation.get(key, 0) + 1
                return default
            return super().get(key, default)

    monkeypatch.setattr(budget_mod, "_cache", Racing())
    got = budget_mod.read_provider("dep", providers["dep"], LocalExecutor(),
                                   tmp_path / "g", cfg, providers=providers,
                                   use_cache=False)
    assert _lines(count) == 1, "the dependent took the published payload"
    assert got.headroom == pytest.approx(0.9)


# ===========================================================================
# Review ag-e6b702 — RM-R4e/PS-R5: the clock is read under `_cache_lock`
# ===========================================================================

def test_e1_a_publication_during_the_clock_read_is_not_a_backward_step(
        tmp_path, monkeypatch):
    """A reader samples the clock; a concurrent fetch publishes a newer
    entry (its `seen` ahead of that sample) before the reader looks. With
    the sample taken outside the lock, the reader judged the fresh entry a
    backward step, destroyed it and fetched again. Taken under the lock, the
    publication waits for the look, and the fresh entry survives."""
    real_time = time.time
    t0 = real_time() - 30
    cfg = tmp_path / "cfg"
    budget_mod._cache["acme"] = budget_mod._CacheEntry(
        t0, budget_mod.Budget("acme", known=True, headroom=0.5))
    budget_mod._cache_source["acme"] = str(cfg)
    fresh = budget_mod._CacheEntry(
        t0 + 20, budget_mod.Budget("acme", known=True, headroom=0.8))
    sampled, published = threading.Event(), threading.Event()
    reader = {}

    def fake_time():
        if threading.get_ident() == reader.get("ident") and not sampled.is_set():
            sampled.set()
            published.wait(1.0)     # times out when the lock holds it off
            return t0 + 10
        return real_time()

    def publish():
        sampled.wait(5)
        with budget_mod._cache_lock:
            budget_mod._cache["acme"] = fresh
            budget_mod._fetch_generation["acme"] = \
                budget_mod._fetch_generation.get("acme", 0) + 1
        published.set()

    def read():
        reader["ident"] = threading.get_ident()
        reader["got"] = budget_mod.read_provider("acme", object(), None, cfg)

    monkeypatch.setattr(budget_mod.time, "time", fake_time)
    threads = [threading.Thread(target=publish), threading.Thread(target=read)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive()
    assert budget_mod._cache.get("acme") is fresh, \
        "the freshly published entry was destroyed as a backward step"
    assert reader["got"].known, "the reader fetched instead of using the cache"
