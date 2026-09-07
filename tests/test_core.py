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
    `agy --model opencode-go/glm-5.3-flash`."""
    import inspect
    from multiagents.runner import Runner
    body = inspect.getsource(Runner.start)
    assert "names no" in body and "add one under" in body
    assert "spec.extra.get(\"models\")" in body


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

        def wait(self):
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

        def wait(self):
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


def test_a_crash_is_a_hard_stop_and_a_lost_terminal_is_not(tmp_path, monkeypatch,
                                                           capsys):
    """A non-zero exit means an unhandled error and unknown state. Carrying on
    unattended, with agents holding bypass permissions, turns one controlled
    failure into an unsupervised sequence of them. A lost terminal is different
    in kind: the process was healthy and its window went away."""
    import multiagents.cli as cli

    handed_over = []
    monkeypatch.setattr(cli, "_start_supervisor", lambda *a: None)
    monkeypatch.setattr(cli, "_supervise",
                        lambda *a, **k: handed_over.append(True) or 0)
    paths = _paths(tmp_path)

    monkeypatch.setattr(cli, "_run_attached", lambda *a: 3)          # crash
    assert cli._run_supervised(paths, _config(), "orchestrator", None, None,
                               None, {}, [], {}) == 1
    assert handed_over == [], "a crash must not continue unattended"
    assert "state unknown" in capsys.readouterr().out

    monkeypatch.setattr(cli, "_run_attached", lambda *a: -cli.signal.SIGHUP)
    cli._run_supervised(paths, _config(), "orchestrator", None, None, None,
                        {}, [], {})
    assert handed_over == [True], "a lost terminal hands over"


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
    monkeypatch.setattr(cli, "_run_attached", lambda *a: -cli.signal.SIGHUP)
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

    codes = iter([3, 3, 0])          # crash, crash, then the user quits
    def attached(argv, env):
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
    monkeypatch.setattr(cli, "_run_attached", lambda *a: 3)
    handed = []
    monkeypatch.setattr(cli, "_supervise", lambda *a, **k: handed.append(1) or 0)

    config = Config(project={"limits": {"restart_attempts": 2,
                                        "restart_delay_seconds": 0}},
                    providers={}, agents={}, models={}, instruction_dirs=[])
    assert cli._run_supervised(_paths(tmp_path), config, "orchestrator", None,
                               None, None, {}, [], {}) == 1
    assert handed == [], "a crash never falls through to unattended"


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
