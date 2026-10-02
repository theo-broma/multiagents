"""BP review round 1 (ag-b3b2ca) — each fix held to the reviewer's own case.

R1-1  `_fetching_allowed` answers exactly as a fresh `config.load` answers,
      on every path: a malformed sibling file means allowed, and a pending
      seeding refresh is applied before the switch is read.
R1-2  a cold read parses the shipped project.yaml once across BOTH caches —
      the layer read and `shipped_limits`' — not once per cache.
R1-3  one file reached through two path spellings (a symlinked layer dir) is
      one parse, keyed by the resolved path.
R1-4  a layer edited mid-read_all is parsed once per call and every provider
      in the call judges that one parse; the edit is seen by the next call.
"""
from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c2_harness as c2  # noqa: E402
from multiagents import budget as budget_mod  # noqa: E402
from multiagents import config as config_mod  # noqa: E402
from multiagents import paths as paths_mod  # noqa: E402
from multiagents.executor.local import LocalExecutor  # noqa: E402
from multiagents.providers import load_providers  # noqa: E402


@pytest.fixture(autouse=True)
def _cold_caches():
    """Every test starts with no parse memo, no lru and no budget state."""
    for _cold in (lambda: budget_mod.invalidate_cache(),
                  lambda: config_mod._yaml_cache.clear(),
                  lambda: config_mod._shipped_section_cached.cache_clear()):
        _cold()
    yield
    for _cold in (lambda: budget_mod.invalidate_cache(),
                  lambda: config_mod._yaml_cache.clear(),
                  lambda: config_mod._shipped_section_cached.cache_clear()):
        _cold()


def _iso(delta_seconds: float) -> str:
    when = datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _spy(monkeypatch):
    """Count every YAML parse, as the tester's BP file does."""
    parses: Counter = Counter()
    real_yaml_load = yaml.load

    def yaml_load(stream, *a, **kw):
        key = getattr(stream, "name", None)
        if key is None:
            key = "<text:%s>" % hash(stream if isinstance(stream, (str, bytes)) else id(stream))
        parses[str(key)] += 1
        return real_yaml_load(stream, *a, **kw)

    monkeypatch.setattr(yaml, "load", yaml_load)
    return parses


class Project:
    """Script providers over a tmp project config layer."""

    def __init__(self, tmp_path, monkeypatch, names=("alpha", "beta")):
        self.tmp = tmp_path
        self.cfg = tmp_path / "proj-config"
        self.cfg.mkdir()
        self.global_dir = tmp_path / "global-config"
        self.global_dir.mkdir()
        monkeypatch.setenv("MULTIAGENTS_CONFIG_DIR", str(self.global_dir))
        raw = {}
        for name in names:
            pf = tmp_path / f"{name}.json"
            pf.write_text(json.dumps({"known": True, "headroom": 0.1,
                                      "resets_at": _iso(-200)}))
            c2.case_script(self.cfg, f"bp-{name}.sh",
                           f'budget) cat "{pf}"; exit 0 ;;')
            raw[name] = {"bin": name, "script": f"bp-{name}.sh"}
        self.providers = load_providers(raw)
        for fname in ("providers.yaml", "agents.yaml", "models.yaml"):
            (self.cfg / fname).write_text("{}\n")
        self.write_project({})

    def write_project(self, data):
        (self.cfg / "project.yaml").write_text(yaml.safe_dump(data))

    def read(self):
        return budget_mod.read_all(self.providers, lambda n: LocalExecutor(),
                                   self.tmp / "g", self.cfg)


# ------------------------------------------------------------------ R1-1 --

def test_r1_1_a_malformed_sibling_file_means_allowed(tmp_path, monkeypatch):
    """global `ask_provider_for_usage: false` beside a malformed
    providers.yaml: a fresh config.load raises and the old fallback said
    allowed — still the answer, the switch is not read on its own."""
    g = tmp_path / "global"
    g.mkdir()
    monkeypatch.setenv("MULTIAGENTS_CONFIG_DIR", str(g))
    (g / "project.yaml").write_text(
        yaml.safe_dump({"limits": {"ask_provider_for_usage": False}}))
    (g / "providers.yaml").write_text("providers: [unclosed\n")
    assert budget_mod._fetching_allowed() is True


