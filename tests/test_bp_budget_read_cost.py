"""BP — the cost of a budget read (context/specs/bp-budget-read-cost.md).

BP-R1  one `budget.read_all` loads the configuration at most once and parses
       each config file at most once, over a project with >= 3 providers.
BP-R2  the answers do not change: missing layer, malformed layer, and a layer
       edited between two calls are all read exactly as before.

BP-R3 (timings) is reported, not asserted.

Black box: the spies sit on the YAML loader (`yaml.load`, which `safe_load`
goes through) and on `multiagents.config.load`; no helper of budget.py is
named. A cache across calls is allowed by the spec only if the file's
mtime/size invalidates it, which the edit test below holds it to.
"""
from __future__ import annotations

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
from multiagents.executor.local import LocalExecutor  # noqa: E402
from multiagents.providers import load_providers  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_budget_cache():
    budget_mod.invalidate_cache()
    yield
    budget_mod.invalidate_cache()


def _iso(delta_seconds: float) -> str:
    when = datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


class Project:
    """Three script providers and a project config layer under tmp_path."""

    def __init__(self, tmp_path, monkeypatch):
        self.tmp = tmp_path
        self.cfg = tmp_path / "proj-config"
        self.cfg.mkdir()
        self.global_dir = tmp_path / "global-config"
        self.global_dir.mkdir()
        monkeypatch.setenv("MULTIAGENTS_CONFIG_DIR", str(self.global_dir))
        raw = {}
        for name, payload in self._payloads().items():
            pf = tmp_path / f"{name}.json"
            pf.write_text(json.dumps(payload))
            c2.case_script(self.cfg, f"bp-{name}.sh",
                           f'budget) cat "{pf}"; exit 0 ;;')
            raw[name] = {"bin": name, "script": f"bp-{name}.sh"}
        self.providers = load_providers(raw)
        for fname in ("providers.yaml", "agents.yaml", "models.yaml"):
            (self.cfg / fname).write_text("{}\n")
        self.write_project({})

    @staticmethod
    def _payloads():
        return {
            # window reset 200 s ago: reset under the 120 s default margin,
            # still live under a 1000 s margin.
            "alpha": {"known": True, "headroom": 0.1, "resets_at": _iso(-200)},
            # a reading 5000 s old: stale under the 3600 s default bound,
            # fresh under a 10000 s bound.
            "beta": {"known": True, "headroom": 0.5, "resets_at": _iso(86400),
                     "stale_seconds": 5000},
            "gamma": {"known": True, "headroom": 0.8, "resets_at": _iso(86400)},
        }

    def write_project(self, data):
        (self.cfg / "project.yaml").write_text(yaml.safe_dump(data))

    def read(self, project_config="default"):
        return budget_mod.read_all(
            self.providers, lambda n: LocalExecutor(), self.tmp / "g",
            self.cfg if project_config == "default" else project_config)


def _view(out):
    return {n: (b.known, b.headroom, b.severity, b.resets_at, b.stale)
            for n, b in out.items()}


# ---------------------------------------------------------------------------
# BP-R1
# ---------------------------------------------------------------------------

def _spy(monkeypatch):
    parses: Counter = Counter()
    loads: list = []
    real_yaml_load = yaml.load
    real_config_load = config_mod.load

    def yaml_load(stream, *a, **kw):
        key = getattr(stream, "name", None)
        if key is None:
            key = "<text:%s>" % hash(stream if isinstance(stream, (str, bytes)) else id(stream))
        parses[str(key)] += 1
        return real_yaml_load(stream, *a, **kw)

    def config_load(*a, **kw):
        loads.append(1)
        return real_config_load(*a, **kw)

    monkeypatch.setattr(yaml, "load", yaml_load)
    monkeypatch.setattr(config_mod, "load", config_load)
    return parses, loads


def test_bp_r1_one_config_load_per_read_all(tmp_path, monkeypatch):
    p = Project(tmp_path, monkeypatch)
    p.write_project({"limits": {"quota_reset_margin_seconds": 300}})
    _, loads = _spy(monkeypatch)
    out = p.read()
    assert len(out) == 3
    assert len(loads) <= 1


def test_bp_r1_each_config_file_parsed_at_most_once(tmp_path, monkeypatch):
    p = Project(tmp_path, monkeypatch)
    p.write_project({"limits": {"quota_reset_margin_seconds": 300},
                     "budget": {"max_reading_age_seconds": 7200}})
    parses, _ = _spy(monkeypatch)
    out = p.read()
    assert len(out) == 3
    repeated = {path: n for path, n in parses.items() if n > 1}
    assert repeated == {}


