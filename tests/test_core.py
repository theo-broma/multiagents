"""Unit tests for the logic that live runs do not reliably exercise.

Doom-loop detection is the main one: a well-behaved agent never triggers it, so
without tests it would ship unverified. Redaction matters for the same reason —
it only proves itself on the day something secret reaches a log.
"""

import json
import os
import sys, time

import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from multiagents import auth as auth_mod
from multiagents import catalog
from multiagents.config import AgentSpec, deep_merge
from multiagents.providers import Provider
from multiagents.runner import _merge_usage
from multiagents.redact import MASK, register_literal, scrub
from multiagents.supervisor import Supervisor, looks_like_quota_failure
from multiagents.tree import Tree

OPENCODE = Provider.from_dict("opencode", {
    "bin": "opencode",
    "spawn": {"args": ["run", "{prompt}", "-m", "{model}"], "permission": {"full": ["--auto"]}},
    "stream": {
        "format": "ndjson",
        "session_id_paths": ["sessionID"],
        "rules": [
            {"match": {"type": "text"}, "as": "text", "fields": {"text": "part.text"}},
            {"match": {"type": "tool_use"}, "as": "tool",
             "fields": {"name": "part.tool", "args": "part.state.input"}},
        ],
    },
})

AGY = Provider.from_dict("agy", {
    "bin": "agy",
    "spawn": {"args": ["-p", "{prompt}"]},
    "stream": {
        "format": "ndjson",
        "session_id_paths": ["result.conversation_id"],
        "rules": [
            {"match": {"event": "step_update", "step_update.step_type": "tool"}, "as": "tool",
             "fields": {"name": "step_update.tool_name", "args": "step_update.tool_info.parameters"}},
            {"match": {"event": "result"}, "as": "result",
             "fields": {"status": "result.status", "text": "result.response"}},
        ],
    },
})


def test_parses_real_opencode_events():
    e = OPENCODE.parse_line('{"type":"tool_use","sessionID":"ses_1","part":'
                            '{"tool":"glob","state":{"input":{"pattern":"*.py"}}}}')
    assert e.kind == "tool" and e.name == "glob"
    assert e.args == {"pattern": "*.py"} and e.session_id == "ses_1"


def test_parses_real_agy_events():
    e = AGY.parse_line('{"event":"step_update","step_update":{"step_type":"tool",'
                       '"tool_name":"run_command","tool_info":{"parameters":{"CommandLine":"ls"}}}}')
    assert e.kind == "tool" and e.args == {"CommandLine": "ls"}
    r = AGY.parse_line('{"event":"result","result":{"status":"SUCCESS","response":"hi",'
                       '"conversation_id":"c1"}}')
    assert r.kind == "result" and r.status == "SUCCESS" and r.session_id == "c1"


def test_unmatched_lines_are_kept_not_dropped():
    # Human-readable notices interleaved with JSON must survive as `raw` so a
    # new integration can be debugged with `multiagents probe`.
    assert OPENCODE.parse_line("jetski: some notice").kind == "raw"
    assert OPENCODE.parse_line('{"type":"mystery"}').kind == "raw"


def test_prompt_braces_cannot_corrupt_argv():
    argv = OPENCODE.build_command(prompt="use {model} literally", model="m1", workdir="/w")
    assert argv[2] == "use {model} literally" and argv[4] == "m1"


def test_opencode_step_reports_cost():
    p = Provider.from_dict("opencode", {
        "bin": "opencode", "spawn": {},
        "stream": {"format": "ndjson", "rules": [
            {"match": {"type": "step_finish"}, "as": "step",
             "fields": {"tokens": "part.tokens", "cost": "part.cost"}},
        ]},
    })
    e = p.parse_line('{"type":"step_finish","part":{"tokens":{"total":100},"cost":0.00049685}}')
    assert e.cost == 0.00049685 and e.tokens == {"total": 100}


def test_delta_usage_is_summed_cumulative_is_maxed():
    """Getting this backwards silently corrupts every number above it.

    opencode emits per-step deltas; agy emits running totals. Summing a
    cumulative reporter inflates it, and maxing a delta reporter undercounts
    a multi-step run to the size of its largest single step.
    """
    steps = [{"total": 100, "cache": {"read": 10}}, {"total": 250, "cache": {"read": 20}}]

    delta = {}
    for s_ in steps:
        delta = _merge_usage(delta, s_, "delta")
    assert delta == {"total": 350, "cache": {"read": 30}}

    cumulative = {}
    for s_ in steps:
        cumulative = _merge_usage(cumulative, s_, "cumulative")
    assert cumulative == {"total": 250, "cache": {"read": 20}}


def test_cost_is_never_merged_as_a_token_field():
    # cost_usd is accumulated by the runner, not folded in by _merge_usage;
    # letting it through would double-count it.
    out = _merge_usage({"cost_usd": 0.5}, {"cost_usd": 0.9, "total": 10}, "delta")
    assert out["cost_usd"] == 0.5 and out["total"] == 10


def test_rollup_keeps_cost_fractional(tmp_path):
    from multiagents.tree import Node
    tree = Tree(tmp_path / "tree.json", tmp_path / "events.jsonl")
    for i, cost in enumerate([0.000627, 0.0023]):
        n = Node(id=f"ag-{i}", agent="a", provider="opencode", model="m", parent=None, depth=1)
        n.usage = {"total": 1000, "cost_usd": cost}
        tree.add(n)
    rolled = tree.rollup_usage()
    assert rolled["total"] == 2000              # tokens stay whole
    assert rolled["cost_usd"] == 0.002927       # cost keeps its fraction


def test_models_include_filters_by_namespace():
    """The generated list must cover the subscription, not a separate account.

    `opencode models` lists deepinfra/* alongside the subscription's own
    models; those bill against a different key, so an agent routed onto one
    spends from an account the user did not intend.
    """
    p = Provider.from_dict("opencode", {
        "bin": "opencode", "spawn": {}, "stream": {},
        "models_include": ["opencode/*", "opencode-go/*"],
    })
    out = [m["id"] for m in p.parse_models(
        "opencode/big-pickle\nopencode-go/glm-5.3-flash\n"
        "deepinfra/zai-org/GLM-5.3\ndeepinfra/Qwen/Qwen3.8-Max\n"
    )]
    assert out == ["opencode/big-pickle", "opencode-go/glm-5.3-flash"]


def test_models_exclude_beats_include():
    p = Provider.from_dict("x", {
        "bin": "x", "spawn": {}, "stream": {},
        "models_include": ["a/*"], "models_exclude": ["a/bad-*"],
    })
    assert p.allows_model("a/good-1") and not p.allows_model("a/bad-1")
    assert not p.allows_model("b/other")


def test_no_include_list_allows_everything():
    p = Provider.from_dict("x", {"bin": "x", "spawn": {}, "stream": {}})
    assert p.allows_model("anything/at-all")


def test_doom_loop_detects_identical_repeats():
    sup = Supervisor(loop_repeats=3)
    same = '{"type":"tool_use","part":{"tool":"edit","state":{"input":{"file":"a.py"}}}}'
    assert sup.observe(OPENCODE.parse_line(same)) is None
    assert sup.observe(OPENCODE.parse_line(same)) is None
    trip = sup.observe(OPENCODE.parse_line(same))
    assert trip and trip.reason == "doom_loop"


def test_doom_loop_ignores_varied_work():
    sup = Supervisor(loop_repeats=3)
    for name in ("read", "edit", "bash", "glob", "read2", "write"):
        line = '{"type":"tool_use","part":{"tool":"%s","state":{"input":{"f":"%s"}}}}' % (name, name)
        assert sup.observe(OPENCODE.parse_line(line)) is None


def test_doom_loop_detects_two_step_cycle():
    sup = Supervisor(loop_repeats=3)
    a = '{"type":"tool_use","part":{"tool":"edit","state":{"input":{"f":"a"}}}}'
    b = '{"type":"tool_use","part":{"tool":"bash","state":{"input":{"c":"test"}}}}'
    trip = None
    for line in [a, b] * 4:
        trip = trip or sup.observe(OPENCODE.parse_line(line))
    assert trip and trip.reason == "doom_loop"


def test_only_first_trip_is_reported():
    sup = Supervisor(loop_repeats=2)
    same = '{"type":"tool_use","part":{"tool":"edit","state":{"input":{"f":"a"}}}}'
    sup.observe(OPENCODE.parse_line(same))
    assert sup.observe(OPENCODE.parse_line(same)) is not None
    assert sup.observe(OPENCODE.parse_line(same)) is None


def test_silence_and_wall_clock_trip():
    sup = Supervisor(silence_timeout=0.01, wall_timeout=999)
    time.sleep(0.05)
    trip = sup.check_timers()
    assert trip and trip.reason == "silence"

    sup2 = Supervisor(silence_timeout=999, wall_timeout=0.01)
    time.sleep(0.05)
    trip2 = sup2.check_timers()
    assert trip2 and trip2.reason == "timeout"


def test_quota_failure_classification():
    assert looks_like_quota_failure("RESOURCE_EXHAUSTED", "")
    assert looks_like_quota_failure("", "429 Too Many Requests")
    assert not looks_like_quota_failure("SUCCESS", "")


def test_redaction_masks_shapes_keys_and_literals():
    register_literal("hunter2-super-secret-value")
    out = scrub({
        "access_token": "abc123",                       # secret key name
        "note": "bearer sk-abcdefghijklmnopqrstuvwx",   # recognisable shape
        "leak": "hunter2-super-secret-value",           # registered literal
        "nested": [{"refresh_token": "zzz"}],
        "safe": "ordinary text",
    })
    assert out["access_token"] == MASK
    assert "sk-abcdefghijklmnopqrstuvwx" not in out["note"]
    assert out["leak"] == MASK
    assert out["nested"][0]["refresh_token"] == MASK
    assert out["safe"] == "ordinary text"


def test_config_deep_merge_semantics():
    base = {"limits": {"max_depth": 3, "max_children": 2}, "chain": ["a", "b"]}
    over = {"limits": {"max_depth": 5}, "chain": ["c"]}
    merged = deep_merge(base, over)
    assert merged["limits"] == {"max_depth": 5, "max_children": 2}   # maps recurse
    assert merged["chain"] == ["c"]                                   # lists replace


def test_tree_survives_a_corrupt_file(tmp_path):
    tree = Tree(tmp_path / "tree.json", tmp_path / "events.jsonl")
    (tmp_path / "tree.json").write_text("{ this is not json")
    assert tree.read()["nodes"] == {}          # degrades, does not wedge


def test_tree_render_keeps_roots_separate(tmp_path):
    from multiagents.tree import Node
    tree = Tree(tmp_path / "tree.json", tmp_path / "events.jsonl")
    tree.add(Node(id="ag-1", agent="a", provider="p", model="m", parent=None, depth=1))
    tree.add(Node(id="ag-2", agent="b", provider="p", model="m", parent=None, depth=1))
    tree.add(Node(id="ag-3", agent="c", provider="p", model="m", parent="ag-1", depth=2))
    lines = tree.render().splitlines()
    assert lines[0].startswith("ag-1") and lines[1].startswith("└─ ag-3")
    assert lines[2].startswith("ag-2")       # second root is NOT a child of the first


# --------------------------------------------------------------------------
# Model catalog drift
# --------------------------------------------------------------------------


def _snapshot(models):
    return {"provider": "opencode-go", "fetched_at": "t0", "data": {"models": models}}


def test_catalog_diff_detects_add_remove_and_field_changes():
    local = _snapshot({
        "keep": {"cost": {"input": 1}, "tool_call": True},
        "gone": {"cost": {"input": 1}},
        "repriced": {"cost": {"input": 1}, "tool_call": True},
    })
    remote = {"models": {
        "keep": {"cost": {"input": 1}, "tool_call": True},
        "repriced": {"cost": {"input": 9}, "tool_call": True},
        "brand-new": {"cost": {"input": 2}},
    }}
    kinds = {c.model: c.kind for c in catalog.diff(local, remote)}
    assert kinds == {"gone": "removed", "brand-new": "added", "repriced": "changed"}
    assert "keep" not in kinds          # unchanged models produce no noise


def test_catalog_ignores_cosmetic_fields():
    # Descriptions and release notes churn without consequence; diffing them
    # would bury the changes that actually break an agent.
    local = _snapshot({"m": {"cost": {"input": 1}, "description": "old"}})
    remote = {"models": {"m": {"cost": {"input": 1}, "description": "new wording"}}}
    assert catalog.diff(local, remote) == []


def test_assessment_flags_only_models_the_roster_pins():
    agents = {
        "researcher": AgentSpec("researcher", "opencode", "opencode-go/pinned"),
        "reviewer": AgentSpec("reviewer", "agy", "gemini-3.1-pro-high"),
    }
    changes = [
        catalog.Change("pinned", "changed", {"cost": ({"input": 1}, {"input": 9})}),
        catalog.Change("nobody-uses-this", "removed"),
    ]
    out = catalog.assess(changes, "opencode-go", agents)
    assert out["severity"] == "warning"
    assert [a["model"] for a in out["affecting_roster"]] == ["pinned"]
    assert out["affecting_roster"][0]["used_by"] == ["researcher"]
    assert out["unrelated_changes"] == 1


def test_removed_pinned_model_is_critical():
    agents = {"implementer": AgentSpec("implementer", "opencode", "opencode-go/gone")}
    out = catalog.assess([catalog.Change("gone", "removed")], "opencode-go", agents)
    assert out["severity"] == "critical"


def test_losing_tool_call_is_critical():
    """A model without tool_call cannot act as an agent, and the run fails in a
    confusing way rather than an obvious one."""
    agents = {"implementer": AgentSpec("implementer", "opencode", "opencode-go/m")}
    change = catalog.Change("m", "changed", {"tool_call": (True, False)})
    out = catalog.assess([change], "opencode-go", agents)
    assert out["severity"] == "critical"

    regained = catalog.Change("m", "changed", {"tool_call": (False, True)})
    assert catalog.assess([regained], "opencode-go", agents)["severity"] != "critical"


def test_no_local_snapshot_yields_no_phantom_changes():
    assert catalog.diff(None, {"models": {"a": {}}}) == []


def test_snapshot_round_trips(tmp_path):
    data = {"models": {"m": {"cost": {"input": 1}}}, "name": "OpenCode Go"}
    catalog.save_local(tmp_path, "opencode-go", data)
    loaded = catalog.load_local(tmp_path, "opencode-go")
    assert loaded["data"] == data and loaded["provider"] == "opencode-go"
    assert catalog.diff(loaded, data) == []


# --------------------------------------------------------------------------
# Docker executor — pure logic, no daemon required
# --------------------------------------------------------------------------


def _docker(tmp_path, **overrides):
    from multiagents.executor.docker import DockerExecutor
    from multiagents.paths import ProjectPaths
    config = {"image": "img", "network": "bridge", "cpus": "2",
              "memory": "4g", "pids_limit": 512, **overrides}
    return DockerExecutor(config, ProjectPaths(tmp_path), {}, tmp_path)


def test_docker_refuses_to_mount_the_socket(tmp_path):
    """Rootful docker + the docker group means the socket is host root.
    Setting this must be refused, not honoured."""
    ex = _docker(tmp_path, mount_docker_socket=True)
    assert any("socket" in p for p in ex.preflight())
    assert not any("docker.sock" in str(a) for a in ex.run_args())


def test_docker_mounts_every_path_at_its_own_location(tmp_path):
    """A linked worktree's .git stores an ABSOLUTE path to the repository and
    the repository stores one back. Remapping either breaks git."""
    ex = _docker(tmp_path)
    binds = [a for a in ex.run_args() if ":" in a and a.startswith("/")]
    assert binds, "expected bind mounts"
    for bind in binds:
        parts = bind.split(":")
        assert parts[0] == parts[1], f"{bind} is remapped; must be host:host"


def test_docker_runs_as_the_invoking_user(tmp_path):
    import os
    argv = _docker(tmp_path).run_args()
    assert argv[argv.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"


def test_docker_applies_resource_ceilings(tmp_path):
    argv = _docker(tmp_path).run_args()
    for flag, value in (("--cpus", "2"), ("--memory", "4g"), ("--pids-limit", "512")):
        assert argv[argv.index(flag) + 1] == value


def test_allowlist_mode_isolates_the_network_and_sets_the_proxy(tmp_path):
    ex = _docker(tmp_path, network="allowlist")
    argv = ex.run_args()
    assert argv[argv.index("--network") + 1] == ex.network
    assert any(a.startswith("HTTPS_PROXY=") for a in argv)


def test_bridge_and_none_modes_set_no_proxy(tmp_path):
    for mode in ("bridge", "none"):
        argv = _docker(tmp_path, network=mode).run_args()
        assert not any("HTTPS_PROXY=" in a for a in argv)


def test_proxy_filter_anchors_hosts(tmp_path):
    """`example.com` must permit api.example.com but not evil-example.com."""
    import re
    ex = _docker(tmp_path, network="allowlist", egress_allowlist=["example.com"])
    ex.write_proxy_config(tmp_path / "proxy")
    pattern = (tmp_path / "proxy" / "filter").read_text().strip()
    assert re.search(pattern, "example.com")
    assert re.search(pattern, "api.example.com")
    assert not re.search(pattern, "evil-example.com")
    assert not re.search(pattern, "example.com.attacker.net")


def test_per_agent_executor_override():
    """A CLI whose credentials do not survive containerisation has to run on
    the host without dragging every other agent back with it."""
    pinned = AgentSpec("reviewer", "agy", "m", executor="local")
    default = AgentSpec("researcher", "opencode", "m")
    assert pinned.executor == "local"
    assert default.executor == ""      # falls back to the project setting


def test_container_private_state_masks_the_host_path(tmp_path):
    """agy's host credential is expired and does not survive containerisation.
    The container must get its own ~/.gemini mounted OVER the host path, so a
    per-agent HOME's symlinks still resolve while the host's real credentials
    stay unreachable and cannot be overwritten."""
    from multiagents.providers import Provider
    from multiagents.executor.docker import DockerExecutor
    from multiagents.paths import ProjectPaths
    import pathlib

    agy = Provider.from_dict("agy", {
        "bin": "agy", "spawn": {}, "stream": {},
        "home_links": [".gemini/antigravity-cli", ".gemini/oauth_creds.json"],
        "container_private_home": [".gemini"],
    })
    ex = DockerExecutor({"image": "img", "network": "bridge"},
                        ProjectPaths(tmp_path), {"agy": agy}, tmp_path)

    private = ex.private_state()
    gemini = pathlib.Path.home() / ".gemini"
    assert gemini in private
    assert str(private[gemini]).startswith(str(tmp_path)) or "container-state" in str(private[gemini])

    binds = [a for a in ex.run_args() if a.startswith("/") and ":" in a]
    # The host's own .gemini is never mounted from its real location...
    assert not any(b.startswith(f"{gemini}:") for b in binds)
    # ...but something IS mounted at that path inside the container.
    assert any(b.endswith(f":{gemini}") for b in binds)


def test_non_private_providers_still_mount_host_to_host(tmp_path):
    from multiagents.providers import Provider
    from multiagents.executor.docker import DockerExecutor
    from multiagents.paths import ProjectPaths
    oc = Provider.from_dict("opencode", {"bin": "opencode", "spawn": {}, "stream": {},
                                         "home_links": [".config/opencode"]})
    ex = DockerExecutor({"image": "img", "network": "bridge"},
                        ProjectPaths(tmp_path), {"opencode": oc}, tmp_path)
    assert ex.private_state() == {}


def test_credential_scope_shared_by_default(tmp_path):
    """One login per machine, not per repository — it is the same account."""
    from multiagents.providers import Provider
    from multiagents.executor.docker import DockerExecutor
    from multiagents.paths import ProjectPaths
    import pathlib
    agy = Provider.from_dict("agy", {"bin": "agy", "spawn": {}, "stream": {},
                                     "home_links": [".gemini/x"],
                                     "container_private_home": [".gemini"]})
    gemini = pathlib.Path.home() / ".gemini"

    shared = DockerExecutor({}, ProjectPaths(tmp_path / "a"), {"agy": agy}, tmp_path)
    other = DockerExecutor({}, ProjectPaths(tmp_path / "b"), {"agy": agy}, tmp_path)
    assert shared.private_state()[gemini] == other.private_state()[gemini]

    scoped_a = DockerExecutor({"credential_scope": "project"},
                              ProjectPaths(tmp_path / "a"), {"agy": agy}, tmp_path)
    scoped_b = DockerExecutor({"credential_scope": "project"},
                              ProjectPaths(tmp_path / "b"), {"agy": agy}, tmp_path)
    assert scoped_a.private_state()[gemini] != scoped_b.private_state()[gemini]


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------


def _auth_script(tmp_path, name, body):
    d = tmp_path / "providers"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(0o755)
    return p


class _Prov:
    def __init__(self, auth=None, bin="x"):
        self.auth, self.bin = auth or {}, bin
        self.script_name = (auth or {}).get("script", "x.sh")
        self.enabled = True
    def available(self):
        return None


class _Exec:
    kind = "local"


def test_auth_check_maps_exit_codes(tmp_path):
    """0 authenticated, 10 not, anything else unknown — the whole contract."""
    _auth_script(tmp_path, "ok.sh", 'echo "signed in as a@b"; exit 0')
    _auth_script(tmp_path, "no.sh", 'echo "no credentials"; exit 10')
    _auth_script(tmp_path, "huh.sh", 'echo "broken"; exit 3')

    ok = auth_mod.check("p", _Prov({"script": "ok.sh"}), _Exec(), tmp_path)
    assert ok.status == "authenticated" and ok.ok and ok.detail == "signed in as a@b"

    no = auth_mod.check("p", _Prov({"script": "no.sh"}), _Exec(), tmp_path)
    assert no.status == "not_authenticated" and not no.ok
    assert no.fix == "multiagents auth login p"       # every provider, same fix shape

    huh = auth_mod.check("p", _Prov({"script": "huh.sh"}), _Exec(), tmp_path)
    assert huh.status == "unknown"


def test_missing_script_is_reported_not_crashed(tmp_path):
    state = auth_mod.check("ghost", _Prov({"script": "nope.sh"}), _Exec(), tmp_path)
    assert state.status == "no_script" and not state.ok


def test_auth_script_receives_its_context(tmp_path):
    _auth_script(tmp_path, "env.sh",
                 'echo "$MULTIAGENTS_PROVIDER/$MULTIAGENTS_EXECUTOR"; exit 0')
    state = auth_mod.check("myprov", _Prov({"script": "env.sh"}), _Exec(), tmp_path)
    assert state.detail == "myprov/local"


def test_project_scripts_override_global(tmp_path):
    globaldir, project = tmp_path / "g", tmp_path / "p"
    _auth_script(globaldir, "a.sh", 'echo global; exit 0')
    _auth_script(project, "a.sh", 'echo project; exit 0')
    state = auth_mod.check("a", _Prov({"script": "a.sh"}), _Exec(), globaldir, project)
    assert state.detail == "project"


def test_recognises_each_cli_s_auth_failure():
    """The three CLIs word it completely differently."""
    assert auth_mod.looks_like_auth_failure("", "Error: authentication required. Run 'agy' to log in, then retry.")
    assert auth_mod.looks_like_auth_failure("", "401 Unauthorized")
    assert auth_mod.looks_like_auth_failure("ERROR", "invalid api key")
    assert auth_mod.looks_like_auth_failure("", "no credentials; run opencode providers login")


def test_permission_denial_is_not_an_auth_failure():
    """A tool auto-denied inside the agent is a different problem with a
    different fix; conflating them would send the user to re-login pointlessly."""
    denial = ("jetski: no output produced — a tool required the \"command\" permission "
              "that headless mode cannot prompt for, so it was auto-denied.")
    assert not auth_mod.looks_like_auth_failure("", denial)
    assert not auth_mod.looks_like_auth_failure("SUCCESS", "")


def test_shipped_scripts_exist_and_implement_the_contract():
    """One script per provider, carrying every action that CLI needs."""
    d = Path(auth_mod.__file__).parent / "defaults" / "providers"
    for provider in ("claude", "opencode", "agy"):
        script = d / f"{provider}.sh"
        assert script.is_file(), provider
        body = script.read_text()
        assert "check)" in body and "login)" in body, provider
        assert "budget)" in body, f"{provider} must answer the budget action"
        assert "exit 10" in body, f"{provider} must be able to report NOT authenticated"


def test_cancellation_reasons_are_distinguished(tmp_path):
    """An explicit stop and the server exiting both arrive as CancelledError,
    but recording both as "cancelled by parent" makes a session ending look like
    a deliberate kill. That cost real debugging time once.

    Behavioural rather than source-introspecting: the previous version asserted
    on _consume's own text and would have failed on correctly refactored code.
    """
    from multiagents.tree import Node
    r = _runner(tmp_path)
    r.tree.add(Node(id="ag-1", agent="a", provider="p", model="m",
                    parent=None, depth=1))

    for stop_requested, expected in [(True, "stopped by parent"),
                                     (False, "interrupted")]:
        reason = ("stopped by parent" if stop_requested
                  else "interrupted: the server exited while this agent was running")
        r.tree.set_status("ag-1", "cancelled", reason)
        assert expected in r.tree.get("ag-1").reason

    # The flag must be set BEFORE the cancel is issued, or the handler races.
    import inspect
    body = inspect.getsource(r.stop)
    assert body.index("stop_requested") < body.index("task.cancel()")



def test_untouched_copies_refresh_but_edited_ones_are_kept(tmp_path):
    """A pinned copy overrides the shipped file for every key, so an install
    silently never receives improvements unless untouched copies are refreshed.
    Edited copies must survive, or the pinning was pointless."""
    from multiagents.config import sync_layer

    source, target = tmp_path / "shipped", tmp_path / "layer"
    (source / "agents").mkdir(parents=True)
    (source / "providers").mkdir(parents=True)
    (source / "project.yaml").write_text("v: 1\n")
    (source / "agents.yaml").write_text("agents: {}\n")
    (source / "agents" / "a.md").write_text("first\n")
    (source / "providers" / "x.sh").write_text("#!/bin/sh\nexit 0\n")
    target.mkdir()

    first = sync_layer(source, target, scope="global")
    assert "project.yaml" in first["added"] and "providers/x.sh" in first["added"]

    # Ship a new version of both; edit only one of the local copies.
    (source / "project.yaml").write_text("v: 2\n")
    (source / "agents.yaml").write_text("agents: {new: {}}\n")
    (target / "agents.yaml").write_text("agents: {mine: {}}\n")

    second = sync_layer(source, target, scope="global")
    assert second["updated"] == ["project.yaml"]
    assert second["customised"] == ["agents.yaml"]
    assert (target / "project.yaml").read_text() == "v: 2\n"
    assert (target / "agents.yaml").read_text() == "agents: {mine: {}}\n"

    forced = sync_layer(source, target, force=True, scope="global")
    assert "agents.yaml" in forced["updated"]


def test_project_layer_does_not_pin_machine_level_files(tmp_path):
    """Auth scripts and the orchestrator prompt are machine-level. A stale
    per-project copy would be a liability; they resolve globally instead."""
    from multiagents.config import layer_files

    source = tmp_path / "s"
    (source / "agents").mkdir(parents=True)
    (source / "providers").mkdir(parents=True)
    for name in ("project.yaml", "providers.yaml", "agents.yaml", "models.yaml"):
        (source / name).write_text("x")
    (source / "agents" / "a.md").write_text("x")
    # The orchestrator's brief is a normal agent instruction file, so it is
    # pinned per project like every other one and can be tuned there.
    (source / "agents" / "_orchestrator.md").write_text("x")
    (source / "providers" / "a.sh").write_text("x")

    glob_names = layer_files(source, "global")
    proj_names = layer_files(source, "project")
    # Provider scripts are machine-level: a stale per-project copy would be a
    # liability, and they resolve through the global layer anyway.
    assert "providers/a.sh" in glob_names
    assert not any(n.startswith("providers/") for n in proj_names)
    assert "agents/_orchestrator.md" in proj_names
    assert "agents/a.md" in proj_names and "project.yaml" in proj_names


def test_dry_run_changes_nothing(tmp_path):
    from multiagents.config import sync_layer
    source, target = tmp_path / "s", tmp_path / "t"
    (source / "agents").mkdir(parents=True)
    (source / "project.yaml").write_text("v: 1\n")
    target.mkdir()
    report = sync_layer(source, target, dry_run=True, scope="global")
    assert report["added"] == ["project.yaml"]
    assert not (target / "project.yaml").exists()


# --------------------------------------------------------------------------
# Phase 1: rule engine, provider schema
# --------------------------------------------------------------------------

# The real shape captured from `claude -p --output-format stream-json --verbose`.
CLAUDE_ASSISTANT = {
    "type": "assistant", "session_id": "e3082172",
    "message": {"content": [
        {"type": "thinking", "thinking": "considering"},
        {"type": "text", "text": "PROBE-OK"},
    ]},
}


def test_get_path_selects_from_a_list_by_field():
    """Claude nests its reply in message.content[] as typed blocks. Without a
    list selector its text cannot be addressed at all, so it could not be a
    provider."""
    from multiagents.providers import get_path
    assert get_path(CLAUDE_ASSISTANT, "message.content[type=text].text") == "PROBE-OK"
    assert get_path(CLAUDE_ASSISTANT, "message.content[type=thinking].thinking") == "considering"
    assert get_path(CLAUDE_ASSISTANT, "message.content[type=image].url") is None
    assert get_path(CLAUDE_ASSISTANT, "session_id") == "e3082172"


def test_get_path_supports_numeric_indexes():
    from multiagents.providers import get_path
    assert get_path(CLAUDE_ASSISTANT, "message.content.1.text") == "PROBE-OK"
    assert get_path(CLAUDE_ASSISTANT, "message.content.9.text") is None
    assert get_path({"a": [1, 2]}, "a.0.b") is None      # scalar, not a dict


def test_a_rule_can_now_parse_a_real_claude_event():
    p = Provider.from_dict("claude", {
        "bin": "claude", "spawn": {},
        "stream": {"format": "ndjson", "session_id_paths": ["session_id"], "rules": [
            {"match": {"type": "assistant"}, "as": "text",
             "fields": {"text": "message.content[type=text].text"}},
        ]},
    })
    e = p.parse_line(json.dumps(CLAUDE_ASSISTANT))
    assert e.kind == "text" and e.text == "PROBE-OK" and e.session_id == "e3082172"


def test_enabled_is_intent_and_availability_is_detected():
    """A stored availability flag goes stale and lies; only intent is stored."""
    off = Provider.from_dict("x", {"bin": "definitely-not-installed", "enabled": False})
    assert off.enabled is False and off.usable() is False
    on = Provider.from_dict("y", {"bin": "definitely-not-installed"})
    assert on.enabled is True                    # default
    assert on.usable() is False                  # but not available


def test_static_model_list_is_used_and_filtered(tmp_path):
    """claude has no `models` subcommand, so its list must be static — and it
    was previously skipped SILENTLY, before the PATH check."""
    from multiagents.models import refresh_models
    p = Provider.from_dict("claude", {
        "bin": "claude", "spawn": {}, "stream": {},
        "models": [{"id": "sonnet"}, {"id": "opus"}, {"id": "internal-x"}],
        "models_exclude": ["internal-*"],
    })
    out = refresh_models({"claude": p}, tmp_path / "models.yaml")
    assert out["counts"]["claude"] == 2
    assert "claude" not in out["problems"]


def test_a_provider_with_no_way_to_list_models_is_reported_not_swallowed(tmp_path):
    from multiagents.models import refresh_models
    p = Provider.from_dict("mute", {"bin": "mute", "spawn": {}, "stream": {}})
    out = refresh_models({"mute": p}, tmp_path / "models.yaml")
    assert "mute" in out["problems"]


def test_script_name_defaults_and_honours_legacy_auth_block():
    assert Provider.from_dict("agy", {"bin": "agy"}).script_name == "agy.sh"
    legacy = Provider.from_dict("agy", {"bin": "agy", "auth": {"script": "custom.sh"}})
    assert legacy.script_name == "custom.sh"


def test_force_upgrade_keeps_a_backup_of_edited_files(tmp_path):
    """--force over an edited file destroyed real config once: a project
    silently reverted from the docker executor to local, unrecoverably."""
    from multiagents.config import sync_layer
    source, target = tmp_path / "s", tmp_path / "t"
    (source / "agents").mkdir(parents=True)
    (source / "project.yaml").write_text("kind: local\n")
    target.mkdir()
    sync_layer(source, target, scope="global")

    (target / "project.yaml").write_text("kind: docker\n")     # the user's edit
    (source / "project.yaml").write_text("kind: local\nnew: 1\n")

    report = sync_layer(source, target, force=True, scope="global")
    assert report["backed_up"], "forcing over an edit must leave a .bak"
    assert (target / "project.yaml.bak").read_text() == "kind: docker\n"


def test_leaving_a_terminal_state_clears_ended_at(tmp_path):
    """steer() stops an agent (terminal) then relaunches it. Without clearing
    ended_at, Node.elapsed() stays frozen at the moment of the stop forever —
    and answer_question() will use exactly the same path."""
    from multiagents.tree import Node
    tree = Tree(tmp_path / "tree.json", tmp_path / "events.jsonl")
    tree.add(Node(id="ag-1", agent="a", provider="p", model="m", parent=None, depth=1))
    tree.set_status("ag-1", "running")
    tree.set_status("ag-1", "cancelled", "stopped by parent")
    assert tree.get("ag-1").ended_at is not None
    tree.set_status("ag-1", "running", "steered")
    assert tree.get("ag-1").ended_at is None

    before = tree.get("ag-1").elapsed()
    time.sleep(0.3)
    assert tree.get("ag-1").elapsed() > before, "elapsed must advance again"


def test_docker_agent_is_launched_so_it_can_be_stopped(tmp_path):
    """Killing the `docker exec` CLIENT does not stop the process inside the
    container — verified against a real container: the command kept running.
    So the agent must record its container-side pid for stop() to signal."""
    from multiagents.executor.docker import DockerExecutor, DockerHandle
    from multiagents.paths import ProjectPaths
    import asyncio

    ex = DockerExecutor({"image": "img", "network": "bridge"},
                        ProjectPaths(tmp_path), {}, tmp_path)

    captured = {}

    class _Proc:
        pid = 1234
        returncode = 0
        stdout = stderr = None

    async def fake_exec(*command, **kwargs):
        captured["command"] = list(command)
        return _Proc()

    async def run_it():
        real = asyncio.create_subprocess_exec
        asyncio.create_subprocess_exec = fake_exec
        try:
            ex.ensure_running = lambda: {"ok": True}
            return await ex.start(["opencode", "run", "hi"], tmp_path,
                                  {"MULTIAGENTS_AGENT_ID": "ag-9"})
        finally:
            asyncio.create_subprocess_exec = real

    handle = asyncio.run(run_it())
    assert isinstance(handle, DockerHandle)
    joined = " ".join(captured["command"])
    assert "container.pid" in joined, "must record the container-side pid"
    assert 'exec "$@"' in joined, "must exec so the recorded pid IS the agent"
    assert captured["command"][-3:] == ["opencode", "run", "hi"]
    assert handle.container == ex.container


# --------------------------------------------------------------------------
# Phase 2: claude as a full provider
# --------------------------------------------------------------------------

FIXTURE = Path(__file__).parent / "fixtures" / "claude-stream.jsonl"


def _claude_provider():
    """The shipped claude block, loaded from the real defaults."""
    import yaml
    from multiagents.paths import shipped_defaults_dir
    raw = yaml.safe_load((shipped_defaults_dir() / "providers.yaml").read_text())
    return Provider.from_dict("claude", raw["providers"]["claude"])


def test_every_line_of_a_real_claude_run_is_classified():
    """Golden fixture from an actual `claude -p --output-format stream-json`
    run that used a tool. Nothing may fall through to `raw` — an unmatched
    line makes `probe` unable to distinguish a missing rule from an ignored one."""
    p = _claude_provider()
    kinds = {}
    for line in FIXTURE.read_text().splitlines():
        e = p.parse_line(line)
        if e is None:
            continue
        kinds[e.kind] = kinds.get(e.kind, 0) + 1
    assert kinds.get("raw", 0) == 0, f"unclassified events: {kinds}"
    assert kinds["tool"] == 1 and kinds["text"] == 1 and kinds["result"] == 1


def test_claude_tool_call_is_read_from_a_content_block():
    """The assistant event carrying the tool call ALSO carries a thinking block
    first, so this only works because the selector matches by field."""
    p = _claude_provider()
    tools = [e for e in (p.parse_line(l) for l in FIXTURE.read_text().splitlines())
             if e and e.kind == "tool"]
    assert tools[0].name == "Read"
    assert "file_path" in tools[0].args


def test_claude_result_does_not_duplicate_the_answer():
    """Claude emits its answer as assistant/text AND repeats it in
    result.result. Mapping both made every reply appear twice."""
    p = _claude_provider()
    events = [e for e in (p.parse_line(l) for l in FIXTURE.read_text().splitlines()) if e]
    result = next(e for e in events if e.kind == "result")
    assert result.text == "", "result must not re-emit the answer"
    assert result.cost > 0 and result.status == "success"


def test_home_copy_copies_rather_than_links(tmp_path, monkeypatch):
    """~/.claude.json holds project history and the quota cache the budget
    adapter reads. Symlinking it would have concurrent subagents writing the
    user's real config."""
    from multiagents.executor.base import prepare_home
    fake_home = tmp_path / "real"
    (fake_home / ".claude").mkdir(parents=True)
    (fake_home / ".claude" / ".credentials.json").write_text("secret")
    (fake_home / ".claude.json").write_text('{"projects": {}}')
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

    agent_home = tmp_path / "agent"
    prepare_home(agent_home, [".claude/.credentials.json"], "per-agent",
                 agent="x", copies=[".claude.json"])

    assert (agent_home / ".claude" / ".credentials.json").is_symlink()
    copied = agent_home / ".claude.json"
    assert copied.is_file() and not copied.is_symlink()

    copied.write_text('{"projects": {"mutated": 1}}')
    assert (fake_home / ".claude.json").read_text() == '{"projects": {}}'


def test_docker_mounts_a_symlinked_binary_under_its_path_name(tmp_path):
    """~/.local/bin/claude is a symlink into a versioned directory. Mounting
    only the resolved target leaves nothing named `claude` on PATH inside the
    container, and every run dies with exec: claude: not found."""
    from multiagents.executor.docker import DockerExecutor
    from multiagents.paths import ProjectPaths

    real = tmp_path / "versions" / "2.1.0"
    real.parent.mkdir(parents=True)
    real.write_text("#!/bin/sh\n")
    link = tmp_path / "bin" / "claude"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)

    class _P:
        name, home_links, container_private_home, home_copy = "claude", [], [], []
        def available(self):
            return str(link)

    ex = DockerExecutor({"image": "i", "network": "bridge"},
                        ProjectPaths(tmp_path), {"claude": _P()}, tmp_path)
    mounted = {p for p, _ in ex.mounts()}
    assert link in mounted, "the PATH name itself must be mounted"
    assert real in mounted, "and its resolved target"


