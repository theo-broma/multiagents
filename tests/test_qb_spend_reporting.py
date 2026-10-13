"""QB2/F160/F161: unknown token spend stays distinguishable from zero."""
import json

import pytest
import c3_harness as h3
from test_codex_engine_models_budget import Project, _executable
from multiagents import server
from multiagents.paths import ProjectPaths
from multiagents.tree import Node, Tree, token_count

CLAUDE = {"input_tokens": 10, "output_tokens": 5,
          "cache_read_input_tokens": 20, "cache_creation_input_tokens": 7, "cost_usd": 0.25}


@pytest.fixture
def world(tmp_path, monkeypatch):
    p = Project(tmp_path, monkeypatch, {"acme": {"bin": "acme", "spawn": {"args": ["x"]}}})
    _executable(p.scripts / "acme.sh", '#!/bin/sh\ncase "$1" in budget) echo \'{"known":false}\';; *) exit 64;; esac\n')
    paths = ProjectPaths(p.root)
    paths.ensure()
    tree = Tree(paths.tree_file, paths.events_file)
    h3.as_root(monkeypatch)
    monkeypatch.setenv("MULTIAGENTS_PROJECT", str(p.root))
    server._reset()  # Existing harness resets the server singleton between projects.
    yield tree
    server._reset()


def add(tree, usage, index=0, model="m", segments=None):
    tree.add(Node(id=f"node-{index}", agent="worker", provider="acme", model=model,
                  parent=None, depth=1, status="done", usage=usage, segments=segments or []))


def status():
    result = server.budget_status()
    return json.loads(result) if isinstance(result, str) else result


@pytest.mark.parametrize("surface", ["spent", "by_model", "tree_usage"])
@pytest.mark.parametrize("records,expected,unreported", [
    ([CLAUDE], 42, 0),
    ([{"cost_usd": 0.5}], None, 1),
    ([CLAUDE, {"cost_usd": 0.5}], 42, 1),
    ([{"total": 0, "cost_usd": 0.5}], 0, 0),
    ([{}], 0, 0),
    ([{"total": True, "cost_usd": 0.5}], None, 1),
    ([{"total": "123", "cost_usd": 0.5}], None, 1),
    ([{"cost_usd": 0.25}, {"cost_usd": 0.5}], None, 2),
])
def test_qb2_reporting_surfaces_distinguish_missing_tokens(world, surface, records, expected, unreported):
    for i, usage in enumerate(records):
        add(world, usage, i)
    before = world.read()
    result = status()
    if surface == "spent":
        row = result["providers"]["acme"]["spent"]
    elif surface == "by_model":
        rows = result["by_model"]
        if not any(records):
            assert rows == []  # No usage record remains neither reported nor unreported.
            return
        row = rows[0]
    else:
        row = result["tree_usage"]
    assert row.get("tokens", "missing") == expected, row
    assert row.get("tokens_unreported", "missing") == unreported, row
    assert row["cost_usd"] == sum(u.get("cost_usd", 0) for u in records)
    assert world.read()["nodes"] == before["nodes"]


@pytest.mark.parametrize("key", ["total", "total_tokens", "input_tokens", "output_tokens",
                                 "cache_read_input_tokens", "cache_creation_input_tokens"])
@pytest.mark.parametrize("value", [0, 1, 2.5])
def test_qb2_each_numeric_token_field_reports(world, key, value):
    usage = {key: value}
    add(world, usage)
    result = status()
    for row in (result["providers"]["acme"]["spent"], result["by_model"][0], result["tree_usage"]):
        assert row.get("tokens", "missing") == token_count(usage)
        assert row.get("tokens_unreported", "missing") == 0


@pytest.mark.parametrize("usage,label", [(CLAUDE, "42tok"), ({"cost_usd": 0.25}, "tok?"),
                                         ({"total": 0}, "0tok"), ({"total": False}, "tok?")])
def test_qb2_render_labels_reported_unknown_and_zero(world, usage, label):
    add(world, usage)
    assert label in world.render()


def test_qb2_by_model_sorts_unknown_below_numeric_zero(world):
    add(world, {"cost_usd": 0.0}, model="unknown")
    add(world, {"total": 0, "cost_usd": 0.0}, index=1, model="zero")
    add(world, {"total": 1, "cost_usd": 0.0}, index=2, model="one")
    assert [r["model"] for r in status()["by_model"]] == ["one", "zero", "unknown"]


def test_qb2_provider_and_model_segments_count_reporting_and_unreported_records(world):
    add(world, {"total": 999}, segments=[
        {"provider": "acme", "model": "m", "usage": CLAUDE},
        {"provider": "acme", "model": "m", "usage": {"cost_usd": 0.5}}])
    result = status()
    for row in (result["providers"]["acme"]["spent"], result["by_model"][0]):
        assert row.get("tokens") == 42
        assert row.get("tokens_unreported") == 1
        assert row["cost_usd"] == 0.75


@pytest.mark.parametrize("key", ["total", "total_tokens", "input_tokens", "output_tokens",
                                 "cache_read_input_tokens", "cache_creation_input_tokens"])
@pytest.mark.parametrize("value", [True, False])
def test_qb2_boolean_token_fields_are_unreported(world, key, value):
    add(world, {key: value, "cost_usd": 0.25})
    result = status()
    for row in (result["providers"]["acme"]["spent"], result["by_model"][0], result["tree_usage"]):
        assert row.get("tokens", "missing") is None
        assert row.get("tokens_unreported", "missing") == 1
    assert "tok?" in world.render()