def test_r1_1_a_well_formed_switch_is_honoured_and_reread_when_it_moves(
        tmp_path, monkeypatch):
    g = tmp_path / "global"
    g.mkdir()
    monkeypatch.setenv("MULTIAGENTS_CONFIG_DIR", str(g))
    (g / "project.yaml").write_text(
        yaml.safe_dump({"limits": {"ask_provider_for_usage": False}}))
    assert budget_mod._fetching_allowed() is False
    (g / "project.yaml").write_text("{}\n")
    assert budget_mod._fetching_allowed() is True


def test_r1_1_a_pending_seeding_refresh_is_applied(tmp_path, monkeypatch):
    """A global copy that was never edited but has drifted from an upgraded
    shipped file: `config.load`'s seeding refreshes it BEFORE the switch is
    read, so the answer is the shipped value, not the obsolete copy's."""
    shipped = tmp_path / "shipped"
    shipped.mkdir()
    (shipped / "project.yaml").write_text(
        yaml.safe_dump({"limits": {"ask_provider_for_usage": True}}))
    for fname in ("providers.yaml", "agents.yaml", "models.yaml"):
        (shipped / fname).write_text("{}\n")
    g = tmp_path / "global"
    g.mkdir()
    old = yaml.safe_dump({"limits": {"ask_provider_for_usage": False}})
    (g / "project.yaml").write_text(old)
    # the manifest marks the copy as never edited, so seeding refreshes it
    (g / ".seeded.json").write_text(json.dumps(
        {"project.yaml": hashlib.sha256(old.encode()).hexdigest()}))
    monkeypatch.setenv("MULTIAGENTS_CONFIG_DIR", str(g))
    monkeypatch.setattr(paths_mod, "shipped_defaults_dir", lambda: shipped)
    monkeypatch.setattr(config_mod, "shipped_defaults_dir", lambda: shipped)
    assert budget_mod._fetching_allowed() is True
    assert "ask_provider_for_usage: true" in (g / "project.yaml").read_text()


# ------------------------------------------------------------------ R1-2 --

def test_r1_2_a_cold_read_parses_the_shipped_file_once(tmp_path, monkeypatch):
    """Both caches cold — the shipped project.yaml is parsed once for the
    whole read, not once by the layer read and once by `shipped_limits`."""
    p = Project(tmp_path, monkeypatch)
    parses = _spy(monkeypatch)
    out = p.read()
    assert len(out) == 2
    shipped = str(paths_mod.shipped_defaults_dir() / "project.yaml")
    assert parses[shipped] == 1, parses
    assert parses[str(p.cfg / "project.yaml")] == 1, parses
    assert len(parses) == 2, parses


# ------------------------------------------------------------------ R1-3 --

def test_r1_3_a_layer_dir_reached_through_a_symlink_is_one_parse(
        tmp_path, monkeypatch):
    """`MULTIAGENTS_CONFIG_DIR` pointing through a symlink at the shipped
    dir: the same real file under two spellings is parsed once."""
    p = Project(tmp_path, monkeypatch)
    link = tmp_path / "shipped-link"
    link.symlink_to(paths_mod.shipped_defaults_dir())
    monkeypatch.setenv("MULTIAGENTS_CONFIG_DIR", str(link))
    calls: Counter = Counter()
    real_read = config_mod._read_yaml

    def counting(path):
        calls[str(path)] += 1
        return real_read(path)

    monkeypatch.setattr(config_mod, "_read_yaml", counting)
    out = p.read()
    assert len(out) == 2
    assert sum(calls.values()) == 2, calls   # shipped's + this project's


# ------------------------------------------------------------------ R1-4 --