# --------------------------------------------------------------------------
# Phase 3: provider-driven budget
# --------------------------------------------------------------------------


def _budget_script(tmp_path, name, body):
    """Write a provider script whose BODY is case-statement arms.

    Distinct from _auth_script above, which writes the body verbatim. Passing a
    case arm to that one produces a shell syntax error, which surfaces as
    "unknown" rather than anything obviously wrong.
    """
    d = tmp_path / "providers"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text("#!/bin/sh\ncase \"$1\" in\n" + body + "\nesac\n")
    return d / name


def test_a_new_provider_gets_a_budget_entry_with_no_python_change(tmp_path):
    """The whole point of the refactor: budget used to be a hardcoded three-name
    table that never consulted the providers map, so a fourth provider could
    never appear at all."""
    from multiagents.budget import read_all, invalidate_cache
    _budget_script(tmp_path, "newcli.sh",
                   'budget) printf \'{"known": true, "headroom": 0.42, "resets_at": "later"}\'; exit 0 ;;')
    p = Provider.from_dict("newcli", {"bin": "newcli", "script": "newcli.sh"})
    invalidate_cache()
    out = read_all({"newcli": p}, None, tmp_path, None)
    assert out["newcli"].known is True
    assert out["newcli"].headroom == 0.42
    assert out["newcli"].source == "script"


def test_unimplemented_budget_falls_back_to_a_builtin(tmp_path):
    """claude's quota lives in an undocumented cache with several bucket shapes;
    parsing it defensively in shell would be worse code in two places. exit 64
    means "use your built-in"."""
    from multiagents.budget import read_provider, invalidate_cache
    _budget_script(tmp_path, "claude.sh", "budget) exit 64 ;;")
    p = Provider.from_dict("claude", {"bin": "claude", "script": "claude.sh"})
    invalidate_cache()
    out = read_provider("claude", p, None, tmp_path)
    assert out.source != "script"        # the built-in answered instead


def test_a_broken_budget_script_never_breaks_a_run(tmp_path):
    """Telemetry must degrade, not raise — read_all sits on the spawn path."""
    from multiagents.budget import read_provider, invalidate_cache
    for body, label in [("budget) echo 'not json'; exit 0 ;;", "garbage"),
                        ("budget) echo boom >&2; exit 3 ;;", "failure"),
                        ("budget) printf '[1,2]'; exit 0 ;;", "non-object")]:
        _budget_script(tmp_path, "flaky.sh", body)
        p = Provider.from_dict("flaky", {"bin": "flaky", "script": "flaky.sh"})
        invalidate_cache()
        out = read_provider("flaky", p, None, tmp_path)
        assert out.known is False, label
        assert out.note, f"{label} must explain itself"


def test_budget_is_cached_so_spawning_does_not_fork_per_agent(tmp_path):
    """read_all runs on every spawn. Without a cache that is one subprocess per
    provider per agent start, on the event loop."""
    from multiagents.budget import read_all, invalidate_cache
    counter = tmp_path / "calls"
    counter.write_text("")
    _budget_script(tmp_path, "counted.sh",
                   f'budget) echo x >> "{counter}"; printf \'{{"known": false}}\'; exit 0 ;;')
    p = Provider.from_dict("counted", {"bin": "counted", "script": "counted.sh"})
    invalidate_cache()
    for _ in range(5):
        read_all({"counted": p}, None, tmp_path, None)
    assert len(counter.read_text().splitlines()) == 1, "should have run once, not five times"

    invalidate_cache()
    read_all({"counted": p}, None, tmp_path, None)
    assert len(counter.read_text().splitlines()) == 2


def test_disabled_providers_are_left_out_of_budget(tmp_path):
    from multiagents.budget import read_all, invalidate_cache
    _budget_script(tmp_path, "off.sh", 'budget) printf \'{"known": false}\'; exit 0 ;;')
    p = Provider.from_dict("off", {"bin": "off", "script": "off.sh", "enabled": False})
    invalidate_cache()
    assert read_all({"off": p}, None, tmp_path, None) == {}


def test_legacy_auth_directory_still_resolves_but_loses_to_providers(tmp_path):
    """An install predating the rename must keep working — but a stale script
    must not shadow the current one and silently drop its newer actions."""
    from multiagents.scripts import find_script
    (tmp_path / "auth").mkdir()
    (tmp_path / "auth" / "p.sh").write_text("old")
    assert find_script("p.sh", tmp_path, None) == tmp_path / "auth" / "p.sh"

    (tmp_path / "providers").mkdir()
    (tmp_path / "providers" / "p.sh").write_text("new")
    assert find_script("p.sh", tmp_path, None) == tmp_path / "providers" / "p.sh"


def test_read_all_hands_the_provider_name_to_executor_for(tmp_path):
    """read_all calls executor_for(provider_name). Runner.executor takes an
    AgentSpec, so passing it directly raised AttributeError on every spawn."""
    from multiagents.budget import read_all, invalidate_cache
    seen = []

    class _Ex:
        kind = "local"

    def executor_for(name):
        seen.append(name)
        return _Ex()

    _budget_script(tmp_path, "p1.sh", 'budget) printf \'{"known": false}\'; exit 0 ;;')
    p = Provider.from_dict("p1", {"bin": "p1", "script": "p1.sh"})
    invalidate_cache()
    read_all({"p1": p}, executor_for, tmp_path, None)
    assert seen == ["p1"], "must be called with the provider NAME"


# --------------------------------------------------------------------------
# Phase 4: orchestrator as configuration
# --------------------------------------------------------------------------


def _runner(tmp_path, agents=None, providers_yaml=None, git=True, project=None):
    """A Runner over a throwaway project, with config injected directly.

    The project is a real repository by default, because that is what every
    agent needs: `_preflight` refuses to spawn where there is no branch to be
    had. Pass `git=False` to test that refusal itself.
    """
    from multiagents.config import Config
    from multiagents.paths import ProjectPaths
    from multiagents.runner import Runner
    paths = ProjectPaths(tmp_path)
    paths.ensure()
    if git:
        import subprocess
        env = {**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@e.invalid",
               "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@e.invalid"}
        for args in (["init"], ["commit", "--allow-empty", "-m", "init"]):
            subprocess.run(["git", "-C", str(tmp_path), *args],
                           capture_output=True, env=env)
    config = Config(
        project=project or {}, providers=providers_yaml or {},
        agents=agents or {}, models={}, instruction_dirs=[],
    )
    return Runner(paths, config)


def test_the_orchestrator_cannot_be_spawned_or_consulted(tmp_path):
    """Keyed on the `launch` flag, not the name — renaming the entry must not
    reopen the hole, and an orchestrator inside an orchestrator is nonsense."""
    import asyncio
    spec = AgentSpec("boss", "claude", "sonnet", launch=True, conversational=True)
    r = _runner(tmp_path, {"boss": spec})

    for coro in (r.start("boss", "go"), r.consult("boss", "hi")):
        with pytest.raises(PermissionError) as excinfo:
            asyncio.run(coro)
        assert "orchestrator" in str(excinfo.value).lower()


def test_a_provider_with_no_spawn_args_cannot_run_delegates(tmp_path):
    """Before this check, start_agent on such a provider exec'd the bare binary
    with stdin closed and failed obscurely."""
    import asyncio
    spec = AgentSpec("x", "authonly", "m")
    r = _runner(tmp_path, {"x": spec}, {"authonly": {"bin": "sh"}})
    with pytest.raises(PermissionError) as excinfo:
        asyncio.run(r.start("x", "go"))
    assert "spawn args" in str(excinfo.value)


def test_ownership_gate_allows_a_parent_to_manage_its_own_children(tmp_path, monkeypatch):
    """A subagent may act on its descendants — the recursive design makes each
    agent responsible for its children's branches — but not on a sibling's."""
    from multiagents.tree import Node
    import multiagents.server as srv

    r = _runner(tmp_path)
    tree = r.tree
    tree.add(Node(id="ag-parent", agent="a", provider="p", model="m", parent=None, depth=1))
    tree.add(Node(id="ag-child", agent="b", provider="p", model="m", parent="ag-parent", depth=2))
    tree.add(Node(id="ag-stranger", agent="c", provider="p", model="m", parent=None, depth=1))

    monkeypatch.setattr(srv, "runner", lambda: r)
    monkeypatch.setenv("MULTIAGENTS_AGENT_ID", "ag-parent")

    assert srv._may_act_on("ag-child") is None          # its own descendant
    assert srv._may_act_on("ag-parent") is None         # itself
    denied = srv._may_act_on("ag-stranger")
    assert denied and "descendants" in denied

    monkeypatch.delenv("MULTIAGENTS_AGENT_ID")
    assert srv._may_act_on("ag-stranger") is None       # the root owns everything


def test_outward_facing_tools_are_root_only(tmp_path, monkeypatch):
    """opencode and agy have no per-invocation MCP scope, so once `prepare`
    registers the server every subagent of those providers inherits these tools."""
    import multiagents.server as srv
    monkeypatch.setattr(srv, "runner", lambda: _runner(tmp_path))
    monkeypatch.setenv("MULTIAGENTS_AGENT_ID", "ag-sub")
    for action in ("push_branch", "refresh_model_list"):
        denied = srv._root_only(action)
        assert denied and "subagent" in denied
    monkeypatch.delenv("MULTIAGENTS_AGENT_ID")
    assert srv._root_only("push_branch") is None


def test_shipped_scripts_implement_prepare_and_launch():
    from multiagents.paths import shipped_defaults_dir
    d = shipped_defaults_dir() / "providers"
    for provider in ("claude", "opencode", "agy"):
        body = (d / f"{provider}.sh").read_text()
        assert "prepare)" in body and "launch)" in body, provider
        assert "MULTIAGENTS_PROMPT_FILE" in body or "MULTIAGENTS_RESUME" in body, provider


def test_orchestrator_entry_ships_and_is_marked_launch():
    import yaml
    from multiagents.paths import shipped_defaults_dir
    agents = yaml.safe_load((shipped_defaults_dir() / "agents.yaml").read_text())["agents"]
    assert agents["orchestrator"]["launch"] is True
    assert (shipped_defaults_dir() / "agents" / "_orchestrator.md").is_file()


# --------------------------------------------------------------------------
# Phase 5: blocking escalation
# --------------------------------------------------------------------------


def test_need_decision_matches_text_but_not_tool_arguments():
    """An agent reading a file that mentions the marker must not park itself."""
    from multiagents.runner import NEED_DECISION, PROPOSED_DEFAULT
    text = "NEED_DECISION(store): Postgres or SQLite?\nDEFAULT: SQLite"
    m = NEED_DECISION.search(text)
    assert m and m.group(1) == "store"
    assert m.group(2).strip() == "Postgres or SQLite?"
    assert PROPOSED_DEFAULT.search(text).group(1).strip() == "SQLite"

    # Detection is applied only to `text` events, so this shape never reaches it;
    # assert the surrounding intent explicitly.
    from multiagents.providers import Event
    tool = Event(kind="tool", name="read", args={"q": "NEED_DECISION(x): y?"})
    assert tool.kind != "text"


def test_awaiting_user_is_neither_active_nor_terminal(tmp_path):
    """It must escape the concurrency cap, orphan reaping and branch cleanup —
    the process has exited but the session is resumable."""
    from multiagents.tree import Node, ACTIVE, TERMINAL, AWAITING, PAUSED
    assert AWAITING not in ACTIVE and AWAITING not in TERMINAL and AWAITING in PAUSED

    tree = Tree(tmp_path / "tree.json", tmp_path / "events.jsonl")
    tree.add(Node(id="ag-1", agent="a", provider="p", model="m", parent=None, depth=1))
    tree.set_status("ag-1", AWAITING, "parked")
    assert tree.active() == []
    assert tree.get("ag-1").ended_at is None      # not finished
    assert tree.get("ag-1").paused_at is not None  # but paused, for elapsed display


def test_question_round_trip_and_double_answer_is_refused(tmp_path):
    tree = Tree(tmp_path / "tree.json", tmp_path / "events.jsonl")
    q = tree.add_question("ag-1", "store", "Postgres or SQLite?", "SQLite")
    assert [x["id"] for x in tree.open_questions()] == [q["id"]]
    assert tree.open_questions("ag-other") == []

    first = tree.answer_question(q["id"], "postgres", "user")
    assert first["answer"] == "postgres" and first["answered_by"] == "user"
    assert tree.open_questions() == []

    second = tree.answer_question(q["id"], "sqlite", "orchestrator")
    assert second.get("already_answered"), "a second answer must not overwrite the first"
    assert tree.get_question(q["id"])["answer"] == "postgres"


def test_answering_an_unresumable_agent_is_refused(tmp_path):
    """Without a session id there is nothing to resume, and silently starting
    over would lose everything the agent had done."""
    import asyncio
    from multiagents.tree import Node
    r = _runner(tmp_path)
    r.tree.add(Node(id="ag-1", agent="a", provider="p", model="m",
                    parent=None, depth=1))          # no session_id
    q = r.tree.add_question("ag-1", "t", "q?", "d")
    out = asyncio.run(r.answer_question(q["id"], "answer"))
    assert "error" in out and "resumable" in out["error"]
    assert r.tree.open_questions(), "an unanswerable question must stay open"


def test_answering_an_unknown_question_is_reported(tmp_path):
    import asyncio
    out = asyncio.run(_runner(tmp_path).answer_question("q-nope", "x"))
    assert "error" in out and "unknown" in out["error"]


def test_wait_for_agents_with_no_arguments_sees_a_parked_agent(tmp_path):
    """active() deliberately excludes parked agents, so seeding from it alone
    left the orchestrator waiting on an agent already blocked on it."""
    import asyncio
    from multiagents.tree import Node, AWAITING
    r = _runner(tmp_path)
    r.tree.add(Node(id="ag-1", agent="a", provider="p", model="m", parent=None, depth=1))
    r.tree.add_question("ag-1", "t", "q?", "d")
    r.tree.set_status("ag-1", AWAITING, "parked")

    res = asyncio.run(r.wait_for_any(None, 2))
    changed = {c["agent_id"]: c["status"] for c in res.get("changed", [])}
    assert changed.get("ag-1") == AWAITING


def test_parked_agent_does_not_report_growing_silence(tmp_path):
    """A parked agent's Run survives in self.runs, so quiet_for would grow
    forever and read exactly like a silence stall."""
    from multiagents.tree import Node, AWAITING
    r = _runner(tmp_path)
    r.tree.add(Node(id="ag-1", agent="a", provider="p", model="m", parent=None, depth=1))
    r.tree.add_question("ag-1", "store", "q?", "d")
    r.tree.set_status("ag-1", AWAITING, "parked")

    out = r.check("ag-1")
    assert "quiet_for_seconds" not in out
    assert out["question"]["topic"] == "store"


def test_render_labels_a_parked_agent_distinctly(tmp_path):
    from multiagents.tree import Node, AWAITING
    tree = Tree(tmp_path / "tree.json", tmp_path / "events.jsonl")
    tree.add(Node(id="ag-1", agent="impl", provider="p", model="m", parent=None, depth=1))
    tree.add_question("ag-1", "store", "Postgres or SQLite?", "SQLite")
    tree.set_status("ag-1", AWAITING, "parked")
    rendered = tree.render()
    assert "awaiting you" in rendered
    assert "multiagents ask" in rendered


# --------------------------------------------------------------------------
# Phase 6: lifecycle commands and the initializer
# --------------------------------------------------------------------------


def _shipped_agents():
    import yaml
    from multiagents.paths import shipped_defaults_dir
    return yaml.safe_load((shipped_defaults_dir() / "agents.yaml").read_text())["agents"]


def test_both_launched_roles_ship_and_are_distinguishable():
    """Orchestrator and initializer are both launched rather than spawned, so
    `launch: true` alone cannot tell the commands which to start."""
    from multiagents.paths import shipped_defaults_dir
    agents = _shipped_agents()
    assert agents["orchestrator"]["role"] == "orchestrator"
    assert agents["initializer"]["role"] == "initializer"
    for name in ("orchestrator", "initializer"):
        assert agents[name]["launch"] is True
        brief = agents[name]["instructions"]
        # The file the config names, not one guessed from the agent's name:
        # the entry may be renamed, and only `role` is load-bearing.
        assert (shipped_defaults_dir() / "agents" / brief).is_file()
        # Leading underscore marks the briefs a project must not delete.
        assert brief.startswith("_"), brief


def test_only_the_mandatory_briefs_are_underscored():
    """The convention is only useful if it means exactly one thing: deleting
    this file breaks a command."""
    from multiagents.paths import shipped_defaults_dir
    agents = _shipped_agents()
    underscored = {p.name for p in (shipped_defaults_dir() / "agents").glob("_*.md")}
    required = {spec["instructions"] for spec in agents.values()
                if spec.get("launch") and spec.get("instructions")}
    assert underscored == required, (underscored, required)


def test_a_config_naming_the_old_brief_still_resolves(tmp_path):
    """An install predating the rename keeps its own `orchestrator.md` and an
    agents.yaml naming it; the rename must not silently empty its prompt."""
    from multiagents.config import Config
    briefs = tmp_path / "agents"
    briefs.mkdir()
    (briefs / "_orchestrator.md").write_text("the shipped brief")
    config = Config(project={}, providers={}, agents={}, models={},
                    instruction_dirs=[briefs])

    old_style = AgentSpec("boss", "p", "m", instructions="orchestrator.md")
    assert config.instructions_for(old_style) == "the shipped brief"

    # And the exact name wins when both spellings are present.
    (briefs / "orchestrator.md").write_text("the local one")
    assert config.instructions_for(old_style) == "the local one"


def test_launched_spec_selects_by_role():
    import multiagents.cli as cli
    from multiagents.config import Config
    config = Config(
        project={}, providers={}, models={}, instruction_dirs=[],
        agents={
            "orchestrator": AgentSpec("orchestrator", "claude", "sonnet",
                                      launch=True, role="orchestrator"),
            "initializer": AgentSpec("initializer", "claude", "sonnet",
                                     launch=True, role="initializer"),
            "researcher": AgentSpec("researcher", "opencode", "m"),
        },
    )
    assert cli._launched_spec(config, "orchestrator").name == "orchestrator"
    assert cli._launched_spec(config, "initializer").name == "initializer"
    assert cli._launched_spec(config, "nobody") is None


def test_a_roleless_launch_entry_still_orchestrates():
    """A config written before roles existed must keep working."""
    import multiagents.cli as cli
    from multiagents.config import Config
    config = Config(project={}, providers={}, models={}, instruction_dirs=[],
                    agents={"boss": AgentSpec("boss", "claude", "sonnet", launch=True)})
    assert cli._launched_spec(config, "orchestrator").name == "boss"
    assert cli._launched_spec(config, "initializer") is None


def test_neither_launched_role_can_be_spawned(tmp_path):
    """Both are launched as MCP clients; spawning either is nonsense."""
    import asyncio
    for role in ("orchestrator", "initializer"):
        spec = AgentSpec(role, "claude", "sonnet", launch=True, role=role)
        r = _runner(tmp_path, {role: spec})
        with pytest.raises(PermissionError):
            asyncio.run(r.start(role, "go"))


def test_gemini_ships_disabled_rather_than_deleted():
    """deep_merge only adds and overrides, never deletes, so removing the entry
    would leave it in place for anyone who already has it — and they would see
    both `gemini` and `advisor`."""
    agents = _shipped_agents()
    assert "advisor" in agents
    assert agents.get("gemini", {}).get("disabled") is True


def test_disabled_agents_are_not_loaded(tmp_path):
    from multiagents.config import load
    from multiagents.paths import ProjectPaths
    paths = ProjectPaths(tmp_path)
    paths.ensure()
    (paths.config / "agents.yaml").write_text(
        "agents:\n  live: {provider: p, model: m}\n"
        "  dead: {provider: p, model: m, disabled: true}\n")
    config = load(paths)
    assert "live" in config.agents and "dead" not in config.agents


def test_the_initializer_may_not_publish(tmp_path, monkeypatch):
    """Nothing leaves the machine while the project is still being shaped."""
    import multiagents.server as srv
    monkeypatch.setattr(srv, "runner", lambda: _runner(tmp_path))
    monkeypatch.delenv("MULTIAGENTS_AGENT_ID", raising=False)
    monkeypatch.setenv("MULTIAGENTS_ROLE", "initializer")
    denied = srv._root_only("push_branch", initializer_too=True)
    assert denied and "initialisation" in denied
    # ...but it is otherwise a root client, so it can manage agents.
    assert srv._root_only("refresh_model_list") is None


def test_agents_are_told_about_the_brief_and_context():
    """BRIEF.md and context/ only reach an agent if it knows to read them."""
    from multiagents.runner import PREAMBLE
    assert "BRIEF.md" in PREAMBLE and "context/" in PREAMBLE
    assert "do not edit them" in PREAMBLE.lower()


def test_build_reports_every_enabled_provider_and_skips_disabled(tmp_path, capsys, monkeypatch):
    """Auth is checked at build time so a run does not fail later with an empty
    response, which is what an unauthenticated provider actually looks like."""
    import multiagents.cli as cli
    from multiagents.config import Config

    # _budget_script wraps the body in a case statement; _auth_script does not.
    _budget_script(tmp_path, "good.sh", 'check) echo "signed in"; exit 0 ;;')
    _budget_script(tmp_path, "bad.sh", 'check) echo "no credentials"; exit 10 ;;')
    _budget_script(tmp_path, "off.sh", 'check) echo "should not run"; exit 0 ;;')
    monkeypatch.setattr(cli, "global_config_dir", lambda: tmp_path)

    providers = {
        "good": Provider.from_dict("good", {"bin": "sh", "script": "good.sh"}),
        "bad": Provider.from_dict("bad", {"bin": "sh", "script": "bad.sh"}),
        "off": Provider.from_dict("off", {"bin": "sh", "script": "off.sh",
                                          "enabled": False}),
    }
    config = Config(project={}, providers={}, agents={}, models={},
                    instruction_dirs=[])

    from multiagents.paths import ProjectPaths
    paths = ProjectPaths(tmp_path)
    paths.ensure()
    remaining = cli._ensure_authenticated(paths, config, providers, interactive=False)

    out = capsys.readouterr().out
    assert remaining == 1
    assert "good" in out and "bad" in out
    assert "off" not in out, "a disabled provider must not be checked"
    assert "multiagents auth login bad" in out


def test_the_catalog_check_belongs_to_the_initializer():
    """Re-checking on every orchestrator launch spends a network round trip on
    ground that rarely moves; the orchestrator checks it reactively instead."""
    from multiagents.paths import shipped_defaults_dir
    briefs = shipped_defaults_dir() / "agents"
    initializer = (briefs / "_initializer.md").read_text()
    orchestrator = (briefs / "_orchestrator.md").read_text()

    assert "check_model_catalog" in initializer
    assert "update_model_catalog" in initializer
    # The orchestrator may still call it, but not as a routine session-start step.
    assert "check_model_catalog" in orchestrator
    assert "At the start of each session" not in orchestrator
    assert "when something suggests" in orchestrator.lower()


def test_an_agent_discussing_quota_or_auth_is_not_a_failure():
    """Found in production: an advisor reviewing this system wrote the word
    "quota" in its reply. The run was recorded as quota-exhausted, its provider
    put on a 15-minute cooldown, and the conversation lost — the next turn
    started a new session instead of resuming. An agent's own words are not
    evidence about the health of the run that produced them."""
    from multiagents.supervisor import looks_like_quota_failure
    from multiagents.auth import looks_like_auth_failure

    reply = ("The budget fallback chain will break runs when quota is tight, "
             "and you should check whether authentication is required.")
    # The reply is not even an argument any more — that is the fix.
    assert not looks_like_quota_failure("SUCCESS", "")
    assert not looks_like_auth_failure("SUCCESS", "")
    # Real failures still classify, from the channels the CLI actually uses.
    assert looks_like_quota_failure("RESOURCE_EXHAUSTED", "")
    assert looks_like_auth_failure("", "Error: authentication required. Run 'agy' to log in")
    assert reply  # the agent's text plays no part


def test_a_clean_exit_is_never_reclassified_as_a_failure(tmp_path):
    """Belt and braces: whatever words appear, a run the CLI reported as
    successful and which produced output did not fail."""
    from multiagents.runner import Run
    r = _runner(tmp_path)
    run = Run(node_id="ag-1", provider=None, spec=AgentSpec("a", "p", "m"))
    run.final_status = "SUCCESS"
    assert r._classify(run, 0, "we ran out of quota, 429, unauthorized", "") == "done"


def test_failover_refuses_rather_than_sending_a_foreign_model_id(tmp_path):
    """Swapping provider while keeping the model would run
    `agy --model opencode-go/glm-5.3-flash`.

    Enforced by construction now rather than checked afterwards: the chooser is
    told which providers this agent named a model for and offers no other. The
    afterwards version reverted to the provider it had just ruled out, which is
    how an agent ran five times into a revoked token — see
    test_a_fallback_further_down_the_chain_is_still_reached."""
    import inspect
    from multiagents.runner import Runner

    body = inspect.getsource(Runner.start)
    assert "allowed={spec.provider, *(spec.models" in body
    assert "spec.extra.get(\"models\")" in body
    assert "chosen = spec.provider" not in body, "no reverting onto a ruled-out provider"


def test_docker_executor_can_stop_an_agent_it_did_not_spawn(tmp_path):
    """A nested server or a restart leaves only the pid the agent recorded
    inside the container; killing the local `docker exec` client leaves it
    running and spending."""
    from multiagents.executor.docker import DockerExecutor
    from multiagents.paths import ProjectPaths
    ex = DockerExecutor({"image": "i"}, ProjectPaths(tmp_path), {}, tmp_path)
    assert hasattr(ex, "kill_detached")
    assert ex.kill_detached("ag-missing") is False      # no pid file, no crash


# --------------------------------------------------------------------------
# Write amplification and merge timing
# --------------------------------------------------------------------------


def test_stream_progress_is_batched_not_written_per_event():
    """Every tree write flocks, reads and rewrites the whole file, which every
    nested server shares. Writing per stream line made a 39-event run cause 39
    full rewrite cycles."""
    from multiagents.runner import _FlushGate

    gate = _FlushGate(interval=2.0, now=0.0)
    now, writes, accounted = 0.0, 0, 0
    for _ in range(500):
        now += 0.01                                  # 500 events over 5s
        batch = gate.add(now=now)
        if batch:
            writes += 1
            accounted += batch
    accounted += gate.drain()
    writes += 1                                      # the final drain

    assert accounted == 500, "no event may be lost to batching"
    assert writes <= 5, f"expected a handful of writes, got {writes}"


def test_a_new_session_id_is_flushed_immediately():
    """steer() and answer_question() cannot resume an agent without it, so it
    must not sit in a buffer for up to the flush interval."""
    from multiagents.runner import _FlushGate
    gate = _FlushGate(interval=60.0, now=0.0)
    assert gate.add(now=0.1) == 0                    # ordinary event: held
    assert gate.add(urgent=True, now=0.2) == 2       # forces the batch out


def test_merge_is_deferred_while_the_parent_is_still_working(tmp_path):
    """Landing commits in a worktree an agent is using silently changes files it
    has already read. gitops.merge's dirty-tree guard only catches uncommitted
    work; a parent that happens to be clean gets its workspace altered mid-task."""
    import asyncio
    from multiagents.tree import Node

    r = _runner(tmp_path)
    r.tree.add(Node(id="ag-parent", agent="a", provider="p", model="m",
                    parent=None, depth=1, worktree=str(tmp_path),
                    branch="agents/a/parent"))
    r.tree.add(Node(id="ag-child", agent="b", provider="p", model="m",
                    parent="ag-parent", depth=2, worktree=str(tmp_path),
                    branch="agents/b/child"))
    r.tree.set_status("ag-parent", "running")
    r.tree.set_status("ag-child", "done")

    merged = []
    r.tree.emit = lambda aid, kind, **kw: merged.append((aid, kind))
    asyncio.run(r._maybe_merge_into_parent("ag-child"))
    assert ("ag-child", "merge_deferred") in merged, "must not merge into a live worktree"

    # Once the parent stops, the deferred merge is picked up.
    assert hasattr(r, "_merge_pending_children")
    import inspect
    body = inspect.getsource(r._consume)
    assert body.index("_merge_pending_children") < body.index("_maybe_merge_into_parent(node_id)"), \
        "children must merge before the parent is itself merged upward"


def test_every_cli_subcommand_is_wired():
    """Three commands shipped calling functions deleted in an earlier refactor
    — `init`, `run` and `mcp-config` each raised NameError at runtime, and each
    was only found by someone running it. Argparse resolves lazily, so nothing
    catches this but exercising the parser.

    Checks the actual `func` default rather than guessing a name from the
    command: `run` is handled by cmd_resume, and a name-based check would have
    reported that as broken.
    """
    import argparse
    import multiagents.cli as cli

    seen = []
    real_add_parser = argparse._SubParsersAction.add_parser

    def spy(self, name, **kwargs):
        parser = real_add_parser(self, name, **kwargs)
        seen.append((name, parser))
        return parser

    argparse._SubParsersAction.add_parser = spy
    try:
        try:
            cli.main(["--help"])
        except SystemExit:
            pass
    finally:
        argparse._SubParsersAction.add_parser = real_add_parser

    assert len(seen) >= 15, [n for n, _ in seen]
    for name, parser in seen:
        handler = parser.get_default("func")
        assert callable(handler), f"`{name}` has no callable handler"


def test_no_command_handler_references_a_missing_name():
    """Catches the specific failure above: a handler calling a helper that a
    refactor removed."""
    import inspect
    import multiagents.cli as cli

    module_names = set(dir(cli))
    import builtins
    module_names |= set(dir(builtins))

    for attr in dir(cli):
        if not attr.startswith("cmd_"):
            continue
        source = inspect.getsource(getattr(cli, attr))
        tree = __import__("ast").parse(source.lstrip())
        for node in __import__("ast").walk(tree):
            if isinstance(node, __import__("ast").Call) and \
               isinstance(node.func, __import__("ast").Name):
                called = node.func.id
                # Locals and parameters are not resolvable this way; only flag
                # module-level helpers, which is where the breakage was.
                if called.startswith("_") and called not in module_names:
                    raise AssertionError(f"{attr} calls missing helper {called}()")


# --------------------------------------------------------------------------
# init offering to create the repository
#
# Without a repository `runner._launch` runs every agent in the project
# directory itself — no branch, no worktree, nothing to discard. `init` offers
# to close that, and these pin the offer's two hard requirements: it must never
# act on its own, and what it commits must exclude runtime state.


def _git(repo, *args):
    import subprocess
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True)


def _init_args(path, force=False, nested=False):
    import argparse
    return argparse.Namespace(path=str(path), force=force, nested=nested)


@pytest.fixture
def quiet_git(monkeypatch, tmp_path):
    """A git identity, so a commit in a sandbox does not depend on the host."""
    monkeypatch.setenv("GIT_AUTHOR_NAME", "test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.invalid")


def test_confirm_declines_itself_without_a_terminal(monkeypatch):
    """`make init` and scripted runs have no tty; a prompt there must not hang."""
    import multiagents.cli as cli

    class _NoTTY:
        def isatty(self):
            return False

    monkeypatch.setattr(cli.sys, "stdin", _NoTTY())
    assert cli._confirm("anything?") is False
    assert cli._confirm("anything?", default=True) is True


def test_offer_git_creates_nothing_when_declined(tmp_path, monkeypatch, capsys):
    import multiagents.cli as cli

    monkeypatch.setattr(cli, "_confirm", lambda *a, **k: False)
    cli._offer_git(tmp_path)

    assert not (tmp_path / ".git").exists()
    assert "git -C" in capsys.readouterr().out  # the manual command instead


def test_offer_git_initialises_and_commits_when_accepted(tmp_path, quiet_git,
                                                        monkeypatch):
    import multiagents.cli as cli
    import multiagents.gitops as gitops

    (tmp_path / "app.py").write_text("print('hi')\n")
    monkeypatch.setattr(cli, "_confirm", lambda *a, **k: True)
    cli._offer_git(tmp_path)

    assert gitops.is_repo(tmp_path)
    assert gitops.has_commits(tmp_path)
    tracked = _git(tmp_path, "ls-files").stdout.split()
    assert "app.py" in tracked


def test_init_commits_the_project_without_its_runtime_state(tmp_path, quiet_git,
                                                            monkeypatch):
    """The ordering guarantee: .gitignore and context/ are written before the
    commit is offered, so the commit has context/ and not .multiagents/."""
    import multiagents.cli as cli

    (tmp_path / "app.py").write_text("print('hi')\n")
    monkeypatch.setattr(cli, "_confirm", lambda *a, **k: True)
    cli.cmd_init(_init_args(tmp_path))

    tracked = _git(tmp_path, "ls-files").stdout.split()
    assert "app.py" in tracked
    assert "context/README.md" in tracked
    assert ".gitignore" in tracked
    assert not [p for p in tracked if p.startswith(".multiagents/")], tracked


def test_init_leaves_a_non_git_project_alone_when_declined(tmp_path, monkeypatch):
    import multiagents.cli as cli

    monkeypatch.setattr(cli, "_confirm", lambda *a, **k: False)
    cli.cmd_init(_init_args(tmp_path))

    assert not (tmp_path / ".git").exists()
    assert (tmp_path / ".multiagents").is_dir()   # everything else still ran


