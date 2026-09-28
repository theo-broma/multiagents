"""Codex provider contract, ENGINE half — billing, routing and roster ids.

`context/specs/codex-provider.md`, amendments after the contract review:

- CX-C5  (replaced) `billing: metered|plan`, a provider field (default
         metered). For a `plan` provider the monitor and `multiagents usage`
         show `plan` where they would show a dollar figure, and each
         `budget_status.by_model` entry gains `"billing": "plan"` with
         `cost_usd` still a number. The rendered tree keeps hiding a zero cost.
- CX-C6  (amended) a roster fallback naming an `enabled: false` provider is
         skipped by routing, not crashed on; the roster invariants hold with a
         fourth family and its fallbacks.

Black box through `Provider.from_dict`, `server.budget_status`, `multiagents
usage`, the monitor's `Screen` (a fake curses window records what is drawn),
`Tree.render` and `Runner.start`. The made-up provider is `acme`.

Stubs: `server.budget_mod.read_all` returns `{}` in the by_model test (quota
readers are not under test, as in `test_phase0_context_window.ServerProject`);
the roster tests swap `test_core._shipped_agents` for a fixture roster, which is
how "run those tests against a roster fixture that includes codex fallbacks"
is expressed without editing `test_core.py`.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path

import pytest
import yaml

from multiagents import cli, server
from multiagents.paths import ProjectPaths, global_config_dir, shipped_defaults_dir
from multiagents.providers import Provider
from multiagents.tree import Node, Tree

sys.path.insert(0, str(Path(__file__).parent / "support"))
import c3_harness as h3  # noqa: E402

PLAN, METERED = "acme", "beta"


def _executable(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


def _shipped_disabled() -> dict:
    shipped = yaml.safe_load((shipped_defaults_dir() / "providers.yaml").read_text())
    return {name: {"enabled": False} for name in shipped["providers"]}


# ===========================================================================
# CX-C5 — billing: plan | metered
# ===========================================================================

def test_cx_c5_billing_defaults_to_metered():
    p = Provider.from_dict(PLAN, {"bin": PLAN, "spawn": {"args": ["x"]}})
    assert getattr(p, "billing", None) == "metered"


def test_cx_c5_billing_plan_is_accepted():
    p = Provider.from_dict(PLAN, {"bin": PLAN, "billing": "plan", "spawn": {"args": ["x"]}})
    assert getattr(p, "billing", None) == "plan"


@pytest.fixture
def billed(tmp_path, monkeypatch):
    """A project with a plan-billed `acme` and a metered `beta`, and one
    finished run on each: acme reports tokens and no cost, beta both."""
    root = h3.make_git_repo((tmp_path / "proj").resolve())
    config = root / ".multiagents" / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "providers.yaml").write_text(yaml.safe_dump({"providers": {
        **_shipped_disabled(),
        PLAN: {"bin": PLAN, "billing": "plan", "spawn": {"args": ["x"]}},
        METERED: {"bin": METERED, "spawn": {"args": ["x"]}},
    }}))
    paths = ProjectPaths(root)
    paths.ensure()
    tree = Tree(paths.tree_file, paths.events_file)
    tree.add(Node(id="ag-plan01", agent="worker", provider=PLAN, model="m",
                  parent=None, depth=1, status="done", task="t",
                  usage={"input_tokens": 1200, "output_tokens": 300, "cost_usd": 0}))
    tree.add(Node(id="ag-meter1", agent="checker", provider=METERED, model="m2",
                  parent=None, depth=1, status="done", task="t",
                  usage={"input_tokens": 800, "output_tokens": 200, "cost_usd": 0.25}))
    h3.as_root(monkeypatch)
    monkeypatch.setenv("MULTIAGENTS_PROJECT", str(root))
    monkeypatch.chdir(root)
    return paths


def test_cx_c5_budget_status_by_model_carries_billing(billed, monkeypatch):
    monkeypatch.setattr(server.budget_mod, "read_all", lambda *a, **k: {})
    server._reset()
    try:
        status = server.budget_status()
    finally:
        server._reset()
    if isinstance(status, str):
        status = json.loads(status)
    rows = status.get("by_model") or []
    plan = [r for r in rows if r.get("provider") == PLAN]
    metered = [r for r in rows if r.get("provider") == METERED]
    assert plan and metered, rows
    for row in plan:
        assert row.get("billing") == "plan", f"a plan provider's by_model entry: {row}"
        assert isinstance(row.get("cost_usd"), (int, float)), row
    for row in metered:
        assert row.get("billing", "metered") == "metered", row


def _row(out: str, key: str) -> str:
    lines = [line for line in out.splitlines() if key in line]
    assert lines, f"no line for {key}:\n{out}"
    return lines[0]


def test_cx_c5_usage_labels_a_plan_provider_plan(billed, capsys):
    rc = cli.main(["--path", str(billed.root), "usage"])
    out = capsys.readouterr().out
    assert rc in (0, None), out
    plan_row = _row(out, f"{PLAN}/m")
    assert "plan" in plan_row.split(f"{PLAN}/m", 1)[1], (
        f"`multiagents usage` must label a plan provider's cost `plan`: {plan_row!r}")
    assert "$" not in plan_row, f"no dollar figure for a plan provider: {plan_row!r}"
    assert "$" in _row(out, f"{METERED}/m2"), "a metered row keeps its dollars"


class FakeWindow:
    """Just enough of a curses window for `Screen.put`."""

    def __init__(self, height=60, width=160):
        self.size = (height, width)
        self.lines: dict[int, str] = {}

    def getmaxyx(self):
        return self.size

    def addnstr(self, y, x, text, n, attr=0):
        line = self.lines.get(y, "")
        line = line.ljust(x) if len(line) < x else line
        self.lines[y] = line[:x] + text[:n] + line[x + len(text[:n]):]

    def text(self) -> str:
        return "\n".join(self.lines[y] for y in sorted(self.lines))


def test_cx_c5_monitor_costs_show_plan_for_a_plan_provider(billed):
    from multiagents.monitor.tui import Screen
    window = FakeWindow()
    screen = Screen(window, billed)
    screen.poll(force=True)
    screen.draw_costs(0)
    out = window.text()
    plan_row = _row(out, f"{PLAN}/m")
    assert "plan" in plan_row.split(f"{PLAN}/m", 1)[1], (
        f"the monitor shows `plan` where it would show a dollar figure: {plan_row!r}")
    assert "$" not in plan_row, plan_row
    assert "$" in _row(out, f"{METERED}/m2"), "a metered row keeps its dollars"


def test_cx_c5_rendered_tree_keeps_hiding_a_zero_cost(billed):
    # Guard: "the rendered tree keeps hiding a zero cost, as it does today".
    text = Tree(billed.tree_file, billed.events_file).render()
    line = _row(text, "ag-plan01")
    assert not re.search(r"\$0(\.0+)?\b", line), line


# ===========================================================================
# CX-C6 — a fallback naming an `enabled: false` provider is skipped
# ===========================================================================

def _recording_cli(path: Path, record: Path, label: str) -> dict:
    _executable(path, (
        f"#!{sys.executable}\n"
        "import json\n"
        f"open({str(record)!r}, 'a').write({label!r} + '\\n')\n"
        "print(json.dumps({'type': 'result', 'subtype': 'success', 'result': 'ok'}))\n"))
    return {"bin": str(path), "spawn": {"args": ["--fake-cli"]},
            "stream": {"format": "ndjson", "rules": [
                {"match": {"type": "result"}, "as": "result",
                 "fields": {"status": "subtype", "text": "result"}}]}}


def _start(r, agent="worker"):
    async def go():
        result = await r.start(agent, "go")
        run = r.runs.get(result.get("agent_id", ""))
        if run is not None:
            await asyncio.wait_for(run.done.wait(), timeout=20)
        return result
    return asyncio.run(go())


def test_cx_c6_disabled_family_sibling_is_never_routed_to(tmp_path, monkeypatch):
    clis = tmp_path.parent / (tmp_path.name + "-clis")
    record = clis / "ran.txt"
    main = _recording_cli(clis / "acme-main", record, "acme-main")
    alt = _recording_cli(clis / "acme-alt", record, "acme-alt")
    providers = {
        "acme-main": {**main, "family": "acme"},
        # Sorts first and is idle, so it is exactly what the instance picker
        # would reach for — were it allowed to.
        "acme-alt": {**alt, "family": "acme", "enabled": False},
    }
    spec = h3.AgentSpec(name="worker", provider="acme-main", model="m1")
    r = h3.make_runner(tmp_path, monkeypatch, agents={"worker": spec}, providers=providers)

    result = _start(r)
    ran = record.read_text().split() if record.exists() else []
    assert "acme-alt" not in ran and result.get("provider") != "acme-alt", (
        f"an `enabled: false` provider must never take work; ran={ran} result={result}")
    assert ran == ["acme-main"], f"the work belongs on acme-main: ran={ran} result={result}"


def test_cx_c6_disabled_provider_in_the_fallback_chain_is_skipped(tmp_path, monkeypatch):
    # Guard: acme is out of quota; the chain names a disabled `ghost` first.
    clis = tmp_path.parent / (tmp_path.name + "-clis")
    record = clis / "ran.txt"
    providers = {
        PLAN: _recording_cli(clis / PLAN, record, PLAN),
        "ghost": {**_recording_cli(clis / "ghost", record, "ghost"), "enabled": False},
        METERED: _recording_cli(clis / METERED, record, METERED),
    }
    _executable(global_config_dir() / "providers" / f"{PLAN}.sh",
                "#!/bin/sh\ncase \"$1\" in budget) echo '{\"known\": true, \"headroom\": 0.0}';;"
                " *) exit 64;; esac\n")
    spec = h3.AgentSpec(name="worker", provider=PLAN, model="m1",
                        models={"ghost": "g1", METERED: "b1"})
    r = h3.make_runner(tmp_path, monkeypatch, agents={"worker": spec}, providers=providers,
                       project={"budget": {"fallback_chain": ["ghost", METERED]}})
    result = _start(r)
    ran = record.read_text().split() if record.exists() else []
    assert ran == [METERED], f"ghost is disabled and acme exhausted: ran={ran} result={result}"


# ===========================================================================
# CX-C6 — the roster invariants hold with a fourth family
# ===========================================================================

import test_core  # noqa: E402  (the roster invariants live there)

ROSTER_TESTS = (
    "test_the_coding_tiers_get_their_own_advisor_on_another_family",
    "test_every_working_agent_names_a_cross_provider_fallback",
    "test_a_checking_pair_never_collapses_onto_one_model",
    "test_every_opencode_pin_is_on_the_sixty_dollar_tier",
)


def _shipped():
    return yaml.safe_load((shipped_defaults_dir() / "agents.yaml").read_text())["agents"]


def _with_fourth_family(agents: dict) -> dict:
    """Every working agent gains a distinct `acme:` fallback — the shape a
    codex roster change would take, under a made-up family name."""
    out = {}
    for name, spec in agents.items():
        spec = dict(spec)
        if not (spec.get("launch") or spec.get("disabled")):
            spec["models"] = {**(spec.get("models") or {}), PLAN: f"acme-model-{name}"}
        out[name] = spec
    return out


@pytest.mark.parametrize("name", ROSTER_TESTS)
def test_cx_c6_roster_invariants_accept_a_fourth_family(monkeypatch, name):
    monkeypatch.setattr(test_core, "_shipped_agents",
                        lambda: _with_fourth_family(_shipped()))
    getattr(test_core, name)()


def test_cx_c6_checking_pair_invariant_covers_the_fourth_family(monkeypatch):
    """With the fourth family down, the tester and the implementer both fall
    back to one shared model. The invariant must see that, which it can only
    do if it is not limited to a hardcoded list of families."""
    agents = _with_fourth_family(_shipped())
    shared = "opencode-go/fixture-shared-model"
    agents["tester"] = {**agents["tester"], "provider": PLAN, "model": "acme-big",
                        "models": {"opencode": shared}}
    agents["implementer"] = {**agents["implementer"], "provider": PLAN, "model": "acme-mid",
                             "models": {"opencode": shared}}
    monkeypatch.setattr(test_core, "_shipped_agents", lambda: agents)
    try:
        test_core.test_a_checking_pair_never_collapses_onto_one_model()
    except AssertionError:
        return
    pytest.fail("test_a_checking_pair_never_collapses_onto_one_model accepted a roster "
                f"where, with {PLAN} down, tester and implementer both run {shared}: "
                "its list of families is hardcoded")
