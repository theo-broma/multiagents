"""Regression: a provider that `extends` one with a built-in reader uses it.

The bug: `claude-b` — an instance declared as `extends: claude` with its own
`CLAUDE_CONFIG_DIR` — was reported ``known: false, source: none`` because the
built-in lookup used the exact provider name (``_BUILTIN.get(name)``), and
``claude-b`` is not ``claude``. The reader is resolved through ``extends``, not
``family``, and it reads the INSTANCE's own credentials: for claude, the
resolved ``CLAUDE_CONFIG_DIR``.

A fake reader is injected via ``monkeypatch`` so no test here touches the real
network or a real credential file.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import c2_harness as h  # noqa: E402

from multiagents import budget as budget_mod  # noqa: E402


class FakeReader:
    """A `read_claude`-shaped stand-in that records how it was called.

    It answers with a headroom derived from the profile it was pointed at, so a
    caller that conflates two accounts is visible in the Budget it gets back,
    not only in the call log.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, **kwargs) -> budget_mod.Budget:
        self.calls.append(kwargs)
        profile = kwargs.get("config_dir")
        headroom = 0.9 if profile is None else 0.4
        return budget_mod.Budget(
            provider="fake", known=True, headroom=headroom,
            source="api/oauth/usage", note=f"profile={profile}")

    @property
    def config_dirs(self) -> list:
        return [call.get("config_dir") for call in self.calls]


def _providers(raw: dict) -> dict:
    return h.load_providers(raw)


def _install_fake(monkeypatch, fake: "FakeReader") -> None:
    """Swap the claude reader for `fake` at the sanctioned `_BUILTIN` seam.

    The profile variable is declared on the provider (`budget_profile_env`),
    inherited through `extends`, so the stand-in needs no special wiring.
    """
    monkeypatch.setitem(budget_mod._BUILTIN, "claude", fake)


def _base_claude() -> dict:
    return {
        "claude": {"bin": "claude", "family": "claude", "script": "claude.sh",
                   "budget_profile_env": "CLAUDE_CONFIG_DIR"},
        "claude-b": {
            "extends": "claude",
            "family": "claude",
            "env": {"CLAUDE_CONFIG_DIR": "/profiles/claude-b"},
        },
    }


def _read(name: str, providers: dict, project_config: Path | None = None):
    # `providers` is passed exactly as `read_all` passes it, so an `extends`
    # chain of more than one hop can be walked (a lone `read_provider` call
    # has only the immediate `extends` to go on).
    return budget_mod.read_provider(
        name, providers[name], h.FakeExecutor(), Path("/config"),
        project_config=project_config, providers=providers)


def test_instance_resolves_the_claude_reader_with_its_own_config_dir(monkeypatch):
    fake = FakeReader()
    monkeypatch.setattr(budget_mod, "_from_script", lambda *a, **k: None)
    _install_fake(monkeypatch, fake)
    budget_mod.invalidate_cache()

    providers = _providers(_base_claude())
    budget = _read("claude-b", providers)

    assert len(fake.calls) == 1, "the claude reader was not reached through extends"
    assert fake.config_dirs == [Path("/profiles/claude-b")]
    assert budget.known is True
    assert budget.source == "api/oauth/usage"


def test_config_dir_tilde_is_expanded(monkeypatch):
    fake = FakeReader()
    monkeypatch.setattr(budget_mod, "_from_script", lambda *a, **k: None)
    _install_fake(monkeypatch, fake)
    budget_mod.invalidate_cache()

    providers = _providers({
        "claude": {"bin": "claude", "family": "claude", "script": "claude.sh",
                   "budget_profile_env": "CLAUDE_CONFIG_DIR"},
        "claude-b": {"extends": "claude", "family": "claude",
                     "env": {"CLAUDE_CONFIG_DIR": "~/.multiagents/profiles/claude-b"}},
    })
    _read("claude-b", providers)

    expected = Path("~/.multiagents/profiles/claude-b").expanduser()
    assert fake.config_dirs == [expected]
    assert not str(fake.config_dirs[0]).startswith("~")


def test_base_claude_is_unaffected(monkeypatch):
    fake = FakeReader()
    monkeypatch.setattr(budget_mod, "_from_script", lambda *a, **k: None)
    _install_fake(monkeypatch, fake)
    budget_mod.invalidate_cache()

    providers = _providers(_base_claude())
    budget = _read("claude", providers)

    assert fake.config_dirs == [None], \
        "the base account must keep reading with no profile directory"
    assert budget.known is True
    assert budget.headroom == 0.9