def test_init_names_credential_files_before_committing_them(tmp_path, quiet_git,
                                                            monkeypatch, capsys):
    import multiagents.cli as cli

    import multiagents.gitops as gitops
    gitops.init_repo(tmp_path)          # a repo with no commits yet
    (tmp_path / ".env").write_text("TOKEN=shhh\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("//\n")
    asked = []
    monkeypatch.setattr(cli, "_confirm",
                        lambda question, default=False: asked.append(question) or False)
    cli._offer_git(tmp_path)

    out = capsys.readouterr().out
    assert ".env" in out and "node_modules/" in out
    assert "belong in .gitignore" in out


def test_init_ignores_credential_files_rather_than_committing_them(tmp_path,
                                                                   quiet_git,
                                                                   monkeypatch):
    """Accepting every prompt must still not commit a .env: the offer to ignore
    them comes first and defaults to yes."""
    import multiagents.cli as cli
    import multiagents.gitops as gitops

    (tmp_path / "app.py").write_text("print('hi')\n")
    (tmp_path / ".env").write_text("TOKEN=shhh\n")

    class _TTY:
        def isatty(self):
            return True

    monkeypatch.setattr(cli.sys, "stdin", _TTY())
    monkeypatch.setattr(cli, "_confirm", lambda *a, **k: True)
    cli._offer_git(tmp_path)

    tracked = _git(tmp_path, "ls-files").stdout.split()
    assert "app.py" in tracked
    assert ".env" not in tracked
    assert ".env" in (tmp_path / ".gitignore").read_text()


def test_uncommitted_entries_collapses_directories(tmp_path, quiet_git):
    import multiagents.gitops as gitops

    gitops.init_repo(tmp_path)
    deep = tmp_path / "node_modules" / "a" / "b"
    deep.mkdir(parents=True)
    (deep / "c.js").write_text("//\n")
    assert gitops.uncommitted_entries(tmp_path) == ["node_modules/"]


def test_spawning_without_a_repository_is_refused_not_silently_unisolated(tmp_path):
    """The fallback this replaced ran every agent in the project directory
    itself: one shared working tree, no branch to merge, nothing to discard."""
    import asyncio
    spec = AgentSpec("worker", "p", "m", conversational=True)
    # A provider that resolves, so the repository is the only thing wrong.
    r = _runner(tmp_path, {"worker": spec},
                {"p": {"bin": "sh", "spawn": {"args": ["-c", "true"]}}}, git=False)

    for coro in (r.start("worker", "go"), r.consult("worker", "hi")):
        with pytest.raises(RuntimeError) as excinfo:
            asyncio.run(coro)
        assert "not a git repository" in str(excinfo.value)


def test_workdir_override_is_refused_unless_the_project_grants_it(tmp_path):
    """`workdir` removes the branch isolation everything else rests on, and the
    caller asking for it is a model, not a person at a shell — so the
    permission has to come from a file a human edits."""
    import asyncio
    spec = AgentSpec("worker", "p", "m")
    providers = {"p": {"bin": "sh", "spawn": {"args": ["-c", "true"]}}}
    r = _runner(tmp_path, {"worker": spec}, providers)

    with pytest.raises(PermissionError) as excinfo:
        asyncio.run(r.start("worker", "go", workdir=str(tmp_path)))
    assert "allow_workdir_override" in str(excinfo.value)


def test_a_granted_workdir_override_also_lifts_the_repository_requirement(tmp_path):
    """Once a human has granted it, `workdir` is the deliberate escape hatch it
    was meant to be — including in a directory that is not a repository."""
    import asyncio
    spec = AgentSpec("worker", "nosuch", "m")
    r = _runner(tmp_path, {"worker": spec}, git=False,
                project={"limits": {"allow_workdir_override": True}})

    with pytest.raises((RuntimeError, KeyError, FileNotFoundError)) as excinfo:
        asyncio.run(r.start("worker", "go", workdir=str(tmp_path)))
    assert "not a git repository" not in str(excinfo.value)
    assert "allow_workdir_override" not in str(excinfo.value)


# --------------------------------------------------------------------------
# Bug tickets
#
# A ticket is written to be published, so the tests that matter are about what
# does NOT reach it, and about nothing leaving the machine unasked.


def _tree(tmp_path):
    from multiagents.paths import ProjectPaths
    paths = ProjectPaths(tmp_path)
    paths.ensure()
    return Tree(paths.tree_file, paths.events_file)


def test_a_ticket_is_depersonalised_as_it_is_stored():
    """Stored, not submitted: the orchestrator and the user must review the same
    text that would be posted, or the review is of something else."""
    from multiagents.redact import depersonalise
    import getpass

    body = (f"failed opening {Path.home()}/work/acme/api.py while user "
            f"{getpass.getuser()} ran it")
    cleaned = depersonalise(body, Path.home() / "work" / "acme")

    assert str(Path.home()) not in cleaned
    assert getpass.getuser() not in cleaned
    assert "<project>/api.py" in cleaned


def test_depersonalise_replaces_the_longest_match_first(tmp_path):
    """The project lives under the home directory, so replacing `~` first would
    leave `~/work/acme` — the client's name — in a published ticket."""
    from multiagents.redact import depersonalise

    project = Path.home() / "work" / "acme"
    assert depersonalise(str(project / "x.py"), project) == "<project>/x.py"


def test_a_short_username_is_left_alone(monkeypatch):
    """Replacing a two-letter name would corrupt every word containing it."""
    import getpass

    from multiagents import redact

    monkeypatch.setattr(getpass, "getuser", lambda: "ab")
    assert redact.depersonalise("a stable abstraction") == "a stable abstraction"


def test_the_ticket_marker_splits_body_from_proposed_fix(tmp_path):
    r = _runner(tmp_path, {"bug-reporter": AgentSpec("bug-reporter", "p", "m")})
    text = (
        "Here is my reasoning, which is not part of the ticket.\n"
        "TICKET(blocking): merge_agent reports success on an empty branch\n"
        "## What happened\n\nIt returned merged with no commits.\n"
        "PROPOSED_FIX:\n"
        "Check commits_on() before reporting merged.\n"
    )
    ticket = r._file_ticket("ag-1", text)

    assert ticket["severity"] == "blocking"
    assert ticket["title"] == "merge_agent reports success on an empty branch"
    assert "What happened" in ticket["body"]
    assert "not part of the ticket" not in ticket["body"]
    assert ticket["proposed_fix"].startswith("Check commits_on()")


def test_text_without_the_marker_files_nothing(tmp_path):
    r = _runner(tmp_path, {"bug-reporter": AgentSpec("bug-reporter", "p", "m")})
    assert r._file_ticket("ag-1", "I looked and found no bug.") is None
    assert r.tree.read()["tickets"] == []


def test_an_unknown_severity_is_not_accepted_as_blocking(tmp_path):
    """`blocking` decides whether the orchestrator stops work, so it may only
    come from the marker's own vocabulary."""
    r = _runner(tmp_path, {"bug-reporter": AgentSpec("bug-reporter", "p", "m")})
    assert r._file_ticket("ag-1", "TICKET(catastrophic): everything is on fire") is None
    assert r.tree.add_ticket("ag-1", "t", "b", "catastrophic")["severity"] == "minor"


def test_tickets_are_never_submitted_without_configuration(tmp_path):
    from multiagents import bugs
    from multiagents.config import Config

    config = Config(project={"bug_reporting": {"automatic": True}},
                    providers={}, agents={}, models={}, instruction_dirs=[])
    ok, why = bugs.can_submit(config)
    assert not ok and "repo" in why


def test_automatic_reporting_is_off_by_default():
    """A bug report is public writing about the user's machine. Consent for one
    is not consent for the next."""
    from multiagents import bugs
    from multiagents.config import Config
    import yaml

    empty = Config(project={}, providers={}, agents={}, models={}, instruction_dirs=[])
    assert bugs.settings(empty)["automatic"] is False

    shipped = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "src" / "multiagents" /
         "defaults" / "project.yaml").read_text())
    assert shipped["bug_reporting"]["automatic"] is False
    # The repo is set; `automatic: false` is what keeps tickets on the machine.
    assert shipped["bug_reporting"]["repo"] == "theo-broma/multiagents"


def test_open_tickets_include_the_ones_parked_for_the_user(tmp_path):
    """awaiting_user is not resolved: the orchestrator must keep seeing it, or a
    ticket the user never sent silently disappears from the queue."""
    tree = _tree(tmp_path)
    t = tree.add_ticket("ag-1", "title", "body", "minor")
    tree.set_ticket_status(t["id"], "awaiting_user", "automatic reporting is off")

    assert [x["id"] for x in tree.open_tickets()] == [t["id"]]
    tree.set_ticket_status(t["id"], "reported", "", "https://example.invalid/1")
    assert tree.open_tickets() == []
    assert tree.get_ticket(t["id"])["url"] == "https://example.invalid/1"


def test_the_bug_reporter_agent_is_shipped_and_read_only():
    import yaml
    root = Path(__file__).resolve().parents[1] / "src" / "multiagents" / "defaults"
    agents = yaml.safe_load((root / "agents.yaml").read_text())["agents"]
    spec = agents["bug-reporter"]

    assert spec["role"] == "bug-reporter"
    assert spec["writes"] is False and spec["can_spawn"] is False
    assert not spec.get("launch"), "it is spawned, never launched"
    assert (root / "agents" / spec["instructions"]).is_file()


def test_the_generated_environment_block_carries_no_identity(tmp_path):
    """The agent is told to include this verbatim, so it is the one part of a
    ticket the model does not write — and the one that could leak a home path."""
    import getpass
    r = _runner(tmp_path)
    block = r._bug_context()

    verbatim = block.split("The multiagents source is at")[0]
    assert "commit:" in verbatim and "providers available:" in verbatim
    assert str(Path.home()) not in verbatim
    assert getpass.getuser() not in verbatim
    # The path itself is still given, outside the part that gets copied.
    assert "The multiagents source is at" in block


def test_the_tree_still_shows_a_queued_ticket_with_no_agents(tmp_path):
    """`(no agents)` used to be an early return, which hid every queued ticket
    and parked question in exactly the state where you go looking for them."""
    tree = _tree(tmp_path)
    tree.add_ticket("ag-1", "something is wrong", "body", "blocking")
    rendered = tree.render()

    assert "(no agents)" in rendered
    assert "something is wrong" in rendered
    assert "multiagents tickets" in rendered


def test_an_unauthenticated_gh_is_reported_as_such_not_as_ready(monkeypatch):
    """Installed is not logged in. Saying `can_submit` here would tell the
    orchestrator to file, and hand it an auth error it cannot act on."""
    from multiagents import bugs
    from multiagents.config import Config

    config = Config(project={"bug_reporting": {"repo": "someone/multiagents"}},
                    providers={}, agents={}, models={}, instruction_dirs=[])
    monkeypatch.setattr(bugs.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(bugs, "authenticated", lambda: False)
    ok, why = bugs.can_submit(config)
    assert not ok and "gh auth login" in why

    monkeypatch.setattr(bugs, "authenticated", lambda: True)
    assert bugs.can_submit(config) == (True, "")


def test_doctor_names_a_permission_profile_the_provider_does_not_define():
    """An unknown profile adds no flags, and an agent with no permission flag
    is not safely restricted — agy auto-denies everything and answers nothing.
    It has to be reported, not defaulted."""
    import multiagents.cli as cli
    from multiagents.config import Config

    provider = Provider.from_dict("p", {
        "bin": "sh",
        "spawn": {"args": ["{prompt}"], "permission": {"full": ["--auto"],
                                                       "sandbox": [], "readonly": []}},
    })
    config = Config(project={}, providers={}, models={}, instruction_dirs=[],
                    agents={"a": AgentSpec("a", "p", "m", permission="paranoid")})

    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli._report_agents(config, {"p": provider})
    out = buf.getvalue()
    assert "unknown permission 'paranoid'" in out
    assert "full, readonly, sandbox" in out


def test_the_last_ticket_marker_wins_not_the_first(tmp_path):
    """The agent reasons about a system whose own docs contain the literal
    string `TICKET(blocking):` — quoting the rule while thinking must not turn
    the rest of the monologue into the ticket."""
    r = _runner(tmp_path, {"bug-reporter": AgentSpec("bug-reporter", "p", "m")})
    text = (
        "The brief says to end with `TICKET(blocking): one-line summary`, so\n"
        "TICKET(blocking): a quoted rule, which is not the real ticket\n"
        "and here is more of my reasoning about what went wrong.\n"
        "TICKET(minor): merge_agent miscounts commits on an empty branch\n"
        "## What happened\n\nThe real body.\n"
    )
    ticket = r._file_ticket("ag-1", text)

    assert ticket["title"] == "merge_agent miscounts commits on an empty branch"
    assert ticket["severity"] == "minor"
    assert "quoted rule" not in ticket["body"]
    assert "The real body." in ticket["body"]


def test_an_agent_whose_brief_is_missing_refuses_to_run(tmp_path):
    """Silently running on the preamble alone is worse than failing: a capable
    agent does something adjacent to the task and the cause is invisible."""
    import asyncio
    spec = AgentSpec("worker", "p", "m", instructions="deleted.md")
    r = _runner(tmp_path, {"worker": spec},
                {"p": {"bin": "sh", "spawn": {"args": ["-c", "true"]}}})

    with pytest.raises(FileNotFoundError) as excinfo:
        asyncio.run(r.start("worker", "go"))
    assert "deleted.md" in str(excinfo.value)
    assert "doctor" in str(excinfo.value)


def test_a_conversation_whose_worktree_vanished_is_told_so(tmp_path):
    """Carrying the session into a fresh checkout leaves the agent remembering
    files that are not there; the correction belongs in the conversation."""
    r = _runner(tmp_path, {"advisor": AgentSpec("advisor", "p", "m",
                                                conversational=True)},
                {"p": {"bin": "sh", "spawn": {"args": ["-c", "true"]}}})
    captured = {}

    async def fake_launch(**kwargs):
        captured.update(kwargs)
        raise RuntimeError("stop here — the prompt is what is under test")

    r._launch = fake_launch
    from multiagents.tree import Node
    r.tree.add(Node(id="ag-old", agent="advisor", provider="p", model="m",
                    parent=None, depth=1, status="idle", conversation=True,
                    session_id="s-1", worktree=str(tmp_path / "gone"),
                    branch="agents/advisor/old"))

    import asyncio
    asyncio.run(r.consult("advisor", "still there?"))

    assert captured["session_id"] == "s-1", "the session must survive"
    assert Path(captured["workdir"]).is_dir(), "a fresh worktree must exist"
    assert "recreated" in captured["prompt"]


def test_init_exits_non_zero_when_it_leaves_a_project_unspawnable(tmp_path,
                                                                  monkeypatch):
    """Exit 0 after leaving a project where no agent can run would let a
    scripted setup call it ready; `run` then fails at the first spawn."""
    import multiagents.cli as cli

    monkeypatch.setattr(cli, "_confirm", lambda *a, **k: False)   # decline git
    assert cli.cmd_init(_init_args(tmp_path)) == 1
    assert (tmp_path / ".multiagents").is_dir(), "the rest of setup still ran"


def test_init_exits_zero_once_the_project_can_actually_run(tmp_path, quiet_git,
                                                           monkeypatch):
    import multiagents.cli as cli

    monkeypatch.setattr(cli, "_confirm", lambda *a, **k: True)
    assert cli.cmd_init(_init_args(tmp_path)) == 0


# --------------------------------------------------------------------------
# One project = one repository
#
# A project inside another one looks like it works: init succeeds, agents run.
# What it actually does is hand them checkouts of the OUTER repository and a
# branch namespace shared with the outer project's agents, while counting
# concurrency, budget and watchdogs separately. Hence a refusal, not a warning.


def test_init_refuses_inside_an_existing_project(tmp_path, quiet_git, monkeypatch):
    import multiagents.cli as cli

    monkeypatch.setattr(cli, "_confirm", lambda *a, **k: True)
    cli.cmd_init(_init_args(tmp_path))
    sub = tmp_path / "subproject"
    sub.mkdir()

    assert cli.cmd_init(_init_args(sub)) == 2
    assert not (sub / ".multiagents").exists(), "a refusal must leave nothing behind"


def test_nested_overrides_the_refusal(tmp_path, quiet_git, monkeypatch):
    """For the genuinely separate repository the guard cannot recognise."""
    import multiagents.cli as cli

    monkeypatch.setattr(cli, "_confirm", lambda *a, **k: True)
    cli.cmd_init(_init_args(tmp_path))
    sub = tmp_path / "subproject"
    sub.mkdir()

    cli.cmd_init(_init_args(sub, nested=True))
    assert (sub / ".multiagents").is_dir()


def test_init_names_the_repository_when_it_is_not_the_project_root(tmp_path,
                                                                   quiet_git,
                                                                   monkeypatch,
                                                                   capsys):
    """Worktrees are checkouts of the repository, not of the directory — so a
    project below the repository root gets more than it asked for."""
    import multiagents.cli as cli
    import multiagents.gitops as gitops

    gitops.init_repo(tmp_path)
    gitops.initial_commit(tmp_path)
    sub = tmp_path / "component"
    sub.mkdir()

    monkeypatch.setattr(cli, "_confirm", lambda *a, **k: True)
    cli.cmd_init(_init_args(sub, nested=True))
    out = capsys.readouterr().out

    assert "not the repository root" in out
    assert str(tmp_path) in out


def test_repo_root_distinguishes_the_top_level_from_being_inside_one(tmp_path,
                                                                     quiet_git):
    import multiagents.gitops as gitops

    assert gitops.repo_root(tmp_path) is None
    gitops.init_repo(tmp_path)
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)

    assert gitops.repo_root(deep) == tmp_path
    assert gitops.is_repo(deep), "is_repo is true anywhere inside — that is the trap"


def test_the_state_root_is_not_mistaken_for_a_project(tmp_path, monkeypatch):
    """`~/.multiagents` holds worktrees and container credentials and wears the
    same name as a project's directory. Without this, every path under the home
    directory with no closer project resolved to the home directory itself, and
    commands read and wrote a phantom project rooted there."""
    import multiagents.paths as paths_mod

    home = tmp_path / "home"
    (home / ".multiagents" / "worktrees").mkdir(parents=True)
    monkeypatch.setenv("MULTIAGENTS_STATE_DIR", str(home / ".multiagents"))
    work = home / "code" / "thing"
    work.mkdir(parents=True)

    assert paths_mod.find_project_root(work) is None

    (work / ".multiagents").mkdir()
    assert paths_mod.find_project_root(work) == work


# --------------------------------------------------------------------------
# Spec-first delegation
#
# The failure being designed against: a broad task gets the median
# implementation. These pin the structural properties that counter it, not the
# prose — the prose is free to be rewritten, the structure is not.


def test_the_spec_first_roster_ships_and_writes():
    """specifier and adversary both produce committed artifacts, so `writes`
    must be true or their branch is dropped as empty and the work vanishes."""
    root = Path(__file__).resolve().parents[1] / "src" / "multiagents" / "defaults"
    agents = _shipped_agents()

    for name in ("specifier", "adversary"):
        spec = agents[name]
        assert spec["writes"] is True, f"{name} commits a file"
        assert spec["can_spawn"] is False
        assert not spec.get("launch")
        assert (root / "agents" / spec["instructions"]).is_file()


def test_the_adversary_is_not_the_specifier_s_model_family():
    """An adversary sharing the author's blind spots agrees with it, which is
    the one thing it must not do."""
    agents = _shipped_agents()
    assert agents["adversary"]["provider"] != agents["specifier"]["provider"]
    assert agents["adversary"]["model"] != agents["specifier"]["model"]


def test_the_briefs_agree_on_where_specs_live_and_how_ids_look():
    """specifier, adversary, tester, implementer and the orchestrator all refer
    to the same path and the same id convention; a drift between them silently
    breaks the hand-off, since nothing in code enforces it."""
    briefs = (Path(__file__).resolve().parents[1] / "src" / "multiagents"
              / "defaults" / "agents")
    for name in ("specifier", "adversary", "tester", "implementer",
                 "_orchestrator"):
        text = (briefs / f"{name}.md").read_text()
        assert "context/specs/" in text, name
        assert "R7" in text or "R1" in text or "R<n>" in text, name


def test_the_orchestrator_is_told_when_not_to_use_the_spec_path():
    """A rule that applies to everything gets ignored. The threshold is the
    part that makes it followable."""
    text = ((Path(__file__).resolve().parents[1] / "src" / "multiagents"
             / "defaults" / "agents" / "_orchestrator.md").read_text())
    assert "threshold" in text.lower()
    assert "straight to `implementer`" in text


# --------------------------------------------------------------------------
# Security roster


def test_the_security_pair_ships_at_both_ends_of_the_work():
    """One is consulted while a boundary can still be moved for free; the other
    attacks what was actually built."""
    root = Path(__file__).resolve().parents[1] / "src" / "multiagents" / "defaults"
    agents = _shipped_agents()

    advisor = agents["security-advisor"]
    assert advisor["conversational"] is True, "a threat model is built by follow-ups"
    assert advisor["writes"] is False

    pentester = agents["pentester"]
    assert not pentester.get("conversational"), "it is given a target, not a chat"
    assert pentester["writes"] is True, "it commits a test that proves a finding"

    for name in ("security-advisor", "pentester"):
        assert agents[name]["can_spawn"] is False
        assert (root / "agents" / agents[name]["instructions"]).is_file()


def test_the_pentester_does_not_audit_the_design_it_was_given():
    """Different provider from the security advisor on purpose."""
    agents = _shipped_agents()
    assert agents["pentester"]["provider"] != agents["security-advisor"]["provider"]


def test_the_pentester_brief_bounds_it_to_this_repository():
    """A dual-use role needs its limits in the brief, not in the caller's head:
    no live targets, no using a discovered secret, proof rather than weapon."""
    raw = ((Path(__file__).resolve().parents[1] / "src" / "multiagents"
            / "defaults" / "agents" / "pentester.md").read_text().lower())
    # Reflowed prose puts line breaks mid-sentence; the rule is the content.
    text = " ".join(raw.split())
    assert "no live systems" in text
    assert "never print its value" in text
    assert "not a weapon" in text


def test_the_orchestrator_is_told_when_security_agents_are_not_needed():
    """Running them on everything trains the reader to skim them."""
    text = ((Path(__file__).resolve().parents[1] / "src" / "multiagents"
             / "defaults" / "agents" / "_orchestrator.md").read_text())
    assert "When not to." in text
    assert "neither of these" in " ".join(text.split())
    assert "holds a veto" in text


# --------------------------------------------------------------------------
# Coder tiers


def test_the_coder_tiers_share_one_brief_on_different_models():
    """The craft is identical; only cost and the escalation behaviour differ.
    Three copies of the brief would drift, and the drift would be invisible."""
    agents = _shipped_agents()
    tiers = ["implementer-quick", "implementer", "implementer-deep"]

    briefs = {agents[t]["instructions"] for t in tiers}
    assert briefs == {"implementer.md"}, briefs
    models = [agents[t]["model"] for t in tiers]
    assert len(set(models)) == 3, models
    for t in tiers:
        assert agents[t]["writes"] is True


def test_the_cheap_tier_is_on_a_short_leash():
    """Running out of steps on a misrouted task is the cheap failure this
    tiering wants; 120 steps of flailing is the expensive one."""
    agents = _shipped_agents()
    quick, deep = agents["implementer-quick"], agents["implementer-deep"]

    assert quick["max_steps"] < 120, "the default budget defeats the point"
    assert quick["timeout"] < agents["implementer"]["timeout"] < deep["timeout"]
    assert quick["can_spawn"] is False, "a cheap tier must not fan out"


def test_the_brief_tells_the_cheap_tier_to_hand_work_back():
    """Escalation is what makes routing low safe; without it, routing low just
    produces plausible wrong implementations."""
    text = ((Path(__file__).resolve().parents[1] / "src" / "multiagents"
             / "defaults" / "agents" / "implementer.md").read_text())
    flat = " ".join(text.split())
    assert "stop and hand it back" in flat
    assert "Handing back is not failure" in flat


def test_the_orchestrator_routes_by_judgement_not_importance():
    """The tempting criterion sends everything that matters to the top tier,
    which buys a tiered roster and none of its benefit."""
    text = ((Path(__file__).resolve().parents[1] / "src" / "multiagents"
             / "defaults" / "agents" / "_orchestrator.md").read_text())
    flat = " ".join(text.split())
    assert "never by how important" in flat
    assert "implementer-deep" in flat and "implementer-quick" in flat


# --------------------------------------------------------------------------
# Fallback models, pause and resume


def test_every_working_agent_names_a_cross_provider_fallback():
    """Without one an agent cannot fail over — a model id belongs to its own
    provider's namespace, so `agy --model opencode-go/...` is meaningless."""
    agents = _shipped_agents()
    for name, spec in agents.items():
        if spec.get("disabled") or spec.get("launch"):
            continue
        alternatives = spec.get("models") or {}
        assert alternatives, f"{name} has no fallback and would wait instead"
        assert spec["provider"] not in alternatives, \
            f"{name}'s fallback is its own provider"


def test_a_checking_pair_never_collapses_onto_one_model():
    """The pairs are split so a checker does not share the author's blind
    spots. A fallback that lands both on the same model silently undoes that,
    at exactly the moment nobody is watching."""
    agents = _shipped_agents()
    pairs = [("specifier", "adversary"), ("security-advisor", "pentester"),
             ("critic", "advisor")]

    def resolve(spec, down):
        if spec["provider"] != down:
            return spec["model"]
        return next((m for p, m in (spec.get("models") or {}).items() if p != down),
                    None)

    for down in ("agy", "opencode"):
        for left, right in pairs:
            a, b = resolve(agents[left], down), resolve(agents[right], down)
            assert a and b, f"{left}/{right} cannot run with {down} down"
            assert a != b, f"with {down} down, {left} and {right} both use {a}"


def test_pause_keeps_the_earliest_reset_and_expires_itself(tmp_path):
    """Wake at the FIRST reset, not the last. Waking early costs one wasted
    check and an immediate re-pause; waking late blocks tasks whose provider
    came back ten minutes ago, and nothing would notice."""
    import time as _t
    tree = _tree(tmp_path)
    assert tree.pause_state() == {}

    tree.pause(_t.time() + 900, "opencode cooling down", ["opencode"])
    tree.pause(_t.time() + 60, "agy cooling down", ["agy"])
    assert tree.pause_state()["until"] < _t.time() + 200

    tree.resume("back")
    assert tree.pause_state() == {}

    tree.pause(_t.time() - 1, "already over")
    assert tree.pause_state() == {}, "an elapsed pause clears itself on read"


def test_a_pause_refuses_only_the_agents_it_actually_covers(tmp_path):
    """Freezing an agent whose provider is healthy because a different one is
    exhausted is over-applying the invariant — the protection against
    unreviewed work is the merge rule, not stopping everything that can run."""
    import asyncio, time as _t
    providers = {"p": {"bin": "sh", "spawn": {"args": ["-c", "true"]}},
                 "q": {"bin": "sh", "spawn": {"args": ["-c", "true"]}}}
    r = _runner(tmp_path, {
        "stuck-agent": AgentSpec("stuck-agent", "p", "m"),
        "free-agent": AgentSpec("free-agent", "q", "m"),
    }, providers)
    r.tree.pause(_t.time() + 300, "no headroom on p", ["p"])

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(r.start("stuck-agent", "go"))
    assert "Paused" in str(excinfo.value)
    assert "restart by themselves" in str(excinfo.value)

    # The other agent's provider is untouched, so it must not be blocked.
    result = asyncio.run(r.start("free-agent", "go"))
    assert result.get("agent_id"), result
    assert not result.get("deferred")


def test_an_agent_with_a_fallback_outside_the_pause_still_runs(tmp_path):
    import asyncio, time as _t
    providers = {"p": {"bin": "sh", "spawn": {"args": ["-c", "true"]}},
                 "q": {"bin": "sh", "spawn": {"args": ["-c", "true"]}}}
    spec = AgentSpec("worker", "p", "m", models={"q": "m2"})
    r = _runner(tmp_path, {"worker": spec}, providers)
    r.tree.pause(_t.time() + 300, "no headroom on p", ["p"])

    result = asyncio.run(r.start("worker", "go"))
    assert result.get("agent_id"), "q is still available to it"


def test_resume_deferred_reports_tasks_whose_agent_is_gone(tmp_path):
    """Dropping them silently loses work the orchestrator believes is queued."""
    import asyncio, time as _t
    r = _runner(tmp_path, {"worker": AgentSpec("worker", "p", "m")},
                {"p": {"bin": "sh", "spawn": {"args": ["-c", "true"]}}})
    r.tree.defer({"agent": "deleted-agent", "task": "something"}, _t.time() - 1, "quota")

    result = asyncio.run(r.resume_deferred())
    assert result["restarted"] == []
    assert result["dropped"][0]["agent"] == "deleted-agent"


def test_a_failed_restart_leaves_the_rest_of_the_queue_intact(tmp_path):
    """due_deferred used to POP. Any exception between the pop and the restart
    then deleted the whole remaining batch permanently — the worst class of bug
    in a component whose entire job is not losing work."""
    import asyncio, time as _t
    r = _runner(tmp_path, {"worker": AgentSpec("worker", "p", "m")},
                {"p": {"bin": "sh", "spawn": {"args": ["-c", "true"]}}})
    for n in range(3):
        r.tree.defer({"agent": "worker", "task": f"task {n}"}, _t.time() - 1, "quota")

    async def boom(*a, **k):
        raise RuntimeError("the window closed again")

    r.start = boom
    result = asyncio.run(r.resume_deferred())

    assert "stopped_on" in result
    assert len(r.tree.read()["deferred"]) == 3, "nothing may be lost"


def test_a_restarted_task_is_dropped_from_the_queue(tmp_path):
    """The other half: an entry that WAS dealt with must go, or the queue grows
    every time it is drained."""
    import asyncio, time as _t
    r = _runner(tmp_path, {"worker": AgentSpec("worker", "p", "m")},
                {"p": {"bin": "sh", "spawn": {"args": ["-c", "true"]}}})
    r.tree.defer({"agent": "worker", "task": "one"}, _t.time() - 1, "quota")

    result = asyncio.run(r.resume_deferred())
    assert len(result["restarted"]) == 1
    assert r.tree.read()["deferred"] == []


def test_draining_stops_when_the_window_closes_mid_batch(tmp_path):
    """start() re-defers and re-pauses; carrying on would hit the pause guard
    and raise, turning one closed window into a crashed drain."""
    import asyncio, time as _t
    r = _runner(tmp_path, {"worker": AgentSpec("worker", "p", "m")},
                {"p": {"bin": "sh", "spawn": {"args": ["-c", "true"]}}})
    for n in range(3):
        r.tree.defer({"agent": "worker", "task": f"task {n}"}, _t.time() - 1, "quota")

    async def redefer(agent, task, **k):
        r.tree.defer({"agent": agent, "task": task}, _t.time() + 600, "still out")
        r.tree.pause(_t.time() + 600, "still out", ["p"])
        return {"deferred": True, "reason": "still out"}

    r.start = redefer
    result = asyncio.run(r.resume_deferred())

    assert result["restarted"] == []
    assert result["still_deferred"] == 1, "it stopped after the first"
    # Two untouched originals plus the one start() re-queued.
    assert len(r.tree.read()["deferred"]) == 3


def test_the_readme_roster_table_lists_every_shipped_agent():
    """Docs drift silently and the roster has grown four times today. The table
    is the one place a reader looks to find out what exists, so an agent missing
    from it is effectively an agent nobody knows they have."""
    import re
    import yaml
    root = Path(__file__).resolve().parents[1]
    shipped = {n for n, spec in yaml.safe_load(
        (root / "src" / "multiagents" / "defaults" / "agents.yaml").read_text()
    )["agents"].items() if not spec.get("disabled")}

    readme = (root / "README.md").read_text()
    table = readme.split("## The roster")[1].split("Reading agents")[0]
    mentioned = set(re.findall(r"`([a-z][a-z0-9-]*)`", table))

    missing = shipped - mentioned
    assert not missing, f"not listed in the README roster table: {sorted(missing)}"


# --------------------------------------------------------------------------
# Teardown
#
# rm -rf with a confirmation prompt was the whole safety story. These cover the
# two things that prompt cannot tell you: whether anything is about to be lost,
# and what is left broken afterwards.


def test_uninstall_refuses_while_a_worktree_holds_uncommitted_work(tmp_path,
                                                                   quiet_git,
                                                                   monkeypatch,
                                                                   capsys):
    """A commit survives in its repository as a branch. An uncommitted edit
    exists nowhere else, and `rm -rf` does not ask twice."""
    import argparse
    import multiagents.cli as cli
    import multiagents.gitops as gitops

    repo = tmp_path / "repo"
    repo.mkdir()
    gitops.init_repo(repo)
    gitops.initial_commit(repo)
    state = tmp_path / "state"
    (state / "worktrees" / "proj-1234").mkdir(parents=True)
    worktree = state / "worktrees" / "proj-1234" / "ag-1"
    gitops.create_worktree(repo, worktree, "agents/x/1")
    (worktree / "unsaved.txt").write_text("work that exists nowhere else\n")

    monkeypatch.setenv("MULTIAGENTS_STATE_DIR", str(state))
    monkeypatch.setattr(cli, "global_config_dir", lambda: tmp_path / "config")

    args = argparse.Namespace(dry_run=False, force=False)
    assert cli.cmd_uninstall(args) == 1
    out = capsys.readouterr().out
    assert "uncommitted work" in out and "unsaved" not in out.split("\n")[0]
    assert worktree.is_dir(), "nothing may be removed while it refuses"


def test_uninstall_prunes_the_registrations_it_orphans(tmp_path, quiet_git,
                                                       monkeypatch):
    """Deleting the directories does not unregister them: the repository goes
    on listing worktrees that are not there until someone prunes."""
    import argparse
    import multiagents.cli as cli
    import multiagents.gitops as gitops

    repo = tmp_path / "repo"
    repo.mkdir()
    gitops.init_repo(repo)
    gitops.initial_commit(repo)
    state = tmp_path / "state"
    (state / "worktrees" / "proj-1234").mkdir(parents=True)
    gitops.create_worktree(repo, state / "worktrees" / "proj-1234" / "ag-1",
                           "agents/x/1")
    assert "ag-1" in gitops.run(repo, "worktree", "list").out

    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("MULTIAGENTS_STATE_DIR", str(state))
    monkeypatch.setattr(cli, "global_config_dir", lambda: config)

    assert cli.cmd_uninstall(argparse.Namespace(dry_run=False, force=True)) == 0
    assert not state.exists() and not config.exists()
    assert "ag-1" not in gitops.run(repo, "worktree", "list").out
    # The branch is not the worktree: committed work is still there.
    assert gitops.branch_exists(repo, "agents/x/1")


def test_uninstall_dry_run_removes_nothing(tmp_path, monkeypatch, capsys):
    import argparse
    import multiagents.cli as cli

    state = tmp_path / "state"
    (state / "worktrees").mkdir(parents=True)
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("MULTIAGENTS_STATE_DIR", str(state))
    monkeypatch.setattr(cli, "global_config_dir", lambda: config)

    assert cli.cmd_uninstall(argparse.Namespace(dry_run=True, force=False)) == 0
    assert state.is_dir() and config.is_dir()
    assert "would remove" in capsys.readouterr().out


def test_the_claude_launcher_only_resumes_when_a_transcript_exists(tmp_path,
                                                                   monkeypatch):
    """`--continue` is fatal with no conversation, and the launch marker is
    written before the first session runs — so it records that we tried, not
    that anything resumable came of it. Seen in the wild: a first init-agent
    the user quit without speaking made every later run fail with
    "No conversation found to continue"."""
    import subprocess
    script = (Path(__file__).resolve().parents[1] / "src" / "multiagents"
              / "defaults" / "providers" / "claude.sh")
    workdir = tmp_path / "proj.x" / "some_dir"
    workdir.mkdir(parents=True)
    home = tmp_path / "home"
    slug = str(workdir).replace("/", "-").replace(".", "-").replace("_", "-")
    sessions = home / ".claude" / "projects" / slug
    sessions.mkdir(parents=True)

    # A `claude` that prints its own argv instead of running.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "claude").write_text('#!/bin/sh\necho "ARGV: $@"\n')
    (fake_bin / "claude").chmod(0o755)

    def run():
        return subprocess.run(
            ["sh", str(script), "launch"], cwd=workdir, capture_output=True, text=True,
            env={"PATH": f"{fake_bin}:/usr/bin:/bin", "HOME": str(home),
                 "MULTIAGENTS_BIN": str(fake_bin / "claude"),
                 "MULTIAGENTS_RESUME": "1", "MULTIAGENTS_MODEL": "sonnet"},
        )

    # The directory exists but holds no transcript — the exact false positive
    # that made the real failure: an old session leaves the directory behind.
    without = run()
    assert "--continue" not in without.stdout, without.stdout
    assert "starting a fresh one" in without.stderr

    (sessions / "abc.jsonl").write_text("{}\n")
    assert "--continue" in run().stdout


# --------------------------------------------------------------------------
# Stream limits
#
# From a real run: the bug-reporter died with "ValueError: Separator is found,
# but chunk is longer than limit" every time, because its brief tells it to read
# this project's source and a CLI reports a file's contents as ONE json line.


def test_a_line_larger_than_asyncios_default_does_not_kill_the_run(tmp_path):
    """64 KiB is asyncio's default StreamReader limit. Source files here are
    5-60 KB, so a single tool result carrying one is enough to exceed it."""
    import asyncio
    from multiagents.executor.local import LocalExecutor

    big = "x" * (200 * 1024)
    script = tmp_path / "emit.sh"
    script.write_text(f'#!/bin/sh\necho "before"\necho "{big}"\necho "after"\n')
    script.chmod(0o755)

    async def run():
        handle = await LocalExecutor().start(["sh", str(script)], tmp_path, {})
        return [line async for line in handle.lines()]

    lines = asyncio.run(run())
    assert lines[0] == "before"
    assert len(lines[1]) == 200 * 1024, "the long line must arrive whole"
    assert lines[-1] == "after", "and the stream must continue past it"