def test_r1_4_a_layer_edited_mid_read_is_parsed_once_and_seen_by_the_next(
        tmp_path, monkeypatch):
    """alpha's script rewrites project.yaml mid-read_all: beta must judge the
    parse the call already made (margin 1000, reset still live -> critical),
    the file must not be parsed twice within the call, and the NEXT call
    must see the edit (shipped 120 s margin -> normal)."""
    p = Project(tmp_path, monkeypatch)
    p.write_project({"limits": {"quota_reset_margin_seconds": 1000}})
    alpha_payload = p.tmp / "alpha.json"
    c2.case_script(
        p.cfg, "bp-alpha.sh",
        f'budget) printf \'{{}}\\n\' > "{p.cfg / "project.yaml"}"; '
        f'cat "{alpha_payload}"; exit 0 ;;')
    parses = _spy(monkeypatch)
    first = p.read()
    assert {b.severity for b in first.values()} == {"critical"}, first
    assert parses[str(p.cfg / "project.yaml")] == 1, parses
    second = p.read()
    assert {b.severity for b in second.values()} == {"normal"}, second


# ============================================================ round two ===

def test_r2_1_a_manifest_only_change_is_not_served_stale(tmp_path, monkeypatch):
    """The manifest is what makes seeding treat a drifted copy as untouched
    and refresh it. Editing it alone — no config file moving — must move the
    answer, not serve the memo (BP review round 2)."""
    shipped = tmp_path / "shipped"
    shipped.mkdir()
    (shipped / "project.yaml").write_text(
        yaml.safe_dump({"limits": {"ask_provider_for_usage": True}}))
    side = {}
    for fname in ("providers.yaml", "agents.yaml", "models.yaml"):
        (shipped / fname).write_text("{}\n")
    g = tmp_path / "global"
    g.mkdir()
    stale = yaml.safe_dump({"limits": {"ask_provider_for_usage": False}})
    (g / "project.yaml").write_text(stale)
    for fname in ("providers.yaml", "agents.yaml", "models.yaml"):
        content = (shipped / fname).read_bytes()
        (g / fname).write_bytes(content)
        side[fname] = hashlib.sha256(content).hexdigest()
    config_mod._write_manifest(g, side)     # nothing pending: call one may cache
    monkeypatch.setenv("MULTIAGENTS_CONFIG_DIR", str(g))
    monkeypatch.setattr(paths_mod, "shipped_defaults_dir", lambda: shipped)
    monkeypatch.setattr(config_mod, "shipped_defaults_dir", lambda: shipped)
    assert budget_mod._fetching_allowed() is False   # customised: stale copy read
    # the manifest alone now marks the drifted copy untouched
    config_mod._write_manifest(g, {**side, "project.yaml":
                                   hashlib.sha256(stale.encode()).hexdigest()})
    assert budget_mod._fetching_allowed() is True    # refreshed, then read
    assert "ask_provider_for_usage: true" in (g / "project.yaml").read_text()


def test_r2_2_a_snapshot_is_dead_once_its_read_has_returned(
        tmp_path, monkeypatch):
    """An asyncio task spawned mid-read_all inherits the snapshot; a later
    standalone read_provider in that task must not judge the finished call's
    parse. The captured snapshot is force-set back into the variable to play
    that task (BP review round 2)."""
    p = Project(tmp_path, monkeypatch)
    p.write_project({"limits": {"quota_reset_margin_seconds": 1000}})
    captured: dict = {}
    real_read = config_mod.read_yaml_cached

    def capture(path, **kw):
        snap = config_mod._snapshot.get()
        if snap is not None and "snap" not in captured:
            captured["snap"] = snap
        return real_read(path, **kw)

    monkeypatch.setattr(config_mod, "read_yaml_cached", capture)
    p.read()
    assert captured["snap"].open is False, "the snapshot outlived its read"
    # the call judged margin 1000; the file has moved on since
    p.write_project({})
    token = config_mod._snapshot.set(captured["snap"])
    try:
        single = budget_mod.read_provider(
            "alpha", p.providers["alpha"], LocalExecutor(),
            p.tmp / "g", p.cfg)
    finally:
        config_mod._snapshot.reset(token)
    # margin 1000 would hold a 200 s-old reset live (critical); the dead
    # snapshot must not, so the read judges the file as it is now: normal.
    assert single.severity == "normal", single


