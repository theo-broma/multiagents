"""Black-box contract for `context/specs/agent-models-replace.md` (MR-R1..MR-R4),
ticket bug-d491e8.

A layer that declares `agents.<name>.models` replaces the map below it whole;
an empty declaration (`[]`, `{}`, null) cancels every fallback; any other list
is a config error; nothing else changes.

Everything goes through `config.load()` over the real shipped defaults plus a
project layer (and, where it matters, a global layer through
MULTIAGENTS_CONFIG_DIR) written to disk. "No shipped-only route is launched" is
checked with the routing harness of tests/test_fs_fallback_scope.py, fed with
the loaded spec.

Shipped facts the tests rely on (checked, not assumed, by the first test):
`tester` runs on `claude` with the single fallback route `opencode-go`;
`researcher` runs on `opencode-go` with `agy`; `dev-advisor` disables its
`opencode-go` route with an empty model.

Silences, deliberately not tested: the exact wording of the MR-R3 message
beyond "names the agent, says mapping of provider to model, or empty"; a
scalar `models: codex`; what a non-empty list in a layer that a higher layer
overrides does.
"""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "support"))
import c3_harness as h  # noqa: E402
from multiagents import config as config_mod  # noqa: E402
from multiagents.paths import ProjectPaths  # noqa: E402
from test_conversation_provider_change import _calls, _fake_cli  # noqa: E402
from test_fs_fallback_scope import _budgets, _deferred, _ran, _start  # noqa: E402

PROVIDERS = ("claude", "codex", "agy", "opencode-go")
SHIPPED_TESTER = {"opencode-go": "opencode-go/qwen3.6-plus"}
EMPTY_FORMS = [("[]", "list"), ("{}", "map"), ("null", "null")]


# ---------------------------------------------------------------------------
# Seams
# ---------------------------------------------------------------------------

def _project(tmp_path, agents_yaml: str, *, global_yaml: str | None = None,
             monkeypatch=None) -> ProjectPaths:
    root = tmp_path / "proj"
    cfg = root / ".multiagents" / "config"
    cfg.mkdir(parents=True)
    (cfg / "agents.yaml").write_text(agents_yaml)
    if global_yaml is not None:
        gdir = tmp_path / "globalcfg"
        gdir.mkdir()
        (gdir / "agents.yaml").write_text(global_yaml)
        monkeypatch.setenv("MULTIAGENTS_CONFIG_DIR", str(gdir))
    return ProjectPaths(root)


def _load(tmp_path, agents_yaml, **kw):
    return config_mod.load(_project(tmp_path, agents_yaml, **kw), seed=False)


def _tester_block(models_text: str, extra: str = "") -> str:
    body = f"    models: {models_text}\n" if models_text is not None else ""
    return f"agents:\n  tester:\n{body}{extra}"


def _models(cfg, agent="tester") -> dict:
    return dict(cfg.agents[agent].models or {})


# The routing harness starts the agent by the key it was registered under, so
# `_start` (which starts "worker") is fed the loaded spec under that key.
def _run_tester(tmp_path, monkeypatch, cfg, **headroom):
    sub = tmp_path / "route"
    sub.mkdir()
    providers, probes = {}, {}
    for name in PROVIDERS:
        providers[name], probes[name] = _fake_cli(sub, name)
    runner = h.make_runner(sub / "project", monkeypatch,
                           agents={"worker": dataclasses.replace(cfg.agents["tester"], instructions="")},
                           providers=providers,
                           project={"budget": {"fallback_chain": []}})
    _budgets(monkeypatch, **headroom)
    return _start(runner), probes


# ---------------------------------------------------------------------------
# The premise: the shipped defaults this suite builds on
# ---------------------------------------------------------------------------

def test_mr_r0_shipped_premise_tester_has_one_opencode_go_route(tmp_path):
    cfg = _load(tmp_path, "agents: {}\n")
    assert cfg.agents["tester"].provider == "claude"
    assert _models(cfg) == SHIPPED_TESTER


# ---------------------------------------------------------------------------
# MR-R1: the highest layer that declares `models` wins whole
# ---------------------------------------------------------------------------