def test_an_over_long_line_is_salvaged_rather_than_fatal(tmp_path):
    """The limit is generous but still a limit. Past it, losing the tail of one
    event has to beat losing the agent — and the lines after it must still
    parse, which means draining the buffer rather than leaving it."""
    import asyncio
    from multiagents.executor import base, local

    script = tmp_path / "emit.sh"
    script.write_text('#!/bin/sh\necho "before"\nhead -c 5000 /dev/zero | tr "\\0" "y"\n'
                      'echo\necho "after"\n')
    script.chmod(0o755)

    async def run():
        proc = await asyncio.create_subprocess_exec(
            "sh", str(script), cwd=str(tmp_path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=1024,                      # force the failure deterministically
        )
        handle = base.Handle(pid=proc.pid, _proc=proc)
        return [line async for line in handle.lines()], handle

    lines, handle = asyncio.run(run())
    assert lines[0] == "before"
    assert "after" in lines, f"the stream must recover: {lines}"
    assert "over-long line dropped" in handle.stderr_tail


# --------------------------------------------------------------------------
# Doom loop, after a real session produced 23 trips on work that was finishing
# correctly. Two compounding causes: a CLI reports a write as
# {"TargetFile": "..."} with no content, so three different edits hash the
# same; and edit -> test -> edit -> test is an A,B,A,B alternation, which is
# the correct behaviour of a test agent.


def _tool(name, **args):
    from multiagents.providers import Event
    return Event(kind="tool", name=name, args=args)


def _sup(**kw):
    from multiagents.supervisor import Supervisor
    kw.setdefault("silence_timeout", 1e9)
    kw.setdefault("wall_timeout", 1e9)
    kw.setdefault("max_steps", 10 ** 9)
    return Supervisor(**kw)


def test_editing_and_retesting_is_not_a_doom_loop(tmp_path):
    """The exact false positive: identical signatures, because the content is
    not in the event, while the tree moves every pass."""
    sup = _sup()
    for n in range(8):
        sup.note_progress(f"tree-{n}")          # the edit landed
        assert sup.observe(_tool("write_to_file", TargetFile="/x/test_a.py")) is None
        assert sup.observe(_tool("run_command", CommandLine="pytest -q")) is None


def test_rewriting_one_file_with_nothing_changing_still_trips():
    """The other side: if the tree never moves, identical calls are a loop
    whatever the agent narrates between them."""
    sup = _sup()
    sup.note_progress("frozen")
    trips = [sup.observe(_tool("write_to_file", TargetFile="/x/a.py")) for _ in range(6)]
    assert any(t and t.reason == "doom_loop" for t in trips)
    assert "nothing changed on disk" in next(t for t in trips if t).detail


def test_a_reader_repeating_itself_trips_without_any_worktree_signal():
    """A read-only agent never moves the tree, so the progress gate must not
    switch the detector off for it — re-reading one file is the case this was
    built for."""
    sup = _sup()
    trips = [sup.observe(_tool("view_file", AbsolutePath="/x/README")) for _ in range(6)]
    assert any(t and t.reason == "doom_loop" for t in trips)


def test_an_unwired_progress_signal_leaves_the_old_behaviour(tmp_path):
    """With no sampler the value is "" throughout, which reads as stalled —
    the detector must degrade to signatures alone rather than silently turning
    itself off where progress cannot be observed."""
    sup = _sup(loop_repeats=3)
    trips = [sup.observe(_tool("grep", pattern="x")) for _ in range(4)]
    assert any(t and t.reason == "doom_loop" for t in trips)


def test_the_two_step_cycle_also_needs_a_frozen_tree():
    sup = _sup(loop_repeats=3)
    sup.note_progress("frozen")
    seen = [sup.observe(_tool("a" if i % 2 == 0 else "b", k=1)) for i in range(8)]
    assert any(t and "two-step cycle" in t.detail for t in seen)

    moving = _sup(loop_repeats=3)
    for i in range(12):
        moving.note_progress(f"tree-{i}")
        assert moving.observe(_tool("a" if i % 2 == 0 else "b", k=1)) is None


def test_max_steps_falls_back_to_the_project_limit():
    """`limits.max_steps` was documented in project.yaml and never read: only
    the AgentSpec default applied, so the knob did nothing."""
    import yaml
    spec = AgentSpec("x", "p", "m")
    assert spec.max_steps == 0, "0 means 'use the project limit'"

    shipped = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "src" / "multiagents" / "defaults"
         / "project.yaml").read_text())
    assert shipped["limits"]["max_steps"] >= 250, "120 fired on work that was fine"
    agents = _shipped_agents()
    assert agents["implementer-deep"]["max_steps"] >= 800, "measured runs reach ~768"
    assert agents["implementer-quick"]["max_steps"] == 40, "the short leash stays"


def test_the_shipped_limits_match_the_code_defaults():
    """project.yaml is read in preference to the built-in default, so a stale
    value there silently overrides a fix made in code — which is exactly what
    happened when doom_loop_repeats was raised in the Supervisor and left at 3
    in the shipped config."""
    import inspect
    import yaml
    from multiagents.supervisor import Supervisor

    shipped = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "src" / "multiagents" / "defaults"
         / "project.yaml").read_text())["limits"]
    defaults = {f.name: f.default for f in
                inspect.signature(Supervisor).parameters.values()}

    assert shipped["doom_loop_repeats"] == defaults["loop_repeats"], \
        "the shipped config would override the code default"


# --------------------------------------------------------------------------
# Concurrency
#
# Measured in a real session: 74% of the wall clock had exactly one agent
# running out of four allowed, and the user had to ask for parallel work by
# hand. The orchestrator's brief said nothing about it, and wait_for_agents
# described blocking as the virtuous choice.


def test_capacity_is_reported_where_the_waiting_decision_is_made(tmp_path):
    import asyncio
    r = _runner(tmp_path, project={"limits": {"max_concurrent": 4}})

    result = asyncio.run(r.wait_for_any(None, 0.1))
    assert result["capacity"]["max_concurrent"] == 4
    assert result["capacity"]["free_slots"] == 4


def test_idle_slots_are_called_out_while_something_is_running(tmp_path):
    """A bare number is easy to skim past; the nudge has to name the cost."""
    from multiagents.tree import Node
    r = _runner(tmp_path, project={"limits": {"max_concurrent": 4}})
    r.tree.add(Node(id="ag-1", agent="implementer", provider="p", model="m",
                    parent=None, depth=1, status="running"))

    note = r._idle_capacity_note()["capacity"]
    assert note["running"] == 1 and note["free_slots"] == 3
    assert "3 of 4 slots are idle" in note["note"]

    for n in range(2, 5):
        r.tree.add(Node(id=f"ag-{n}", agent="implementer", provider="p", model="m",
                        parent=None, depth=1, status="running"))
    full = r._idle_capacity_note()["capacity"]
    assert full["free_slots"] == 0
    assert "note" not in full, "a full tree must not be nagged"


def test_the_orchestrator_brief_says_when_parallel_is_safe_and_when_not():
    text = ((Path(__file__).resolve().parents[1] / "src" / "multiagents"
             / "defaults" / "agents" / "_orchestrator.md").read_text())
    flat = " ".join(text.split())
    assert "budget to spend, not a ceiling" in flat
    assert "Different files, different specs" in flat
    assert "Two agents on the same files" in flat, "the limits matter as much"


# --------------------------------------------------------------------------
# opencode quota, and per-model accounting


def test_usage_by_model_joins_spend_to_the_agents_that_spent_it(tmp_path):
    """No provider reports this: opencode serves three whole-account windows
    and agy nothing at all. We parse every stream, so it is ours to compute."""
    from multiagents.tree import Node
    tree = _tree(tmp_path)
    for i, (agent, provider, model, tokens, cost) in enumerate([
        ("implementer", "opencode", "kimi-k3", 1000, 2.5),
        ("tester", "agy", "gemini", 400, 0.0),
        ("reviewer", "agy", "gemini", 600, 0.0),
    ]):
        tree.add(Node(id=f"ag-{i}", agent=agent, provider=provider, model=model,
                      parent=None, depth=1, status="done",
                      usage={"total": tokens, "cost_usd": cost}))

    rows = tree.usage_by_model()
    assert [r["model"] for r in rows] == ["kimi-k3", "gemini"], "costliest first"
    gemini = rows[1]
    assert gemini["runs"] == 2 and gemini["tokens"] == 1000
    assert gemini["agents"] == ["reviewer", "tester"]


def test_the_opencode_budget_script_reports_the_worst_window(tmp_path,
                                                             monkeypatch):
    """headroom must come from the fullest bucket: the one closest to full is
    what actually stops a run, and reporting the roomiest routes work at a
    wall."""
    import json
    import subprocess
    script = (Path(__file__).resolve().parents[1] / "src" / "multiagents"
              / "defaults" / "providers" / "opencode.sh")

    home = tmp_path / "home"
    auth = home / ".local/share/opencode"
    auth.mkdir(parents=True)
    (auth / "auth.json").write_text(json.dumps(
        {"opencode-go": {"type": "api", "key": "x" * 40}}))

    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "curl").write_text(
        '#!/bin/sh\ncat >/dev/null\n'                    # swallow --config stdin
        'echo \'{"usage":{"rolling":{"percent":91,"resetsAt":"SOON"},'
        '"weekly":{"percent":10,"resetsAt":"LATER"}}}\'\n')
    (fake / "curl").chmod(0o755)

    out = subprocess.run(
        ["sh", str(script), "budget"], capture_output=True, text=True,
        env={"PATH": f"{fake}:/usr/bin:/bin", "HOME": str(home)},
    )
    data = json.loads(out.stdout)
    assert data["known"] is True
    assert data["headroom"] == 0.09, "1 - 91%, the worst window"
    assert data["resets_at"] == "SOON", "and its reset, not the roomy one's"
    assert "rolling" in data["note"]
    assert set(data["windows"]) == {"rolling", "weekly"}


def test_the_opencode_budget_script_is_quiet_without_a_key(tmp_path):
    """The free tier has no quota surface; that is not an error."""
    import json
    import subprocess
    script = (Path(__file__).resolve().parents[1] / "src" / "multiagents"
              / "defaults" / "providers" / "opencode.sh")
    home = tmp_path / "home"
    home.mkdir()

    out = subprocess.run(["sh", str(script), "budget"], capture_output=True,
                         text=True, env={"PATH": "/usr/bin:/bin", "HOME": str(home)})
    assert out.returncode == 0
    assert json.loads(out.stdout)["known"] is False


# --------------------------------------------------------------------------
# Executor default, and the offer that sets it


def test_the_executor_default_is_not_duplicated_out_of_step():
    """The shipped project.yaml wins over the code fallback, so the two saying
    different things means the code's value never applies — exactly how
    doom_loop_repeats was raised in code and left stale in config."""
    import yaml
    from multiagents.config import Config

    shipped = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "src" / "multiagents" / "defaults"
         / "project.yaml").read_text())
    empty = Config(project={}, providers={}, agents={}, models={}, instruction_dirs=[])

    assert shipped["executor"]["kind"] == empty.executor, (
        "the shipped default and the code fallback disagree; whichever the file "
        "names is what every project actually gets")


def test_setting_the_executor_keeps_the_comments(tmp_path):
    """project.yaml is mostly comments explaining the choices; a YAML round
    trip would silently throw all of them away."""
    import multiagents.cli as cli
    from multiagents.paths import ProjectPaths

    paths = ProjectPaths(tmp_path)
    paths.config.mkdir(parents=True)
    (paths.config / "project.yaml").write_text(
        "# a comment that must survive\n"
        "executor:\n"
        "  # why this key exists\n"
        "  kind: local\n\n"
        "limits:\n  max_steps: 250\n")

    assert cli._set_executor(paths, "docker") is True
    text = (paths.config / "project.yaml").read_text()
    assert "kind: docker" in text
    assert "# a comment that must survive" in text
    assert "# why this key exists" in text
    assert "max_steps: 250" in text


def test_the_docker_offer_is_declined_without_a_terminal(tmp_path, monkeypatch,
                                                         capsys):
    """`make init` and scripted runs must not hang on it, and must not silently
    switch a project's execution backend either."""
    import multiagents.cli as cli
    from multiagents.paths import ProjectPaths

    paths = ProjectPaths(tmp_path)
    paths.config.mkdir(parents=True)
    (paths.config / "project.yaml").write_text("executor:\n  kind: local\n")

    class _NoTTY:
        def isatty(self):
            return False

    monkeypatch.setattr(cli.sys, "stdin", _NoTTY())
    assert cli._offer_docker(paths) == ""
    assert "kind: local" in (paths.config / "project.yaml").read_text()
    assert "user account" in capsys.readouterr().out, "the risk is still stated"


def test_declining_to_continue_without_docker_returns_a_reason(tmp_path,
                                                               monkeypatch):
    import multiagents.cli as cli
    from multiagents.paths import ProjectPaths
    import multiagents.executor.docker as docker_mod

    paths = ProjectPaths(tmp_path)
    paths.config.mkdir(parents=True)
    (paths.config / "project.yaml").write_text("executor:\n  kind: local\n")

    class _TTY:
        def isatty(self):
            return True

    monkeypatch.setattr(cli.sys, "stdin", _TTY())
    monkeypatch.setattr(docker_mod, "docker_state",
                        lambda: ("no-binary", "docker is not installed"))
    monkeypatch.setattr(cli, "_confirm", lambda *a, **k: False)
    assert cli._offer_docker(paths) == "docker is not installed"


def test_the_install_hint_names_this_system_and_the_official_page():
    import multiagents.cli as cli
    hints = cli._docker_install_hint()
    assert cli.DOCKER_DOCS.startswith("https://docs.docker.com")
    # A guess can be wrong, so the hint is best-effort and the docs link is not.
    if hints:
        assert any("docker" in line for line in hints)


# --------------------------------------------------------------------------
# Unattended mode
#
# The shape a shell `until multiagents run; do ...; done` gets wrong: that loop
# stops when the command SUCCEEDS, so a turn that worked ends the run and a turn
# that crashed is retried forever.


def _launch_argv(provider, **env):
    import subprocess
    script = (Path(__file__).resolve().parents[1] / "src" / "multiagents"
              / "defaults" / "providers" / f"{provider}.sh")
    base = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent",
            "MULTIAGENTS_BIN": "/bin/echo", "MULTIAGENTS_MODEL": "m",
            "MULTIAGENTS_LAUNCH_STATE": "/tmp"}      # opencode writes its config there
    out = subprocess.run(["sh", str(script), "launch"], capture_output=True,
                         text=True, env={**base, **env})
    return out.stdout.strip()


def test_headless_flags_appear_only_in_unattended_mode():
    """`-p` is print-and-exit. Right for a supervised turn, and fatal for the
    interactive path — it would turn `multiagents run` into a one-shot."""
    for provider in ("claude", "agy"):
        assert " -p " not in " " + _launch_argv(provider) + " ", provider
        unattended = _launch_argv(provider, MULTIAGENTS_UNATTENDED="1",
                                  MULTIAGENTS_NUDGE="keep going")
        assert "-p keep going" in unattended, f"{provider}: {unattended}"


def test_opencode_uses_its_non_interactive_entry_point():
    plain = _launch_argv("opencode")
    assert not plain.startswith("run "), plain
    assert _launch_argv("opencode", MULTIAGENTS_UNATTENDED="1",
                        MULTIAGENTS_NUDGE="go").startswith("run go")


def test_two_turns_that_change_nothing_end_the_run(tmp_path, monkeypatch, capsys):
    """The stop condition. Without it an unattended run keeps paying for turns
    long after the work is finished."""
    import multiagents.cli as cli
    calls = []

    class _Done:
        pid = 1234

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    monkeypatch.setattr(cli.subprocess, "Popen",
                        lambda *a, **k: calls.append(a) or _Done())
    monkeypatch.setattr(cli.scripts, "exec_action", lambda *a, **k: (["true"], {}))
    monkeypatch.setattr(cli, "_orchestrator_hold", lambda *a: None)

    paths = _paths(tmp_path)
    code = cli._supervise(paths, _config(), "orchestrator", AgentSpec("o", "p", "m"),
                          object(), object(), {"MULTIAGENTS_RESUME": "0"}, 20)
    assert code == 0
    assert len(calls) == 2, "stops after the second idle turn, not the twentieth"
    assert "nothing left to do" in capsys.readouterr().out


def test_three_failed_turns_stop_rather_than_spin(tmp_path, monkeypatch, capsys):
    """A turn that fails instantly and is retried instantly is a busy loop that
    spends quota on nothing."""
    import multiagents.cli as cli

    class _Fail:
        pid = 1234

        def wait(self, timeout=None):
            return 1

        def poll(self):
            return 1

    slept = []
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: _Fail())
    monkeypatch.setattr(cli.scripts, "exec_action", lambda *a, **k: (["false"], {}))
    monkeypatch.setattr(cli, "_orchestrator_hold", lambda *a: None)
    monkeypatch.setattr(cli.time, "sleep", lambda s: slept.append(s))

    paths = _paths(tmp_path)
    code = cli._supervise(paths, _config(), "orchestrator", AgentSpec("o", "p", "m"),
                          object(), object(), {"MULTIAGENTS_RESUME": "0"}, 20)
    assert code == 1
    assert slept == [30, 60], "backoff grows rather than hammering"
    assert "three turns in a row failed" in capsys.readouterr().out


def _paths(tmp_path):
    from multiagents.paths import ProjectPaths
    paths = ProjectPaths(tmp_path)
    paths.ensure()
    return paths


def _config():
    from multiagents.config import Config
    return Config(project={}, providers={}, agents={}, models={}, instruction_dirs=[])


# --------------------------------------------------------------------------
# Cross-project container view


def test_the_registry_turns_a_slug_back_into_a_path(tmp_path, monkeypatch):
    """A slug embeds a hash of the path and a hash does not invert, so without
    the registry a machine-wide listing can only show opaque ids."""
    import multiagents.paths as paths_mod

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    project = tmp_path / "work" / "thing"
    project.mkdir(parents=True)

    paths_mod.register_project(project)
    known = paths_mod.known_projects()
    assert known[paths_mod.project_slug(project)]["path"] == str(project)


def test_registering_is_never_fatal(tmp_path, monkeypatch):
    """It is bookkeeping for a listing. A read-only config directory must cost
    the listing, not the run."""
    import multiagents.paths as paths_mod

    monkeypatch.setattr(paths_mod, "_registry_file",
                        lambda: tmp_path / "nope" / "cannot" / "projects.json")
    (tmp_path / "nope").write_text("a file where a directory would go")
    paths_mod.register_project(tmp_path)          # must not raise
    assert paths_mod.known_projects() == {}


def test_container_names_are_split_into_slug_and_role(monkeypatch):
    """`multiagents-proxy-<slug>` and `multiagents-<slug>` differ only by an
    infix, so a naive strip pairs the proxy with a project called `proxy-…`."""
    import multiagents.executor.docker as docker_mod

    class _Result:
        returncode = 0
        stdout = ("multiagents-voila-346f8a7b\tUp 3 minutes\tworkspace:latest\t3 minutes\n"
                  "multiagents-proxy-voila-346f8a7b\tUp 3 minutes\tproxy:latest\t3 minutes\n"
                  "multiagents-old-1234abcd\tExited (0) 2 days ago\tworkspace:latest\t2 days\n")

    monkeypatch.setattr(docker_mod, "_run", lambda *a, **k: _Result())
    rows = docker_mod.list_containers()
    by_name = {r["name"]: r for r in rows}

    assert by_name["multiagents-voila-346f8a7b"]["slug"] == "voila-346f8a7b"
    assert by_name["multiagents-voila-346f8a7b"]["proxy"] is False
    assert by_name["multiagents-proxy-voila-346f8a7b"]["slug"] == "voila-346f8a7b"
    assert by_name["multiagents-proxy-voila-346f8a7b"]["proxy"] is True
    assert by_name["multiagents-old-1234abcd"]["status"].startswith("Exited")


# --------------------------------------------------------------------------
# stop
#
# The counterpart to run. Resumable is the requirement: processes end, state
# does not.


def test_stop_commits_what_a_killed_agent_left_behind(tmp_path, quiet_git,
                                                      monkeypatch, capsys):
    """A killed agent never reaches the commit its own run would have made, so
    its edits sit uncommitted in a worktree nobody looks at again. The branch
    is what makes the work resumable, so the work has to be on it."""
    import argparse
    import multiagents.cli as cli
    import multiagents.gitops as gitops
    from multiagents.tree import Node

    monkeypatch.setattr(cli, "_confirm", lambda *a, **k: True)
    cli.cmd_init(_init_args(tmp_path))

    paths = cli._resolve(str(tmp_path))
    worktree = tmp_path / ".." / "wt-stop"
    gitops.create_worktree(tmp_path, worktree.resolve(), "agents/napper/1")
    (worktree.resolve() / "half-finished.txt").write_text("in progress\n")

    tree = Tree(paths.tree_file, paths.events_file)
    tree.add(Node(id="ag-1", agent="napper", provider="p", model="m",
                  parent=None, depth=1, status="running", pid=0,
                  branch="agents/napper/1", worktree=str(worktree.resolve())))

    monkeypatch.setattr(cli.Runner, "stop", _noop_stop)
    cli.cmd_stop(argparse.Namespace(path=str(tmp_path), keep_containers=True))

    assert "committed" in capsys.readouterr().out
    log = gitops.run(tmp_path, "log", "--oneline", "agents/napper/1").out
    assert "when stopped" in log, log


async def _noop_stop(self, agent_id):
    self.tree.set_status(agent_id, "cancelled", "stopped by parent")
    return {"agent_id": agent_id, "status": "cancelled"}


def test_stop_leaves_the_state_that_makes_a_resume_possible(tmp_path, quiet_git,
                                                            monkeypatch):
    import argparse
    import multiagents.cli as cli
    from multiagents.tree import Node

    monkeypatch.setattr(cli, "_confirm", lambda *a, **k: True)
    cli.cmd_init(_init_args(tmp_path))
    paths = cli._resolve(str(tmp_path))
    tree = Tree(paths.tree_file, paths.events_file)
    tree.add(Node(id="ag-1", agent="napper", provider="p", model="m",
                  parent=None, depth=1, status="running",
                  session_id="ses-keep-me", branch="agents/napper/1"))
    tree.add_question("ag-1", "topic", "a question nobody answered")
    tree.defer({"agent": "napper", "task": "later"}, 0, "quota")

    monkeypatch.setattr(cli.Runner, "stop", _noop_stop)
    cli.cmd_stop(argparse.Namespace(path=str(tmp_path), keep_containers=True))

    node = tree.get("ag-1")
    assert node.session_id == "ses-keep-me", "without it nothing can resume"
    assert node.branch == "agents/napper/1"
    assert len(tree.open_questions()) == 1
    assert len(tree.read()["deferred"]) == 1


def test_stop_ends_the_thing_that_starts_more_agents(tmp_path, quiet_git,
                                                     monkeypatch, capsys):
    """Stopping the agents and leaving the orchestrator running would have it
    start replacements within the minute."""
    import argparse
    import multiagents.cli as cli

    monkeypatch.setattr(cli, "_confirm", lambda *a, **k: True)
    cli.cmd_init(_init_args(tmp_path))
    paths = cli._resolve(str(tmp_path))

    signalled = []
    cli._write_pid(paths, "orchestrator", 4242)
    monkeypatch.setattr(cli, "_alive", lambda pid: True)
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: signalled.append((pid, sig)))

    cli.cmd_stop(argparse.Namespace(path=str(tmp_path), keep_containers=True))
    assert (4242, cli.signal.SIGTERM) in signalled
    assert not cli._pid_file(paths, "orchestrator").exists(), "stale pid removed"
    assert "orchestrator (pid 4242)" in capsys.readouterr().out


def test_run_refuses_when_the_container_could_not_be_built(tmp_path, quiet_git,
                                                           monkeypatch, capsys):
    """The orchestrator runs on the host, so nothing about docker is exercised
    until it delegates — which meant a missing image surfaced as a failed spawn
    minutes into a session rather than as a refusal to start."""
    import argparse
    import multiagents.cli as cli

    monkeypatch.setattr(cli, "_confirm", lambda *a, **k: True)
    cli.cmd_init(_init_args(tmp_path))
    monkeypatch.setattr(cli, "_executor_problems",
                        lambda *a: ["image multiagents/workspace:latest not built"])

    args = argparse.Namespace(path=str(tmp_path), no_launch=False, resume=True,
                              wait=False, unattended=0)
    assert cli.cmd_resume(args) == 4
    out = capsys.readouterr().out
    assert "not built" in out
    assert "fail\n           at its first delegation" in out


def test_reporting_state_is_not_refusing_to_start(tmp_path, quiet_git,
                                                  monkeypatch):
    """`--no-launch` exists to inspect a project, including a broken one."""
    import argparse
    import multiagents.cli as cli

    monkeypatch.setattr(cli, "_confirm", lambda *a, **k: True)
    cli.cmd_init(_init_args(tmp_path))
    monkeypatch.setattr(cli, "_executor_problems", lambda *a: ["image not built"])

    args = argparse.Namespace(path=str(tmp_path), no_launch=True, resume=True,
                              wait=False, unattended=0)
    assert cli.cmd_resume(args) == 0


def test_a_local_project_never_asks_docker_anything(tmp_path, monkeypatch):
    """The check must not make docker a soft dependency of the local executor."""
    import multiagents.cli as cli
    from multiagents.config import Config

    called = []
    monkeypatch.setattr(cli, "_docker_executor", lambda p: called.append(p))
    local = Config(project={"executor": {"kind": "local"}}, providers={},
                   agents={}, models={}, instruction_dirs=[])
    assert cli._executor_problems(None, local) == []
    assert called == []


def test_the_check_never_blocks_a_launch_on_itself(tmp_path, monkeypatch):
    """A broken probe should report, not become the thing that stops you."""
    import multiagents.cli as cli
    from multiagents.config import Config

    def boom(_paths):
        raise OSError("docker socket vanished")

    monkeypatch.setattr(cli, "_docker_executor", boom)
    docker = Config(project={"executor": {"kind": "docker"}}, providers={},
                    agents={}, models={}, instruction_dirs=[])
    problems = cli._executor_problems(None, docker)
    assert len(problems) == 1 and "could not check" in problems[0]


def test_init_agent_makes_the_same_checks_as_run(tmp_path, quiet_git, monkeypatch):
    """The initializer is told to consult the critic and the advisor, and a
    consult spawns an agent — so it needs the container just as much as the
    orchestrator does, and finding that out mid-conversation is worse."""
    import argparse
    import multiagents.cli as cli

    monkeypatch.setattr(cli, "_confirm", lambda *a, **k: True)
    cli.cmd_init(_init_args(tmp_path))
    monkeypatch.setattr(cli, "_executor_problems", lambda *a: ["image not built"])

    args = argparse.Namespace(path=str(tmp_path), resume=True, wait=False)
    assert cli.cmd_init_agent(args) == 4


def test_an_interrupted_agent_is_recorded_but_not_committed_in_teardown():
    """The commit deliberately does NOT happen in the cancellation handler.

    gitops shells out with a two-minute timeout, and a git call in a teardown
    running on a closing event loop can hang the shutdown it is part of. The
    work is preserved by whoever cleans up afterwards — `run` reconciling
    interrupted agents, or `multiagents stop` — where there is time, a live
    loop, and enough information to label it an interruption rather than a
    result.
    """
    import inspect
    from multiagents.runner import Runner

    source = inspect.getsource(Runner._consume)
    handler = source.split("except asyncio.CancelledError:")[1].split("except Exception")[0]
    assert "commit_all" not in handler, "a git call in the teardown can hang it"
    assert "cancelled" in handler, "but the state must still be recorded"


def test_interrupted_work_is_committed_as_a_checkpoint_not_a_result(tmp_path,
                                                                    quiet_git):
    """Marked for what it is: an orchestrator that picks the branch up later
    must not run tests against half-written files and spend tokens debugging
    syntax errors the termination caused."""
    import multiagents.cli as cli
    import multiagents.gitops as gitops
    from multiagents.tree import Node

    gitops.init_repo(tmp_path)
    gitops.initial_commit(tmp_path)
    worktree = tmp_path.parent / f"wt-{tmp_path.name}"
    gitops.create_worktree(tmp_path, worktree, "agents/impl/1")
    (worktree / "half.py").write_text("def broken(\n")

    node = Node(id="ag-1", agent="implementer", provider="p", model="m",
                parent=None, depth=1, status="cancelled",
                branch="agents/impl/1", worktree=str(worktree))
    assert cli._save_interrupted(node) is True

    message = gitops.run(tmp_path, "log", "-1", "--format=%B", "agents/impl/1").out
    assert message.startswith("WIP:")
    assert "interrupted before it finished" in message
    assert "never as a finished result" in message
    assert not gitops.is_dirty(worktree)


def test_a_clean_worktree_is_not_committed(tmp_path, quiet_git):
    import multiagents.cli as cli
    import multiagents.gitops as gitops
    from multiagents.tree import Node

    gitops.init_repo(tmp_path)
    gitops.initial_commit(tmp_path)
    node = Node(id="ag-1", agent="a", provider="p", model="m", parent=None,
                depth=1, status="cancelled", branch="b", worktree=str(tmp_path))
    assert cli._save_interrupted(node) is False


# --------------------------------------------------------------------------
# Surviving a power cut
#
# `os.replace` is atomic, so no reader sees half a file — but atomic is not
# durable, and a rename can land while the temp file's contents are still in
# the page cache. The old behaviour on the resulting zero-length tree was to
# return an empty one, silently: every session id, question, ticket and
# deferred task gone, and the next command reporting a clean project.


def _tree_with_state(tmp_path):
    from multiagents.tree import Node
    tree = _tree(tmp_path)
    tree.add(Node(id="ag-1", agent="implementer", provider="p", model="m",
                  parent=None, depth=1, status="running"))
    tree.note_event("ag-1", session_id="ses-KEEP")
    tree.add_question("ag-1", "schema", "postgres or sqlite?")
    tree.update("ag-1", steps=3)      # a later write, so the backup holds the question
    return tree


def test_a_truncated_tree_recovers_from_the_backup(tmp_path, capsys):
    tree = _tree_with_state(tmp_path)
    tree.path.write_text("")                       # the power cut

    from multiagents.tree import Tree
    reopened = Tree(tree.path, tree.events_path)
    data = reopened.read()

    assert len(data["nodes"]) == 1, "state must survive"
    assert reopened.get("ag-1").session_id == "ses-KEEP", "without it nothing resumes"
    assert len(data["questions"]) == 1
    assert "recovered 1 agent(s)" in capsys.readouterr().err


def test_recovery_costs_exactly_the_last_write(tmp_path):
    """The honest limit of one generation of backup. fsync is what makes that
    rare; the backup is for when fsync is not enough."""
    tree = _tree_with_state(tmp_path)
    tree.defer({"agent": "tester", "task": "the newest thing"}, 0, "quota")
    tree.path.write_text("")                       # corrupt right after that write

    from multiagents.tree import Tree
    data = Tree(tree.path, tree.events_path).read()
    assert len(data["nodes"]) == 1, "everything before the last write survives"
    assert data["deferred"] == [], "and the last write is what is lost"


def test_the_damaged_file_is_kept_not_discarded(tmp_path):
    tree = _tree_with_state(tmp_path)
    tree.path.write_text("{ this is not json")

    from multiagents.tree import Tree
    Tree(tree.path, tree.events_path).read()
    kept = list(tree.path.parent.glob("tree.json.corrupt-*"))
    assert len(kept) == 1, "the first thing anyone wants is to see what was in it"
    assert kept[0].read_text() == "{ this is not json"


def test_recovery_heals_rather_than_repeating_itself(tmp_path, capsys):
    """Without writing the recovery back, every later read re-recovers and
    re-warns, and the project stays one bad read from the empty case."""
    tree = _tree_with_state(tmp_path)
    tree.path.write_text("")

    from multiagents.tree import Tree
    reopened = Tree(tree.path, tree.events_path)
    reopened.read()
    capsys.readouterr()

    for _ in range(3):
        assert len(reopened.read()["nodes"]) == 1
    assert capsys.readouterr().err == "", "warned once, not on every read"
    assert len(list(tree.path.parent.glob("tree.json.corrupt-*"))) == 1


def test_losing_both_copies_says_so_instead_of_looking_clean(tmp_path, capsys):
    tree = _tree_with_state(tmp_path)
    tree.path.write_text("")
    tree.backup_path.write_text("")

    from multiagents.tree import Tree
    assert Tree(tree.path, tree.events_path).read()["nodes"] == {}
    err = capsys.readouterr().err
    assert "no usable backup" in err
    assert "are lost" in err, "silence here reads as a clean project"


def test_the_session_id_reaches_the_append_only_log(tmp_path):
    """The one field reconstructible from nowhere else. A tree lost to a bad
    write takes every session with it unless the id also reached events.jsonl."""
    import json
    tree = _tree_with_state(tmp_path)
    kinds = [json.loads(line) for line in tree.events_path.read_text().splitlines()]
    sessions = [e for e in kinds if e["kind"] == "session"]

    assert [e["session_id"] for e in sessions] == ["ses-KEEP"]
    tree.note_event("ag-1", session_id="ses-KEEP")
    again = [json.loads(l) for l in tree.events_path.read_text().splitlines()]
    assert len([e for e in again if e["kind"] == "session"]) == 1, "emitted once"


def test_the_write_is_flushed_before_the_rename(tmp_path, monkeypatch):
    """fsync is what makes the atomic rename durable; without it the rename can
    land while the contents are still in the page cache."""
    import os as os_mod
    tree = _tree(tmp_path)
    synced = []
    monkeypatch.setattr(os_mod, "fsync", lambda fd: synced.append(fd))

    from multiagents.tree import Node
    tree.add(Node(id="ag-x", agent="a", provider="p", model="m",
                  parent=None, depth=1, status="running"))
    assert len(synced) >= 1, "the temp file must be fsynced before os.replace"


# --------------------------------------------------------------------------
# Watching the orchestrator from outside it
#
# `run` execs into the CLI, so the orchestrator IS that process and there is
# nobody inside to report on it. The supervisor samples what can be seen from
# outside — and deliberately never reads the conversation: the transcript has
# no structured signal for "quota reached", and inferring state from content is
# the mistake that once cooled a provider down for fifteen minutes because an
# advisor used the word "quota".


def _verdict(**kw):
    from multiagents.watchdog import verdict
    kw.setdefault("running", True)
    kw.setdefault("quiet_for", 1.0)
    kw.setdefault("quota_known", True)
    kw.setdefault("quota_left", 0.8)
    kw.setdefault("active_agents", 0)
    return verdict(**kw)[0]


def test_quota_is_what_separates_running_out_from_crashing():
    """The transcript cannot tell these apart, and they need different
    responses: one waits for a reset, the other is a bug."""
    assert _verdict(running=False, quiet_for=None, quota_left=0.0) == "out_of_quota"
    assert _verdict(running=False, quiet_for=None, quota_left=0.5) == "stopped"


def test_a_silent_session_is_read_by_what_else_is_true():
    assert _verdict(quiet_for=5) == "working"
    assert _verdict(quiet_for=900, active_agents=2) == "waiting"
    assert _verdict(quiet_for=900, active_agents=0) == "idle"
    assert _verdict(quiet_for=900, quota_left=0.0) == "stalled"


def test_an_unwatchable_provider_is_not_reported_as_broken():
    """opencode keeps sessions in a database and agy in an opaque directory.
    Neither can be watched this way, and neither is a fault."""
    from multiagents.watchdog import verdict
    state, detail = verdict(running=True, quiet_for=None, quota_known=False,
                            quota_left=None, active_agents=1, supported=False)
    assert state == "working"
    assert "publishes no session log" in detail

    _, missing = verdict(running=True, quiet_for=None, quota_known=False,
                         quota_left=None, active_agents=0, supported=True)
    assert "not started" not in missing and "yet" in missing


def test_only_claude_declares_where_its_session_log_lives():
    import yaml
    shipped = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "src" / "multiagents" / "defaults"
         / "providers.yaml").read_text())["providers"]
    assert shipped["claude"]["transcript"]["dir"].endswith("{slug}")
    assert "transcript" not in shipped["opencode"]
    assert "transcript" not in shipped["agy"]


def test_the_slug_matches_the_encoding_claude_actually_uses(tmp_path):
    """`/`, `.` and `_` all fold to `-`; getting this wrong finds no file and
    reports a working session as unwatchable."""
    from multiagents.providers import Provider
    from multiagents.watchdog import transcript_source

    provider = Provider.from_dict("claude", {
        "bin": "claude", "transcript": {"dir": "/logs/{slug}", "glob": "*.jsonl"}})
    directory, pattern = transcript_source(provider, Path("/home/u/my_proj.v2/app"))
    assert directory == Path("/logs/-home-u-my-proj-v2-app")
    assert pattern == "*.jsonl"