def test_r2_3_a_malformed_shipped_file_still_raises(tmp_path, monkeypatch):
    """Decided in review round 2: only layer files degrade to {}; the
    package's own broken file raises, as the direct read always did — on a
    cold parse and again out of the warm cache."""
    shipped = tmp_path / "shipped"
    shipped.mkdir()
    (shipped / "project.yaml").write_text("limits: [unclosed\n")
    for fname in ("providers.yaml", "agents.yaml", "models.yaml"):
        (shipped / fname).write_text("{}\n")
    monkeypatch.setattr(config_mod, "shipped_defaults_dir", lambda: shipped)
    monkeypatch.setattr(paths_mod, "shipped_defaults_dir", lambda: shipped)
    with pytest.raises(yaml.YAMLError):
        config_mod.shipped_limits()
    with pytest.raises(yaml.YAMLError):     # warm cache re-raises the stored error
        config_mod.shipped_budget()
    with pytest.raises(yaml.YAMLError):
        budget_mod._reset_margin(None)


def test_r2_4_a_cold_builtin_claude_read_parses_the_shipped_file_once(
        tmp_path, monkeypatch):
    """The built-in claude read's `_fetching_allowed` runs a full
    config.load, and config.load's YAML reads go through the same shared
    cache as the helpers' — so the shipped project.yaml is parsed once for
    the cold read, not twice (BP review round 2). Fetch stubbed: no
    network."""
    p = Project(tmp_path, monkeypatch, names=("alpha", "beta"))
    p.write_project({"limits": {"quota_reset_margin_seconds": 1000}})
    c2.case_script(p.cfg, "bp-claude.sh", "")    # every action unimplemented
    providers = dict(p.providers)
    providers["claude"] = load_providers(
        {"claude": {"bin": "claude", "script": "bp-claude.sh"}})["claude"]
    profile = p.tmp / "claude-profile"
    profile.mkdir()
    (profile / ".claude.json").write_text(json.dumps(
        {"cachedUsageUtilization": {"utilization":
            {"five_hour": {"utilization": 10}}, "fetchedAtMs": 1000}}))
    monkeypatch.setattr(budget_mod, "CLAUDE_STATE", profile / ".claude.json")
    monkeypatch.setattr(budget_mod, "_shared_cache_file",
                        lambda config_dir=None: p.tmp / "usage-claude.json")
    monkeypatch.setattr(budget_mod, "fetch_claude_usage",
                        lambda config_dir=None: (
                            {"five_hour": {"utilization": 10}}, ""))
    parses = _spy(monkeypatch)
    out = budget_mod.read_all(providers, lambda n: LocalExecutor(),
                              p.tmp / "g", p.cfg)
    assert out["claude"].known and out["claude"].headroom == 0.9, out["claude"]
    shipped = str(paths_mod.shipped_defaults_dir() / "project.yaml")
    assert parses[shipped] == 1, parses
    assert parses[str(p.cfg / "project.yaml")] == 1, parses


# ========================================================== round three ===

def test_r3_1_a_strict_load_surfaces_a_stat_error(tmp_path, monkeypatch):
    """A layer file that cannot be stat()ed is not an absent one for a
    strict read: `config.load` raises, cold and with the file's earlier
    parse warm in the cache — never a cached or empty value. The budget's
    own layer read still skips it, as `_read_yaml`'s is_file() did."""
    import pathlib

    g = tmp_path / "global"
    g.mkdir()
    monkeypatch.setenv("MULTIAGENTS_CONFIG_DIR", str(g))
    target = g / "project.yaml"
    target.write_text(yaml.safe_dump(
        {"limits": {"quota_reset_margin_seconds": 1000}}))
    assert config_mod.load(None, seed=False).project["limits"][
        "quota_reset_margin_seconds"] == 1000          # warm the cache
    real_stat = pathlib.Path.stat

    def stat(self, *a, **kw):
        if self == target:
            raise PermissionError(13, "Permission denied", str(self))
        return real_stat(self, *a, **kw)

    monkeypatch.setattr(pathlib.Path, "stat", stat)
    with pytest.raises(PermissionError):
        config_mod.load(None, seed=False)
    with config_mod.parse_once():                     # a layer read first...
        assert budget_mod._reset_margin(None) == 120
        with pytest.raises(PermissionError):          # ...does not mask it
            config_mod.load(None, seed=False)


