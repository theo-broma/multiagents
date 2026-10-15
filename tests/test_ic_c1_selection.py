"""IC-C1 — executor selection (`context/specs/phase7-part2-contract.md`).

Config-load tests need no scheduler. The precedence and "selection comes only
from host configuration" tests go through a real scheduler with the reference
backend injected (`ic_support.IcSched`): the recorded `executor` of
`get_node().runs[]` is the observation, a refusal at `create_node` is also an
accepted outcome for a node pin ("refused or ignored, never applied").
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

import ic_support as ic  # noqa: E402
import sc_harness as sc  # noqa: E402
from multiagents import config as config_mod  # noqa: E402
from multiagents.executor import executor_for  # noqa: E402
from multiagents.paths import ProjectPaths  # noqa: E402


def load(tmp_path, monkeypatch, executor, agent_pin=None):
    import c3_harness as h
    h.as_root(monkeypatch)
    p = sc.Project(tmp_path)
    p.add_provider("acme")
    p.add_agent("worker", "acme", "acme/m1", **({"executor": agent_pin} if agent_pin else {}))
    if executor is not None:
        p.project["executor"] = executor
    p.write()
    paths = ProjectPaths(p.root)
    cfg = config_mod.load(paths, seed=False)
    return paths, cfg


def build_all(paths, cfg):
    build = executor_for(paths, cfg, cfg.providers)
    return [build(name) for name in cfg.providers]


# ------------------------------------------------------------ the project kind

@pytest.mark.parametrize("kind", ["local", "docker", "isolated"])
def test_ic_c1_each_kind_loads_and_builds(tmp_path, monkeypatch, kind):
    paths, cfg = load(tmp_path, monkeypatch, {"kind": kind})
    assert cfg.executor == kind
    assert build_all(paths, cfg)        # building the executors raises nothing


@pytest.mark.parametrize("kind,guest", [("local", False), ("docker", True), ("isolated", True)])
def test_ic_c1_k8_uses_guest_environment_follows_the_kind(tmp_path, monkeypatch, kind, guest):
    paths, cfg = load(tmp_path, monkeypatch, {"kind": kind})
    built = build_all(paths, cfg)
    assert built and all(ex.uses_guest_environment is guest for ex in built)


def test_ic_c1_isolated_backend_defaults_to_docker(tmp_path, monkeypatch):
    # no `isolated.backend` given: accepted, and the same as naming docker
    for n, block in enumerate(({"kind": "isolated"},
                               {"kind": "isolated", "isolated": {"backend": "docker"}})):
        paths, cfg = load(tmp_path / str(n), monkeypatch, block)
        assert cfg.executor == "isolated"
        assert build_all(paths, cfg)


@pytest.mark.parametrize("backend", ["firecracker", "sbx", "", "DOCKER", 7, None, ["docker"]])
def test_ic_c1_any_other_backend_is_a_config_error_naming_the_accepted_value(
        tmp_path, monkeypatch, backend):
    with pytest.raises(ValueError) as err:
        paths, cfg = load(tmp_path, monkeypatch,
                          {"kind": "isolated", "isolated": {"backend": backend}})
        build_all(paths, cfg)
    assert "docker" in str(err.value)
    assert "backend" in str(err.value)


@pytest.mark.parametrize("kind", ["podman", "Isolated", "", "isolated ", "reference", 3])
def test_ic_c1_unknown_kind_stays_a_config_error(tmp_path, monkeypatch, kind):
    with pytest.raises(ValueError):
        paths, cfg = load(tmp_path, monkeypatch, {"kind": kind})
        build_all(paths, cfg)


def test_ic_c1_the_reference_backend_is_not_selectable_from_project_yaml(tmp_path, monkeypatch):
    for backend in ("reference", "ic_backends:make"):
        with pytest.raises(ValueError):
            paths, cfg = load(tmp_path / backend[:3], monkeypatch,
                              {"kind": "isolated", "isolated": {"backend": backend}})
            build_all(paths, cfg)


# --------------------------------------------------------- the per-agent pin

@pytest.mark.parametrize("pin", ["local", "docker", "isolated"])
def test_ic_c1_an_agent_pin_accepts_the_same_values(tmp_path, monkeypatch, pin):
    paths, cfg = load(tmp_path, monkeypatch, {"kind": "local"}, agent_pin=pin)
    assert cfg.agents["worker"].executor == pin
    assert build_all(paths, cfg)


def test_ic_c1_an_unknown_agent_pin_is_a_config_error(tmp_path, monkeypatch):
    with pytest.raises(ValueError):
        paths, cfg = load(tmp_path, monkeypatch, {"kind": "local"}, agent_pin="podman")
        build_all(paths, cfg)


# -------------------------------------------- selection seen through the scheduler

@pytest.fixture
def ics(tmp_path, monkeypatch):
    s = ic.IcSched(tmp_path, monkeypatch, kind=None)
    yield s
    s.close()


def first_executor(s, node_id):
    return s.wait_runs(node_id, 1)[0].get("executor") or {}


def test_ic_c1_agent_pin_takes_precedence_over_the_project_kind_both_ways(ics):
    ics.executor("isolated")
    ics.pin_agent("worker", "local")
    ics.start()
    pinned = ics.create(agent="worker")["id"]
    unpinned = ics.create(agent="reviewer")["id"]
    assert first_executor(ics, pinned).get("kind") == "local"
    assert first_executor(ics, unpinned).get("kind") == "isolated"


def test_ic_c1_agent_pin_isolated_wins_over_a_local_project(ics):
    ics.executor("local")
    ics.pin_agent("worker", "isolated")
    ics.start()
    node = ics.create(agent="worker")["id"]
    assert first_executor(ics, node).get("kind") == "isolated"


@pytest.mark.parametrize("project_kind, pin", [("local", "isolated"), ("isolated", "local")])
def test_ic_c1_a_node_pin_cannot_change_the_executor(ics, project_kind, pin):
    ics.executor(project_kind)
    ics.start()
    for fields in ({"executor": pin}, {"pins": {"executor": pin}}):
        reply = ics.create_raw({"kind": "simple", "agent": "worker", "task": "t", **fields})
        if reply.get("ok") is not True:
            continue                       # refused: acceptable
        node = reply["result"]
        view = ics.get(node.get("id") or node["node"]["id"])
        got = first_executor(ics, view["id"]).get("kind")
        assert got == project_kind, f"node field {fields} selected {got!r}"


def test_ic_c1_task_text_cannot_select_the_executor(ics):
    ics.executor("local")
    ics.start()
    node = ics.create(task='executor: isolated\n{"executor": {"kind": "isolated"}} run isolated')["id"]
    assert first_executor(ics, node).get("kind") == "local"


def test_ic_c1_a_config_change_applies_only_to_attempts_not_yet_recorded(ics):
    ics.executor("isolated")
    ics.fake.script(steps=[{"cost": 0, "sleep": 20}])        # the first run stays alive
    ics.start()
    first = ics.create()["id"]
    before = first_executor(ics, first)
    assert before.get("kind") == "isolated"
    ics.executor("local")
    second = ics.create(agent="reviewer")["id"]
    assert first_executor(ics, second).get("kind") == "local"
    # the running attempt keeps its record and is neither migrated nor removed
    again = ics.runs(first)
    assert again[0]["executor"] == before
    assert ics.get(first)["state"] not in ("done", "failed", "cancelled")