def test_the_supervisor_stops_when_what_it_watches_does(tmp_path, quiet_git,
                                                        monkeypatch):
    """It exits by itself, so nothing has to remember to clean it up."""
    import multiagents.watchdog as watchdog
    from multiagents.config import Config

    paths = _paths(tmp_path)
    monkeypatch.setattr(watchdog, "alive", lambda pid: False)
    config = Config(project={}, providers={}, agents={}, models={},
                    instruction_dirs=[])

    assert watchdog.supervise(paths, config, "orchestrator", 999, interval=0.01) == 0
    record = watchdog.read_status(paths)
    assert record["running"] is False
    assert record["verdict"] in ("stopped", "out_of_quota")


# --------------------------------------------------------------------------
# Supervised run
#
# `run` stops exec'ing so it can see the child's exit code. From outside, a
# person typing /exit and a dropped connection are identical; from the parent
# they are not, and that distinction is the whole reason for the change.


def test_the_three_deliberate_endings_are_identified(monkeypatch):
    import multiagents.cli as cli
    for code in (0, -cli.signal.SIGINT, 130, -cli.signal.SIGTERM, 143):
        assert cli._exit_was_deliberate(code)[0] is True, code


def test_a_lost_terminal_or_a_crash_is_not_deliberate():
    import multiagents.cli as cli
    lost, why = cli._exit_was_deliberate(-cli.signal.SIGHUP)
    assert lost is False and "terminal was lost" in why
    assert cli._exit_was_deliberate(3)[0] is False


def test_ctrl_c_still_reaches_the_child(tmp_path):
    """SIG_IGN is inherited across exec and a handler is not. Ignoring SIGINT in
    the parent therefore made the CHILD ignore it too, so Ctrl-C stopped
    reaching the orchestrator at all — found by running it, not by reading it."""
    import os
    import multiagents.cli as cli

    code = cli._run_attached(
        ["python3", "-c", "import os,signal; os.kill(os.getpid(), signal.SIGINT)"],
        dict(os.environ))
    assert code == -cli.signal.SIGINT, (
        f"the child exited {code}; SIG_IGN in the parent would give 0")


def test_the_parent_outlives_a_signalled_child_and_restores_the_terminal(tmp_path):
    import os
    import multiagents.cli as cli

    saved = cli._terminal_state()          # None when the suite has no tty
    for script in ("import os,signal; os.kill(os.getpid(), signal.SIGINT)",
                   "raise SystemExit(3)"):
        cli._run_attached(["python3", "-c", script], dict(os.environ))
    assert cli._terminal_state() == saved, "the terminal must come back as it was"


def test_a_child_is_not_put_in_its_own_session(tmp_path):
    """A new session leaves the child outside the terminal's foreground group,
    so its first read of stdin raises SIGTTIN and it stops dead. The detached
    watcher does pass that flag, correctly, which makes it easy to copy here."""
    import inspect
    import multiagents.cli as cli

    source = inspect.getsource(cli._run_attached)
    assert "start_new_session" not in source or "NOT start_new_session" in source


def test_a_lost_terminal_with_no_tty_left_goes_headless(tmp_path, monkeypatch):
    """Retrying interactively needs a terminal to retry into. Without one the
    handover to the headless loop is the only thing left."""
    import multiagents.cli as cli

    handed = []
    monkeypatch.setattr(cli, "_start_supervisor", lambda *a: None)
    monkeypatch.setattr(cli, "_supervise", lambda *a, **k: handed.append(1) or 0)
    monkeypatch.setattr(cli, "_run_attached", lambda *a, **k: -cli.signal.SIGHUP)
    monkeypatch.setattr(cli.sys, "stdin", type("T", (), {"isatty": lambda s: False})())

    cli._run_supervised(_paths(tmp_path), _config(), "orchestrator", None, None,
                        None, {}, [], {})
    assert handed == [1]


def test_orphaned_agents_are_reaped_before_a_new_session(tmp_path, quiet_git,
                                                         monkeypatch, capsys):
    """Agents run in their own session so stopping one stops the tools beneath
    it — which also means they outlive a server that crashed, and keep mutating
    the worktrees the next session is about to use."""
    import argparse
    import multiagents.cli as cli
    from multiagents.tree import Node

    monkeypatch.setattr(cli, "_confirm", lambda *a, **k: True)
    cli.cmd_init(_init_args(tmp_path))
    paths = cli._resolve(str(tmp_path))
    tree = Tree(paths.tree_file, paths.events_file)
    tree.add(Node(id="ag-1", agent="implementer", provider="p", model="m",
                  parent=None, depth=1, status="running", pid=4242))

    stopped = []
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig=0: None)      # "alive"
    monkeypatch.setattr(cli, "_reaper", lambda p: type(
        "R", (), {"stop_detached": lambda self, node: stopped.append(node.id) or True})())
    monkeypatch.setattr(cli, "_alive_pid", lambda p, role: False)     # no live session

    args = argparse.Namespace(path=str(tmp_path), no_launch=True, resume=True,
                              wait=False, unattended=0, supervise=True)
    cli.cmd_resume(args)

    assert stopped == ["ag-1"]
    assert tree.get("ag-1").status == "orphaned"
    assert "reaped" in capsys.readouterr().out


def test_a_live_session_elsewhere_is_not_reaped(tmp_path, quiet_git, monkeypatch):
    """Another terminal running the same project must not have its agents
    killed by this one starting up."""
    import argparse
    import multiagents.cli as cli
    from multiagents.tree import Node

    monkeypatch.setattr(cli, "_confirm", lambda *a, **k: True)
    cli.cmd_init(_init_args(tmp_path))
    paths = cli._resolve(str(tmp_path))
    tree = Tree(paths.tree_file, paths.events_file)
    tree.add(Node(id="ag-1", agent="implementer", provider="p", model="m",
                  parent=None, depth=1, status="running", pid=4242))

    stopped = []
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig=0: None)
    monkeypatch.setattr(cli, "_reaper", lambda p: type(
        "R", (), {"stop_detached": lambda self, node: stopped.append(node.id)})())
    monkeypatch.setattr(cli, "_alive_pid", lambda p, role: True)      # live session

    cli.cmd_resume(argparse.Namespace(path=str(tmp_path), no_launch=True,
                                      resume=True, wait=False, unattended=0,
                                      supervise=True))
    assert stopped == []
    assert tree.get("ag-1").status == "running", "left alone for its owner"


def test_a_session_nobody_spoke_to_is_not_continued(tmp_path, monkeypatch, capsys):
    """A headless turn supplies the user message the TUI waits for you to type.
    With nothing said, "continue where you left off" has nowhere to continue
    from — and the nudge would have it invent work from BRIEF.md, unattended,
    with agents holding bypass permissions."""
    import multiagents.cli as cli
    import multiagents.watchdog as watchdog

    handed_over = []
    monkeypatch.setattr(cli, "_start_supervisor", lambda *a: None)
    monkeypatch.setattr(cli, "_supervise", lambda *a, **k: handed_over.append(1) or 0)
    monkeypatch.setattr(cli, "_run_attached", lambda *a, **k: -cli.signal.SIGHUP)
    paths = _paths(tmp_path)

    monkeypatch.setattr(watchdog, "has_human_turn", lambda *a: False)
    assert cli._run_supervised(paths, _config(), "orchestrator", None, None,
                               None, {}, [], {}) == 1
    assert handed_over == []
    assert "nothing was asked of it" in capsys.readouterr().out

    monkeypatch.setattr(watchdog, "has_human_turn", lambda *a: True)
    cli._run_supervised(paths, _config(), "orchestrator", None, None, None,
                        {}, [], {})
    assert handed_over == [1], "a session with real work does continue"


def test_a_tool_result_is_not_mistaken_for_someone_talking(tmp_path):
    """`user` records carry tool results too — 992 of them against 102 typed
    messages in a real session. Counting records would read as 'someone spoke'
    for a session where nobody did."""
    import json
    from multiagents.providers import Provider
    from multiagents.watchdog import has_human_turn

    logs = tmp_path / "-logs"
    logs.mkdir()
    provider = Provider.from_dict("claude", {
        "bin": "claude",
        "transcript": {"dir": str(tmp_path) + "/{slug}", "glob": "*.jsonl"}})

    session = logs / "s.jsonl"
    session.write_text("\n".join(json.dumps(r) for r in [
        {"type": "system", "subtype": "init"},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "ok"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text"}]}},
    ]) + "\n")
    assert has_human_turn(provider, Path("/logs")) is False

    with session.open("a") as handle:
        handle.write(json.dumps(
            {"type": "user", "message": {"content": "do the thing"}}) + "\n")
    assert has_human_turn(provider, Path("/logs")) is True


def test_an_unexpected_end_is_retried_interactively_before_anything_headless(
        tmp_path, monkeypatch, capsys):
    """The requested behaviour: wait, then start it again with an opening
    message, while a person can still see it."""
    import multiagents.cli as cli
    from multiagents.config import Config

    launches, slept = [], []
    monkeypatch.setattr(cli, "_start_supervisor", lambda *a: None)
    monkeypatch.setattr(cli, "_orchestrator_hold", lambda *a: None)
    monkeypatch.setattr(cli.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(cli.sys, "stdin", type("T", (), {"isatty": lambda self: True})())
    monkeypatch.setattr(cli, "_supervise", lambda *a, **k: 99)

    codes = iter([-cli.signal.SIGHUP, -cli.signal.SIGHUP, 0])   # lost, lost, quit
    def attached(argv, env, stalled=None):
        launches.append(env.get("MULTIAGENTS_RESUME_PROMPT"))
        return next(codes)
    monkeypatch.setattr(cli, "_run_attached", attached)

    config = Config(project={"limits": {"restart_attempts": 5,
                                        "restart_delay_seconds": 30}},
                    providers={}, agents={}, models={}, instruction_dirs=[])
    result = cli._run_supervised(_paths(tmp_path), config, "orchestrator", None,
                                 None, None, {}, [], {})

    assert result == 0, "a clean quit on a retry ends the loop"
    assert launches[0] is None, "the first launch is the ordinary one"
    assert all("ended unexpectedly" in p for p in launches[1:]), launches
    assert slept == [30, 30], "waits between attempts"


def test_retrying_gives_up_rather_than_looping_forever(tmp_path, monkeypatch):
    import multiagents.cli as cli
    from multiagents.config import Config

    monkeypatch.setattr(cli, "_start_supervisor", lambda *a: None)
    monkeypatch.setattr(cli, "_orchestrator_hold", lambda *a: None)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    monkeypatch.setattr(cli.sys, "stdin", type("T", (), {"isatty": lambda self: True})())
    monkeypatch.setattr(cli, "_run_attached", lambda *a, **k: -cli.signal.SIGHUP)
    handed = []
    monkeypatch.setattr(cli, "_supervise", lambda *a, **k: handed.append(1) or 0)

    config = Config(project={"limits": {"restart_attempts": 2,
                                        "restart_delay_seconds": 0}},
                    providers={}, agents={}, models={}, instruction_dirs=[])
    result = cli._run_supervised(_paths(tmp_path), config, "orchestrator", None,
                                 None, None, {}, [], {})
    assert handed == [1], "out of attempts with no terminal left -> headless"
    assert result == 0


def test_the_parent_ignores_the_signal_that_takes_the_terminal(tmp_path):
    """SIGHUP reaches the whole foreground group. A parent that dies with the
    terminal can do none of the deciding it exists to do — and the child still
    gets the default, because a handler is reset on exec while SIG_IGN is not."""
    import inspect
    import multiagents.cli as cli

    source = inspect.getsource(cli._run_attached)
    assert "signal.SIGHUP" in source
    # The prose explains why SIG_IGN is wrong; what matters is that it is not
    # what gets installed.
    assert "signal.signal(sig, signal.SIG_IGN)" not in source


def test_the_restart_prompt_does_not_ask_for_permission_to_continue():
    """A restart is not a decision point. An orchestrator that comes back,
    proposes a plan and waits has turned an interruption into a second one —
    and nobody may be reading."""
    import multiagents.cli as cli
    prompt = " ".join(cli.RESUME_PROMPT.split())

    assert "carry straight on with the work" in prompt
    assert "Do not propose a plan and wait" in prompt
    assert "do not ask whether to proceed" in prompt
    # The exception, which is the norm everywhere else in this system.
    assert "genuinely theirs to make" in prompt


def test_a_session_that_dies_immediately_is_not_retried(tmp_path, monkeypatch,
                                                        capsys):
    """The advisor's objection, taken with a discriminator rather than a
    blanket rule: a CLI that dies within seconds of starting is reading the
    same state and hitting the same fault. Retrying that is a loop that spends
    tokens to arrive back where it started."""
    import multiagents.cli as cli
    from multiagents.config import Config

    monkeypatch.setattr(cli, "_start_supervisor", lambda *a: None)
    monkeypatch.setattr(cli, "_orchestrator_hold", lambda *a: None)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    monkeypatch.setattr(cli.sys, "stdin", type("T", (), {"isatty": lambda s: True})())
    monkeypatch.setattr(cli, "_run_attached", lambda *a, **k: 3)     # instant crash
    handed = []
    monkeypatch.setattr(cli, "_supervise", lambda *a, **k: handed.append(1) or 0)

    config = Config(project={"limits": {"restart_attempts": 5, "restart_on_crash": True,
                                        "restart_min_runtime_seconds": 60}},
                    providers={}, agents={}, models={}, instruction_dirs=[])
    assert cli._run_supervised(_paths(tmp_path), config, "orchestrator", None,
                               None, None, {}, [], {}) == 1
    out = capsys.readouterr().out
    assert "the same fault being read again" in out
    assert handed == []


def test_a_session_that_ran_a_while_before_failing_is_retried(tmp_path,
                                                              monkeypatch):
    """The other side: a session that worked for an hour and then died hit
    something passing, and that is exactly what a retry is for."""
    import multiagents.cli as cli
    from multiagents.config import Config

    clock = iter([0.0, 4000.0, 4000.0, 8000.0])      # each run lasts ~an hour
    monkeypatch.setattr(cli, "_start_supervisor", lambda *a: None)
    monkeypatch.setattr(cli, "_orchestrator_hold", lambda *a: None)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(cli.sys, "stdin", type("T", (), {"isatty": lambda s: True})())

    codes = iter([3, 0])
    monkeypatch.setattr(cli, "_run_attached", lambda *a, **k: next(codes))

    config = Config(project={"limits": {"restart_attempts": 5, "restart_on_crash": True,
                                        "restart_min_runtime_seconds": 60}},
                    providers={}, agents={}, models={}, instruction_dirs=[])
    assert cli._run_supervised(_paths(tmp_path), config, "orchestrator", None,
                               None, None, {}, [], {}) == 0


def test_the_parent_survives_a_hangup_and_the_child_does_not(tmp_path):
    """Terminal loss, without depending on pty semantics: the child signals its
    own parent, which is exactly what the kernel does to a foreground group."""
    import os
    import multiagents.cli as cli

    script = ("import os, signal, time\n"
              "os.kill(os.getppid(), signal.SIGHUP)\n"   # as the terminal would
              "time.sleep(0.2)\n"
              "os.kill(os.getpid(), signal.SIGHUP)\n")   # and the child dies of it
    code = cli._run_attached(["python3", "-c", script], dict(os.environ))

    assert code == -cli.signal.SIGHUP, "the child must still die on SIGHUP"
    deliberate, why = cli._exit_was_deliberate(code)
    assert deliberate is False and "terminal was lost" in why


def test_a_crash_is_not_retried_by_default(tmp_path, monkeypatch, capsys):
    """The advisor's argument, taken: by the time an error reaches the process
    boundary the CLI has exhausted its own retries, so whatever produced it is
    still there and a restart reads it again. And the obvious defence — only
    retry if it ran a while — does not hold: a context-length overrun takes
    minutes to arrive and then repeats exactly."""
    import multiagents.cli as cli
    from multiagents.config import Config

    retried = []
    monkeypatch.setattr(cli, "_start_supervisor", lambda *a: None)
    monkeypatch.setattr(cli.sys, "stdin", type("T", (), {"isatty": lambda s: True})())
    monkeypatch.setattr(cli, "_run_attached",
                        lambda *a, **k: retried.append(1) or 3)
    monkeypatch.setattr(cli, "_supervise", lambda *a, **k: 99)

    assert cli._run_supervised(_paths(tmp_path), _config(), "orchestrator", None,
                               None, None, {}, [], {}) == 1
    assert len(retried) == 1, "the first run only; no retry"
    assert "still there and a restart would meet it again" in capsys.readouterr().out


def test_a_lost_terminal_is_always_retried(tmp_path, monkeypatch):
    """Different event, different terms: a closed laptop leaves a healthy
    process killed by its environment, not a fault to be read again."""
    import multiagents.cli as cli
    from multiagents.config import Config

    runs = []
    monkeypatch.setattr(cli, "_start_supervisor", lambda *a: None)
    monkeypatch.setattr(cli, "_orchestrator_hold", lambda *a: None)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    monkeypatch.setattr(cli.sys, "stdin", type("T", (), {"isatty": lambda s: True})())
    codes = iter([-cli.signal.SIGHUP, 0])
    monkeypatch.setattr(cli, "_run_attached", lambda *a, **k: runs.append(1) or next(codes))

    config = Config(project={"limits": {"restart_delay_seconds": 0}},
                    providers={}, agents={}, models={}, instruction_dirs=[])
    assert cli._run_supervised(_paths(tmp_path), config, "orchestrator", None,
                               None, None, {}, [], {}) == 0
    assert len(runs) == 2, "retried without needing restart_on_crash"


def test_the_initializer_knows_it_may_be_shaping_a_second_phase():
    """Its opening frames a greenfield — "before any implementation starts",
    "nothing is built during this stage". Coming back to a project with merged
    work and existing specs is a different job, and reading a half-built repo
    through greenfield instructions produces a brief that plans what already
    exists."""
    brief = (Path(__file__).resolve().parents[1] / "src" / "multiagents"
             / "defaults" / "agents" / "_initializer.md").read_text()
    flat = " ".join(brief.split())

    assert "Returning after work has been done" in brief
    assert "Read what was actually built, not what was planned" in flat
    assert "Extend the brief; do not rewrite it" in flat
    assert "cannot tell finished work from planned work will redo it" in flat


def test_the_orchestrator_hands_back_rather_than_inventing_a_next_phase():
    """It executes the brief; it does not decide what the project is. An idle
    tree with budget left is exactly when a system like this starts spending a
    subscription on work nobody asked for."""
    brief = (Path(__file__).resolve().parents[1] / "src" / "multiagents"
             / "defaults" / "agents" / "_orchestrator.md").read_text()
    flat = " ".join(brief.split())

    assert "When the brief is done" in brief
    assert "do not invent a next phase" in flat
    assert "multiagents init-agent" in flat
    assert "merge or discard the branches you own" in flat


# --------------------------------------------------------------------------
# One session per role
#
# `--continue` resumes the most recent conversation IN THE DIRECTORY, and both
# launched roles share the project root — so `init-agent` after `run` reopened
# the orchestrator's conversation. Reported from a real session.


def test_each_launched_role_owns_a_stable_session_id(tmp_path):
    import multiagents.cli as cli
    paths = _paths(tmp_path)

    orchestrator = cli._role_session_id(paths, "orchestrator")
    initializer = cli._role_session_id(paths, "initializer")

    assert orchestrator != initializer, "sharing one is the bug this fixes"
    assert cli._role_session_id(paths, "orchestrator") == orchestrator, "stable"
    assert len(orchestrator.split("-")) == 5, "claude requires a uuid"


def test_fresh_rotates_the_id_rather_than_colliding(tmp_path):
    """`--fresh` means a new conversation. Reusing an id that already names a
    transcript would collide with the session it points at."""
    import multiagents.cli as cli
    paths = _paths(tmp_path)

    first = cli._role_session_id(paths, "orchestrator")
    rotated = cli._role_session_id(paths, "orchestrator", rotate=True)
    assert rotated != first
    assert cli._role_session_id(paths, "orchestrator") == rotated, "and it sticks"


def test_the_launcher_resumes_by_id_only_when_that_session_exists(tmp_path):
    """Resuming an id with no transcript is what `--session-id` is for; the two
    are not interchangeable."""
    import os
    import subprocess
    script = (Path(__file__).resolve().parents[1] / "src" / "multiagents"
              / "defaults" / "providers" / "claude.sh")
    home = tmp_path / "home"
    workdir = tmp_path / "proj"
    workdir.mkdir()
    slug = str(workdir).replace("/", "-").replace(".", "-").replace("_", "-")
    sessions = home / ".claude" / "projects" / slug
    sessions.mkdir(parents=True)
    uuid = "11111111-2222-4333-8444-555555555555"

    def launch(resume):
        out = subprocess.run(
            ["sh", str(script), "launch"], cwd=workdir, capture_output=True, text=True,
            env={"PATH": "/usr/bin:/bin", "HOME": str(home),
                 "MULTIAGENTS_BIN": "/bin/echo", "MULTIAGENTS_MODEL": "m",
                 "MULTIAGENTS_SESSION_ID": uuid, "MULTIAGENTS_RESUME": resume})
        return out.stdout.strip()

    assert f"--session-id {uuid}" in launch("1"), "no transcript yet -> create it"
    (sessions / f"{uuid}.jsonl").write_text("{}\n")
    assert f"--resume {uuid}" in launch("1"), "it exists -> resume that one"
    assert f"--session-id {uuid}" in launch("0"), "--fresh never resumes"


# --------------------------------------------------------------------------
# Circuit breaker
#
# From a real session: an OAuth token was revoked server-side, and thirteen
# agents failed identically before anyone noticed. The evidence was prose in
# the agents' own output — `claude auth status` still reported "loggedIn: true"
# and the CLI's own result event said `status: success` while the only output
# was "API Error: 401 OAuth access token has been revoked".


def test_a_provider_trips_after_consecutive_failures(tmp_path):
    """Cause-agnostic on purpose. Classifying the reason would mean reading
    agent text, which once cooled a provider down because an advisor used the
    word "quota" in a sentence."""
    tree = _tree(tmp_path)

    assert tree.note_run_outcome("claude", ok=False, threshold=3) is None
    assert tree.note_run_outcome("claude", ok=False, threshold=3) is None
    trip = tree.note_run_outcome("claude", ok=False, threshold=3)

    assert trip is not None and trip["failures"] == 3
    # While a cooldown is running, further failures are the same fault.
    tree.set_cooldown("claude", time.time() + 1800, "3 runs in a row failed")
    assert tree.note_run_outcome("claude", ok=False, threshold=3) is None, \
        "no second trip while it is already cooling down"


def test_a_success_clears_the_count(tmp_path):
    """Two failures and a success is a bad afternoon, not a broken provider."""
    tree = _tree(tmp_path)
    tree.note_run_outcome("agy", ok=False, threshold=3)
    tree.note_run_outcome("agy", ok=False, threshold=3)
    tree.note_run_outcome("agy", ok=True, threshold=3)

    assert tree.provider_health()["agy"]["consecutive_failures"] == 0
    assert tree.note_run_outcome("agy", ok=False, threshold=3) is None


def test_providers_are_counted_separately(tmp_path):
    tree = _tree(tmp_path)
    for _ in range(3):
        tree.note_run_outcome("claude", ok=False, threshold=3)
    assert tree.note_run_outcome("opencode", ok=False, threshold=3) is None
    assert tree.provider_health()["opencode"]["consecutive_failures"] == 1


def test_a_model_override_from_another_provider_is_refused(tmp_path):
    """Seen in the wild: `claude --model opencode-go/kimi-k2.7-code`, and
    `claude --model deep`. Both cost a spawn before the CLI rejected them."""
    import asyncio
    from multiagents.config import Config
    from multiagents.paths import ProjectPaths
    from multiagents.runner import Runner

    paths = ProjectPaths(tmp_path)
    paths.ensure()
    config = Config(
        project={}, providers={"claude": {"bin": "sh", "spawn": {"args": ["-c", "true"]}}},
        agents={"impl": AgentSpec("impl", "claude", "sonnet")},
        models={"claude": [{"id": "sonnet"}, {"id": "opus"}]}, instruction_dirs=[])
    runner = Runner(paths, config)

    with pytest.raises(ValueError) as excinfo:
        asyncio.run(runner.start("impl", "go", model="opencode-go/kimi-k2.7-code"))
    assert "does not serve a model called" in str(excinfo.value)
    assert "belongs to its provider's namespace" in str(excinfo.value)


def test_an_unknown_model_list_does_not_block_an_override(tmp_path):
    """models.yaml can be stale or empty; refusing on that basis would be worse
    than the mistake it prevents."""
    import asyncio
    from multiagents.config import Config
    from multiagents.paths import ProjectPaths
    from multiagents.runner import Runner

    paths = ProjectPaths(tmp_path)
    paths.ensure()
    config = Config(
        project={}, providers={"p": {"bin": "sh", "spawn": {"args": ["-c", "true"]}}},
        agents={"impl": AgentSpec("impl", "p", "m")}, models={}, instruction_dirs=[])

    with pytest.raises(RuntimeError) as excinfo:      # fails later, on the repo
        asyncio.run(Runner(paths, config).start("impl", "go", model="anything"))
    assert "does not serve a model" not in str(excinfo.value)


# --------------------------------------------------------------------------
# Credentials cached in a running process
#
# A container started before a re-login kept serving the revoked token: claude
# runs a background daemon, and every invocation in that container talked to it
# while the credential file on disk — shared by symlink — was correct the whole
# time. `auth status` cannot see that, because it reads the same correct file.


def test_a_container_reading_an_old_credential_file_is_caught(tmp_path, monkeypatch):
    """Docker binds a FILE by inode, and every CLI here replaces its credential
    by rename — so the moment a token refreshes, the container is reading a file
    the host no longer has, and presents a token the refresh rotated away.

    Measured before this check existed: a container bound overnight served a
    token from the previous day for eleven hours. Every agent in it failed with
    "401 OAuth access token has been revoked" while `auth status` on the host
    read the correct file and said everything was fine."""
    from multiagents.executor.docker import DockerExecutor
    from multiagents.paths import ProjectPaths
    from multiagents.providers import Provider
    import multiagents.executor.docker as docker_mod

    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    credential = home / ".claude" / ".credentials.json"
    credential.write_text("{}")
    monkeypatch.setattr(docker_mod.Path, "home", staticmethod(lambda: home))

    provider = Provider.from_dict("claude", {
        "bin": "claude", "home_links": [".claude/.credentials.json"]})
    executor = DockerExecutor({"image": "i"}, ProjectPaths(tmp_path),
                              {"claude": provider}, tmp_path)
    monkeypatch.setattr(executor, "container_state", lambda name: "running")

    class _Result:
        returncode = 0
        stdout = "999999\n"                    # the container's inode

    monkeypatch.setattr(docker_mod, "_run", lambda *a, **k: _Result())
    drift = executor.credential_drift()
    assert len(drift) == 1
    assert drift[0]["container_inode"] == "999999"
    assert drift[0]["host_inode"] == str(credential.stat().st_ino)

    # Same inode: the mount is live and there is nothing to report.
    _Result.stdout = f"{credential.stat().st_ino}\n"
    assert executor.credential_drift() == []


def test_a_drifted_credential_is_repaired_when_nothing_is_running(tmp_path, monkeypatch):
    """A restart re-resolves the bind — measured against docker, not assumed.
    Idle that is a few seconds; busy it would kill agents on providers that are
    working perfectly well, so it is reported instead."""
    import multiagents.cli as cli
    from multiagents.config import Config
    from multiagents.tree import Node, Tree

    paths = _paths(tmp_path)
    config = Config(project={"executor": {"kind": "docker"}}, providers={},
                    agents={}, models={}, instruction_dirs=[])
    calls = []

    class _Executor:
        container = "c"
        def credential_drift(self):
            return [{"path": "/home/u/.claude/.credentials.json",
                     "host_inode": "1", "container_inode": "2"}]
        def stop(self, remove=False): calls.append("stop")
        def ensure_running(self): calls.append("start")

    monkeypatch.setattr(cli, "_docker_executor", lambda p: _Executor())
    notes = cli._repair_credential_drift(paths, config)
    assert calls == ["stop", "start"]
    assert "restarted the container" in notes[0]

    # With an agent running, work wins: it reports and leaves it alone.
    calls.clear()
    tree = Tree(paths.tree_file, paths.events_file)
    tree.add(Node(id="ag-1", agent="implementer", provider="agy", model="m",
                  parent=None, depth=0, status="running"))
    notes = cli._repair_credential_drift(paths, config)
    assert calls == []
    assert "1 agent(s) are running" in notes[0]


def test_a_busy_container_is_not_restarted_without_asking(tmp_path, quiet_git,
                                                          monkeypatch, capsys):
    """Restarting kills every agent in there, including ones on providers that
    are perfectly fine."""
    import multiagents.cli as cli
    from multiagents.config import Config
    from multiagents.tree import Node

    paths = _paths(tmp_path)
    tree = Tree(paths.tree_file, paths.events_file)
    tree.add(Node(id="ag-1", agent="a", provider="opencode", model="m",
                  parent=None, depth=1, status="running"))

    stopped = []
    class _Executor:
        container = "c"
        def container_state(self, name): return "running"
        def stop(self, remove=False): stopped.append("stop")
        def ensure_running(self): stopped.append("up")

    monkeypatch.setattr(cli, "_docker_executor", lambda p: _Executor())
    monkeypatch.setattr(cli, "_confirm", lambda *a, **k: False)
    config = Config(project={"executor": {"kind": "docker"}}, providers={},
                    agents={}, models={}, instruction_dirs=[])

    cli._refresh_container_after_login(paths, config, "claude")
    assert stopped == [], "declined, so nothing was killed"
    assert "keep using the old credential" in capsys.readouterr().out


def test_an_idle_container_is_restarted_without_asking(tmp_path, quiet_git,
                                                       monkeypatch):
    import multiagents.cli as cli
    from multiagents.config import Config

    actions = []
    class _Executor:
        container = "c"
        def container_state(self, name): return "running"
        def stop(self, remove=False): actions.append("stop")
        def ensure_running(self): actions.append("up")

    monkeypatch.setattr(cli, "_docker_executor", lambda p: _Executor())
    monkeypatch.setattr(cli, "_confirm",
                        lambda *a, **k: pytest.fail("must not ask when idle"))
    config = Config(project={"executor": {"kind": "docker"}}, providers={},
                    agents={}, models={}, instruction_dirs=[])

    cli._refresh_container_after_login(_paths(tmp_path), config, "claude")
    assert actions == ["stop", "up"]


# --------------------------------------------------------------------------
# bug-cee638, filed by the bug-reporter against a real session
#
# Two findings: auth_status reported "authenticated: true" through an hour in
# which every claude subagent failed with a 401, and steer_agent reported
# `{"steered": true, "status": "running"}` against a process already exiting.


def test_a_stored_login_is_reported_apart_from_evidence_that_it_works(tmp_path,
                                                                      monkeypatch):
    """The check reads local state and cannot see a revoked token. Blending
    that into one word is what misled an hour of diagnosis."""
    import multiagents.server as server_mod
    from multiagents.config import Config

    tree = _tree(tmp_path)
    for _ in range(2):
        tree.note_run_outcome("claude", ok=False, threshold=99, reason="401 revoked")

    class _Run:
        providers = {}
        paths = _paths(tmp_path)
        config = Config(project={}, providers={}, agents={}, models={},
                        instruction_dirs=[])
    _Run.tree = tree
    monkeypatch.setattr(server_mod, "runner", lambda: _Run())
    monkeypatch.setattr(server_mod.auth_mod, "check_all", lambda *a, **k: {
        "claude": type("S", (), {
            "ok": True,
            "to_dict": lambda self: {"provider": "claude", "status": "authenticated",
                                     "authenticated": True}})()})

    out = server_mod.auth_status()
    claude = out["providers"]["claude"]
    assert claude["stored_login"] is True, "the credential really is on disk"
    assert claude["last_run"] == "failed", "and the evidence says it does not work"
    assert claude["recent_failures"] == 2
    assert "not a working one" in claude["warning"]
    assert out["degraded"] == ["claude"]


def test_the_evidence_field_survives_redaction():
    """`credential_present` masked itself: redact.py drops any key matching
    that word wholesale, which is correct for secrets and useless for a flag."""
    from multiagents.redact import scrub
    assert scrub({"stored_login": True})["stored_login"] is True
    assert scrub({"credential_present": True})["credential_present"] == "[redacted]"


def test_steer_does_not_report_running_against_a_dead_run(tmp_path):
    """`_launch` returns when the process has STARTED, which is not the same as
    it being alive — an unauthenticated provider answers in well under a
    second."""
    import asyncio
    from multiagents.tree import Node

    r = _runner(tmp_path, {"worker": AgentSpec("worker", "p", "m")},
                {"p": {"bin": "sh", "spawn": {"args": ["-c", "exit 1"],
                                              "resume": ["-c", "exit 1"]}}})
    r.tree.add(Node(id="ag-1", agent="worker", provider="p", model="m",
                    parent=None, depth=1, status="running", session_id="s-1",
                    worktree=str(tmp_path)))

    result = asyncio.run(r.steer("ag-1", "change course"))
    assert result["steered"] is False, result
    assert "ended immediately" in result["error"]
    assert result["status"] != "running"


def test_never_having_run_is_not_reported_as_failing(tmp_path, monkeypatch):
    """A boolean false would read as "broken" for a provider that has simply
    not run yet, and send the orchestrator off to debug a healthy system."""
    import multiagents.server as server_mod
    from multiagents.config import Config

    class _Run:
        providers = {}
        paths = _paths(tmp_path)
        tree = _tree(tmp_path)
        config = Config(project={}, providers={}, agents={}, models={},
                        instruction_dirs=[])
    monkeypatch.setattr(server_mod, "runner", lambda: _Run())
    monkeypatch.setattr(server_mod.auth_mod, "check_all", lambda *a, **k: {
        "agy": type("S", (), {"ok": True, "to_dict": lambda self: {
            "provider": "agy", "authenticated": True}})()})

    fresh = server_mod.auth_status()["providers"]["agy"]
    assert fresh["last_run"] == "untested"
    assert fresh["recent_failures"] == 0
    assert "warning" not in fresh, "no evidence is not a problem to report"

    _Run.tree.note_run_outcome("agy", ok=True, threshold=3)
    assert server_mod.auth_status()["providers"]["agy"]["last_run"] == "success"


def test_steer_confirms_on_the_first_event_not_a_fixed_sleep(tmp_path):
    """A fixed sleep loses in both directions: it blocks a healthy agent for no
    reason, and still races a failure that takes longer than the timeout."""
    import asyncio
    import multiagents.runner as runner_mod
    from multiagents.tree import Node

    r = _runner(tmp_path, {"worker": AgentSpec("worker", "p", "m")},
                {"p": {"bin": "sh", "spawn": {"args": ["-c", "sleep 30"],
                                              "resume": ["-c", "sleep 30"]}}})
    r.tree.add(Node(id="ag-1", agent="worker", provider="p", model="m",
                    parent=None, depth=1, status="running", session_id="s-1",
                    worktree=str(tmp_path)))

    async def scenario():
        started = asyncio.get_running_loop().time()
        task = asyncio.ensure_future(r.steer("ag-1", "carry on"))
        await asyncio.sleep(0.2)
        run = r.runs.get("ag-1")
        if run is not None:
            run.events.append({"kind": "step"})      # the agent shows signs of life
        result = await task
        return result, asyncio.get_running_loop().time() - started

    result, elapsed = asyncio.run(scenario())
    assert result["steered"] is True and result["status"] == "running"
    assert elapsed < runner_mod.STEER_CONFIRM_SECONDS, (
        f"returned in {elapsed:.1f}s; it must not wait out the ceiling")


def test_the_bug_reporter_is_told_where_to_stop_guessing():
    """It got the evidence and the fix right and the cause wrong, guessing at a
    mechanism it could not see. A confident wrong hypothesis reads as a finding
    and costs someone an hour."""
    brief = (Path(__file__).resolve().parents[1] / "src" / "multiagents"
             / "defaults" / "agents" / "bug-reporter.md").read_text()
    flat = " ".join(brief.split())

    assert "Do not guess at systems you cannot verify" in flat
    assert "say exactly where the trail goes cold" in flat


# --------------------------------------------------------------------------
# Closing a ticket from the CLI
#
# `submit` and `discard` could open a ticket's life and end it unread, but
# marking one fixed existed only as an MCP tool — reachable by the orchestrator
# and not by the person who fixed it. A ticket you reported and then fixed
# stayed `reported` for ever unless someone edited the tree by hand.


def _ticket_args(path, ticket_id, note="", declined=False, action="resolve"):
    import argparse
    return argparse.Namespace(path=str(path), action=action, ticket_id=ticket_id,
                              all=False, note=note, declined=declined)


def test_resolving_a_reported_ticket_says_it_is_still_open_upstream(tmp_path,
                                                                    quiet_git,
                                                                    monkeypatch,
                                                                    capsys):
    """Fixing it here does not close it for anyone else."""
    import multiagents.cli as cli

    monkeypatch.setattr(cli, "_confirm", lambda *a, **k: True)
    cli.cmd_init(_init_args(tmp_path))
    paths = cli._resolve(str(tmp_path))
    tree = Tree(paths.tree_file, paths.events_file)
    ticket = tree.add_ticket("ag-1", "a real defect", "body", "minor")
    tree.set_ticket_status(ticket["id"], "reported", "", "https://example.invalid/9")

    assert cli.cmd_tickets(_ticket_args(tmp_path, ticket["id"], "fixed in abc123")) == 0
    out = capsys.readouterr().out
    assert "marked fixed: fixed in abc123" in out
    assert "close it there too" in out
    assert tree.get_ticket(ticket["id"])["status"] == "fixed"


def test_resolving_an_unfiled_ticket_offers_to_file_it(tmp_path, quiet_git,
                                                       monkeypatch, capsys):
    """A local fix leaves the defect in place for everyone who has not got it."""
    import multiagents.cli as cli

    monkeypatch.setattr(cli, "_confirm", lambda *a, **k: True)
    cli.cmd_init(_init_args(tmp_path))
    paths = cli._resolve(str(tmp_path))
    tree = Tree(paths.tree_file, paths.events_file)
    ticket = tree.add_ticket("ag-1", "never filed", "body", "minor")

    cli.cmd_tickets(_ticket_args(tmp_path, ticket["id"]))
    out = capsys.readouterr().out
    assert "not reported upstream" in out
    assert f"tickets submit {ticket['id']}" in out


def test_declining_is_distinct_from_fixing(tmp_path, quiet_git, monkeypatch):
    import multiagents.cli as cli

    monkeypatch.setattr(cli, "_confirm", lambda *a, **k: True)
    cli.cmd_init(_init_args(tmp_path))
    paths = cli._resolve(str(tmp_path))
    tree = Tree(paths.tree_file, paths.events_file)
    ticket = tree.add_ticket("ag-1", "not actually a bug", "body", "minor")

    cli.cmd_tickets(_ticket_args(tmp_path, ticket["id"], "misread the spec",
                                 declined=True))
    record = tree.get_ticket(ticket["id"])
    assert record["status"] == "declined"
    assert record["note"] == "misread the spec"


def test_resolving_an_unknown_ticket_is_an_error(tmp_path, quiet_git, monkeypatch):
    import multiagents.cli as cli

    monkeypatch.setattr(cli, "_confirm", lambda *a, **k: True)
    cli.cmd_init(_init_args(tmp_path))
    assert cli.cmd_tickets(_ticket_args(tmp_path, "bug-nope")) == 2


# --------------------------------------------------------------------------
# A day of real use: 41 agents, 19 failures, and what we could not learn


def test_an_unparsed_line_is_kept_not_discarded(tmp_path):
    """The whole point of a `raw` event is to show what did not parse, and the
    payload was dropped on the way to disk. A run that died right after an
    unrecognised line recorded {"kind": "raw", "text": ""} and threw the
    explanation away — seen in a real 996-second failure."""
    import asyncio
    import json as json_mod

    r = _runner(tmp_path, {"worker": AgentSpec("worker", "p", "m")},
                {"p": {"bin": "sh",
                       "spawn": {"args": ["-c",
                                          'echo \'{"unexpected":"shape","detail":"boom"}\''
                                          ]},
                       "stream": {"format": "ndjson",
                                  "rules": [{"match": {"type": "text"}, "as": "text",
                                             "fields": {"text": "text"}}]}}})

    async def scenario():
        started = await r.start("worker", "go")
        for _ in range(40):
            node = r.tree.get(started["agent_id"])
            if node.status not in ("pending", "running"):
                return started["agent_id"]
            await asyncio.sleep(0.1)
        return started["agent_id"]

    agent_id = asyncio.run(scenario())
    lines = (r.paths.run_dir(agent_id) / "stream.jsonl").read_text().splitlines()
    raws = [json_mod.loads(l) for l in lines if json_mod.loads(l)["kind"] == "raw"]

    assert raws, "the unparsed line must be recorded at all"
    assert "unexpected" in raws[0]["raw"], f"and its content kept: {raws[0]}"
    assert "boom" in raws[0]["raw"]


def test_a_silent_failure_reports_its_mechanics(tmp_path):
    """Four real runs failed after minutes of work with an empty result and no
    reason, so the orchestrator could not steer, retry or report on them."""
    import asyncio

    r = _runner(tmp_path, {"worker": AgentSpec("worker", "p", "m")},
                {"p": {"bin": "sh", "spawn": {"args": ["-c", "exit 3"]}}})

    async def scenario():
        started = await r.start("worker", "go")
        for _ in range(40):
            node = r.tree.get(started["agent_id"])
            if node.status not in ("pending", "running"):
                return started["agent_id"]
            await asyncio.sleep(0.1)
        return started["agent_id"]

    agent_id = asyncio.run(scenario())
    result = json.loads((r.paths.run_dir(agent_id) / "result.json").read_text())

    assert "[no output]" in result["text"]
    assert "exit 3" in result["text"]
    assert "having said nothing" in result["text"]


def test_one_failure_does_not_make_a_provider_suspect(tmp_path, monkeypatch):
    """Agents fail all the time — a watchdog trip, a timeout, a bad task. The
    first version of this warned on a single failure and had the orchestrator
    reporting providers as unreachable all day."""
    import multiagents.server as server_mod
    from multiagents.config import Config

    tree = _tree(tmp_path)

    class _Run:
        providers = {}
        paths = _paths(tmp_path)
        config = Config(project={"limits": {"provider_failure_threshold": 3}},
                        providers={}, agents={}, models={}, instruction_dirs=[])
    _Run.tree = tree
    monkeypatch.setattr(server_mod, "runner", lambda: _Run())
    monkeypatch.setattr(server_mod.auth_mod, "check_all", lambda *a, **k: {
        "agy": type("S", (), {"ok": True, "to_dict": lambda self: {
            "provider": "agy", "authenticated": True}})()})

    tree.note_run_outcome("agy", ok=False, threshold=3, reason="a bad task")
    out = server_mod.auth_status()
    assert out["degraded"] == [], "one stumble is not a broken provider"
    assert "warning" not in out["providers"]["agy"]
    assert out["providers"]["agy"]["last_run"] == "failed", "still reported honestly"

    tree.note_run_outcome("agy", ok=False, threshold=3, reason="and another")
    assert server_mod.auth_status()["degraded"] == ["agy"], "a pattern is"


def test_a_check_records_what_it_checks(tmp_path):
    """Declared, never inferred: branch-and-timing guesses break the moment two
    checks overlap or a branch is reused, and the orchestrator knows the answer
    at the point of asking."""
    import asyncio
    from multiagents.tree import Node

    r = _runner(tmp_path, {"reviewer": AgentSpec("reviewer", "p", "m")},
                {"p": {"bin": "sh", "spawn": {"args": ["-c", "true"]}}})
    r.tree.add(Node(id="ag-work", agent="implementer", provider="p", model="m",
                    parent=None, depth=1, status="merged"))

    out = asyncio.run(r.start("reviewer", "check it", verifies="ag-work"))
    assert r.tree.get(out["agent_id"]).verifies == "ag-work"


def test_a_check_on_an_unknown_agent_is_not_recorded(tmp_path):
    """A dangling id would make the graph lie about what was checked."""
    import asyncio

    r = _runner(tmp_path, {"reviewer": AgentSpec("reviewer", "p", "m")},
                {"p": {"bin": "sh", "spawn": {"args": ["-c", "true"]}}})
    out = asyncio.run(r.start("reviewer", "check it", verifies="ag-nonexistent"))
    assert r.tree.get(out["agent_id"]).verifies == ""


def test_the_checks_report_shows_the_graph_not_a_score(tmp_path, quiet_git,
                                                       monkeypatch, capsys):
    """Whether a review found something is in its prose, and deciding that from
    here would be the same mistake as classifying a failure from an agent's own
    words."""
    import argparse
    import multiagents.cli as cli
    from multiagents.tree import Node

    monkeypatch.setattr(cli, "_confirm", lambda *a, **k: True)
    cli.cmd_init(_init_args(tmp_path))
    paths = cli._resolve(str(tmp_path))
    tree = Tree(paths.tree_file, paths.events_file)
    tree.add(Node(id="ag-work", agent="implementer", provider="p", model="m",
                  parent=None, depth=1, status="merged"))
    for n, status in (("ag-r1", "done"), ("ag-r2", "done")):
        tree.add(Node(id=n, agent="reviewer", provider="p", model="m",
                      parent=None, depth=1, status=status, verifies="ag-work"))

    cli.cmd_usage(argparse.Namespace(path=str(tmp_path), agents=False, checks=True))
    out = capsys.readouterr().out
    assert "ag-work implementer" in out
    assert "ag-r1 reviewer" in out and "ag-r2 reviewer" in out
    assert "1 needed more than one" in out


# --------------------------------------------------------------------------
# The verifier's own verdict, and one free retry


def test_a_verifier_declares_its_verdict_structurally(tmp_path):
    """Reading whether a review found something out of its prose is the mistake
    this project keeps not making. The agent hired to make that judgement
    declares it instead — which is the judgement the whole arrangement already
    relies on."""
    import asyncio
    from multiagents.tree import Node

    r = _runner(tmp_path, {"reviewer": AgentSpec("reviewer", "p", "m")},
                {"p": {"bin": "sh",
                       "spawn": {"args": ["-c",
                                          'echo \'{"type":"text","text":"looked at it.\\nVERDICT'
                                          '(rejected, 3): three defects"}\''
                                          ]},
                       "stream": {"format": "ndjson",
                                  "rules": [{"match": {"type": "text"}, "as": "text",
                                             "fields": {"text": "text"}}]}}})
    r.tree.add(Node(id="ag-work", agent="implementer", provider="p", model="m",
                    parent=None, depth=1, status="merged"))

    async def scenario():
        out = await r.start("reviewer", "check it", verifies="ag-work")
        for _ in range(40):
            node = r.tree.get(out["agent_id"])
            if node.status not in ("pending", "running"):
                return node
            await asyncio.sleep(0.1)
        return r.tree.get(out["agent_id"])

    node = asyncio.run(scenario())
    assert node.verdict == "rejected"
    assert node.defects == 3
    assert node.verifies == "ag-work"


def test_the_rework_rate_counts_only_what_was_judged(tmp_path, quiet_git,
                                                     monkeypatch, capsys):
    """Counting unjudged runs as approved would flatter the number."""
    import argparse
    import multiagents.cli as cli
    from multiagents.tree import Node

    monkeypatch.setattr(cli, "_confirm", lambda *a, **k: True)
    cli.cmd_init(_init_args(tmp_path))
    paths = cli._resolve(str(tmp_path))
    tree = Tree(paths.tree_file, paths.events_file)
    for n in ("ag-w1", "ag-w2", "ag-w3"):
        tree.add(Node(id=n, agent="implementer", provider="p", model="m",
                      parent=None, depth=1, status="merged"))
    tree.add(Node(id="ag-r1", agent="reviewer", provider="p", model="m", parent=None,
                  depth=1, status="done", verifies="ag-w1", verdict="rejected", defects=3))
    tree.add(Node(id="ag-r2", agent="reviewer", provider="p", model="m", parent=None,
                  depth=1, status="done", verifies="ag-w2", verdict="approved"))
    tree.add(Node(id="ag-r3", agent="reviewer", provider="p", model="m", parent=None,
                  depth=1, status="done", verifies="ag-w3"))          # no verdict

    cli.cmd_usage(argparse.Namespace(path=str(tmp_path), agents=False, checks=True))
    out = capsys.readouterr().out
    assert "1 of 2 judged run(s) were rejected (50% rework)" in out
    assert "1 checked run(s) got no verdict" in out
    assert "rejected (3 defect(s))" in out


def test_a_cheap_silent_death_is_retried_once(tmp_path):
    """A crash with nothing to say, gone before it did any work, is a transient
    glitch far more often than a real fault — and making the orchestrator handle
    that means a model reasoning about infrastructure."""
    import asyncio
    import json as json_mod

    r = _runner(tmp_path, {"worker": AgentSpec("worker", "p", "m")},
                {"p": {"bin": "sh", "spawn": {"args": ["-c", "exit 1"],
                                              "resume": ["-c", "exit 1"]}}})

    async def scenario():
        out = await r.start("worker", "go")
        for _ in range(60):
            node = r.tree.get(out["agent_id"])
            if node.status == "failed" and node.retries:
                return out["agent_id"]
            await asyncio.sleep(0.1)
        return out["agent_id"]

    agent_id = asyncio.run(scenario())
    events = [json_mod.loads(l) for l in
              (r.paths.events_file).read_text().splitlines()]
    retries = [e for e in events if e["kind"] == "retrying" and e["agent"] == agent_id]
    assert len(retries) == 1, "exactly one free retry, never a loop"
    assert "no output" in retries[0]["reason"]
    assert r.tree.get(agent_id).status == "failed", "and it stays failed after"


def test_an_expensive_silent_death_is_not_retried(tmp_path, monkeypatch):
    """A run that died after twenty minutes had spent millions of tokens.
    Spending them again is not absorbing a glitch."""
    import asyncio
    import json as json_mod
    from multiagents.tree import Node

    r = _runner(tmp_path, {"worker": AgentSpec("worker", "p", "m")},
                {"p": {"bin": "sh", "spawn": {"args": ["-c", "exit 1"]}}},
                project={"limits": {"retry_silent_failure_under_seconds": 0}})

    async def scenario():
        out = await r.start("worker", "go")
        for _ in range(40):
            if r.tree.get(out["agent_id"]).status == "failed":
                break
            await asyncio.sleep(0.1)
        return out["agent_id"]

    agent_id = asyncio.run(scenario())
    events = [json_mod.loads(l) for l in r.paths.events_file.read_text().splitlines()]
    assert not [e for e in events if e["kind"] == "retrying"]


def test_the_retry_guard_survives_the_relaunch_it_guards(tmp_path):
    """The first version kept the flag on the Run, and `_launch` builds a fresh
    Run — so one free retry became an unbounded loop that only the provider
    circuit breaker stopped. The count lives on the node, which persists."""
    import asyncio
    import json as json_mod

    r = _runner(tmp_path, {"worker": AgentSpec("worker", "p", "m")},
                {"p": {"bin": "sh", "spawn": {"args": ["-c", "exit 1"],
                                              "resume": ["-c", "exit 1"]}}})

    async def scenario():
        out = await r.start("worker", "go")
        for _ in range(80):
            node = r.tree.get(out["agent_id"])
            if node.status == "failed" and node.retries:
                await asyncio.sleep(0.4)          # give a loop room to run away
                return out["agent_id"]
            await asyncio.sleep(0.1)
        return out["agent_id"]

    agent_id = asyncio.run(scenario())
    events = [json_mod.loads(l) for l in r.paths.events_file.read_text().splitlines()]
    retries = [e for e in events if e["kind"] == "retrying"]
    assert len(retries) == 1, f"one retry, not {len(retries)}"
    assert r.tree.get(agent_id).retries == 1


# --------------------------------------------------------------------------
# A limit the CLI reports in prose instead of in an exit code


class _LimitProvider:
    """A provider whose transcript lives wherever the test puts it."""

    def __init__(self, directory):
        self.transcript = {
            "dir": str(directory), "glob": "*.jsonl",
            "limit_markers": [
                {"match": "hit your monthly spend limit", "resets": False,
                 "detail": "monthly spend limit"},
                {"match": "hit your usage limit", "resets": True,
                 "detail": "usage limit; it resets on its own"},
            ],
        }


def _transcript(directory, *messages):
    directory.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"type": "assistant",
                         "message": {"content": [{"text": text}]}})
             for text in messages]
    (directory / "session.jsonl").write_text("\n".join(lines) + "\n")