def test_bp_r1_a_malformed_layer_is_still_parsed_at_most_once(tmp_path, monkeypatch):
    p = Project(tmp_path, monkeypatch)
    (p.cfg / "project.yaml").write_text("limits: [unclosed\n")
    parses, loads = _spy(monkeypatch)
    out = p.read()
    assert len(out) == 3
    assert len(loads) <= 1
    assert {path: n for path, n in parses.items() if n > 1} == {}


# ---------------------------------------------------------------------------
# BP-R2 — values are today's values
# ---------------------------------------------------------------------------

def test_bp_r2_defaults_when_the_project_layer_sets_nothing(tmp_path, monkeypatch):
    p = Project(tmp_path, monkeypatch)
    v = _view(p.read())
    assert v["alpha"] == (True, 1.0, "normal", None, False)      # reset: 200 >= 120
    assert v["beta"][4] is True                                    # 5000 s > 3600
    assert v["beta"][1] == 0.5
    assert v["gamma"][1:3] == (0.8, "normal") and v["gamma"][4] is False


def test_bp_r2_project_layer_values_win(tmp_path, monkeypatch):
    p = Project(tmp_path, monkeypatch)
    p.write_project({"limits": {"quota_reset_margin_seconds": 1000},
                     "budget": {"max_reading_age_seconds": 10000}})
    v = _view(p.read())
    assert v["alpha"][1:3] == (0.1, "critical") and v["alpha"][3] is not None
    assert v["beta"][4] is False


def test_bp_r2_missing_project_layer_files(tmp_path, monkeypatch):
    p = Project(tmp_path, monkeypatch)
    (p.cfg / "project.yaml").unlink()
    expected = (True, 1.0, "normal", None, False)
    assert _view(p.read())["alpha"] == expected


def test_bp_r2_no_project_config_at_all_uses_global_layer(tmp_path, monkeypatch):
    p = Project(tmp_path, monkeypatch)
    (p.global_dir / "project.yaml").write_text(
        yaml.safe_dump({"limits": {"quota_reset_margin_seconds": 1000}}))
    v = _view(p.read())
    assert v["alpha"][1:3] == (0.1, "critical")


def test_bp_r2_malformed_project_layer_does_not_raise_and_falls_back(tmp_path, monkeypatch):
    p = Project(tmp_path, monkeypatch)
    (p.cfg / "project.yaml").write_text("limits: [unclosed\n")
    v = _view(p.read())
    assert v["alpha"] == (True, 1.0, "normal", None, False)      # shipped 120 s
    assert v["beta"][4] is True                                    # shipped 3600 s


def test_bp_r2_malformed_project_layer_keeps_the_global_layer(tmp_path, monkeypatch):
    p = Project(tmp_path, monkeypatch)
    (p.global_dir / "project.yaml").write_text(
        yaml.safe_dump({"limits": {"quota_reset_margin_seconds": 1000},
                        "budget": {"max_reading_age_seconds": 10000}}))
    (p.cfg / "project.yaml").write_text("limits: [unclosed\n")
    v = _view(p.read())
    assert v["alpha"][1:3] == (0.1, "critical")
    assert v["beta"][4] is False


def test_bp_r2_a_layer_edited_between_two_calls_is_seen_by_the_second(tmp_path, monkeypatch):
    p = Project(tmp_path, monkeypatch)
    first = _view(p.read())
    assert first["alpha"] == (True, 1.0, "normal", None, False)
    assert first["beta"][4] is True
    p.write_project({"limits": {"quota_reset_margin_seconds": 1000},
                     "budget": {"max_reading_age_seconds": 10000}})
    second = _view(p.read())
    assert second["alpha"][1:3] == (0.1, "critical")
    assert second["beta"][4] is False


def test_bp_r2_a_layer_broken_between_two_calls_is_seen_by_the_second(tmp_path, monkeypatch):
    p = Project(tmp_path, monkeypatch)
    p.write_project({"limits": {"quota_reset_margin_seconds": 1000}})
    assert _view(p.read())["alpha"][1:3] == (0.1, "critical")
    (p.cfg / "project.yaml").write_text("limits: [unclosed\n")
    assert _view(p.read())["alpha"] == (True, 1.0, "normal", None, False)