def test_mr_r1_project_models_replace_the_shipped_map_exactly(tmp_path):
    cfg = _load(tmp_path, _tester_block("\n      codex: gpt-x\n      agy: gemini-3.8-flash-high\n"))
    models = _models(cfg)
    assert list(models) == ["codex", "agy"], models
    assert models["codex"] == "gpt-x" and models["agy"] == "gemini-3.8-flash-high"
    assert "opencode-go" not in models


def test_mr_r1_the_order_is_the_project_layers_order(tmp_path):
    cfg = _load(tmp_path, _tester_block("\n      agy: gemini-3.8-flash-high\n      codex: gpt-x\n"))
    assert list(_models(cfg)) == ["agy", "codex"]


def test_mr_r1_a_project_map_that_re_lists_a_shipped_route_keeps_only_its_own_value(tmp_path):
    cfg = _load(tmp_path, _tester_block("\n      codex: gpt-x\n      opencode-go: opencode-go/glm-5.1\n"))
    assert _models(cfg) == {"codex": "gpt-x", "opencode-go": "opencode-go/glm-5.1"}
    assert list(_models(cfg)) == ["codex", "opencode-go"]


def test_mr_r1_other_agents_keep_their_shipped_maps(tmp_path):
    cfg = _load(tmp_path, _tester_block("\n      codex: gpt-x\n"))
    assert _models(cfg, "researcher") == {"agy": "gemini-3.8-flash-medium"}
    assert _models(cfg, "reviewer") == {"opencode-go": "opencode-go/minimax-m3"}


def test_mr_r1_a_new_project_only_agent_is_unaffected(tmp_path):
    cfg = _load(tmp_path, "agents:\n  scribe:\n    provider: claude\n    model: opus\n"
                          "    models:\n      codex: gpt-x\n")
    assert _models(cfg, "scribe") == {"codex": "gpt-x"}


def test_mr_r1_every_other_field_still_merges_recursively(tmp_path):
    plain = _load(tmp_path / "a", _tester_block(None, "    description: mine\n"))
    both = _load(tmp_path / "b", _tester_block("\n      codex: gpt-x\n", "    description: mine\n"))
    for cfg in (plain, both):
        spec = cfg.agents["tester"]
        assert spec.description.strip() == "mine"
        assert spec.provider == "claude" and spec.model == "opus"
        assert spec.writes is True
        assert spec.instructions == "team/tester.md"


def test_mr_r1_a_layer_that_does_not_mention_models_leaves_the_map_untouched(tmp_path):
    cfg = _load(tmp_path, _tester_block(None, "    model: sonnet\n"))
    assert cfg.agents["tester"].model == "sonnet"
    assert _models(cfg) == SHIPPED_TESTER


def test_mr_r1_an_override_of_another_field_keeps_the_shipped_map_for_all_agents(tmp_path):
    cfg = _load(tmp_path, "agents:\n  researcher:\n    model: opencode-go/glm-5.1\n")
    assert _models(cfg, "researcher") == {"agy": "gemini-3.8-flash-medium"}


def test_mr_r1_global_layer_replaces_shipped_and_project_silence_keeps_it(tmp_path, monkeypatch):
    cfg = _load(tmp_path, _tester_block(None, "    model: sonnet\n"),
                global_yaml=_tester_block("\n      codex: gpt-x\n"), monkeypatch=monkeypatch)
    assert _models(cfg) == {"codex": "gpt-x"}


def test_mr_r1_project_replaces_the_global_map_as_well(tmp_path, monkeypatch):
    cfg = _load(tmp_path, _tester_block("\n      agy: gemini-3.8-flash-high\n"),
                global_yaml=_tester_block("\n      codex: gpt-x\n"), monkeypatch=monkeypatch)
    assert _models(cfg) == {"agy": "gemini-3.8-flash-high"}


def test_mr_r1_with_the_primary_exhausted_the_project_route_runs_and_no_shipped_only_one(
        tmp_path, monkeypatch):
    cfg = _load(tmp_path, _tester_block("\n      codex: gpt-x\n"))
    result, probes = _run_tester(tmp_path, monkeypatch, cfg,
                                 claude=0.0, codex=1.0, agy=1.0, **{"opencode-go": 1.0})
    assert result.get("provider") == "codex", result
    assert _ran(probes, *PROVIDERS) == ["codex"]