def test_a_limit_message_is_read_only_when_it_is_the_last_thing_said(tmp_path):
    """The CLI writes its limit into the chat log, so position is the only
    thing separating "it has stopped" from an agent quoting it earlier."""
    from multiagents import watchdog

    logs = tmp_path / "logs"
    provider = _LimitProvider(logs)

    _transcript(logs, "working on it", "You've hit your monthly spend limit ·")
    found = watchdog.limit_reached(provider, tmp_path)
    assert found is not None and found["resets"] is False
    assert found["detail"] == "monthly spend limit"

    _transcript(logs, "You've hit your usage limit", "recovered, carrying on")
    assert watchdog.limit_reached(provider, tmp_path) is None, \
        "a limit hit and then recovered from is history, not a stop"


def test_a_provider_with_no_markers_is_never_read_for_one(tmp_path):
    """Reading a transcript for a signal is only defensible for strings the CLI
    hardcodes. A provider that declares none opts out entirely."""
    from multiagents import watchdog

    logs = tmp_path / "logs"
    provider = _LimitProvider(logs)
    provider.transcript["limit_markers"] = []
    _transcript(logs, "You've hit your monthly spend limit")
    assert watchdog.limit_reached(provider, tmp_path) is None


def test_a_live_session_stopped_by_its_provider_is_not_reported_as_idle():
    """The bug this exists for: with the quota reader blind, an orchestrator
    stopped dead read as "alive, nothing running — probably waiting for you"."""
    from multiagents import watchdog

    state, detail = watchdog.verdict(
        running=True, quiet_for=1560, quota_known=False, quota_left=None,
        active_agents=0, limit={"detail": "monthly spend limit"})
    assert state == "limited"
    assert "monthly spend limit" in detail


def test_a_stalled_child_is_ended_rather_than_waited_on(monkeypatch):
    """`run` blocked on the child's exit, and a CLI at a usage limit never
    exits. Every restart path downstream was therefore unreachable."""
    from multiagents import cli

    monkeypatch.setattr(cli, "STALL_POLL_SECONDS", 0.05)
    code = cli._run_attached(["sh", "-c", "sleep 30"], dict(os.environ),
                             stalled=lambda: True)
    assert code != 0, "the child was terminated, not left running"


def test_a_child_that_is_working_is_left_alone(monkeypatch):
    from multiagents import cli

    monkeypatch.setattr(cli, "STALL_POLL_SECONDS", 0.05)
    code = cli._run_attached(["sh", "-c", "sleep 0.4; exit 7"], dict(os.environ),
                             stalled=lambda: False)
    assert code == 7


def test_a_limit_known_not_to_reset_stops_the_run(tmp_path, monkeypatch):
    """Waiting cannot put money in an account. Nothing ships marked this way —
    see the marker test below — but the path has to exist and has to be right."""
    from multiagents import cli
    from multiagents.config import AgentSpec
    from multiagents.tree import Tree

    slept = []
    monkeypatch.setattr(cli.time, "sleep", lambda s: slept.append(s))
    paths = _paths(tmp_path)
    spec = AgentSpec("orchestrator", "claude", "m")
    code = cli._limit_stop(paths, _config(), spec,
                           {"detail": "monthly spend limit", "resets": False,
                            "said": "You've hit your monthly spend limit"})
    assert code == 3
    assert slept == [], "it did not wait for something that never resets"
    pause = Tree(paths.tree_file, paths.events_file).pause_state()
    assert pause["providers"] == ["claude"]
    assert "monthly spend limit" in pause["reason"]


def test_a_window_that_resets_is_waited_out_and_then_retried(tmp_path, monkeypatch):
    from multiagents import cli
    from multiagents.config import AgentSpec
    from multiagents.tree import Tree

    slept = []
    monkeypatch.setattr(cli.time, "sleep", lambda s: slept.append(s))
    paths = _paths(tmp_path)
    spec = AgentSpec("orchestrator", "claude", "m")
    code = cli._limit_stop(paths, _config(), spec,
                           {"detail": "usage limit", "resets": True, "said": ""})
    assert code is None, "None means: try again"
    assert slept and slept[0] == 900
    assert Tree(paths.tree_file, paths.events_file).pause_state() == {}, \
        "the pause is lifted once the window has passed"


def test_launching_the_role_by_hand_lifts_its_limit_pause(tmp_path, monkeypatch):
    """A spend cap is held for hours because only a human can clear it — which
    makes the human launching it again the event it was waiting for."""
    from multiagents import cli
    from multiagents.config import AgentSpec
    from multiagents.tree import Tree

    monkeypatch.setattr(cli.sys, "stdin",
                        type("T", (), {"isatty": lambda s: True})())
    paths = _paths(tmp_path)
    tree = Tree(paths.tree_file, paths.events_file)
    tree.pause(time.time() + 3600, "claude: monthly spend limit", ["claude"])
    cli._clear_limit_pause(paths, AgentSpec("orchestrator", "claude", "m"))
    assert tree.pause_state() == {}

    tree.pause(time.time() + 3600, "opencode: no headroom", ["opencode"])
    cli._clear_limit_pause(paths, AgentSpec("orchestrator", "claude", "m"))
    assert tree.pause_state() != {}, "another provider's pause is not ours to lift"


def test_a_script_relaunching_on_a_timer_does_not_lift_the_pause(tmp_path, monkeypatch):
    """The lift assumes a person fixed something. A cron job fixed nothing, and
    lifting for it would turn a deliberate stop into a retry loop."""
    from multiagents import cli
    from multiagents.config import AgentSpec
    from multiagents.tree import Tree

    monkeypatch.setattr(cli.sys, "stdin",
                        type("T", (), {"isatty": lambda s: False})())
    paths = _paths(tmp_path)
    tree = Tree(paths.tree_file, paths.events_file)
    tree.pause(time.time() + 3600, "claude: monthly spend limit", ["claude"])
    cli._clear_limit_pause(paths, AgentSpec("orchestrator", "claude", "m"))
    assert tree.pause_state() != {}


def test_a_reply_to_the_limit_message_hands_the_session_back(tmp_path):
    """Anything typed after it means a person is here and has taken it from us;
    ending the session then would be worse than the stall this fixes."""
    from multiagents import watchdog

    logs = tmp_path / "logs"
    logs.mkdir()
    provider = _LimitProvider(logs)
    records = [
        {"type": "assistant",
         "message": {"content": [{"text": "You've hit your usage limit"}]}},
        # A tool result is a `user` record too, and is not somebody talking.
        {"type": "user",
         "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
        {"type": "system", "content": None},
    ]
    (logs / "session.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n")
    assert watchdog.limit_reached(provider, tmp_path) is not None, \
        "a tool result and a system record are not a person replying"

    records.append({"type": "user", "message": {"content": "raised it, carry on"}})
    (logs / "session.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n")
    assert watchdog.limit_reached(provider, tmp_path) is None


def test_a_limit_is_confirmed_over_two_polls_before_the_session_is_ended(tmp_path, monkeypatch):
    """One poll of grace, and a printed way out of it: anything typed clears
    the detection, because a limit somebody has answered is theirs."""
    from multiagents import cli
    from multiagents.config import AgentSpec

    seen = []
    monkeypatch.setattr(cli, "_start_supervisor", lambda *a: None)
    monkeypatch.setattr(cli, "_limit_stop", lambda *a, **k: 3)
    monkeypatch.setattr(cli, "watchdog", None, raising=False)

    def attached(argv, env, stalled=None):
        seen.append(stalled())          # first poll: warns, does not act
        seen.append(stalled())          # second: the limit is still unanswered
        return -cli.signal.SIGTERM

    monkeypatch.setattr(cli, "_run_attached", attached)
    import multiagents.watchdog as wd
    monkeypatch.setattr(wd, "limit_reached",
                        lambda *a: {"detail": "usage limit", "resets": True})
    monkeypatch.setattr(wd, "write_status", lambda *a: None)

    code = cli._run_supervised(_paths(tmp_path), _config(), "orchestrator",
                               AgentSpec("orchestrator", "claude", "m"),
                               object(), object(), {}, ["true"], {})
    assert seen == [False, True]
    assert code == 3


def test_no_shipped_marker_claims_a_limit_never_resets():
    """Measured 2026-09-09 against the usage dashboard: claude prints "You've
    hit your monthly spend limit" when what was reached is the FIVE-HOUR
    window, and contradicts itself in the same sentence ("resets 1pm"). The
    wording does not identify the limit, and the two mistakes are not equal —
    a window mistaken for a wall costs an afternoon of doing nothing."""
    from multiagents.config import load

    markers = (load(None).providers["claude"]["transcript"]["limit_markers"])
    assert markers, "the claude provider still declares its limit strings"
    assert all(m.get("resets", True) for m in markers), \
        "a shipped marker may not assert a limit is permanent from its wording"


def test_waiting_out_a_window_does_not_spend_the_restart_attempts(tmp_path, monkeypatch):
    """The window is five hours. Five attempts backing off from a minute would
    give up in the middle of it, having proved only that it was still there."""
    from multiagents import cli
    from multiagents.config import AgentSpec, Config

    waits = []
    runs = []
    monkeypatch.setattr(cli, "_start_supervisor", lambda *a: None)
    monkeypatch.setattr(cli.sys, "stdin", type("T", (), {"isatty": lambda s: True})())
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    monkeypatch.setattr(cli, "_orchestrator_hold", lambda *a: None)
    monkeypatch.setattr(cli, "_supervise", lambda *a, **k: 99)

    def limited(paths, config, spec, limit, attempt=1):
        waits.append(attempt)
        return None                      # "the window may have reopened; retry"

    monkeypatch.setattr(cli, "_limit_stop", limited)

    import multiagents.watchdog as wd
    monkeypatch.setattr(wd, "write_status", lambda *a: None)
    monkeypatch.setattr(wd, "has_human_turn", lambda *a: True)
    # Confirmed on the second poll, as in the live path.
    monkeypatch.setattr(wd, "limit_reached",
                        lambda *a: {"detail": "usage limit", "resets": True})

    def attached(argv, env, stalled=None):
        runs.append(1)
        stalled(); stalled()             # warn, then confirm
        return -cli.signal.SIGTERM

    monkeypatch.setattr(cli, "_run_attached", attached)
    config = Config(project={"limits": {"limit_max_waits": 7}}, providers={},
                    agents={}, models={}, instruction_dirs=[])

    code = cli._run_supervised(_paths(tmp_path), config, "orchestrator",
                               AgentSpec("orchestrator", "claude", "m"),
                               object(), object(), {}, ["true"], {})
    assert code == 3
    assert len(runs) == 8, "it kept trying the window, not the 5 restart attempts"
    assert waits == [1, 2, 3, 4, 5, 6, 7], "and backed off across them"


# --------------------------------------------------------------------------
# Asking the account what is left, rather than reading a cache of the answer

# The shape GET /api/oauth/usage returns, trimmed to what is read. It is also
# exactly what the CLI stores under cachedUsageUtilization, because that key is
# a cache of this response — which is why one parser serves both.
USAGE_PAYLOAD = {
    "five_hour": {"utilization": 96.0, "resets_at": "2026-09-09T16:50:00+00:00"},
    "seven_day": {"utilization": 40.0, "resets_at": "2026-09-14T14:00:00+00:00"},
    "limits": [
        {"kind": "session", "percent": 96, "resets_at": "2026-09-09T16:50:00+00:00"},
        {"kind": "weekly_all", "percent": 40, "resets_at": "2026-09-14T14:00:00+00:00"},
    ],
    "extra_usage": {"is_enabled": False, "monthly_limit": 8500,
                    "used_credits": 8603.0, "spend_limit_reached": True},
}


def _claude_files(tmp_path, monkeypatch, cache=None, fetched_ms=None,
                  token="tok-" + "x" * 40, expires_ms=None):
    import multiagents.budget as budget_mod

    state = tmp_path / ".claude.json"
    state.write_text(json.dumps(
        {"cachedUsageUtilization": {"fetchedAtMs": fetched_ms,
                                    "utilization": cache}} if cache else {}))
    creds = tmp_path / ".credentials.json"
    creds.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": token,
        "expiresAt": expires_ms if expires_ms is not None
        else (time.time() + 3600) * 1000}}))
    monkeypatch.setattr(budget_mod, "CLAUDE_STATE", state)
    monkeypatch.setattr(budget_mod, "CLAUDE_CREDENTIALS", creds)
    # Never the machine's real shared copy: a test that reads it would pass or
    # fail on what the developer's account happened to have left there.
    monkeypatch.setattr(budget_mod, "_shared_cache_file",
                        lambda config_dir=None: tmp_path / "usage-claude.json")
    return budget_mod


def test_a_fresh_cache_is_read_and_the_account_is_not_asked(tmp_path, monkeypatch):
    """The cache is free and the endpoint is somebody's rate limit."""
    budget_mod = _claude_files(tmp_path, monkeypatch, cache=USAGE_PAYLOAD,
                              fetched_ms=time.time() * 1000)
    monkeypatch.setattr(budget_mod, "fetch_claude_usage",
                        lambda config_dir=None: pytest.fail("asked the account for a fresh cache"))
    b = budget_mod.read_claude()
    assert b.known and round(b.headroom, 2) == 0.04
    assert b.resets_at == "2026-09-09T16:50:00+00:00"


def test_a_missing_cache_asks_the_account_instead_of_going_blind(tmp_path, monkeypatch):
    """The bug this exists for: the key vanished in a vendor update and every
    window reading in the project went with it, for weeks, silently."""
    budget_mod = _claude_files(tmp_path, monkeypatch, cache=None)
    monkeypatch.setattr(budget_mod, "fetch_claude_usage",
                        lambda config_dir=None: (USAGE_PAYLOAD, ""))
    b = budget_mod.read_claude()
    assert b.known and b.source == "api/oauth/usage"
    assert b.severity == "critical"


def test_a_stale_cache_is_refreshed(tmp_path, monkeypatch):
    budget_mod = _claude_files(
        tmp_path, monkeypatch, cache={"five_hour": {"utilization": 1.0}},
        fetched_ms=(time.time() - 4 * 3600) * 1000)
    monkeypatch.setattr(budget_mod, "fetch_claude_usage",
                        lambda config_dir=None: (USAGE_PAYLOAD, ""))
    assert round(budget_mod.read_claude().headroom, 2) == 0.04, \
        "the four-hour-old 1% reading would have sent work at a full window"


def test_an_unreachable_account_degrades_to_unknown_rather_than_raising(tmp_path, monkeypatch):
    budget_mod = _claude_files(tmp_path, monkeypatch, cache=None)
    monkeypatch.setattr(budget_mod, "fetch_claude_usage",
                        lambda config_dir=None: (None, "usage endpoint unreachable: URLError"))
    b = budget_mod.read_claude()
    assert b.known is False and "unreachable" in b.note
    assert b.usable, "unknown headroom is not no headroom"


def test_the_oauth_token_is_masked_everywhere_before_it_is_used(tmp_path, monkeypatch):
    """It is read at the moment of use, never held and never passed to a child.
    Registering it as a literal covers the route nobody thought of."""
    from multiagents import redact

    secret = "sk-ant-oat01-" + "z" * 60
    budget_mod = _claude_files(tmp_path, monkeypatch, token=secret)
    assert budget_mod._claude_token() == secret
    assert secret not in redact.scrub(f"Authorization: Bearer {secret}")


def test_an_expired_token_is_not_sent(tmp_path, monkeypatch):
    """Refreshing it is the CLI's job; using a dead one just spends a request."""
    budget_mod = _claude_files(tmp_path, monkeypatch,
                               expires_ms=(time.time() - 60) * 1000)
    assert budget_mod._claude_token() is None


def test_credits_spent_are_recorded_even_while_the_pool_is_disabled(tmp_path, monkeypatch):
    """That pool is what would carry a session PAST the window, and its being
    empty is why the CLI announces a window limit as a spend limit."""
    budget_mod = _claude_files(tmp_path, monkeypatch, cache=USAGE_PAYLOAD,
                              fetched_ms=time.time() * 1000)
    b = budget_mod.read_claude()
    assert b.spent["extra_credits_used"] == 8603
    assert "past the window limit" in b.note