def test_r3_2_mutating_a_loaded_config_does_not_change_the_next_load(
        tmp_path, monkeypatch):
    g = tmp_path / "global"
    g.mkdir()
    monkeypatch.setenv("MULTIAGENTS_CONFIG_DIR", str(g))
    (g / "models.yaml").write_text(yaml.safe_dump(
        {"models": {"m": {"tags": ["a"]}}}))
    (g / "project.yaml").write_text(yaml.safe_dump(
        {"limits": {"quota_reset_margin_seconds": 1000}, "extra": ["x"]}))
    first = config_mod.load(None, seed=False)
    first.models["m"]["tags"].append("bogus")
    first.project["extra"].append("bogus")
    first.project["limits"]["quota_reset_margin_seconds"] = 5
    second = config_mod.load(None, seed=False)
    assert second.models["m"]["tags"] == ["a"]
    assert second.project["extra"] == ["x"]
    assert second.project["limits"]["quota_reset_margin_seconds"] == 1000
    # and within one call's view, too
    with config_mod.parse_once():
        one = config_mod.read_yaml_cached(g / "project.yaml")
        one["extra"].append("bogus")
        assert config_mod.read_yaml_cached(g / "project.yaml")["extra"] == ["x"]


def test_r3_3_config_load_inside_a_read_sees_the_reads_version(
        tmp_path, monkeypatch):
    """A layer rewritten after a read's helpers parsed it: the `config.load`
    later in the same read uses that parse — not a second one of the new
    version — and the next read sees the edit."""
    g = tmp_path / "global"
    g.mkdir()
    monkeypatch.setenv("MULTIAGENTS_CONFIG_DIR", str(g))
    target = g / "project.yaml"
    target.write_text(yaml.safe_dump(
        {"limits": {"quota_reset_margin_seconds": 1000}}))
    parses = _spy(monkeypatch)
    with config_mod.parse_once():
        assert budget_mod._reset_margin(None) == 1000
        target.write_text(yaml.safe_dump(
            {"limits": {"quota_reset_margin_seconds": 7}}))
        assert config_mod.load(None, seed=False).limits[
            "quota_reset_margin_seconds"] == 1000
    assert parses[str(target)] == 1, parses
    assert config_mod.load(None, seed=False).limits[
        "quota_reset_margin_seconds"] == 7


def test_r3_3_a_seeding_rewrite_inside_a_read_is_seen_by_its_load(
        tmp_path, monkeypatch):
    """The one exception to the call's view: a file this process's own
    seeding refreshes mid-read is read as refreshed, as before BP."""
    shipped = tmp_path / "shipped"
    shipped.mkdir()
    (shipped / "project.yaml").write_text(
        yaml.safe_dump({"limits": {"ask_provider_for_usage": True}}))
    for fname in ("providers.yaml", "agents.yaml", "models.yaml"):
        (shipped / fname).write_text("{}\n")
    g = tmp_path / "global"
    g.mkdir()
    old = yaml.safe_dump({"limits": {"ask_provider_for_usage": False}})
    (g / "project.yaml").write_text(old)
    (g / ".seeded.json").write_text(json.dumps(
        {"project.yaml": hashlib.sha256(old.encode()).hexdigest()}))
    monkeypatch.setenv("MULTIAGENTS_CONFIG_DIR", str(g))
    monkeypatch.setattr(paths_mod, "shipped_defaults_dir", lambda: shipped)
    monkeypatch.setattr(config_mod, "shipped_defaults_dir", lambda: shipped)
    with config_mod.parse_once():
        assert config_mod.read_yaml_cached(g / "project.yaml")["limits"] == {
            "ask_provider_for_usage": False}
        assert budget_mod._fetching_allowed() is True