def test_mr_r1_with_every_declared_route_exhausted_the_shipped_only_route_is_not_used(
        tmp_path, monkeypatch):
    cfg = _load(tmp_path, _tester_block("\n      codex: gpt-x\n"))
    result, probes = _run_tester(tmp_path, monkeypatch, cfg,
                                 claude=0.0, codex=0.0, agy=1.0, **{"opencode-go": 1.0})
    assert _deferred(result), result
    assert not _ran(probes, *PROVIDERS), "a route the project left out was launched"


def test_mr_r1_without_a_declaration_the_shipped_route_is_still_used(tmp_path, monkeypatch):
    cfg = _load(tmp_path, _tester_block(None, "    model: sonnet\n"))
    result, probes = _run_tester(tmp_path, monkeypatch, cfg,
                                 claude=0.0, codex=1.0, **{"opencode-go": 1.0})
    assert result.get("provider") == "opencode-go", result
    assert _ran(probes, *PROVIDERS) == ["opencode-go"]


# ---------------------------------------------------------------------------
# MR-R2: an empty declaration cancels every fallback
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,_id", EMPTY_FORMS, ids=[i for _, i in EMPTY_FORMS])
def test_mr_r2_an_empty_declaration_gives_an_empty_effective_map(tmp_path, text, _id):
    cfg = _load(tmp_path, _tester_block(text))
    assert not _models(cfg), _models(cfg)
    assert cfg.agents["tester"].provider == "claude"


@pytest.mark.parametrize("text,_id", EMPTY_FORMS, ids=[i for _, i in EMPTY_FORMS])
def test_mr_r2_empty_cancels_the_map_of_another_shipped_agent_too(tmp_path, text, _id):
    cfg = _load(tmp_path, f"agents:\n  researcher:\n    models: {text}\n")
    assert not _models(cfg, "researcher")
    assert _models(cfg, "tester") == SHIPPED_TESTER


@pytest.mark.parametrize("text,_id", EMPTY_FORMS, ids=[i for _, i in EMPTY_FORMS])
def test_mr_r2_empty_keeps_every_other_field(tmp_path, text, _id):
    cfg = _load(tmp_path, _tester_block(text))
    spec = cfg.agents["tester"]
    assert spec.model == "opus" and spec.writes is True
    assert spec.instructions == "team/tester.md"


@pytest.mark.parametrize("text,_id", EMPTY_FORMS, ids=[i for _, i in EMPTY_FORMS])
def test_mr_r2_empty_over_a_global_map_cancels_it(tmp_path, monkeypatch, text, _id):
    cfg = _load(tmp_path, _tester_block(text),
                global_yaml=_tester_block("\n      codex: gpt-x\n"), monkeypatch=monkeypatch)
    assert not _models(cfg)


@pytest.mark.parametrize("text,_id", EMPTY_FORMS, ids=[i for _, i in EMPTY_FORMS])
def test_mr_r2_a_higher_non_empty_map_beats_a_lower_empty_one(tmp_path, monkeypatch, text, _id):
    cfg = _load(tmp_path, _tester_block("\n      agy: gemini-3.8-flash-high\n"),
                global_yaml=_tester_block(text), monkeypatch=monkeypatch)
    assert _models(cfg) == {"agy": "gemini-3.8-flash-high"}


@pytest.mark.parametrize("text,_id", EMPTY_FORMS, ids=[i for _, i in EMPTY_FORMS])
def test_mr_r2_an_exhausted_primary_defers_and_no_fallback_is_launched(
        tmp_path, monkeypatch, text, _id):
    cfg = _load(tmp_path, _tester_block(text))
    result, probes = _run_tester(tmp_path, monkeypatch, cfg,
                                 claude=0.0, codex=1.0, agy=1.0, **{"opencode-go": 1.0})
    assert _deferred(result), result
    assert not _ran(probes, *PROVIDERS)