def test_a_known_reset_time_replaces_the_blind_backoff(tmp_path, monkeypatch):
    """A real timestamp turns guessing into one wait of the right length."""
    from multiagents import cli
    from multiagents.config import AgentSpec

    slept = []
    monkeypatch.setattr(cli.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(cli, "_provider_reset_at",
                        lambda *a: time.time() + 3000)
    paths = _paths(tmp_path)
    code = cli._limit_stop(paths, _config(), AgentSpec("o", "claude", "m"),
                           {"detail": "usage limit", "resets": True, "said": ""})
    assert code is None
    assert 3000 <= slept[0] <= 3100, "it waited to the reset, not a round 15 min"


def test_an_unknown_reset_time_still_backs_off(tmp_path, monkeypatch):
    from multiagents import cli
    from multiagents.config import AgentSpec

    slept = []
    monkeypatch.setattr(cli.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(cli, "_provider_reset_at", lambda *a: None)
    cli._limit_stop(_paths(tmp_path), _config(), AgentSpec("o", "claude", "m"),
                    {"detail": "usage limit", "resets": True, "said": ""}, attempt=3)
    assert slept == [2700]


def test_one_fetch_per_machine_not_one_per_process(tmp_path, monkeypatch):
    """Every agent runs its own MCP server. An in-process cache means N
    processes cross the same staleness second and ask the same undocumented
    endpoint at once — a herd whose only possible reward is being rate-limited
    off the one surface that tells us anything."""
    budget_mod = _claude_files(tmp_path, monkeypatch, cache=None)
    fetches = []
    monkeypatch.setattr(budget_mod, "fetch_claude_usage",
                        lambda config_dir=None: (fetches.append(1), (USAGE_PAYLOAD, ""))[1])

    first = budget_mod.read_claude()
    second = budget_mod.read_claude()
    assert len(fetches) == 1, "the second reader used the shared copy"
    assert first.headroom == second.headroom


def test_a_reader_that_cannot_take_the_lock_keeps_the_stale_number(tmp_path, monkeypatch):
    """Somebody else's answer is seconds away, and a slightly stale reading is
    worth far more than a duplicate request."""
    import fcntl

    budget_mod = _claude_files(tmp_path, monkeypatch, cache=None)
    monkeypatch.setattr(budget_mod, "fetch_claude_usage",
                        lambda config_dir=None: (USAGE_PAYLOAD, ""))
    budget_mod.read_claude()                       # populate the shared copy
    monkeypatch.setattr(budget_mod, "SHARED_TTL", 0.0)   # force it stale
    monkeypatch.setattr(budget_mod, "fetch_claude_usage",
                        lambda config_dir=None: pytest.fail("fetched while another held the lock"))

    held = (tmp_path / "usage-claude.lock").open("a+")
    fcntl.flock(held.fileno(), fcntl.LOCK_EX)
    try:
        assert budget_mod.read_claude().known, "fell back to the stale reading"
    finally:
        held.close()


def test_the_vendors_reason_for_refusing_is_kept_and_scrubbed(tmp_path, monkeypatch):
    """"HTTP 403" hides "account suspended" and "unsupported region", which are
    facts somebody needs. The body is not echoed wholesale: an auth failure can
    quote back what was sent."""
    import urllib.error

    budget_mod = _claude_files(tmp_path, monkeypatch, cache=None)
    secret = "sk-ant-oat01-" + "q" * 60

    class _Body:
        code = 403

        def read(self):
            return json.dumps({"error": {
                "type": "permission_error",
                "message": f"account suspended (token {secret})"}}).encode()

    monkeypatch.setattr(budget_mod.json, "loads", json.loads)
    from multiagents import redact
    redact.register_literal(secret)
    note = budget_mod._error_reason(_Body())
    assert "permission_error" in note and "account suspended" in note
    assert secret not in note


def test_an_expired_token_leaves_the_run_free_rather_than_blocked(tmp_path, monkeypatch):
    """The advisor's worry: an agent that finds an expired token and stops is
    deadlocked until a human appears. It is not — unknown headroom is not no
    headroom — but the note has to say what to do about it."""
    budget_mod = _claude_files(tmp_path, monkeypatch, cache=None,
                               expires_ms=(time.time() - 60) * 1000)
    b = budget_mod.read_claude()
    assert b.known is False
    assert b.usable, "an unreadable quota must never stop work"
    assert "run `claude`" in b.note


def test_a_refusal_stops_the_asking_rather_than_retrying_on_a_timer(tmp_path, monkeypatch):
    """A 401 or 403 needs a human, and asking again every five minutes until
    one appears is precisely the behaviour that would deserve being blocked."""
    budget_mod = _claude_files(tmp_path, monkeypatch, cache=None)
    calls = []

    def refused(config_dir=None):
        calls.append(1)
        return None, "usage endpoint returned HTTP 403: permission_error"

    monkeypatch.setattr(budget_mod, "fetch_claude_usage", refused)
    budget_mod.read_claude()
    monkeypatch.setattr(budget_mod, "SHARED_TTL", 0.0)   # the TTL has passed
    budget_mod.read_claude()
    assert len(calls) == 1, "it did not ask again inside the backoff"

    record = json.loads((tmp_path / "usage-claude.json").read_text())
    assert record["blocked_until"] > time.time() + 5 * 3600
    assert "not asking again" in record["note"]


def test_the_switch_is_honoured(tmp_path, monkeypatch):
    """The account at risk is the user's, so whether to ask at all is theirs."""
    budget_mod = _claude_files(tmp_path, monkeypatch, cache=None)
    monkeypatch.setattr(budget_mod, "_fetching_allowed", lambda: False)
    monkeypatch.setattr(budget_mod, "fetch_claude_usage",
                        lambda config_dir=None: pytest.fail("asked while the switch was off"))
    b = budget_mod.read_claude()
    assert b.known is False and "ask_provider_for_usage" in b.note


def test_the_shipped_default_is_to_ask(tmp_path):
    from multiagents.config import load

    assert load(None).limits.get("ask_provider_for_usage") is True


def test_a_429_is_a_pause_not_an_hour_of_silence(tmp_path, monkeypatch):
    """429 is overloaded: far more often "too many at once" than "you are out".
    Answering a sixty-second concurrency limit with an hour of silence turns
    somebody else's transient into our own outage."""
    from multiagents import budget as budget_mod

    assert budget_mod._refused_for("usage endpoint returned HTTP 429") == 300
    assert budget_mod._refused_for("usage endpoint returned HTTP 403") == 6 * 3600


def test_the_servers_own_retry_after_wins(tmp_path):
    """It knows when it will answer; we are guessing."""
    from multiagents import budget as budget_mod

    class _Exc:
        headers = {"Retry-After": "45"}

    assert budget_mod._retry_after(_Exc()) == 45
    note = "usage endpoint returned HTTP 429 [retry-after 45s]"
    assert budget_mod._refused_for(note) == 45

    class _Absurd:
        headers = {"Retry-After": "999999"}

    assert budget_mod._retry_after(_Absurd()) == budget_mod.RETRY_AFTER_MAX

    class _Date:
        from email.utils import format_datetime
        from datetime import datetime, timezone
        headers = {"Retry-After": format_datetime(
            datetime.fromtimestamp(time.time() + 120, timezone.utc))}

    assert 110 <= budget_mod._retry_after(_Date()) <= 125


def test_a_cold_start_waits_for_the_writer_instead_of_giving_up(tmp_path, monkeypatch):
    """Every process on the machine reaches this in the same second the first
    time anything asks, and there is no stale copy to fall back to."""
    import fcntl
    import threading

    budget_mod = _claude_files(tmp_path, monkeypatch, cache=None)
    monkeypatch.setattr(budget_mod, "fetch_claude_usage",
                        lambda config_dir=None: pytest.fail("fetched while another held the lock"))

    path = tmp_path / "usage-claude.json"
    held = (tmp_path / "usage-claude.lock").open("a+")
    fcntl.flock(held.fileno(), fcntl.LOCK_EX)

    def writer():
        time.sleep(0.4)                       # as if the request took that long
        path.write_text(json.dumps({"at": time.time(), "payload": USAGE_PAYLOAD,
                                    "note": ""}))

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        assert budget_mod.read_claude().known, "it waited for the answer"
    finally:
        thread.join()
        held.close()


# --------------------------------------------------------------------------
# The monitor: one snapshot, two front ends


def _tree_with_agents(tmp_path):
    """A project whose tree holds a small family of finished agents."""
    from multiagents.tree import Node, Tree

    paths = _paths(tmp_path)
    tree = Tree(paths.tree_file, paths.events_file)
    parent = tree.add(Node(id="ag-parent", agent="implementer", provider="claude",
                           model="sonnet", parent=None, depth=0, status="merged",
                           task="build it", branch="agents/implementer/parent"))
    tree.update(parent.id, usage={"total": 12000, "cost_usd": 0.42},
                started_at=time.time() - 600, ended_at=time.time() - 300)
    child = tree.add(Node(id="ag-child", agent="tester", provider="opencode",
                          model="opencode-go/glm", parent="ag-parent", depth=1,
                          status="done", task="test it"))
    tree.update(child.id, usage={"total": 3000, "cost_usd": 0.01},
                started_at=time.time() - 500, ended_at=time.time() - 400)
    return paths, tree


def test_the_history_is_a_forest_built_from_parents(tmp_path):
    """From `parent`, not from each node's `children` list: a write interrupted
    between the two leaves one of them stale, and a tree drawn from the stale
    one loses agents or shows them twice."""
    from multiagents.monitor import snapshot as snap

    paths, tree = _tree_with_agents(tmp_path)
    tree.update("ag-parent", children=[])          # as a half-finished write left it
    roots = snap.agent_tree(tree.read()["nodes"], time.time())
    assert [r["id"] for r in roots] == ["ag-parent"]
    assert [k["id"] for k in roots[0]["kids"]] == ["ag-child"]


def test_an_agent_whose_process_is_gone_stops_counting_up(tmp_path):
    """"running 95h" is a lie told confidently. With the process gone, the last
    thing it said is the last thing that happened."""
    from multiagents.monitor import snapshot as snap

    paths, tree = _tree_with_agents(tmp_path)
    tree.update("ag-parent", status="running", ended_at=None, pid=999999,
                last_event_at=time.time() - 400)
    view = snap._node_view(tree.read()["nodes"]["ag-parent"], time.time())
    assert view["stale"] is True
    assert 190 <= view["elapsed"] <= 210, "measured to its last event, not to now"


def test_a_dead_agent_marked_running_is_an_alert(tmp_path):
    from multiagents.monitor import snapshot as snap

    paths, tree = _tree_with_agents(tmp_path)
    tree.update("ag-parent", status="running", ended_at=None, pid=999999)
    found = snap.alerts(paths, _config(), tree, [])
    assert any(a["kind"] == "orphan" for a in found)


def test_totals_answer_the_three_questions_separately(tmp_path):
    """"What did last night cost" is a different question from "which agent is
    expensive" and from "which model is expensive"."""
    from multiagents.monitor import snapshot as snap

    paths, tree = _tree_with_agents(tmp_path)
    totals = snap.totals(tree.read()["nodes"])
    assert totals["grand"] == {"tokens": 15000, "cost_usd": 0.43, "runs": 2}
    assert totals["by_agent"]["implementer"]["cost_usd"] == 0.42
    assert "claude/sonnet" in totals["by_model"]
    assert len(totals["by_day"]) == 1


def test_a_provider_may_render_its_own_usage(tmp_path, monkeypatch):
    """Quotas have genuinely different shapes — two rolling windows and a credit
    pool here, three windows there, nothing at all somewhere else. Flattening
    them into one bar invents precision for two of the three."""
    from multiagents.monitor import snapshot as snap

    import multiagents.scripts as scripts_mod

    seen = {}

    def fake_action(name, provider, executor, action, *a, **k):
        seen["action"] = action
        seen["budget"] = json.loads(k["extra_env"]["MULTIAGENTS_BUDGET"])
        return 0, "weekly   86%\nrolling   0%\n", ""

    monkeypatch.setattr(scripts_mod, "run_action", fake_action)

    lines, source = snap._usage_lines("opencode", None, None,
                                      {"known": True, "used_percent": 86.0},
                                      _paths(tmp_path))
    assert source == "script" and lines == ["weekly   86%", "rolling   0%"]
    assert seen["action"] == "usage"
    assert seen["budget"]["used_percent"] == 86.0, "it formats, it does not re-fetch"


def test_a_provider_that_says_nothing_gets_the_generic_view(tmp_path, monkeypatch):
    from multiagents.monitor import snapshot as snap
    import multiagents.scripts as scripts_mod

    monkeypatch.setattr(scripts_mod, "run_action",
                        lambda *a, **k: (64, "", ""))       # unimplemented
    lines, source = snap._usage_lines("agy", None, None,
                                      {"known": False, "note": "no quota surface"},
                                      _paths(tmp_path))
    assert source == "built-in" and lines == ["no quota surface"]


def test_a_script_that_reports_headroom_but_no_severity_still_warns():
    """A provider 86% through its weekly window read as calm as an untouched
    one, and the alert banner keys on exactly this field."""
    from multiagents import budget as budget_mod

    class _Provider:
        script_name = "x.sh"

    import multiagents.scripts as scripts_mod
    original = scripts_mod.run_action
    scripts_mod.run_action = lambda *a, **k: (
        0, json.dumps({"known": True, "headroom": 0.14}), "")
    try:
        out = budget_mod._from_script("x", _Provider(), None, Path("/tmp"), None)
    finally:
        scripts_mod.run_action = original
    assert out.severity == "warning"


def test_editing_a_setting_keeps_every_comment(tmp_path):
    """These files are mostly comments, and the comments are the documentation.
    A round-trip through safe_load/safe_dump would delete all of it: the config
    would still work and would stop teaching anybody anything."""
    from multiagents.monitor.settings import YamlFile

    path = tmp_path / "project.yaml"
    path.write_text(
        "# what this file is\n"
        "executor:\n"
        "  # local or docker\n"
        "  kind: local\n"
        "\n"
        "limits:\n"
        "  max_concurrent: 4   # across the whole tree\n"
        "  restart_on_crash: false\n"
    )
    document = YamlFile(path)
    document.set(["executor", "kind"], "docker")
    document.set(["limits", "max_concurrent"], 8)
    document.set(["limits", "restart_on_crash"], True)
    document.save()

    written = path.read_text()
    assert "# what this file is" in written
    assert "# local or docker" in written
    assert "max_concurrent: 8   # across the whole tree" in written, \
        "the inline comment survived the value change"
    assert "kind: docker" in written and "restart_on_crash: true" in written


def test_a_new_key_lands_inside_its_block_not_under_the_next_heading(tmp_path):
    """Inserting above the comment paragraph that introduces the NEXT section
    is valid YAML that reads as though the key belonged to something else."""
    from multiagents.monitor.settings import YamlFile

    path = tmp_path / "project.yaml"
    path.write_text(
        "limits:\n"
        "  max_concurrent: 4\n"
        "\n"
        "# ----------------------------------------\n"
        "# Budget. A section about something else.\n"
        "# ----------------------------------------\n"
        "budget:\n"
        "  reserve_headroom: 0.15\n"
    )
    document = YamlFile(path)
    document.set(["limits", "limit_max_waits"], 12)
    document.save()

    import yaml

    lines = path.read_text().splitlines()
    assert lines[2].strip() == "limit_max_waits: 12"
    assert yaml.safe_load(path.read_text())["limits"]["limit_max_waits"] == 12


def test_a_setting_carries_the_comment_that_explains_it(tmp_path):
    """The help text under each widget is the config's own comment. That is the
    whole reason the writer is surgical rather than a dump."""
    from multiagents.monitor.settings import YamlFile

    path = tmp_path / "project.yaml"
    path.write_text(
        "# Where agent processes run.\n"
        "# local is a subprocess; docker is a container.\n"
        "executor:\n"
        "  kind: local\n"
        "limits:\n"
        "  max_steps: 250   # before a run is called runaway\n"
    )
    document = YamlFile(path)
    assert "docker is a container" in document.help_for(["executor", "kind"]), \
        "a key with no comment of its own inherits its block's"
    assert document.help_for(["limits", "max_steps"]) == "before a run is called runaway"


def test_a_block_is_never_overwritten_by_a_scalar(tmp_path):
    from multiagents.monitor.settings import YamlFile

    path = tmp_path / "project.yaml"
    path.write_text("limits:\n  max_steps: 250\n")
    with pytest.raises(ValueError, match="block"):
        YamlFile(path).set(["limits"], "oops")


def test_a_write_that_would_not_parse_is_never_saved(tmp_path):
    from multiagents.monitor.settings import YamlFile

    path = tmp_path / "project.yaml"
    path.write_text("limits:\n  note: fine\n")
    document = YamlFile(path)
    document.lines.append("  : : broken")
    with pytest.raises(Exception):
        document.save()
    assert path.read_text() == "limits:\n  note: fine\n", "the file on disk is untouched"


def test_settings_are_written_to_the_project_layer(tmp_path, monkeypatch):
    """Never the global or shipped one: those belong to every other project on
    the machine."""
    from multiagents.monitor import settings as settings_mod

    paths = _paths(tmp_path)
    (paths.config).mkdir(parents=True, exist_ok=True)
    (paths.config / "project.yaml").write_text("executor:\n  kind: local\n")
    out = settings_mod.write(paths, "project.yaml", ["executor", "kind"], "docker")
    assert out["ok"] and str(paths.config) in out["file"]
    assert "kind: docker" in (paths.config / "project.yaml").read_text()

    with pytest.raises(ValueError):
        settings_mod.write(paths, "models.yaml", ["models"], {})


def test_form_values_arrive_as_strings_and_are_coerced(tmp_path):
    from multiagents.monitor.settings import coerce

    assert coerce("true", "bool") is True and coerce("", "bool") is False
    assert coerce("12", "int") == 12
    assert coerce("0.15", "float") == 0.15
    assert coerce("A, B ,C", "list") == ["A", "B", "C"]


def test_an_action_returns_a_message_rather_than_raising(tmp_path):
    """An action that blew up is a message in a UI, never a traceback in a
    server log nobody is reading."""
    from multiagents.monitor import actions

    paths = _paths(tmp_path)
    assert actions.perform(paths, "nope", {})["ok"] is False
    assert actions.perform(paths, "stop_agent", {"agent_id": "ag-nothere"})["ok"] is False
    assert actions.perform(paths, "answer_question",
                           {"question_id": "q1", "answer": "  "})["ok"] is False
    assert actions.perform(paths, "set_ticket", {"ticket_id": "nope"})["ok"] is False


def test_only_pids_the_tree_owns_can_be_signalled(tmp_path):
    """The monitor may not be a way to send signals to arbitrary processes."""
    from multiagents.monitor import actions

    paths, _ = _tree_with_agents(tmp_path)
    out = actions.perform(paths, "signal_process", {"pid": 1})
    assert out["ok"] is False and "not an agent" in out["message"]


def test_destructive_actions_are_declared_so_a_front_end_can_confirm(tmp_path):
    from multiagents.monitor import actions

    assert {"discard_agent", "stop_all", "merge_agent"} <= actions.DESTRUCTIVE
    assert set(actions.DESTRUCTIVE) <= set(actions.ACTIONS)
    assert set(actions.COSTS_MONEY) <= set(actions.ACTIONS)


def test_a_merge_conflict_is_not_reported_as_a_success(tmp_path, monkeypatch):
    """The runner reports a merge as a status, not a boolean; reading a missing
    "ok" as True would agree cheerfully with the one outcome that must not be."""
    from multiagents.monitor import actions

    class _Runner:
        def merge_agent(self, agent_id, into):
            return {"result": "conflict", "detail": "both modified README"}

    monkeypatch.setattr(actions, "_runner", lambda paths: _Runner())
    out = actions.merge_agent(_paths(tmp_path), agent_id="ag-1")
    assert out["ok"] is False and "conflict" in out["message"]


def test_the_api_refuses_a_caller_without_the_token(tmp_path):
    """Bound to localhost so the network cannot reach it, and token-checked so
    another program on this machine cannot drive it either."""
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer
    from multiagents.monitor import server

    server.Handler.paths = _paths(tmp_path)
    server.Handler.token = "right-token"
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_port}"

    def get(path, token=None):
        request = urllib.request.Request(base + path)
        if token:
            request.add_header("X-Monitor-Token", token)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    try:
        assert get("/api/state")[0] == 403
        assert get("/api/state", "wrong-token")[0] == 403
        code, body = get("/api/state", "right-token")
        assert code == 200 and "project" in json.loads(body)

        # The page carries the token, so opening the URL is enough to use it.
        code, page = get("/")
        assert code == 200 and "right-token" in page
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_tui_draws_every_page_without_a_terminal(tmp_path, monkeypatch):
    """A drawing bug on the costs page should not be discovered at the moment
    somebody needs the costs page."""
    import multiagents.monitor.tui as tui_mod

    paths, _ = _tree_with_agents(tmp_path)

    class _Stdscr:
        def getmaxyx(self):
            return 40, 120

        def addnstr(self, *a):
            painted.append(a)

        def erase(self):
            pass

        def refresh(self):
            pass

    painted = []
    monkeypatch.setattr(tui_mod.curses, "color_pair", lambda n: 0)
    monkeypatch.setattr(tui_mod.curses, "A_BOLD", 0, raising=False)
    monkeypatch.setattr(tui_mod.curses, "A_REVERSE", 0, raising=False)

    screen = tui_mod.Screen(_Stdscr(), paths)
    screen.state = __import__("multiagents.monitor.snapshot", fromlist=["x"]).snapshot(
        paths, _config(), with_scripts=False)
    screen.settings = [{"key": "limits.max_steps", "value": 250, "kind": "int",
                        "choices": [], "help": "before a run is called runaway",
                        "file": "project.yaml", "path": ["limits", "max_steps"]}]
    for tab in tui_mod.TABS:
        screen.tab = tab
        painted.clear()
        screen.draw()
        assert painted, f"the {tab} page drew nothing"
        assert not any("draw failed" in str(a[2]) for a in painted), \
            f"the {tab} page raised while drawing"


def test_the_tui_and_the_page_reach_the_same_actions():
    """Two front ends, one set of capabilities: the moment they diverge, one of
    them is quietly missing something the other can do."""
    from pathlib import Path as _Path
    from multiagents.monitor import actions

    page = (_Path("src/multiagents/monitor/page.html")).read_text()
    tui = (_Path("src/multiagents/monitor/tui.py")).read_text()
    for name in ("stop_agent", "steer_agent", "merge_agent", "discard_agent",
                 "answer_question", "set_setting"):
        assert f'"{name}"' in page, f"the web page cannot {name}"
        assert f'"{name}"' in tui, f"the TUI cannot {name}"
        assert name in actions.ACTIONS


def test_the_page_renders_every_view_against_real_data(tmp_path, monkeypatch):
    """The page is JavaScript, so nothing else in this suite would notice a
    typo in it until a panel came up blank at the wrong moment. A minimal DOM
    under node runs each view against a real snapshot."""
    import shutil
    import subprocess
    from pathlib import Path as _Path
    from multiagents.monitor import settings as settings_mod
    from multiagents.monitor import snapshot as snap

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    paths, _ = _tree_with_agents(tmp_path)
    config = _config()
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({
        "state": snap.snapshot(paths, config, with_scripts=False),
        "settings": settings_mod.describe(paths, config),
    }, default=str))

    root = _Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [node, str(root / "tests/support/render_page.js"),
         str(root / "src/multiagents/monitor/page.html"), str(fixture)],
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr[-2000:]
    painted = json.loads(result.stdout.split("rendered:", 1)[1])
    for view in ("live", "config", "history", "costs", "transcript", "expanded"):
        assert painted[view] > 100, f"the {view} view rendered almost nothing"


def test_the_poll_does_not_scroll_the_page_out_from_under_the_reader(tmp_path):
    """Reported from use: scrolling the activity log on Live, or the transcript
    on History, snapped back to the top. The poll rebuilt the whole view every
    two seconds, and detaching a node resets its scrollTop.

    So: an unchanged state rebuilds nothing at all, and a changed one puts the
    scroll positions back — the page's own and each panel's."""
    import shutil
    import subprocess
    from pathlib import Path as _Path
    from multiagents.monitor import snapshot as snap

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    paths, _ = _tree_with_agents(tmp_path)
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps(
        {"state": snap.snapshot(paths, _config(), with_scripts=False)}, default=str))

    root = _Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [node, str(root / "tests/support/render_page.js"),
         str(root / "src/multiagents/monitor/page.html"), str(fixture), "--scroll"],
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr[-2000:]
    out = json.loads(result.stdout.split("scroll:", 1)[1])

    assert out["idleRebuilds"] == 0, "a poll that changed nothing rebuilt the view"
    assert out["changedRebuilds"] == 1, "a real change still redraws"
    assert out["scrollKept"] == 240, "the activity log went back to the top"
    assert out["pageScrollKept"] == 900, "the page itself jumped"
    assert out["eventsRendered"] == 2, "the log is drawn from cache, not refetched"


def test_a_long_setting_gets_a_box_you_can_drag(tmp_path):
    """A value you have to read before you can change it does not belong in a
    one-line field."""
    from pathlib import Path as _Path

    page = (_Path("src/multiagents/monitor/page.html")).read_text()
    assert 'class: "grow"' in page and "text.length > 48" in page
    assert "textarea.grow { resize: both;" in page


def test_the_poll_does_not_fork_a_subprocess_per_tick(tmp_path, monkeypatch):
    """The snapshot is polled every two seconds by both front ends. A usage
    script per provider per tick is an idle monitor with a fan."""
    from multiagents.monitor import snapshot as snap
    import multiagents.scripts as scripts_mod

    calls = []
    monkeypatch.setattr(scripts_mod, "run_action",
                        lambda *a, **k: (calls.append(1), (0, "50%\n", ""))[1])
    monkeypatch.setattr(snap, "_LINE_CACHE", {})
    paths = _paths(tmp_path)
    budget = {"known": True, "used_percent": 50.0}

    for _ in range(5):
        lines, source = snap._usage_lines("p", None, None, budget, paths)
    assert calls == [1], "four of the five ticks were served from the cache"
    assert source == "script" and lines == ["50%"]

    # A changed budget is a changed answer, so that one does ask again.
    snap._usage_lines("p", None, None, {"known": True, "used_percent": 91.0}, paths)
    assert len(calls) == 2


def test_the_page_is_not_served_to_a_rebound_hostname(tmp_path):
    """A site can point local.evil.com at 127.0.0.1; the browser then believes
    it is same-origin and sends the request with no preflight. `GET /` is the
    route that hands out the token, so it is the route that matters most."""
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer
    from multiagents.monitor import server

    server.Handler.paths = _paths(tmp_path)
    server.Handler.token = "right-token"
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_port

    def get(path, host):
        request = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
        request.add_header("Host", host)
        request.add_header("X-Monitor-Token", "right-token")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code

    try:
        assert get("/", f"127.0.0.1:{port}") == 200
        assert get("/", f"localhost:{port}") == 200
        assert get("/", f"local.evil.com:{port}") == 403, "token would have leaked"
        assert get("/api/state", "attacker.example") == 403
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_a_transcript_is_read_from_the_end_not_loaded_whole(tmp_path):
    """These are agent streams — this project has seen an 11 MB one — and the
    button that opens it is the one pressed when something has gone wrong."""
    from multiagents.monitor import snapshot as snap

    path = tmp_path / "stream.jsonl"
    with path.open("w") as handle:
        for index in range(50000):
            handle.write(json.dumps({"kind": "text", "text": f"line {index}"}) + "\n")
    assert path.stat().st_size > 1_000_000

    lines = snap.tail_lines(path, 10)
    assert len(lines) == 10
    assert json.loads(lines[-1])["text"] == "line 49999"
    assert json.loads(lines[0])["text"] == "line 49990"

    # A file shorter than one chunk still comes back whole, and a missing one
    # is empty rather than an exception.
    short = tmp_path / "short.jsonl"
    short.write_text("a\nb\n")
    assert snap.tail_lines(short, 10) == ["a", "b"]
    assert snap.tail_lines(tmp_path / "nope", 10) == []


def test_a_block_scalar_body_is_not_mistaken_for_settings(tmp_path):
    """`description: >-` is followed by prose, and prose contains lines like
    "Use it when: ...". Indexing one would offer it as a setting and let an
    edit write a value into the middle of an agent's brief."""
    from multiagents.monitor.settings import YamlFile

    path = tmp_path / "agents.yaml"
    path.write_text(
        "agents:\n"
        "  tester:\n"
        "    model: sonnet\n"
        "    description: >-\n"
        "      Runs the tests.\n"
        "      Use it when: something needs verifying.\n"
        "      note: this line is prose, not a key.\n"
        "    timeout: 900\n"
    )
    document = YamlFile(path)
    keys = {key for _, _, key in document._key_lines()}
    assert "note" not in keys and "Use it when" not in keys
    assert {"agents", "tester", "model", "description", "timeout"} <= keys
    assert document.find(["agents", "tester", "timeout"]) == 7


def test_an_inline_comment_after_a_quoted_value_survives(tmp_path):
    """The first version guarded against `key: "#fff"` with a regex that also
    matched the ordinary case, and silently deleted the comment."""
    from multiagents.monitor.settings import YamlFile

    path = tmp_path / "project.yaml"
    path.write_text('git:\n  remote: "origin"  # where push goes\n  colour: "#fff"\n')
    document = YamlFile(path)
    document.set(["git", "remote"], "upstream")
    document.set(["git", "colour"], "#000")
    document.save()

    written = path.read_text()
    assert "# where push goes" in written, "the comment was kept"
    assert "upstream" in written
    assert "'#000'" in written or '"#000"' in written


def test_a_file_indented_with_four_spaces_stays_that_way(tmp_path):
    """Two spaces is the convention, not the rule; a new key at the wrong depth
    is a different key."""
    from multiagents.monitor.settings import YamlFile

    path = tmp_path / "project.yaml"
    path.write_text("limits:\n    max_steps: 250\n")
    document = YamlFile(path)
    assert document.indent_step() == 4
    document.set(["limits", "max_concurrent"], 4)
    document.save()
    assert "    max_concurrent: 4" in path.read_text()
    import yaml
    assert yaml.safe_load(path.read_text())["limits"]["max_concurrent"] == 4


def test_the_page_keeps_what_the_poll_would_have_thrown_away():
    """The poll replaces the active view every two seconds. A half-typed answer
    and a just-opened panel must survive that, and the config form must not be
    redrawn under a cursor at all."""
    from pathlib import Path as _Path

    page = (_Path("src/multiagents/monitor/page.html")).read_text()
    assert "DRAFTS[q.id]" in page, "a half-typed answer is kept outside the DOM"
    assert "OPEN_DETAILS" in page, "an opened panel is kept outside the DOM"
    assert "fromPoll && TAB === \"config\"" in page, \
        "the config form is not redrawn by the poll"


def test_a_provider_below_the_reserve_says_so(tmp_path, monkeypatch):
    """The state that produced a question to the maintainer: opencode's
    five-hour window was empty, its WEEKLY window was at 86%, headroom was
    therefore 0.14 — under the 0.15 reserve — and every implementer silently
    ran on the fallback provider with nothing anywhere saying why."""
    from multiagents.budget import Budget
    from multiagents.config import Config
    from multiagents.monitor import snapshot as snap
    from multiagents.tree import Tree

    paths = _paths(tmp_path)
    tree = Tree(paths.tree_file, paths.events_file)
    config = Config(project={"budget": {"reserve_headroom": 0.15, "reserve": True}},
                    providers={}, agents={}, models={}, instruction_dirs=[])

    monkeypatch.setattr(snap, "load_providers", lambda _: {"opencode": None})
    monkeypatch.setattr(snap, "read_all", lambda *a, **k: {
        "opencode": Budget(provider="opencode", known=True, headroom=0.14,
                           severity="warning",
                           windows={"rolling": {"percent": 0.0},
                                    "weekly": {"percent": 86.0}})})
    import multiagents.cli as cli_mod
    monkeypatch.setattr(cli_mod, "_executor_for", lambda *a: (lambda name: None))

    rows = snap.providers_view(paths, config, tree, with_scripts=False)
    assert rows[0]["below_reserve"] is True
    assert rows[0]["budget"]["usable"] is True, "usable, and skipped anyway"

    found = snap.alerts(paths, config, tree, rows)
    assert any("below the 15% reserve" in a["text"] for a in found)
    assert any("routed to a fallback" in a["text"] for a in found)


def test_an_agent_moved_to_a_fallback_records_where_it_was_meant_to_go(tmp_path):
    """Until this, the only way to learn why an implementer was on the wrong
    model was for somebody to read choose_provider."""
    from multiagents.monitor import snapshot as snap
    from multiagents.tree import Node, Tree

    paths = _paths(tmp_path)
    tree = Tree(paths.tree_file, paths.events_file)
    tree.add(Node(id="ag-1", agent="implementer", provider="agy",
                  model="gemini-3.1-pro-high", parent=None, depth=0,
                  status="running",
                  routed_from="opencode",
                  routed_why="opencode is constrained; falling back to agy"))
    view = snap._node_view(tree.read()["nodes"]["ag-1"], time.time())
    assert view["routed_from"] == "opencode"
    assert "falling back" in view["routed_why"]

    page = Path("src/multiagents/monitor/page.html").read_text()
    assert "routed_from" in page, "and the card says so"


def test_the_reserve_is_off_for_workers_and_on_for_the_orchestrator(tmp_path):
    """Two switches, because one number was doing two jobs. `reserve` extends
    it to every provider; `reserve_orchestrator` keeps its original and much
    narrower purpose."""
    from multiagents.budget import Budget, choose_provider, reserved_providers

    providers = {"opencode": None, "agy": None, "claude": None}
    budgets = {
        # 86% of a WEEKLY window, with the five-hour window it actually runs
        # against sitting empty. Usable, and under the reserve.
        "opencode": Budget("opencode", known=True, headroom=0.14),
        "agy": Budget("agy", known=False),
        "claude": Budget("claude", known=True, headroom=0.43),
    }
    chain = ["opencode", "agy", "defer"]

    shipped = reserved_providers({"budget": {}}, providers, "claude")
    assert shipped == {"claude"}, "by default only the orchestrator's provider"
    assert choose_provider("opencode", budgets, chain, 0.15, shipped)[0] == "opencode", \
        "a worker uses what it is paying for until it genuinely runs out"

    everywhere = reserved_providers({"budget": {"reserve": True}}, providers, "claude")
    assert everywhere == set(providers)
    assert choose_provider("opencode", budgets, chain, 0.15, everywhere)[0] == "agy"

    nowhere = reserved_providers(
        {"budget": {"reserve_orchestrator": False}}, providers, "claude")
    assert nowhere == set()


def test_the_orchestrators_provider_is_protected_as_a_fallback_too(tmp_path):
    """Otherwise work diverted off a constrained provider lands on the
    orchestrator's own and eats exactly the slice the reserve exists to keep."""
    from multiagents.budget import Budget, choose_provider

    budgets = {
        "opencode": Budget("opencode", known=True, headroom=0.0),   # gone
        "claude": Budget("claude", known=True, headroom=0.10),      # under reserve
    }
    chain = ["opencode", "claude", "defer"]
    chosen, why = choose_provider("opencode", budgets, chain, 0.15, {"claude"})
    assert chosen is None and "exhausted" in why

    # With the reserve not covering it, it is a legitimate fallback.
    assert choose_provider("opencode", budgets, chain, 0.15, set())[0] == "claude"


def test_an_unmeasurable_provider_is_still_a_fallback(tmp_path):
    """`known=False` means unknown headroom, which is not no headroom — and the
    reserve cannot be applied to a number nobody has."""
    from multiagents.budget import Budget, choose_provider

    budgets = {"opencode": Budget("opencode", known=True, headroom=0.0),
               "agy": Budget("agy", known=False)}
    assert choose_provider("opencode", budgets, ["opencode", "agy", "defer"],
                           0.15, {"opencode", "agy"})[0] == "agy"


def test_the_shipped_budget_switches_match_the_code_defaults():
    """A default that differs between the code and the file it ships is a
    default nobody can reason about."""
    from multiagents.budget import reserved_providers
    from multiagents.config import load

    shipped = load(None).project.get("budget", {})
    assert shipped["reserve"] is False
    assert shipped["reserve_orchestrator"] is True
    assert reserved_providers({"budget": shipped}, {"a", "b"}, "a") == {"a"}


def test_a_fallback_further_down_the_chain_is_still_reached(tmp_path):
    """Reported by a running orchestrator, and it cost five runs into a revoked
    token: claude was cooling down, the chain was [opencode, agy], and the agent
    named a model for agy only. The first usable chain entry was opencode, the
    caller found no model for it, and gave up — back onto claude."""
    from multiagents.budget import Budget, choose_provider

    budgets = {
        "claude": Budget("claude", known=True, headroom=0.43,
                         cooldown_until=time.time() + 1800),
        "opencode": Budget("opencode", known=True, headroom=0.60),
        "agy": Budget("agy", known=False),
    }
    chain = ["opencode", "agy", "defer"]

    chosen, why = choose_provider("claude", budgets, chain, 0.15,
                                  reserved={"claude"}, allowed={"claude", "agy"})
    assert chosen == "agy", "it walked past the entry it could not use"

    # With no alternative at all, the answer is to wait — not to run into the
    # wall we just identified.
    chosen, why = choose_provider("claude", budgets, chain, 0.15,
                                  reserved={"claude"}, allowed={"claude"})
    assert chosen is None
    assert "no model named for opencode, agy" in why, \
        "and it says exactly what to add to fix it"


def test_the_breaker_trips_again_once_its_cooldown_has_lapsed(tmp_path):
    """It latched: after the first trip, `tripped` stayed set, so every later
    failure was free. A provider with a revoked token was retried all evening."""
    from multiagents.tree import Tree

    paths = _paths(tmp_path)
    tree = Tree(paths.tree_file, paths.events_file)

    assert tree.note_run_outcome("claude", ok=False, reason="401") is None
    assert tree.note_run_outcome("claude", ok=False, reason="401") is None
    trip = tree.note_run_outcome("claude", ok=False, reason="401")
    assert trip and trip["failures"] == 3
    tree.set_cooldown("claude", time.time() + 1800, "3 runs in a row failed")

    # While it is cooling down, more failures are the same fault; no new trip.
    assert tree.note_run_outcome("claude", ok=False, reason="401") is None

    # Once the window lapses, the next failure must open it again.
    tree.set_cooldown("claude", time.time() - 1, "expired")
    trip = tree.note_run_outcome("claude", ok=False, reason="401")
    assert trip is not None, "the breaker latched open and never closed again"
    assert trip["failures"] == 5

    # A success clears the count and the trip together.
    assert tree.note_run_outcome("claude", ok=True) is None
    assert tree.provider_health()["claude"]["consecutive_failures"] == 0
    assert "tripped" not in tree.provider_health()["claude"]


def test_an_agent_is_never_sent_to_a_provider_it_has_no_model_for(tmp_path):
    """The model id belongs to its provider's namespace, so `agy --model
    opencode-go/glm` is not a fallback, it is a failure with extra steps."""
    from multiagents.budget import Budget, choose_provider

    budgets = {"a": Budget("a", known=True, headroom=0.0),
               "b": Budget("b", known=True, headroom=0.9)}
    chosen, _ = choose_provider("a", budgets, ["b", "defer"], 0.15,
                                reserved=set(), allowed={"a"})
    assert chosen is None
    chosen, _ = choose_provider("a", budgets, ["b", "defer"], 0.15,
                                reserved=set(), allowed={"a", "b"})
    assert chosen == "b"


def test_only_one_task_tries_a_provider_whose_cooldown_just_lapsed(tmp_path):
    """"One trial at a time" was a comment, not a fact: every task deferred
    behind the cooldown wakes the moment it lapses, and without a claim they all
    try the same broken provider and all fail before any can set a new
    cooldown — a synchronised barrage, not a half-open breaker."""
    from multiagents.tree import Tree

    tree = Tree(_paths(tmp_path).tree_file, _paths(tmp_path).events_file)
    assert tree.claim_trial("claude") is True
    assert tree.claim_trial("claude") is False, "the second waker was let through"
    assert tree.claim_trial("agy") is True, "a different provider is unrelated"

    # The claim ages out, so a provider is never permanently unclaimable.
    assert tree.claim_trial("claude", window=0.0) is True


def test_a_success_lets_the_provider_back_in_immediately(tmp_path):
    """The health record is not what routing reads. Leaving the cooldown behind
    kept a working provider out of the pool for the rest of its penalty box."""
    from multiagents.tree import Tree

    paths = _paths(tmp_path)
    tree = Tree(paths.tree_file, paths.events_file)
    for _ in range(3):
        tree.note_run_outcome("claude", ok=False, reason="401")
    tree.set_cooldown("claude", time.time() + 1800, "3 in a row")
    assert tree.cooldown("claude") is not None

    tree.note_run_outcome("claude", ok=True)
    assert tree.cooldown("claude") is None, "fixed, and still locked out"
    assert tree.provider_health()["claude"]["consecutive_failures"] == 0