def test_r3_3_a_read_all_edit_mid_call_does_not_reach_the_builtin_load(
        tmp_path, monkeypatch):
    """The reviewer's case: alpha's script turns `ask_provider_for_usage`
    off in the global project.yaml after the helpers have read it; claude's
    `_fetching_allowed` load in the same read_all judges the call's version
    (allowed), parsing the file once."""
    p = Project(tmp_path, monkeypatch, names=("alpha", "beta"))
    gproject = p.global_dir / "project.yaml"
    gproject.write_text("{}\n")
    alpha_payload = p.tmp / "alpha.json"
    c2.case_script(
        p.cfg, "bp-alpha.sh",
        f'budget) printf \'limits: {{ask_provider_for_usage: false}}\\n\' '
        f'> "{gproject}"; cat "{alpha_payload}"; exit 0 ;;')
    c2.case_script(p.cfg, "bp-claude.sh", "")
    providers = {"alpha": p.providers["alpha"],
                 "claude": load_providers(
                     {"claude": {"bin": "claude", "script": "bp-claude.sh"}})["claude"],
                 "beta": p.providers["beta"]}
    profile = p.tmp / "claude-profile"
    profile.mkdir()
    (profile / ".claude.json").write_text(json.dumps(
        {"cachedUsageUtilization": {"utilization":
            {"five_hour": {"utilization": 10}}, "fetchedAtMs": 1000}}))
    monkeypatch.setattr(budget_mod, "CLAUDE_STATE", profile / ".claude.json")
    monkeypatch.setattr(budget_mod, "_shared_cache_file",
                        lambda config_dir=None: p.tmp / "usage-claude.json")
    monkeypatch.setattr(budget_mod, "fetch_claude_usage",
                        lambda config_dir=None: (
                            {"five_hour": {"utilization": 20}}, ""))
    parses = _spy(monkeypatch)
    out = budget_mod.read_all(providers, lambda n: LocalExecutor(),
                              p.tmp / "g", p.cfg)
    assert out["claude"].source == "api/oauth/usage", out["claude"]
    assert out["claude"].headroom == 0.8, out["claude"]
    assert parses[str(gproject)] == 1, parses
    assert "ask_provider_for_usage: false" in gproject.read_text()


# =========================================================== round four ===

def _same_size_rewrite(path, text, *, via_rename):
    """Replace `path` with same-size `text` carrying the old mtime."""
    import os

    old = path.stat()
    assert len(text.encode()) == old.st_size
    if via_rename:
        tmp = path.with_name(path.name + ".new")
        tmp.write_text(text)
        os.utime(tmp, ns=(old.st_atime_ns, old.st_mtime_ns))
        os.replace(tmp, path)
    else:
        path.write_text(text)
        os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
    new = path.stat()
    assert (new.st_mtime_ns, new.st_size) == (old.st_mtime_ns, old.st_size)


@pytest.mark.parametrize("via_rename", [False, True])
def test_r4_1_a_same_size_same_mtime_replacement_is_seen(
        tmp_path, monkeypatch, via_rename):
    target = tmp_path / "project.yaml"
    target.write_text("limits: {quota_reset_margin_seconds: 1000}\n")
    assert config_mod.read_yaml_cached(target)["limits"][
        "quota_reset_margin_seconds"] == 1000
    _same_size_rewrite(target, "limits: {quota_reset_margin_seconds: 2000}\n",
                       via_rename=via_rename)
    assert config_mod.read_yaml_cached(target)["limits"][
        "quota_reset_margin_seconds"] == 2000