def test_extending_instance_without_config_dir_is_unknown(monkeypatch):
    fake = FakeReader()
    monkeypatch.setattr(budget_mod, "_from_script", lambda *a, **k: None)
    _install_fake(monkeypatch, fake)
    budget_mod.invalidate_cache()

    providers = _providers({
        "claude": {"bin": "claude", "family": "claude", "script": "claude.sh",
                   "budget_profile_env": "CLAUDE_CONFIG_DIR"},
        "claude-c": {"extends": "claude", "family": "claude"},
    })
    budget = _read("claude-c", providers)

    assert budget.known is False
    assert budget.source == "none"
    assert "CLAUDE_CONFIG_DIR" in budget.note
    assert fake.calls == [], \
        "an instance with no profile must not read the base account's quota"


def test_two_profiles_do_not_share_a_reading(monkeypatch):
    fake = FakeReader()
    monkeypatch.setattr(budget_mod, "_from_script", lambda *a, **k: None)
    _install_fake(monkeypatch, fake)
    budget_mod.invalidate_cache()

    providers = _providers({
        "claude": {"bin": "claude", "family": "claude", "script": "claude.sh",
                   "budget_profile_env": "CLAUDE_CONFIG_DIR"},
        "claude-b": {"extends": "claude", "family": "claude",
                     "env": {"CLAUDE_CONFIG_DIR": "/profiles/b"}},
        "claude-c": {"extends": "claude", "family": "claude",
                     "env": {"CLAUDE_CONFIG_DIR": "/profiles/c"}},
    })
    first = _read("claude-b", providers)
    second = _read("claude-c", providers)

    assert fake.config_dirs == [Path("/profiles/b"), Path("/profiles/c")]
    assert first.provider == "claude-b"
    assert second.provider == "claude-c"
    assert sorted(p for p in budget_mod._cache) == ["claude-b", "claude-c"], \
        "each instance keeps its own cache slot"
    assert len(budget_mod._cache) == 2


def test_extends_chain_is_walked_past_an_intermediate(monkeypatch):
    fake = FakeReader()
    monkeypatch.setattr(budget_mod, "_from_script", lambda *a, **k: None)
    _install_fake(monkeypatch, fake)
    budget_mod.invalidate_cache()

    providers = _providers({
        "claude": {"bin": "claude", "family": "claude", "script": "claude.sh",
                   "budget_profile_env": "CLAUDE_CONFIG_DIR"},
        "claude-work": {"extends": "claude", "family": "claude-work",
                        "env": {"CLAUDE_CONFIG_DIR": "/profiles/work"}},
        "claude-work-child": {"extends": "claude-work", "family": "claude-work",
                              "env": {"CLAUDE_CONFIG_DIR": "/profiles/work-child"}},
    })
    _read("claude-work-child", providers)

    assert fake.config_dirs == [Path("/profiles/work-child")]


def test_family_is_not_the_lookup_key(monkeypatch):
    """A family member on another binary must not borrow claude's reader."""
    fake = FakeReader()
    monkeypatch.setattr(budget_mod, "_from_script", lambda *a, **k: None)
    _install_fake(monkeypatch, fake)
    budget_mod.invalidate_cache()

    providers = _providers({
        "claude": {"bin": "claude", "family": "shared-family", "script": "claude.sh",
                   "budget_profile_env": "CLAUDE_CONFIG_DIR"},
        "other-cli": {"bin": "other", "family": "shared-family",
                      "env": {"CLAUDE_CONFIG_DIR": "/profiles/other"}},
    })
    budget = _read("other-cli", providers)

    assert fake.calls == [], "family alone must not reach another CLI's reader"
    assert budget.known is False
    assert budget.source == "none"


def test_instance_without_the_profile_field_is_unknown_never_the_base(monkeypatch):
    """An instance reaching a reader but with no declared profile variable.

    The base here declares no `budget_profile_env`, so there is nothing to
    point the reader at. The instance's own `CLAUDE_CONFIG_DIR` must NOT be
    silently ignored, and the base account's reading must never be relabelled
    as the instance's: the reading is unknown, naming the missing field.
    """
    fake = FakeReader()
    monkeypatch.setattr(budget_mod, "_from_script", lambda *a, **k: None)
    _install_fake(monkeypatch, fake)
    budget_mod.invalidate_cache()

    providers = _providers({
        "claude": {"bin": "claude", "family": "claude", "script": "claude.sh"},
        "claude-work": {"extends": "claude", "family": "claude",
                        "env": {"CLAUDE_CONFIG_DIR": "/profiles/work"}},
    })
    budget = _read("claude-work", providers)

    assert budget.known is False
    assert budget.source == "none"
    assert "budget_profile_env" in budget.note
    assert fake.calls == [], "the base account's reading must not be used"