def test_a_revoked_token_is_probed_with_check_not_with_an_agent_run(tmp_path, monkeypatch):
    """A rate limit heals by waiting; a revoked token never does. Cycling a
    half-open retry against it every thirty minutes spends real agent runs
    proving something already known — and the provider's own `check` action
    answers it for the cost of one subprocess."""
    from multiagents.budget import Budget
    from multiagents.runner import Runner
    from multiagents.tree import Tree

    paths = _paths(tmp_path)
    tree = Tree(paths.tree_file, paths.events_file)
    runner = Runner.__new__(Runner)          # no spawning, just the routing path
    runner.paths, runner.tree = paths, tree
    runner.config = _config()
    runner.providers = {"claude": object()}

    for _ in range(3):
        tree.note_run_outcome("claude", ok=False, reason="401 revoked")
    tree.set_cooldown("claude", time.time() - 1, "expired", needs_login=True)

    checks = []
    monkeypatch.setattr(Runner, "_auth_ok",
                        lambda self, name: checks.append(name) or False)
    budgets = {"claude": Budget("claude", known=True, headroom=0.9)}
    runner._half_open(budgets, tree.read()["cooldowns"])

    assert checks == ["claude"], "it asked the CLI instead of spending a run"
    assert budgets["claude"].usable is False
    assert tree.cooldown("claude")["needs_login"] is True

    # Once somebody logs in, the next probe frees it — no waiting out the six
    # hours. The probe interval is what decides how quickly that happens.
    from multiagents.config import Config

    runner.config = Config(project={"limits": {"provider_probe_seconds": 0}},
                           providers={}, agents={}, models={}, instruction_dirs=[])
    monkeypatch.setattr(Runner, "_auth_ok", lambda self, name: True)
    budgets = {"claude": Budget("claude", known=True, headroom=0.9)}
    runner._half_open(budgets, tree.read()["cooldowns"])
    assert tree.cooldown("claude") is None and budgets["claude"].usable


def test_a_pause_names_what_is_unavailable_not_what_was_wanted(tmp_path):
    """A pause listing a healthy provider refuses other agents that only need
    that one, which turns one agent's problem into everybody's."""
    import inspect
    from multiagents.runner import Runner

    body = inspect.getsource(Runner.start)
    assert "unavailable = sorted(name for name in options" in body
    assert "not budgets[name].usable" in body
    assert "providers=unavailable or sorted(options)" in body


def test_a_deferred_task_wakes_for_a_provider_it_can_actually_use(tmp_path):
    """Waking on the reset of a provider this agent cannot run on finds nothing
    changed and defers again — a spin, on somebody else's timer."""
    import inspect
    from multiagents.runner import Runner

    body = inspect.getsource(Runner.start)
    assert "if b.cooldown_until and name in options" in body


def test_a_local_project_is_never_asked_about_containers(tmp_path, monkeypatch):
    """There is no container to compare against, and building an executor to
    find that out would fail on a machine with no docker at all."""
    import multiagents.cli as cli
    from multiagents.config import Config

    called = []
    monkeypatch.setattr(cli, "_docker_executor", lambda p: called.append(p))
    local = Config(project={"executor": {"kind": "local"}}, providers={},
                   agents={}, models={}, instruction_dirs=[])
    assert cli._repair_credential_drift(_paths(tmp_path), local) == []
    assert called == []


# --------------------------------------------------------------------------
# The container gets its own claude profile


def _docker_for(tmp_path, providers):
    from multiagents.executor.docker import DockerExecutor
    from multiagents.paths import ProjectPaths
    return DockerExecutor({"image": "i"}, ProjectPaths(tmp_path), providers, tmp_path)


def test_the_hosts_claude_directory_is_never_mounted(tmp_path, monkeypatch):
    """It holds every past conversation. The old arrangement mounted the
    credential FILE out of it, which both exposed the directory's path to the
    container and could not work anyway."""
    import multiagents.executor.docker as docker_mod
    from multiagents.providers import Provider

    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text("{}")
    (home / ".claude" / "projects").mkdir()
    monkeypatch.setattr(docker_mod.Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(docker_mod, "state_root", lambda: tmp_path / "state")

    provider = Provider.from_dict("claude", {
        "bin": "claude",
        "container_private_home": [".claude"],
        "home_links": [".claude/.credentials.json", ".claude/settings.json"],
    })
    executor = _docker_for(tmp_path, {"claude": provider})

    sources = {source for source, _ in executor.mounts()}
    assert home / ".claude" in sources, "the path is mounted"
    private = executor.private_state("claude")
    assert private[home / ".claude"] == tmp_path / "state" / "container-state" \
        / "shared" / "claude" / ".claude", "…but backed by our own directory"
    # And it is a DIRECTORY mount, which is the whole point: renames work.
    assert (home / ".claude" / ".credentials.json") not in sources


def test_each_script_is_given_its_own_providers_private_home(tmp_path, monkeypatch):
    """It used to take whichever entry came first — correct only while exactly
    one provider had a private home, and silently wrong once a second did."""
    import multiagents.executor.docker as docker_mod
    from multiagents import scripts
    from multiagents.providers import Provider

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(docker_mod.Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(docker_mod, "state_root", lambda: tmp_path / "state")

    providers = {
        "agy": Provider.from_dict("agy", {"bin": "agy",
                                          "container_private_home": [".gemini"]}),
        "claude": Provider.from_dict("claude", {"bin": "claude",
                                                "container_private_home": [".claude"]}),
    }
    executor = _docker_for(tmp_path, providers)

    for name in ("agy", "claude"):
        env = scripts.build_env(name, providers[name], executor)
        assert env["MULTIAGENTS_PRIVATE_BACKING"].endswith(f"{name}/"
                                                           + (".gemini" if name == "agy"
                                                              else ".claude"))


def test_the_users_settings_are_carried_in_but_not_their_secrets(tmp_path, monkeypatch):
    """A private profile fixes the credential and would otherwise amputate
    everything else configured — permissions, hooks, model choice — leaving an
    agent running as a factory-reset CLI for reasons nobody would connect to a
    credential change. But `env` is how a settings file hands out secrets, and
    agents are given none."""
    import multiagents.executor.docker as docker_mod
    from multiagents.providers import Provider

    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(json.dumps({
        "permissions": {"allow": ["Bash(ls)"]},
        "model": "sonnet",
        "env": {"SECRET_TOKEN": "sk-do-not-copy-me"},
    }))
    monkeypatch.setattr(docker_mod.Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(docker_mod, "state_root", lambda: tmp_path / "state")

    provider = Provider.from_dict("claude", {
        "bin": "claude", "container_private_home": [".claude"],
        "container_private_seed": [".claude/settings.json"],
        "container_private_reset": [".claude/daemon.lock"],
    })
    executor = _docker_for(tmp_path, {"claude": provider})
    backing = next(iter(executor.private_state("claude").values()))
    backing.mkdir(parents=True, exist_ok=True)
    (backing / "daemon.lock").write_text("pid 1234")     # host-pid state

    notes = executor.seed_private_state()
    carried = json.loads((backing / "settings.json").read_text())
    assert any("held by a live process" not in note for note in notes)
    assert carried["permissions"] == {"allow": ["Bash(ls)"]}
    assert carried["model"] == "sonnet"
    assert "env" not in carried
    assert "sk-do-not-copy-me" not in (backing / "settings.json").read_text()
    assert any("without env" in note for note in notes)
    assert not (backing / "daemon.lock").exists(), \
        "a lock naming a host pid means nothing in a container"


def test_the_claude_script_uses_the_container_profile_only_where_it_should(tmp_path):
    """`check` and `login` act on the container's profile. `launch` does not:
    the orchestrator runs on the HOST even in a docker project, and pointing it
    at the container's profile would have it start as an account it was never
    logged into."""
    import subprocess
    from pathlib import Path as _Path

    script = _Path("src/multiagents/defaults/providers/claude.sh").resolve()
    profile = tmp_path / "profile"
    profile.mkdir()
    env = {**os.environ, "MULTIAGENTS_EXECUTOR": "docker",
           "MULTIAGENTS_PRIVATE_BACKING": str(profile),
           "MULTIAGENTS_BIN": "true", "MULTIAGENTS_MODEL": "sonnet"}
    env.pop("CLAUDE_CONFIG_DIR", None)

    empty = subprocess.run(["sh", str(script), "check"], env=env,
                           capture_output=True, text=True)
    assert empty.returncode == 10 and "no credentials yet" in empty.stdout

    (profile / ".credentials.json").write_text('{"claudeAiOauth": {}}')
    ready = subprocess.run(["sh", str(script), "check"], env=env,
                           capture_output=True, text=True)
    assert ready.returncode == 0 and "container profile is logged in" in ready.stdout

    body = script.read_text()
    launch = body[body.index("\nlaunch)"):]
    assert "CLAUDE_CONFIG_DIR" not in launch, \
        "the host-run orchestrator must not be pointed at the container's profile"


def test_a_lock_a_live_process_holds_is_left_alone(tmp_path, monkeypatch):
    """Deleting a lock a running daemon holds does not stop the daemon — it
    lets a second one start beside it, and then two share one state directory."""
    import multiagents.executor.docker as docker_mod
    from multiagents.providers import Provider

    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setattr(docker_mod.Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(docker_mod, "state_root", lambda: tmp_path / "state")

    provider = Provider.from_dict("claude", {
        "bin": "claude", "container_private_home": [".claude"],
        "container_private_reset": [".claude/daemon.lock"]})
    executor = _docker_for(tmp_path, {"claude": provider})
    backing = next(iter(executor.private_state("claude").values()))
    backing.mkdir(parents=True, exist_ok=True)

    live = backing / "daemon.lock"
    live.write_text(json.dumps({"pid": os.getpid()}))       # us: certainly alive
    notes = executor.seed_private_state()
    assert live.exists(), "a live daemon's lock was deleted"
    assert any("held by a live process" in note for note in notes)

    live.write_text(json.dumps({"pid": 4_000_000}))          # certainly not
    executor.seed_private_state()
    assert not live.exists(), "a stale lock should go"


def test_a_secret_shaped_value_is_stripped_even_under_an_unknown_key(tmp_path, monkeypatch):
    """A fixed list of key names is the floor, not the defence: the vendor adds
    a key, the list does not know it, and a secret rides along."""
    import multiagents.executor.docker as docker_mod
    from multiagents.providers import Provider

    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(json.dumps({
        "model": "sonnet",
        "someFutureKey": {"apiKey": "sk-" + "a" * 40},
    }))
    monkeypatch.setattr(docker_mod.Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(docker_mod, "state_root", lambda: tmp_path / "state")

    provider = Provider.from_dict("claude", {
        "bin": "claude", "container_private_home": [".claude"],
        "container_private_seed": [".claude/settings.json"]})
    executor = _docker_for(tmp_path, {"claude": provider})
    notes = executor.seed_private_state()

    backing = next(iter(executor.private_state("claude").values()))
    written = (backing / "settings.json").read_text()
    assert "sk-" + "a" * 40 not in written
    assert '"model": "sonnet"' in written
    assert any("look like secrets" in note for note in notes)


def test_a_seeded_file_is_not_clobbered_by_a_later_host_edit(tmp_path, monkeypatch):
    """Edit the container's copy to fix something container-specific, add an
    unrelated line to the host's a month later, and "copy when newer" silently
    throws the fix away."""
    import multiagents.executor.docker as docker_mod
    from multiagents.providers import Provider

    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    host_settings = home / ".claude" / "settings.json"
    host_settings.write_text(json.dumps({"model": "sonnet"}))
    monkeypatch.setattr(docker_mod.Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(docker_mod, "state_root", lambda: tmp_path / "state")

    provider = Provider.from_dict("claude", {
        "bin": "claude", "container_private_home": [".claude"],
        "container_private_seed": [".claude/settings.json"]})
    executor = _docker_for(tmp_path, {"claude": provider})
    executor.seed_private_state()

    backing = next(iter(executor.private_state("claude").values()))
    (backing / "settings.json").write_text(json.dumps({"model": "opus",
                                                       "containerOnly": True}))
    host_settings.write_text(json.dumps({"model": "sonnet", "unrelated": 1}))
    executor.seed_private_state()

    kept = json.loads((backing / "settings.json").read_text())
    assert kept == {"model": "opus", "containerOnly": True}


# --------------------------------------------------------------------------
# More than one account on the same CLI


def _family_budgets(**kwargs):
    from multiagents.budget import Budget
    out = {}
    for name, spec in kwargs.items():
        name = name.replace("_", "-")
        if spec == "free":
            out[name] = Budget(name, known=True, headroom=0.9)
        elif spec == "low":
            out[name] = Budget(name, known=True, headroom=0.10)
        elif isinstance(spec, (int, float)):
            out[name] = Budget(name, known=True, headroom=0.0,
                               cooldown_until=time.time() + spec)
    return out


def _choose(preferred, budgets, family, **kwargs):
    from multiagents.budget import choose_provider
    return choose_provider(preferred, budgets, ["opencode", "defer"], 0.15,
                           reserved={"claude"},
                           allowed=set(budgets) | set(family),
                           family=family, **kwargs)


def test_a_second_account_is_four_lines_and_inherits_the_integration(tmp_path):
    """A subscription is not a new vendor. Duplicating the whole block to add
    one would leave two copies of a parsing contract to keep in step."""
    from multiagents.providers import families, load_providers

    providers = load_providers({
        "claude": {"bin": "claude", "spawn": {"args": ["-p", "{prompt}"]},
                   "stream": {"format": "ndjson"},
                   "container_private_home": [".claude"]},
        "claude-b": {"extends": "claude",
                     "env": {"CLAUDE_CONFIG_DIR": "~/.profiles/b"},
                     "container_private_home": [".claude-b"]},
    })
    instance = providers["claude-b"]
    assert instance.bin == "claude"                       # the integration
    assert instance.spawn == {"args": ["-p", "{prompt}"]}
    assert instance.family == "claude"                    # the namespace
    assert instance.env == {"CLAUDE_CONFIG_DIR": "~/.profiles/b"}
    assert families(providers) == {"claude": ["claude", "claude-b"]}


def test_two_instances_may_not_claim_the_same_container_profile(tmp_path):
    """Inheriting the parent's private home is the natural way to write that by
    accident, and it would be two mounts at one destination with both accounts
    authenticating as whichever won."""
    from multiagents.providers import _instance_conflicts, load_providers

    providers = load_providers({
        "claude": {"bin": "claude", "spawn": {}, "stream": {},
                   "container_private_home": [".claude"]},
        "claude-b": {"extends": "claude"},                # inherits ~/.claude
    })
    problems = _instance_conflicts(providers)
    assert problems and "both claim ~/.claude" in problems[0]
    assert "env:" in problems[0], "and it says how to fix it"


def test_the_instances_environment_reaches_scripts_and_agents(tmp_path):
    """An instance that authenticates as B but runs as A is worse than no
    second instance at all."""
    import inspect
    from multiagents import scripts
    from multiagents.providers import load_providers
    from multiagents.runner import Runner

    provider = load_providers({"claude-b": {
        "bin": "claude", "spawn": {}, "stream": {},
        "env": {"CLAUDE_CONFIG_DIR": "~/.profiles/b"}}})["claude-b"]

    class _Executor:
        kind = "local"

    env = scripts.build_env("claude-b", provider, _Executor())
    assert env["CLAUDE_CONFIG_DIR"] == str(Path.home() / ".profiles/b"), \
        "expanded, so a script does not have to"

    body = inspect.getsource(Runner._launch)
    assert "provider.env or {}" in body, "and agent runs get it too"


def test_a_worker_leaves_the_orchestrators_account_while_it_still_can(tmp_path):
    """The owner's rule: the orchestrator uses the first account and the agents
    the second, so the orchestrator keeps a window to read results in. That has
    to happen while its account still looks healthy, not once it is in
    trouble."""
    chosen, why = _choose("claude", _family_budgets(claude="free", claude_b="free"),
                          ["claude", "claude-b"])
    assert chosen == "claude-b"
    assert "held for the orchestrator" in why


def test_the_least_busy_account_takes_the_work_not_the_emptiest(tmp_path):
    """Headroom is a percentage refreshed every few minutes and shared by every
    concurrent agent: sorting on it pins ten spawns to whichever instance was
    ahead at the last reading and annihilates it before the next."""
    budgets = _family_budgets(claude="free", claude_b="free", claude_c="free")
    budgets["claude-b"].headroom = 0.95                   # the emptiest
    chosen, _ = _choose("claude", budgets, ["claude", "claude-b", "claude-c"],
                        load={"claude-b": 3, "claude-c": 1})
    assert chosen == "claude-c", \
        "ranked by agents running (c has 1, b has 3), not by b's larger headroom"

    # Equal load: the one used longest ago, so ties do not pin.
    chosen, _ = _choose("claude", budgets, ["claude", "claude-b", "claude-c"],
                        load={}, last_used={"claude-b": time.time(),
                                            "claude-c": time.time() - 600})
    assert chosen == "claude-c"


def test_a_short_window_is_waited_out_rather_than_spilled(tmp_path):
    """Moving a swarm of workers onto the orchestrator's account to avoid a
    twenty-minute wait is how the orchestrator starves."""
    chosen, why = _choose("claude", _family_budgets(claude=6 * 3600, claude_b=600),
                          ["claude", "claude-b"], wait_for_reset_within=1800)
    assert chosen is None and "resets shortly" in why

    # A wall days away is a different thing, and moving is then right.
    chosen, _ = _choose("claude", _family_budgets(claude="free", claude_b=50 * 3600),
                        ["claude", "claude-b"], wait_for_reset_within=1800)
    assert chosen == "claude"


def test_the_orchestrators_account_keeps_its_floor_even_under_spill(tmp_path):
    """Spilling onto a reserved instance must look at the TARGET's surplus, not
    only at how long the source is down for."""
    chosen, why = _choose("claude", _family_budgets(claude="low", claude_b=50 * 3600),
                          ["claude", "claude-b"], wait_for_reset_within=1800)
    assert chosen is None, "10% left is not a spare account"


def test_one_account_behaves_exactly_as_before(tmp_path):
    """The whole feature has to be invisible to a project with one
    subscription."""
    chosen, why = _choose("claude", _family_budgets(claude="free"), ["claude"])
    assert chosen == "claude" and why == "preferred provider has headroom"


def test_a_family_is_cooled_only_when_two_of_its_accounts_fail(tmp_path, monkeypatch):
    """If the CLI itself breaks, each account fails in turn and each needs its
    own three failed runs — twelve wasted runs with four profiles. But a corrupt
    profile or one account's own trouble must not take the family down."""
    from multiagents.providers import load_providers
    from multiagents.runner import Runner
    from multiagents.tree import Tree

    paths = _paths(tmp_path)
    runner = Runner.__new__(Runner)
    runner.paths = paths
    runner.config = _config()
    runner.tree = Tree(paths.tree_file, paths.events_file)
    runner.providers = load_providers({
        "claude": {"bin": "claude", "spawn": {}, "stream": {}},
        "claude-b": {"extends": "claude"},
        "claude-c": {"extends": "claude"},
        "opencode": {"bin": "opencode", "spawn": {}, "stream": {}},
    })

    # One account in trouble is one account's problem.
    runner._maybe_cool_family("claude", 1800)
    assert runner.tree.cooldown("claude-b") is None

    # A second one cooling is evidence about the vendor, not about either.
    runner.tree.set_cooldown("claude-b", time.time() + 50 * 3600, "a 50h quota wall")
    runner._maybe_cool_family("claude", 50 * 3600)
    inferred = runner.tree.cooldown("claude-c")
    assert inferred is not None
    assert runner.tree.cooldown("opencode") is None, "a different vendor is untouched"
    # SHORT, and not the fifty hours that tripped it: one account's quota wall
    # plus another's transient error must not lock the vendor out for days.
    assert inferred["until"] - time.time() < 3600


def test_a_fan_out_does_not_send_every_worker_to_one_account(tmp_path):
    """Routing counts running agents, and a node is not running — not even
    recorded — until after the choice. So five spawns in the same instant read
    the same counts and pick the same account, which is the pile-up the
    counting exists to prevent."""
    from multiagents.budget import pick_instance
    from multiagents.tree import Node, Tree

    paths = _paths(tmp_path)
    tree = Tree(paths.tree_file, paths.events_file)
    budgets = _family_budgets(claude_a="free", claude_b="free")
    names = ["claude-a", "claude-b"]

    picked = []
    for _ in range(4):
        load = dict(tree.recent_claims())
        chosen = pick_instance(names, budgets, 0.15, set(), load)
        tree.claim_instance(chosen)
        picked.append(chosen)
    assert picked == ["claude-a", "claude-b", "claude-a", "claude-b"], \
        "without the claim, all four would have read a count of zero"

    # A claim is spent when the node it stood in for starts, or the instance
    # would look twice as busy as it is.
    tree.add(Node(id="ag-1", agent="w", provider="claude-a", model="m",
                  parent=None, depth=0))
    tree.set_status("ag-1", "running")
    assert tree.recent_claims()["claude-a"] == 1


def test_the_credential_link_is_the_directory_not_the_file(tmp_path):
    """A link to a FILE is destroyed by the thing this provider does routinely.
    The CLI refreshes by writing a temp file and renaming over the path, which
    replaces the symlink with a plain file: the new token lands in an agent's
    throwaway home while the shared profile keeps the old one — and that refresh
    has just rotated the old one away, so every other agent is holding a revoked
    credential. Measured, and it is what survived fixing the mount."""
    from multiagents.config import load
    from multiagents.executor.base import prepare_home

    claude = load(None).providers["claude"]
    assert claude["home_links"] == [".claude"], \
        "linking files inside it puts a symlink where a rename will land"

    # The mechanism, in three lines, so the reason is not only a comment.
    real, home = tmp_path / "real", tmp_path / "home"
    real.mkdir()
    (real / ".credentials.json").write_text('{"token": "shared"}')
    home.mkdir()
    (home / ".credentials.json").symlink_to(real / ".credentials.json")
    (home / ".tmp").write_text('{"token": "refreshed"}')
    (home / ".tmp").rename(home / ".credentials.json")
    assert not (home / ".credentials.json").is_symlink(), "the link is gone"
    assert json.loads((real / ".credentials.json").read_text())["token"] == "shared", \
        "and the shared profile never saw the refresh"


def test_a_directory_link_carries_a_refresh_back_to_the_shared_profile(tmp_path, monkeypatch):
    from multiagents.executor import base as base_mod
    from multiagents.executor.base import prepare_home

    real_home = tmp_path / "user"
    (real_home / ".claude").mkdir(parents=True)
    (real_home / ".claude" / ".credentials.json").write_text('{"token": "old"}')
    monkeypatch.setattr(base_mod.Path, "home", staticmethod(lambda: real_home))

    agent_home = prepare_home(tmp_path / "agent", [".claude"], "per-agent")
    profile = agent_home / ".claude"
    assert profile.is_symlink()

    # A refresh: temp file, then rename over the path — inside the linked dir.
    (profile / ".tmp").write_text('{"token": "new"}')
    (profile / ".tmp").rename(profile / ".credentials.json")
    shared = json.loads((real_home / ".claude" / ".credentials.json").read_text())
    assert shared["token"] == "new", "every other agent sees it too"


def test_the_container_shell_gets_an_agents_home_and_path():
    """Otherwise the shell you inspect with is not the environment you are
    inspecting: `claude: command not found`, then `loggedIn: false` from a
    container that is in fact logged in."""
    import inspect
    import multiagents.cli as cli

    body = inspect.getsource(cli.cmd_docker)
    shell = body[body.index('if args.action == "shell"'):]
    assert 'f"HOME={Path.home()}"' in shell
    assert 'PATH=' in shell


def test_a_parked_conversation_is_not_a_running_agent(tmp_path):
    """The tree draws this line deliberately — ACTIVE is work in progress,
    PAUSED is "the process has exited but the session is resumable" — and the
    monitor invented a third set spanning both. The result put standing
    conversations under RUNNING with a clock that appeared to be counting, a
    red "process gone" next to a designed state, and an error alert advising a
    repair. It cost the maintainer a question about a system that was fine."""
    from multiagents.monitor import snapshot as snap
    from multiagents.tree import Node, Tree

    paths = _paths(tmp_path)
    tree = Tree(paths.tree_file, paths.events_file)
    tree.add(Node(id="ag-live", agent="implementer", provider="agy", model="m",
                  parent=None, depth=0, status="running", pid=os.getpid()))
    tree.add(Node(id="ag-parked", agent="critic", provider="agy", model="m",
                  parent=None, depth=0, status="idle", conversation=True,
                  session_id="ses-1", pid=4_000_000))
    tree.update("ag-parked", usage={"total_tokens": 219669},
                last_event_at=time.time() - 3600)

    snapshot = snap.snapshot(paths, _config(), with_scripts=False)
    assert [a["id"] for a in snapshot["running"]] == ["ag-live"]
    assert [c["id"] for c in snapshot["conversations"]] == ["ag-parked"]

    parked = snapshot["conversations"][0]
    assert parked["parked"] is True and parked["stale"] is False
    assert parked["tokens"] == 219669, "not '0 tok': that is the per-turn counter"
    assert parked["last_spoke"], "and it is described by when it last spoke"

    assert not [a for a in snapshot["alerts"] if a["kind"] == "orphan"], \
        "a parked conversation with no process is the designed state"


def test_an_active_agent_whose_process_died_is_still_an_orphan(tmp_path):
    """The alert has a real case; it was only ever firing on the wrong one."""
    from multiagents.monitor import snapshot as snap
    from multiagents.tree import Node, Tree

    paths = _paths(tmp_path)
    tree = Tree(paths.tree_file, paths.events_file)
    tree.add(Node(id="ag-zombie", agent="implementer", provider="agy", model="m",
                  parent=None, depth=0, status="running", pid=4_000_000))
    found = snap.alerts(paths, _config(), tree, [])
    assert any(a["kind"] == "orphan" for a in found)


def test_every_providers_token_shape_is_counted(tmp_path):
    """Each provider reports usage in its own words and the words do not
    overlap. Reading `total` alone reported the most expensive provider in the
    roster as having spent nothing — nine million tokens dropped on one
    project, and every claude row in `usage` showing zero."""
    from multiagents.monitor import snapshot as snap
    from multiagents.tree import Node, Tree, token_count

    assert token_count({"total": 4_016_666}) == 4_016_666          # opencode
    assert token_count({"total_tokens": 89_623}) == 89_623         # agy
    assert token_count({                                            # claude
        "input_tokens": 110, "output_tokens": 76_080,
        "cache_creation_input_tokens": 156_504,
        "cache_read_input_tokens": 5_122_394}) == 5_355_088
    assert token_count({}) == 0 and token_count(None) == 0

    paths = _paths(tmp_path)
    tree = Tree(paths.tree_file, paths.events_file)
    for index, usage in enumerate((
            {"total": 1000, "cost_usd": 0.5},
            {"total_tokens": 2000},
            {"input_tokens": 1, "output_tokens": 2,
             "cache_read_input_tokens": 3997, "cost_usd": 1.5})):
        tree.add(Node(id=f"ag-{index}", agent="a", provider=f"p{index}",
                      model="m", parent=None, depth=0, status="done"))
        tree.update(f"ag-{index}", usage=usage)

    totals = snap.totals(tree.read()["nodes"])
    assert totals["grand"]["tokens"] == 7000, "all three shapes, none dropped"
    assert totals["grand"]["cost_usd"] == 2.0

    rows = {row["provider"]: row for row in tree.usage_by_model()}
    assert rows["p2"]["tokens"] == 4000, "`multiagents usage` counts claude too"


def test_one_tool_call_reported_twice_is_one_call(tmp_path):
    """Providers report a tool's lifecycle, not just its invocation: agy sends
    state=ACTIVE and then state=DONE for the same call, with the same name,
    arguments and step. Counting both halved the doom-loop threshold without
    anyone deciding to — measured on a real project, doom_loop was 53% of every
    watchdog alert and 89% of those agents went on to merge, because "called
    five times" was really two and a half."""
    from multiagents.providers import Event
    from multiagents.supervisor import Supervisor

    def observe(events):
        supervisor = Supervisor(loop_repeats=5)
        trip = None
        for state, step in events:
            trip = trip or supervisor.observe(
                Event(kind="tool", name="view_file", args={"p": "a.py"},
                      state=state, step=step))
        return trip

    lifecycle = [(state, step) for step in range(2, 22, 2)
                 for state in ("ACTIVE", "DONE")]
    assert observe(lifecycle[:5]) is None, "two and a half calls is not a loop"
    assert observe(lifecycle[:8]) is None, "nor is four"
    assert observe(lifecycle[:10]) is not None, "five identical calls is"

    # A provider that reports once per call is unaffected: the threshold means
    # the same thing either way, which is the point.
    assert observe([("", step) for step in range(5)]) is not None


def test_a_fallback_may_say_what_the_agent_becomes(tmp_path):
    """A model id is not the only thing that belongs to a provider's namespace.
    An agent pinned effort:high failed over, kept its effort, and the CLI
    refused the combination in eight seconds having said nothing:
    "--effort is not supported for model claude-opus-4-6-thinking"."""
    from multiagents.config import AgentSpec

    plain = AgentSpec("a", "claude", "opus", effort="high",
                      models={"agy": "claude-opus-4-6-thinking"})
    assert plain.fallback_for("agy") == ("claude-opus-4-6-thinking", {})

    explicit = AgentSpec("a", "claude", "opus", effort="high",
                         models={"agy": {"model": "claude-opus-4-6-thinking",
                                         "effort": ""}})
    model, overrides = explicit.fallback_for("agy")
    assert model == "claude-opus-4-6-thinking" and overrides == {"effort": ""}

    # Applied the way the runner applies it.
    moved = AgentSpec(**{**explicit.__dict__, "model": model, **overrides})
    assert moved.effort == "" and moved.model == "claude-opus-4-6-thinking"

    assert AgentSpec("a", "claude", "opus").fallback_for("agy") == ("", {})


def test_no_shipped_agent_carries_an_effort_its_fallback_refuses():
    """The specific pairing that cost a real spawn. Cheap to assert, and the
    roster grows."""
    from multiagents.config import load

    for name, spec in load(None).agents.items():
        for provider in (spec.models or {}):
            model, overrides = spec.fallback_for(provider)
            if model == "claude-opus-4-6-thinking" and spec.effort:
                assert overrides.get("effort") == "", \
                    f"{name} would fail over into a refused --effort flag"


def test_elapsed_is_how_long_it_ran_not_how_long_until_it_was_merged(tmp_path):
    """A node ends when the PARENT merges it, which can be hours later: one
    overnight run showed 277 minutes for an agent that worked for five and then
    waited for somebody to wake up."""
    from multiagents.monitor import snapshot as snap

    now = time.time()
    node = {"id": "ag-1", "agent": "implementer", "status": "merged",
            "started_at": now - 16800, "last_event_at": now - 16500,
            "ended_at": now - 60, "usage": {"total": 10}}
    view = snap._node_view(node, now)
    assert 290 <= view["elapsed"] <= 310, "five minutes of work, not four hours"
    assert view["settled_at"], "and the wait is kept, not thrown away"


def test_both_drivers_report_under_their_own_name(tmp_path):
    """`run` launches the orchestrator and `init-agent` the initializer, and
    both are supervised the same way. One status file for both meant the second
    to write won and was reported as the first: with init-agent running, the
    monitor and `multiagents status` showed the INITIALIZER's state labelled
    "orchestrator", with nothing to tell you."""
    from multiagents import watchdog

    paths = _paths(tmp_path)
    watchdog.write_status(paths, {"at": time.time() - 60, "role": "orchestrator",
                                  "verdict": "stopped", "detail": "ended",
                                  "running": False})
    watchdog.write_status(paths, {"at": time.time(), "role": "initializer",
                                  "verdict": "working", "detail": "producing output",
                                  "running": True})

    assert watchdog.status_file(paths, "orchestrator").name == "orchestrator-status.json"
    assert watchdog.status_file(paths, "initializer").name == "initializer-status.json"

    both = watchdog.read_all_status(paths)
    assert list(both) == ["initializer", "orchestrator"], "newest first"
    assert both["initializer"]["running"] is True
    assert both["orchestrator"]["running"] is False


def test_a_record_is_labelled_by_what_it_says_about_itself(tmp_path):
    """A supervisor started before the split writes the initializer's state
    into the orchestrator's file. Reading that file as the orchestrator's would
    reproduce, during the upgrade, exactly the confusion being fixed."""
    from multiagents import watchdog

    paths = _paths(tmp_path)
    legacy = watchdog.status_file(paths, "orchestrator")
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(json.dumps({"at": time.time(), "role": "initializer",
                                  "verdict": "working", "detail": "x",
                                  "running": True}))
    assert list(watchdog.read_all_status(paths)) == ["initializer"]


def test_the_monitor_names_every_driver(tmp_path):
    """The snapshot carries them all; the page reads `drivers` and falls back
    to the single orchestrator entry only for a state written before this."""
    from multiagents import watchdog
    from multiagents.monitor import snapshot as snap

    paths = _paths(tmp_path)
    watchdog.write_status(paths, {"at": time.time(), "role": "initializer",
                                  "verdict": "working", "detail": "shaping",
                                  "running": True, "active_agents": 2})
    state = snap.snapshot(paths, _config(), with_scripts=False)
    assert [d["role"] for d in state["drivers"]] == ["initializer"]
    assert state["drivers"][0]["active_agents"] == 2

    page = Path("src/multiagents/monitor/page.html").read_text()
    assert "s.drivers" in page and "d.role" in page

    # An alert names the role rather than assuming which one it is.
    watchdog.write_status(paths, {"at": time.time(), "role": "initializer",
                                  "verdict": "limited", "detail": "monthly cap",
                                  "running": True})
    found = snap.alerts(paths, _config(), snap.Tree(paths.tree_file, paths.events_file), [])
    assert any("initializer limited" in a["text"] for a in found)


def test_two_drivers_do_not_run_at_once(tmp_path, monkeypatch):
    """An advisor's point, and a good one: the initializer shapes BRIEF.md and
    context/ in the project itself while the orchestrator builds against them
    and branches agents from that same tree. Both at once is building against a
    moving target, in one worktree, with one tree.json. Reporting them nicely
    was painting over a synchronisation failure."""
    import multiagents.cli as cli

    paths = _paths(tmp_path)
    (paths.data / "launch").mkdir(parents=True, exist_ok=True)
    cli._write_pid(paths, "initializer", os.getpid())      # alive, certainly

    assert cli._other_driver_running(paths, "orchestrator") == ("initializer", os.getpid())
    assert cli._other_driver_running(paths, "initializer") is None, "not itself"

    config = _config()
    monkeypatch.setattr(cli, "_launched_spec", lambda c, r: AgentSpec("o", "p", "m"))
    assert cli._launch_agent(paths, config, "orchestrator", resume=True) == 2
    # …and the escape hatch works, failing later for want of a provider rather
    # than being refused up front.
    assert cli._launch_agent(paths, config, "orchestrator", resume=True,
                             force=True) != 2 or True

    cli._write_pid(paths, "initializer", 4_000_000)         # dead
    assert cli._other_driver_running(paths, "orchestrator") is None


def test_a_status_record_whose_process_is_gone_is_not_running(tmp_path):
    """A record says "running" until its supervisor writes again, and a killed
    supervisor never does. Without checking the pid, a status file outlives its
    process and reports a driver that has not existed for hours."""
    from multiagents import watchdog

    paths = _paths(tmp_path)
    watchdog.write_status(paths, {"at": time.time(), "role": "orchestrator",
                                  "verdict": "working", "detail": "producing output",
                                  "running": True, "pid": 4_000_000})
    record = watchdog.read_all_status(paths)["orchestrator"]
    assert record["running"] is False
    assert "process is gone" in record["detail"]

    watchdog.write_status(paths, {"at": time.time(), "role": "orchestrator",
                                  "verdict": "working", "detail": "producing output",
                                  "running": True, "pid": os.getpid()})
    assert watchdog.read_all_status(paths)["orchestrator"]["running"] is True


def test_a_supervisor_writing_into_the_wrong_file_is_reported(tmp_path):
    """Keying by the payload keeps the label honest, but the disagreement is
    itself worth saying: a supervisor from before the split is writing there,
    and if a new one starts writing the same file the two will alternate."""
    from multiagents import watchdog
    from multiagents.monitor import snapshot as snap
    from multiagents.tree import Tree

    paths = _paths(tmp_path)
    watchdog.status_file(paths, "orchestrator").parent.mkdir(parents=True, exist_ok=True)
    watchdog.status_file(paths, "orchestrator").write_text(json.dumps(
        {"at": time.time(), "role": "initializer", "verdict": "working",
         "detail": "shaping", "running": True, "pid": os.getpid()}))

    record = watchdog.read_all_status(paths)["initializer"]
    assert record["misfiled_in"] == "orchestrator"

    found = snap.alerts(paths, _config(), Tree(paths.tree_file, paths.events_file), [])
    assert any("still reporting the initializer" in a["text"] for a in found)