def test_r4_1_seedings_own_rewrite_is_seen_even_with_the_old_stamp(
        tmp_path, monkeypatch):
    """`copy2` gives the refreshed global copy the shipped file's mtime; with
    the same size and that mtime equal to the old copy's, only the eviction
    of this process's own write keeps the next read honest."""
    import os

    shipped = tmp_path / "shipped"
    shipped.mkdir()
    new = "limits: {ask_provider_for_usage: true}\n"
    old = "limits: {ask_provider_for_usage: off}\n"
    old += " " * (len(new) - len(old))            # same size as the shipped text
    (shipped / "project.yaml").write_text(new)
    for fname in ("providers.yaml", "agents.yaml", "models.yaml"):
        (shipped / fname).write_text("{}\n")
    g = tmp_path / "global"
    g.mkdir()
    (g / "project.yaml").write_text(old)
    st = (shipped / "project.yaml").stat()
    os.utime(g / "project.yaml", ns=(st.st_atime_ns, st.st_mtime_ns))
    (g / ".seeded.json").write_text(json.dumps(
        {"project.yaml": hashlib.sha256(old.encode()).hexdigest()}))
    monkeypatch.setenv("MULTIAGENTS_CONFIG_DIR", str(g))
    monkeypatch.setattr(paths_mod, "shipped_defaults_dir", lambda: shipped)
    monkeypatch.setattr(config_mod, "shipped_defaults_dir", lambda: shipped)
    assert config_mod.read_yaml_cached(g / "project.yaml")["limits"] == {
        "ask_provider_for_usage": False}
    # no stamp help at all: a memo entry, once there, is never re-validated
    real = config_mod._parse_versioned

    def frozen_stamp(key):
        hit = config_mod._yaml_cache.get(key)
        return (hit[1], hit[2]) if hit is not None else real(key)

    monkeypatch.setattr(config_mod, "_parse_versioned", frozen_stamp)
    config_mod.seed_global()
    assert "ask_provider_for_usage: true" in (g / "project.yaml").read_text()
    assert config_mod.read_yaml_cached(g / "project.yaml")["limits"] == {
        "ask_provider_for_usage": True}


def test_r4_2_a_read_in_a_copied_context_keeps_its_own_view(tmp_path):
    """A thread started mid-read with a copy of the context does not borrow
    the parent's snapshot: when the parent's read returns first, the child's
    read still sees one version of the file throughout."""
    import contextvars
    import threading

    target = tmp_path / "project.yaml"
    target.write_text("v: 1\n")
    child_started, parent_done = threading.Event(), threading.Event()
    seen: list = []

    def child():
        with config_mod.parse_once():
            seen.append(config_mod.read_yaml_cached(target)["v"])
            child_started.set()
            parent_done.wait(10)
            seen.append(config_mod.read_yaml_cached(target)["v"])

    with config_mod.parse_once():
        assert config_mod.read_yaml_cached(target)["v"] == 1
        ctx = contextvars.copy_context()
        worker = threading.Thread(target=ctx.run, args=(child,))
        worker.start()
        assert child_started.wait(10)
    target.write_text("v: 22\n")                 # parent's read is over
    parent_done.set()
    worker.join(10)
    assert seen == [1, 1]


def test_r4_2_an_asyncio_task_spawned_mid_read_opens_its_own_view(tmp_path):
    import asyncio

    target = tmp_path / "project.yaml"
    target.write_text("v: 1\n")
    seen: list = []

    async def child(go):
        with config_mod.parse_once():
            seen.append(config_mod.read_yaml_cached(target)["v"])
            await go.wait()
            seen.append(config_mod.read_yaml_cached(target)["v"])

    async def main():
        go = asyncio.Event()
        with config_mod.parse_once():
            config_mod.read_yaml_cached(target)
            task = asyncio.create_task(child(go))
            await asyncio.sleep(0)
        target.write_text("v: 22\n")
        go.set()
        await task

    asyncio.run(main())
    assert seen == [1, 1]


def test_r4_3_a_cached_parse_error_is_raised_fresh_each_time(tmp_path):
    import traceback

    target = tmp_path / "project.yaml"
    target.write_text("limits: [unclosed\n")
    raised, depths = [], set()
    for _ in range(100):
        with pytest.raises(yaml.YAMLError) as info:
            config_mod.read_yaml_cached(target, strict=True)
        raised.append(info.value)
        depths.add(len(traceback.extract_tb(info.value.__traceback__)))
    assert len(depths) == 1, depths
    assert raised[0] is not raised[-1]
    assert str(raised[0]) == str(raised[-1])
    assert "unclosed" in str(raised[-1]) or "line" in str(raised[-1])
    stored = config_mod._yaml_cache[target.resolve()][2]
    assert stored.__traceback__ is None