def test_two_hop_chain_resolves_without_the_callers_map(monkeypatch):
    """The owner is recorded at load, so a lone read resolves the chain.

    `read_provider` is called with no `providers` map, as the watchdog and
    `refresh-quota` do. Before the owner was attached at load, a two-hop chain
    needed the full map to walk and came back unknown here.
    """
    fake = FakeReader()
    monkeypatch.setattr(budget_mod, "_from_script", lambda *a, **k: None)
    _install_fake(monkeypatch, fake)
    budget_mod.invalidate_cache()

    providers = _providers({
        "claude": {"bin": "claude", "family": "claude", "script": "claude.sh",
                   "budget_profile_env": "CLAUDE_CONFIG_DIR"},
        "claude-work": {"extends": "claude", "family": "claude-work",
                        "env": {"CLAUDE_CONFIG_DIR": "/profiles/work"}},
        "claude-work-child": {"extends": "claude-work", "family": "claude-work",
                              "env": {"CLAUDE_CONFIG_DIR": "/profiles/work-child"}},
    })
    budget = budget_mod.read_provider(
        "claude-work-child", providers["claude-work-child"], h.FakeExecutor(),
        Path("/config"))

    assert fake.config_dirs == [Path("/profiles/work-child")]
    assert budget.known is True


class SpendReader:
    """A reader shaped like one that takes only the caller's spend.

    `read_opencode` / `read_agy` are this shape: no `config_dir`, so they can
    only ever read the default account. Records its calls.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, spent=None) -> budget_mod.Budget:
        self.calls.append({"spent": spent})
        return budget_mod.Budget(provider="fake", known=True, headroom=0.5,
                                 source="auth.json + tree accounting")


def test_instance_of_a_profile_less_reader_is_unknown(monkeypatch):
    """A `budget_profile_env` on an instance whose reader has no config_dir.

    `read_opencode`/`read_agy` take only the caller's spend, so there is no
    profile to point at. The instance must be unknown — never the base
    account's reading relabelled as the instance's — with a note saying the
    reader cannot read a per-instance profile.
    """
    fake = SpendReader()
    monkeypatch.setattr(budget_mod, "_from_script", lambda *a, **k: None)
    monkeypatch.setitem(budget_mod._BUILTIN, "opencode", fake)
    budget_mod.invalidate_cache()

    providers = _providers({
        "opencode": {"bin": "opencode", "family": "opencode", "script": "opencode.sh"},
        "opencode-x": {"extends": "opencode", "family": "opencode",
                       "env": {"XDG_CONFIG_HOME": "/profiles/x"}},
    })
    # An explicit `budget_profile_env` is refused at load, so a directly-set
    # one stands in for a config that reached here another way (an override
    # applying the field to an already-loaded instance).
    providers["opencode-x"].budget_profile_env = "XDG_CONFIG_HOME"
    budget = _read("opencode-x", providers)

    assert budget.known is False
    assert budget.source == "none"
    assert "cannot read a per-instance profile" in budget.note
    assert fake.calls == [], "the base account's reading must not be used"


def test_declaring_a_profile_env_for_a_profile_less_reader_is_rejected():
    """The same mistake is caught at load, naming the key and the provider."""
    import pytest

    with pytest.raises(ValueError) as caught:
        h.load_providers({
            "opencode": {"bin": "opencode", "family": "opencode",
                         "script": "opencode.sh"},
            "opencode-x": {"extends": "opencode", "family": "opencode",
                           "budget_profile_env": "XDG_CONFIG_HOME",
                           "env": {"XDG_CONFIG_HOME": "/profiles/x"}},
        })
    message = str(caught.value)
    assert "opencode-x" in message
    assert "budget_profile_env" in message


def test_a_provider_with_its_own_script_is_exempt_from_the_profile_check():
    """A provider that supplies its own script owns its `budget` action.

    Its script may implement `budget` and read its own state, so it is not
    bound by the inherited built-in reader's signature. Declaring the field must
    load; the previous validation rejected it regardless of the script.
    """
    providers = h.load_providers({
        "opencode": {"bin": "opencode", "family": "opencode", "script": "opencode.sh"},
        "opencode-x": {"extends": "opencode", "family": "opencode",
                       "budget_profile_env": "XDG_CONFIG_HOME",
                       "script": "opencode-x.sh",
                       "env": {"XDG_CONFIG_HOME": "/profiles/x"}},
    })
    assert providers["opencode-x"].budget_profile_env == "XDG_CONFIG_HOME"


def test_cache_identity_includes_the_account_profile(monkeypatch):
    """Two readings of one provider name for different accounts must not share.

    The MCP server reloads its config on every call, so the same provider name
    can point at a different account within the cache's TTL. The resolved
    profile is part of the cache identity: the second read must call its own
    reader with ITS profile, not return the first account's windows.
    """
    fake = FakeReader()
    monkeypatch.setattr(budget_mod, "_from_script", lambda *a, **k: None)
    _install_fake(monkeypatch, fake)
    budget_mod.invalidate_cache()

    def loaded(profile: str) -> dict:
        return h.load_providers({
            "claude": {"bin": "claude", "family": "claude", "script": "claude.sh",
                       "budget_profile_env": "CLAUDE_CONFIG_DIR"},
            "claude-b": {"extends": "claude", "family": "claude",
                         "env": {"CLAUDE_CONFIG_DIR": profile}},
        })

    first = loaded("/profiles/base")
    second = loaded("/profiles/b")
    a = budget_mod.read_provider("claude-b", first["claude-b"], h.FakeExecutor(),
                                 Path("/config"), providers=first)
    b = budget_mod.read_provider("claude-b", second["claude-b"], h.FakeExecutor(),
                                 Path("/config"), providers=second)

    assert fake.config_dirs == [Path("/profiles/base"), Path("/profiles/b")], \
        "the second account's reader was not called"
    assert a.note != b.note


def test_profile_env_dollar_home_is_expanded(monkeypatch):
    """`$HOME/.claude-work` is read where the launch path points the CLI.

    The launch and script sides resolve every `env:` value with `expandvars`
    then `expanduser`; the reader used `expanduser` alone, so `$HOME/...`
    stayed literal and the profile was reported as missing credentials.
    """
    fake = FakeReader()
    monkeypatch.setattr(budget_mod, "_from_script", lambda *a, **k: None)
    _install_fake(monkeypatch, fake)
    budget_mod.invalidate_cache()

    providers = _providers({
        "claude": {"bin": "claude", "family": "claude", "script": "claude.sh",
                   "budget_profile_env": "CLAUDE_CONFIG_DIR"},
        "claude-b": {"extends": "claude", "family": "claude",
                     "env": {"CLAUDE_CONFIG_DIR": "$HOME/.claude-work"}},
    })
    budget = _read("claude-b", providers)

    assert fake.config_dirs == [Path.home() / ".claude-work"], \
        "the reader must look where the CLI's expanded value points"
    assert budget.known is True


def test_cache_identity_uses_the_expanded_profile(monkeypatch):
    """`$HOME/x` and its expansion are one identity, not two accounts.

    The identity is built from the RESOLVED profile, so two spellings of one
    directory share a cache slot; a literal one would fetch the same account
    twice, once per spelling.
    """
    fake = FakeReader()
    monkeypatch.setattr(budget_mod, "_from_script", lambda *a, **k: None)
    _install_fake(monkeypatch, fake)
    budget_mod.invalidate_cache()

    def loaded(profile: str) -> dict:
        return h.load_providers({
            "claude": {"bin": "claude", "family": "claude", "script": "claude.sh",
                       "budget_profile_env": "CLAUDE_CONFIG_DIR"},
            "claude-b": {"extends": "claude", "family": "claude",
                         "env": {"CLAUDE_CONFIG_DIR": profile}},
        })

    literal = loaded("$HOME/.claude-work")
    expanded = loaded(str(Path.home() / ".claude-work"))
    a = budget_mod.read_provider("claude-b", literal["claude-b"], h.FakeExecutor(),
                                 Path("/config"), providers=literal)
    b = budget_mod.read_provider("claude-b", expanded["claude-b"], h.FakeExecutor(),
                                 Path("/config"), providers=expanded)

    assert fake.config_dirs == [Path.home() / ".claude-work"], \
        "the expanded path must be the cache identity, so this is one read"
    assert a.note == b.note


def test_a_relative_profile_resolves_the_same_for_the_cli_and_the_reader(monkeypatch):
    """A relative value is made absolute by the one shared helper.

    Neither the reader's cwd nor the agent's worktree is a meaningful base for
    a profile, so the helper resolves it against the user's home — and the
    launch environment is built with the same helper, so the CLI and the
    reader agree on the directory instead of each resolving it differently.
    """
    fake = FakeReader()
    monkeypatch.setattr(budget_mod, "_from_script", lambda *a, **k: None)
    _install_fake(monkeypatch, fake)
    budget_mod.invalidate_cache()

    providers = _providers({
        "claude": {"bin": "claude", "family": "claude", "script": "claude.sh",
                   "budget_profile_env": "CLAUDE_CONFIG_DIR"},
        "claude-b": {"extends": "claude", "family": "claude",
                     "env": {"CLAUDE_CONFIG_DIR": "profiles/claude-b"}},
    })
    _read("claude-b", providers)
    launched = h.build_env("claude-b", providers["claude-b"], h.FakeExecutor())

    expected = Path.home() / "profiles" / "claude-b"
    assert fake.config_dirs == [expected]
    assert launched["CLAUDE_CONFIG_DIR"] == str(expected), \
        "the CLI must be launched on the same directory the reader reads"
