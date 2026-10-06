"""Black-box contract for OG-R2 of context/specs/opencode-go-rename.md.

OG-R2  old configs keep working, loudly: `opencode` named as a route is read as
       `opencode-go`, and each load prints ONE deprecation warning naming the
       file and line and saying to write `opencode-go`. One test per surface:

         agents.yaml `provider: opencode`          (project layer, global layer)
         agents.yaml `models:` chain entry
         providers.yaml `opencode:` override, route-level keys
         providers.yaml `opencode:` override, CLI-level keys
         `multiagents auth login | status opencode`
         `multiagents refresh-quota opencode`
         MCP `list_models(provider="opencode")`
         MCP node `pins.provider`

Red today: `opencode` is a route of its own, so nothing is aliased and nothing
warns. Each test asserts the effective provider and the warning, so the first
failing assertion is the missing alias, not a typo.

Silences (assumptions, also reported in the run result):
- the warning channel is not named, so every channel is read (see og_support);
- a deprecation line is one that contains `opencode-go` and one of
  deprecat/renamed/alias/no longer;
- a block that sets only CLI-level keys is a legitimate use of the base, so no
  warning is asserted either way for it (only the effect);
- an MCP argument has no file, so only the warning text is asserted there.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "support"))
import og_support as og  # noqa: E402
from og_support import (FAKE_BIN, GO_MODEL, NEW, OLD, ZEN_MODEL, Project,  # noqa: E402
                        deprecations, line_of, load, named_line, providers_of)

CLI_TIMEOUT = 30


def _agents_text(provider_line: str = f"provider: {OLD}") -> str:
    return (
        "agents:\n"
        "  helper:\n"
        "    description: a placeholder helper\n"
        f"    {provider_line}\n"
        f"    model: {GO_MODEL}\n"
    )


def _only_one(text: str, file: Path) -> str:
    found = deprecations(text, about=file)
    assert len(found) == 1, (
        f"expected exactly one deprecation warning naming {file.name}, got "
        f"{len(found)}:\n{text}")
    return found[0]


# ===========================================================================
# agents.yaml
# ===========================================================================

def test_og_r2_agent_provider_opencode_is_read_as_opencode_go_with_one_warning(
        tmp_path, capsys, caplog):
    p = Project(tmp_path)
    agents = p.write("agents.yaml", _agents_text())
    cfg, text = load(p, capsys, caplog)
    assert cfg.agents["helper"].provider == NEW
    warning = _only_one(text, agents)
    assert named_line(warning, agents) == line_of(agents, f"provider: {OLD}")


def test_og_r2_agent_provider_opencode_go_is_silent(tmp_path, capsys, caplog):
    p = Project(tmp_path)
    agents = p.write("agents.yaml", _agents_text(f"provider: {NEW}"))
    cfg, text = load(p, capsys, caplog)
    assert cfg.agents["helper"].provider == NEW
    assert deprecations(text, about=agents) == []


SIBLINGS = {"opencode-zen": "opencode/space-bunny-free",
            "opencode-zai": "zai-coding-plan/glm-4.7",
            "opencode-deepinfra": "deepinfra/Qwen/Qwen3.8-Max"}


@pytest.mark.parametrize("sibling", sorted(SIBLINGS))
def test_og_r2_the_alias_is_the_exact_name_not_a_prefix(tmp_path, capsys, caplog, sibling):
    p = Project(tmp_path)
    agents = p.write("agents.yaml",
                     "agents:\n  helper:\n    description: x\n"
                     f"    provider: {sibling}\n    model: {SIBLINGS[sibling]}\n")
    cfg, text = load(p, capsys, caplog)
    assert cfg.agents["helper"].provider == sibling
    assert deprecations(text, about=agents) == []


def test_og_r2_each_load_warns_once_not_once_per_process(tmp_path, capsys, caplog):
    p = Project(tmp_path)
    agents = p.write("agents.yaml", _agents_text())
    _, first = load(p, capsys, caplog)
    _, second = load(p, capsys, caplog)
    _only_one(first, agents)
    _only_one(second, agents)


def test_og_r2_two_old_names_in_one_file_are_two_warnings_on_their_own_lines(
        tmp_path, capsys, caplog):
    p = Project(tmp_path)
    agents = p.write("agents.yaml",
                     "agents:\n"
                     "  one:\n    description: x\n"
                     f"    provider: {OLD}\n    model: {GO_MODEL}\n"
                     "  two:\n    description: y\n"
                     f"    provider: {OLD}\n    model: {GO_MODEL}\n")
    cfg, text = load(p, capsys, caplog)
    assert cfg.agents["one"].provider == cfg.agents["two"].provider == NEW
    found = deprecations(text, about=agents)
    assert sorted(named_line(w, agents) for w in found) == [
        line_of(agents, f"provider: {OLD}", 1), line_of(agents, f"provider: {OLD}", 2)]


def test_og_r2_models_chain_entry_opencode_is_read_as_opencode_go_with_one_warning(
        tmp_path, capsys, caplog):
    p = Project(tmp_path)
    agents = p.write("agents.yaml",
                     "agents:\n  helper:\n    description: x\n"
                     "    provider: claude\n    model: sonnet\n"
                     "    models:\n"
                     f"      {OLD}: {GO_MODEL}\n"
                     "      claude: sonnet\n")
    cfg, text = load(p, capsys, caplog)
    spec = cfg.agents["helper"]
    assert NEW in spec.models and OLD not in spec.models, list(spec.models)
    assert spec.fallback_for(NEW)[0] == GO_MODEL
    assert "claude" in spec.models                      # the other entry is untouched
    warning = _only_one(text, agents)
    assert named_line(warning, agents) == line_of(agents, f"{OLD}: {GO_MODEL}")


def test_og_r2_global_config_layer_is_read_and_names_the_global_file(
        tmp_path, capsys, caplog):
    from multiagents.paths import global_config_dir
    gdir = global_config_dir()
    gdir.mkdir(parents=True, exist_ok=True)
    gfile = gdir / "agents.yaml"
    gfile.write_text(_agents_text())                    # an old, hand-edited global copy
    p = Project(tmp_path)
    cfg, text = load(p, capsys, caplog)
    assert cfg.agents["helper"].provider == NEW
    warning = _only_one(text, gfile)
    assert named_line(warning, gfile) == line_of(gfile, f"provider: {OLD}")


def test_og_r2_a_zen_model_on_the_aliased_route_is_still_refused_naming_opencode_go(
        tmp_path, capsys, caplog):
    """The alias happens before route validation: `opencode` + a zen model id
    is an error about `opencode-go`, not a silent pass on the base."""
    p = Project(tmp_path)
    p.write("agents.yaml", _agents_text().replace(GO_MODEL, ZEN_MODEL))
    with pytest.raises(ValueError) as err:
        load(p, capsys, caplog)
    assert NEW in str(err.value), str(err.value)


# ===========================================================================
# providers.yaml override block named `opencode`
# ===========================================================================

def test_og_r2_override_route_level_keys_apply_to_opencode_go_not_to_its_siblings(
        tmp_path, capsys, caplog):
    p = Project(tmp_path)
    providers = p.write(
        "providers.yaml",
        "providers:\n"
        f"  {OLD}:\n"
        "    enabled: false\n"
        "    models_include: [\"opencode-go/*\", \"opencode-go/example-extra\"]\n"
        "    env:\n"
        "      OG_EXAMPLE_FLAG: \"1\"\n")
    cfg, text = load(p, capsys, caplog)
    ps = providers_of(cfg)
    go = ps[NEW]
    assert go.enabled is False
    assert list(go.models_include) == ["opencode-go/*", "opencode-go/example-extra"]
    assert go.env.get("OG_EXAMPLE_FLAG") == "1"
    for sibling in ("opencode-zen", "opencode-zai", "opencode-deepinfra"):
        assert ps[sibling].env.get("OG_EXAMPLE_FLAG") is None, (
            f"a route-level key meant for opencode-go leaked to {sibling}")
        assert "opencode-go/example-extra" not in ps[sibling].models_include
    warning = _only_one(text, providers)
    block = line_of(providers, f"{OLD}:")
    assert block <= named_line(warning, providers) <= line_of(providers, "OG_EXAMPLE_FLAG")


def test_og_r2_override_budget_key_applies_to_opencode_go(tmp_path, capsys, caplog):
    p = Project(tmp_path)
    providers = p.write("providers.yaml",
                        f"providers:\n  {OLD}:\n    max_concurrent: 1\n")
    cfg, text = load(p, capsys, caplog)
    ps = providers_of(cfg)
    assert ps[NEW].max_concurrent == 1
    _only_one(text, providers)


def test_og_r2_override_cli_level_keys_apply_to_the_base_and_so_to_every_child(
        tmp_path, capsys, caplog):
    p = Project(tmp_path)
    p.write("providers.yaml", f"providers:\n  {OLD}:\n    bin: {FAKE_BIN}\n")
    cfg, _ = load(p, capsys, caplog)
    ps = providers_of(cfg)
    assert ps[OLD].bin == FAKE_BIN
    for child in (NEW, "opencode-zen", "opencode-zai", "opencode-deepinfra"):
        assert ps[child].bin == FAKE_BIN, child
    assert ps[NEW].enabled is True                      # a CLI key is not a route key
    assert list(ps[NEW].models_include) == ["opencode-go/*"]


def test_og_r2_override_with_both_kinds_splits_by_key_and_warns_once(
        tmp_path, capsys, caplog):
    p = Project(tmp_path)
    providers = p.write(
        "providers.yaml",
        "providers:\n"
        f"  {OLD}:\n"
        f"    bin: {FAKE_BIN}\n"
        "    enabled: false\n")
    cfg, text = load(p, capsys, caplog)
    ps = providers_of(cfg)
    assert ps[OLD].bin == FAKE_BIN and ps[NEW].bin == FAKE_BIN
    assert ps[NEW].enabled is False                     # route-level -> opencode-go
    assert ps["opencode-zen"].enabled is False          # shipped disabled; unchanged
    _only_one(text, providers)


def test_og_r2_override_named_opencode_go_is_silent_and_wins(tmp_path, capsys, caplog):
    p = Project(tmp_path)
    providers = p.write("providers.yaml", f"providers:\n  {NEW}:\n    enabled: false\n")
    cfg, text = load(p, capsys, caplog)
    assert providers_of(cfg)[NEW].enabled is False
    assert deprecations(text, about=providers) == []


# ===========================================================================
# the CLI
# ===========================================================================

def _fake_opencode(tmp_path: Path) -> tuple[Path, Path]:
    """A fake `opencode` that records its argv and exits 0 (it stands in for
    `opencode providers login` and `providers list`)."""
    calls = tmp_path / "fake-opencode.calls"
    binary = tmp_path / "bin" / "opencode"
    binary.parent.mkdir()
    binary.write_text(f'#!/bin/sh\necho "$*" >> "{calls}"\necho "0 credentials"\nexit 0\n')
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return binary, calls


def _cli(project: Project, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "multiagents.cli", "--path", str(project.root), *args],
        capture_output=True, text=True, timeout=CLI_TIMEOUT, cwd=project.root,
        env={**os.environ, "PATH": "/usr/bin:/bin"})


def _project_with_fake_bin(tmp_path: Path):
    p = Project(tmp_path)
    binary, calls = _fake_opencode(tmp_path)
    p.write("providers.yaml", f"providers:\n  opencode:\n    bin: {binary}\n")
    return p, calls


def test_og_r2_auth_login_opencode_acts_on_opencode_go_and_warns(tmp_path):
    p, calls = _project_with_fake_bin(tmp_path)
    out = _cli(p, "auth", "login", OLD)
    both = out.stdout + out.stderr
    assert "unknown provider" not in both, both
    assert out.returncode == 0, both
    assert calls.exists() and "login" in calls.read_text(), "the login did not reach the CLI"
    found = deprecations(both)
    assert len(found) == 1, both


def test_og_r2_auth_login_opencode_go_is_silent(tmp_path):
    p, calls = _project_with_fake_bin(tmp_path)
    out = _cli(p, "auth", "login", NEW)
    both = out.stdout + out.stderr
    assert out.returncode == 0, both
    assert calls.exists() and "login" in calls.read_text()
    assert deprecations(both) == [], both


def test_og_r2_auth_status_opencode_reports_opencode_go_and_warns(tmp_path):
    p, _ = _project_with_fake_bin(tmp_path)
    out = _cli(p, "auth", "status", OLD)
    both = out.stdout + out.stderr
    rows = [line.split()[1] if line.split()[:1] in (["ok"], ["!!"], ["?"]) else line.split()[0]
            for line in out.stdout.splitlines() if line.strip()]
    assert NEW in rows, f"no `{NEW}` row in:\n{out.stdout}"
    found = deprecations(both)
    assert len(found) == 1, both


def test_og_r2_refresh_quota_opencode_reports_opencode_go_and_warns(tmp_path):
    p, _ = _project_with_fake_bin(tmp_path)
    out = _cli(p, "refresh-quota", OLD)
    both = out.stdout + out.stderr
    assert "unknown provider" not in both, both
    first = out.stdout.split()[:1]
    assert first == [NEW], f"the row is not for {NEW}:\n{out.stdout}"
    assert len(deprecations(both)) == 1, both


# ===========================================================================
# MCP
# ===========================================================================

def _server_for(project: Project, monkeypatch):
    from multiagents import server
    monkeypatch.setenv("MULTIAGENTS_PROJECT", str(project.root))
    monkeypatch.chdir(project.root)
    server._reset()
    return server


def test_og_r2_mcp_list_models_opencode_answers_for_opencode_go_and_warns(
        tmp_path, monkeypatch, capsys, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    p = Project(tmp_path)
    p.write("models.yaml",
            "models:\n  opencode-go:\n"
            f"    - id: {GO_MODEL}\n      name: placeholder\n")
    server = _server_for(p, monkeypatch)
    try:
        capsys.readouterr()
        reply = server.list_models(provider=OLD)
    finally:
        server._reset()
    if isinstance(reply, str):
        reply = json.loads(reply)
    body = json.dumps(reply)
    assert reply["models"].get(NEW), f"no opencode-go models in the answer: {body}"
    assert OLD not in reply["models"] and reply["counts"].get(NEW) == 1, body
    seen = body + "\n" + og.warning_text(capsys, caplog)
    assert len(deprecations(seen)) >= 1, seen


def test_og_r2_mcp_node_pin_provider_opencode_is_stored_as_opencode_go_and_warns(
        tmp_path, monkeypatch):
    """Needs a live scheduler: the pin is validated and stored by it."""
    sys.path.insert(0, str(Path(__file__).parent / "support"))
    from nc_harness import Sched
    sched = Sched(tmp_path, monkeypatch, enabled=True)
    try:
        sched.start()
        reply = sched.create_raw({"kind": "simple", "agent": "worker", "task": "t",
                                  "pins": {"provider": OLD}})
        assert reply.get("ok") is True, reply
        node = reply["result"]
        stored = sched.get(node["id"])
        assert stored["pins"]["provider"] == NEW, stored["pins"]
        log = sched._log.read_text() if sched._log.exists() else ""
        seen = json.dumps(reply) + "\n" + log
        assert len(deprecations(seen)) >= 1, seen
    finally:
        sched.close()