@pytest.mark.parametrize("text,_id", EMPTY_FORMS, ids=[i for _, i in EMPTY_FORMS])
def test_mr_r2_with_room_the_agent_runs_on_its_primary_provider(
        tmp_path, monkeypatch, text, _id):
    cfg = _load(tmp_path, _tester_block(text))
    result, probes = _run_tester(tmp_path, monkeypatch, cfg,
                                 claude=1.0, codex=1.0, **{"opencode-go": 1.0})
    assert result.get("provider") == "claude", result
    assert _ran(probes, *PROVIDERS) == ["claude"]


# ---------------------------------------------------------------------------
# MR-R3: any other list is a config error
# ---------------------------------------------------------------------------

def _assert_models_list_error(exc, agent):
    text = str(exc.value)
    assert agent in text, text
    low = text.lower()
    assert "models" in low and "mapping" in low and "provider" in low and "model" in low, text
    assert "empty" in low, text


def test_mr_r3_a_non_empty_list_is_rejected_at_load_naming_the_agent(tmp_path):
    with pytest.raises(ValueError) as exc:
        _load(tmp_path, _tester_block("[codex]"))
    _assert_models_list_error(exc, "tester")


def test_mr_r3_the_error_names_the_agent_that_has_the_list(tmp_path):
    with pytest.raises(ValueError) as exc:
        _load(tmp_path, "agents:\n  researcher:\n    models: [codex, agy]\n")
    _assert_models_list_error(exc, "researcher")
    assert "tester" not in str(exc.value)


def test_mr_r3_a_list_of_mappings_is_rejected_too(tmp_path):
    with pytest.raises(ValueError) as exc:
        _load(tmp_path, _tester_block("\n      - codex: gpt-x\n"))
    _assert_models_list_error(exc, "tester")


def test_mr_r3_a_list_on_a_project_only_agent_is_rejected(tmp_path):
    with pytest.raises(ValueError) as exc:
        _load(tmp_path, "agents:\n  scribe:\n    provider: claude\n    model: opus\n"
                        "    models: [codex]\n")
    _assert_models_list_error(exc, "scribe")


def test_mr_r3_a_list_in_the_global_layer_is_rejected(tmp_path, monkeypatch):
    with pytest.raises(ValueError) as exc:
        _load(tmp_path, "agents: {}\n", global_yaml=_tester_block("[codex]"),
              monkeypatch=monkeypatch)
    _assert_models_list_error(exc, "tester")


# ---------------------------------------------------------------------------
# MR-R4: nothing else changes (guards, green before and after)
# ---------------------------------------------------------------------------

def test_mr_r4_the_shipped_empty_model_route_stays_disabled(tmp_path):
    cfg = _load(tmp_path, "agents: {}\n")
    assert _models(cfg, "dev-advisor") == {"opencode-go": ""}


def test_mr_r4_an_empty_model_still_disables_that_one_route(tmp_path, monkeypatch):
    cfg = _load(tmp_path, _tester_block(
        "\n      opencode-go: ''\n      codex: ''\n      agy: gemini-3.8-flash-high\n"))
    result, probes = _run_tester(tmp_path, monkeypatch, cfg,
                                 claude=0.0, codex=1.0, agy=1.0, **{"opencode-go": 1.0})
    assert result.get("provider") == "agy", result
    assert _ran(probes, *PROVIDERS) == ["agy"]


def test_mr_r4_a_model_the_provider_does_not_allow_is_still_rejected(tmp_path):
    with pytest.raises(ValueError) as exc:
        _load(tmp_path, _tester_block("\n      opencode-go: not-a-go-model/x\n"))
    assert "tester" in str(exc.value) and "opencode-go" in str(exc.value)


def test_mr_r4_a_route_that_is_not_a_route_is_still_rejected(tmp_path):
    with pytest.raises(ValueError) as exc:
        _load(tmp_path, _tester_block("\n      opencode: opencode/x\n"))
    assert "opencode" in str(exc.value)


def test_mr_r4_a_map_with_one_valid_route_loads_as_written(tmp_path):
    cfg = _load(tmp_path, _tester_block("\n      codex: gpt-x\n"))
    assert cfg.agents["tester"].fallback_for("codex")[0] == "gpt-x"
