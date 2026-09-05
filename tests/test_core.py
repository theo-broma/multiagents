"""Unit tests for the logic that live runs do not reliably exercise.

Doom-loop detection is the main one: a well-behaved agent never triggers it, so
without tests it would ship unverified. Redaction matters for the same reason —
it only proves itself on the day something secret reaches a log.
"""

import sys, time
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
    assert looks_like_quota_failure("RESOURCE_EXHAUSTED", "", "")
    assert looks_like_quota_failure("", "429 Too Many Requests", "")
    assert not looks_like_quota_failure("SUCCESS", "", "all done")


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
    d = tmp_path / "auth"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(0o755)
    return p


class _Prov:
    def __init__(self, auth=None, bin="x"):
        self.auth, self.bin = auth or {}, bin
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
    assert auth_mod.looks_like_auth_failure("", "", "401 Unauthorized")
    assert auth_mod.looks_like_auth_failure("ERROR", "invalid api key")
    assert auth_mod.looks_like_auth_failure("", "no credentials; run opencode providers login")


def test_permission_denial_is_not_an_auth_failure():
    """A tool auto-denied inside the agent is a different problem with a
    different fix; conflating them would send the user to re-login pointlessly."""
    denial = ("jetski: no output produced — a tool required the \"command\" permission "
              "that headless mode cannot prompt for, so it was auto-denied.")
    assert not auth_mod.looks_like_auth_failure("", denial)
    assert not auth_mod.looks_like_auth_failure("SUCCESS", "", "all done")


def test_shipped_scripts_exist_and_implement_the_contract():
    d = Path(auth_mod.__file__).parent / "defaults" / "auth"
    for provider in ("claude", "opencode", "agy"):
        script = d / f"{provider}.sh"
        assert script.is_file(), provider
        body = script.read_text()
        assert "check)" in body and "login)" in body, provider
        assert "exit 10" in body, f"{provider} must be able to report NOT authenticated"


def test_cancellation_reasons_are_distinguished():
    """An explicit stop and the server exiting both surface as CancelledError,
    but recording both as "cancelled by parent" makes a session ending look
    like a deliberate kill. That cost real debugging time once."""
    import inspect
    from multiagents import runner as runner_mod
    body = inspect.getsource(runner_mod.Runner._consume)
    assert "stop_requested" in body
    assert "stopped by parent" in body
    assert "interrupted" in body

    stop_body = inspect.getsource(runner_mod.Runner.stop)
    # The flag must be set BEFORE the task is cancelled, or the handler races.
    assert stop_body.index("stop_requested") < stop_body.index("task.cancel()")
