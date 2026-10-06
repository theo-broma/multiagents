"""Black-box contract for the opencode-deepinfra provider: context/specs/deepinfra-provider.md.

DI-R1  a shipped `opencode-deepinfra` provider instance (providers.yaml, refresh-models)
DI-R2  model ids with two slashes (`deepinfra/Qwen/Qwen3.8-Max`) work end to end
DI-R3  `check` / `login` of opencode.sh when MULTIAGENTS_OPENCODE_PLAN=deepinfra
DI-R4  `budget` for a metered provider: no windows, unknown headroom, no network
DI-R6  no regression for Go, Z.AI and the roster (DI-R5 is spend-caps.md, not here)

Prerequisite confirmed (see the run report): opencode's own provider catalog
(models.dev, embedded in the opencode binary) defines the provider
`{id: "deepinfra", name: "Deep Infra", env: ["DEEPINFRA_API_KEY"]}`; opencode's
auth.json is keyed by provider id, so the entry name is `deepinfra`.

The script is driven as a subprocess with a temporary HOME / XDG_DATA_HOME
holding a fake auth.json, a fake `opencode`, and a PATH `curl` that records
that it was invoked. Nothing here touches the network or the real auth store.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from multiagents import server
from multiagents.paths import ProjectPaths, global_config_dir, shipped_defaults_dir

sys.path.insert(0, str(Path(__file__).parent / "support"))
import c3_harness as h3  # noqa: E402

DEFAULTS = shipped_defaults_dir()
SCRIPT = DEFAULTS / "providers" / "opencode.sh"
NAME = "opencode-deepinfra"
PLAN = "deepinfra"
KEY = "di-test-1234567890abcdef.SECRETtail"
ZAI_KEY = "zk-NEVER-ACCEPT-FOR-DEEPINFRA-4242"
GO_KEY = "go-key-NEVER-ACCEPT-FOR-DEEPINFRA-9876"
MODEL = "deepinfra/Qwen/Qwen3.8-Max"
MODEL_B = "deepinfra/openai/gpt-oss-20b"


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------

def _exe(path: Path, body: str, shebang: str = "#!/bin/sh\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(shebang + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


class Env:
    """Temporary HOME/XDG_DATA_HOME, a fake `opencode`, and a PATH `curl` that
    records every call and fails (exit 7) so a network attempt is visible both
    as a marker file and as a changed result."""

    def __init__(self, tmp_path: Path):
        self.root = tmp_path
        self.data = tmp_path / "xdg"
        self.home = tmp_path / "home"
        self.bindir = tmp_path / "pathbin"
        self.data.mkdir()
        self.home.mkdir()
        self.bindir.mkdir()
        self.bin = _exe(tmp_path / "opencode", "echo '1 credentials'\n")
        self.calls = tmp_path / "bin-calls.txt"
        self.curl_log = tmp_path / "curl-calls.txt"
        self.set_curl(f'echo "CURL $*" >> "{self.curl_log}"; cat >/dev/null 2>&1; exit 7\n')

    def set_curl(self, body: str) -> None:
        _exe(self.bindir / "curl", body)

    @property
    def curl_calls(self) -> list[str]:
        return self.curl_log.read_text().splitlines() if self.curl_log.exists() else []

    def store(self, entries) -> None:
        d = self.data / "opencode"
        d.mkdir(parents=True, exist_ok=True)
        (d / "auth.json").write_text(entries if isinstance(entries, str)
                                     else json.dumps(entries))

    def di_store(self, key=KEY, **extra) -> None:
        self.store({PLAN: {"type": "api", "key": key}, **extra})

    def env(self, plan=PLAN, extra=None) -> dict:
        env = {k: v for k, v in os.environ.items()
               if not k.lower().endswith("_proxy") and not k.startswith("MULTIAGENTS_")
               and k not in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME")}
        env.update(HOME=str(self.home), XDG_DATA_HOME=str(self.data), TZ="UTC",
                   PATH=f"{self.bindir}{os.pathsep}/usr/bin{os.pathsep}/bin",
                   MULTIAGENTS_BIN=str(self.bin))
        if plan is not None:
            env["MULTIAGENTS_OPENCODE_PLAN"] = plan
        env.update(extra or {})
        return env

    def run(self, action, *, plan=PLAN, extra=None, timeout=60):
        return subprocess.run(["sh", str(SCRIPT), action], env=self.env(plan, extra),
                              text=True, stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=timeout)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def assert_no_key(cp, *keys):
    for k in keys or (KEY,):
        assert k not in cp.stdout and k not in cp.stderr, (
            f"the key leaked:\nstdout={cp.stdout!r}\nstderr={cp.stderr!r}")


def _shipped_raw():
    return yaml.safe_load((DEFAULTS / "providers.yaml").read_text())["providers"]


def _load(raw):
    from multiagents.providers import load_providers
    return load_providers(copy.deepcopy(raw))


# ===========================================================================
# DI-R1: a shipped provider instance
# ===========================================================================

def test_di_r1_shipped_file_defines_the_instance_disabled_metered_own_family():
    p = _load(_shipped_raw()).get(NAME)
    assert p is not None, "shipped providers.yaml has no `opencode-deepinfra` instance"
    assert p.enabled is False
    assert p.billing == "metered"
    assert p.family == NAME
    assert list(p.models_include) == ["deepinfra/*"]
    assert p.env.get("MULTIAGENTS_OPENCODE_PLAN") == PLAN


def test_di_r1_inherits_opencode_spawn_stream_and_script():
    ps = _load(_shipped_raw())
    d, o = ps.get(NAME), ps["opencode"]
    assert d is not None, "no `opencode-deepinfra` instance"
    assert d.spawn == o.spawn and d.spawn.get("args")
    assert d.stream == o.stream and d.stream.get("rules")
    assert d.bin == o.bin
    assert d.auth == o.auth, "the instance must use the same opencode.sh"
    assert d.usage_mode == o.usage_mode == "delta"


def test_di_r1_failover_is_never_implicit():
    from multiagents.providers import families
    fam = families(_load(_shipped_raw()))
    assert fam.get(NAME) == [NAME], fam
    assert NAME not in fam.get("opencode-go", []), fam
    assert NAME not in fam.get("opencode-zai", []), fam


def test_di_r1_opencode_and_opencode_zai_are_unchanged():
    ps = _load(_shipped_raw())
    assert ps.get(NAME) is not None
    o, z = ps["opencode-go"], ps["opencode-zai"]      # OG-R1: the go route is `opencode-go`
    assert list(o.models_include) == ["opencode-go/*"]      # OZ-R4: zen is its own provider
    assert o.family == "opencode-go" and o.enabled is not False
    assert "MULTIAGENTS_OPENCODE_PLAN" not in (o.env or {})
    assert list(z.models_include) == ["zai-coding-plan/*"]
    assert z.family == "opencode-zai" and z.enabled is False and z.billing == "plan"
    assert z.env.get("MULTIAGENTS_OPENCODE_PLAN") == "zai-coding-plan"


def test_di_r1_shipped_yaml_documents_enabling_and_that_it_bills_real_money():
    text = (DEFAULTS / "providers.yaml").read_text()
    i = text.find(f"\n  {NAME}:")
    assert i >= 0, "no opencode-deepinfra block in the shipped providers.yaml"
    comment = "\n".join(line for line in text[max(0, i - 1200):i].splitlines()
                        if line.lstrip().startswith("#")).lower()
    assert "enabled" in comment, "the comment must say how to enable it"
    assert "multiagents auth login opencode-deepinfra" in comment
    assert any(w in comment for w in ("real money", "billed", "bills", "metered")), comment


def _models_for(tmp_path, monkeypatch, listing, enable=True):
    from multiagents.models import refresh_models
    raw = _shipped_raw()
    assert NAME in raw, "no `opencode-deepinfra` instance in the shipped file"
    raw = {k: raw[k] for k in ("opencode", "opencode-go", "opencode-zai", NAME)}
    raw["opencode-zai"]["enabled"] = True
    raw[NAME]["enabled"] = enable
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _exe(bindir / "opencode", "[ \"$1\" = models ] && printf '%s\\n' " + listing + "\n")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}/usr/bin{os.pathsep}/bin")
    monkeypatch.setenv("HOME", str(tmp_path))
    target = tmp_path / "models.yaml"
    out = refresh_models(_load(raw), target, config_dir=tmp_path / "cfg")
    return yaml.safe_load(target.read_text())["models"], out


def test_di_r1_refresh_models_splits_the_namespaces_keeping_two_slash_ids(tmp_path, monkeypatch):
    models, out = _models_for(
        tmp_path, monkeypatch,
        "opencode/big-pickle opencode-go/glm-5.1 zai-coding-plan/glm-4.7 "
        f"{MODEL} {MODEL_B} deepinfra/zai-org/GLM-4.7/extra")
    ids = lambda n: sorted(m["id"] for m in models.get(n) or [])   # noqa: E731
    assert ids(NAME) == sorted([MODEL, MODEL_B, "deepinfra/zai-org/GLM-4.7/extra"]), (models, out)
    assert ids("opencode-go") == ["opencode-go/glm-5.1"], models   # OZ-R4: no opencode/* under go
    assert ids("opencode-zai") == ["zai-coding-plan/glm-4.7"], models
    for other in ("opencode-go", "opencode-zai"):
        assert not [i for i in ids(other) if i.startswith("deepinfra/")], models


def test_di_r1_disabled_instance_is_not_refreshed(tmp_path, monkeypatch):
    models, _ = _models_for(tmp_path, monkeypatch, f"opencode/x {MODEL}", enable=False)
    assert NAME not in (models or {})
    assert not [m for ms in (models or {}).values() for m in ms
                if m["id"].startswith("deepinfra/")], "deepinfra/* attributed to someone else"


# ===========================================================================
# DI-R2: model ids with two slashes work end to end
# ===========================================================================

STEP_FINISH = {"type": "step_finish", "sessionID": "ses_di1",
               "part": {"type": "step-finish", "reason": "stop", "cost": 0.00029121,
                        "tokens": {"input": 9000, "output": 575, "reasoning": 0,
                                   "cache": {"read": 0, "write": 0}}}}
TEXT = {"type": "text", "sessionID": "ses_di1", "part": {"type": "text", "text": "done"}}


def _fake_opencode(tmp_path: Path, argv_file: Path) -> Path:
    """A fake opencode: records argv one per line, prints a DeepInfra step_finish."""
    events = [TEXT, STEP_FINISH]
    return _exe(tmp_path / "clis" / "opencode",
                "import json, sys\n"
                f"open({str(argv_file)!r}, 'w').write('\\n'.join(sys.argv[1:]))\n"
                f"for e in {events!r}:\n    print(json.dumps(e)); sys.stdout.flush()\n",
                shebang=f"#!{sys.executable}\n")


def _deepinfra_runner(tmp_path, monkeypatch, model, argv_file, *, project=None):
    from multiagents.providers import resolve_inheritance
    raw = _shipped_raw()
    assert NAME in raw, "no `opencode-deepinfra` instance in the shipped file"
    d = copy.deepcopy(resolve_inheritance(copy.deepcopy(raw))[NAME])
    d.pop("extends", None)
    d["bin"] = str(_fake_opencode(tmp_path, argv_file))
    d["bin_search"] = []
    d["enabled"] = True
    spec = h3.AgentSpec(name="worker", provider=NAME, model=model)
    return h3.make_runner(tmp_path / "proj", monkeypatch, agents={"worker": spec},
                          providers={NAME: d}, project=project)


def _start(r, task="go"):
    async def go():
        result = await r.start("worker", task)
        run = r.runs.get(result.get("agent_id", ""))
        if run is not None:
            await asyncio.wait_for(run.done.wait(), timeout=30)
        return result
    return asyncio.run(go())


def test_di_r2_two_slash_model_is_the_exact_dash_m_argument(tmp_path, monkeypatch):
    argv_file = tmp_path / "argv.txt"
    r = _deepinfra_runner(tmp_path, monkeypatch, MODEL, argv_file)
    result = _start(r)
    assert argv_file.exists(), f"the CLI never ran: {result}"
    argv = argv_file.read_text().split("\n")
    assert "-m" in argv, argv
    assert argv[argv.index("-m") + 1] == MODEL, argv
    assert argv.count(MODEL) == 1, argv


def test_di_r2_recorded_model_is_the_full_id_and_creates_no_directory(tmp_path, monkeypatch):
    argv_file = tmp_path / "argv.txt"
    r = _deepinfra_runner(tmp_path, monkeypatch, MODEL, argv_file)
    _start(r)
    rows = r.tree.usage_by_model()
    assert [row["model"] for row in rows] == [MODEL], rows
    assert rows[0]["provider"] == NAME
    nodes = r.tree.read()["nodes"]
    assert [n.get("model") for n in nodes.values()] == [MODEL]
    # the extra slash must never become a directory anywhere under the project,
    # its state, or the machine state the test is confined to
    seen = []
    for base in (tmp_path / "proj", Path(os.environ["MULTIAGENTS_STATE_DIR"])):
        for p in base.rglob("*"):
            if p.name in ("Qwen", "Qwen3.8-Max", "deepinfra") and ".git" not in p.parts:
                seen.append(str(p))
    assert not seen, f"a model-derived path was created: {seen}"


def test_di_r2_two_models_with_the_same_org_keep_separate_rows(tmp_path, monkeypatch):
    argv_file = tmp_path / "argv.txt"
    r = _deepinfra_runner(tmp_path, monkeypatch, "deepinfra/Qwen/Qwen3.8-Max", argv_file)
    _start(r)
    r2 = _deepinfra_runner(tmp_path / "second", monkeypatch, "deepinfra/Qwen/Qwen3.8-Plus",
                           tmp_path / "argv2.txt")
    _start(r2)
    assert [x["model"] for x in r2.tree.usage_by_model()] == ["deepinfra/Qwen/Qwen3.8-Plus"]
    assert [x["model"] for x in r.tree.usage_by_model()] == ["deepinfra/Qwen/Qwen3.8-Max"]


def test_di_r2_a_pinned_two_slash_model_passes_the_allowlist(tmp_path, monkeypatch):
    argv_file = tmp_path / "argv.txt"
    _authed_host(tmp_path, monkeypatch)
    r = _deepinfra_runner(tmp_path, monkeypatch, MODEL_B, argv_file)

    async def go():
        result = await r.start("worker", "go", model=MODEL)
        run = r.runs.get(result.get("agent_id", ""))
        if run is not None:
            await asyncio.wait_for(run.done.wait(), timeout=30)
        return result
    result = asyncio.run(go())
    assert "refus" not in json.dumps(result).lower(), result
    assert argv_file.exists(), result
    argv = argv_file.read_text().split("\n")
    assert argv[argv.index("-m") + 1] == MODEL, argv


def test_di_r2_models_catalog_split_keeps_the_full_id(tmp_path, monkeypatch):
    # The catalog is `{provider: [{id: ...}]}`; the id stays whole, never
    # truncated to `deepinfra/Qwen` nor split into a nested structure.
    models, _ = _models_for(tmp_path, monkeypatch, f"opencode/x {MODEL}")
    entries = models.get(NAME) or []
    assert [m["id"] for m in entries] == [MODEL], models
    assert all("/" in m["id"] and m["id"].count("/") == 2 for m in entries)


# ===========================================================================
# DI-R3: check and login
# ===========================================================================

def test_di_r3_check_exits_0_when_the_entry_has_a_key(env):
    env.bin = _exe(env.root / "opencode", "echo '0 credentials'\n")   # providers list: none
    env.di_store()
    cp = env.run("check")
    assert cp.returncode == 0, (cp.stdout, cp.stderr)
    assert_no_key(cp)


def test_di_r3_check_does_not_need_the_opencode_binary(env):
    env.bin = _exe(env.root / "opencode", "exit 99\n")
    env.di_store()
    assert env.run("check").returncode == 0


def test_di_r3_check_missing_entry_exits_10_naming_entry_and_fix(env):
    env.store({"opencode-go": {"type": "api", "key": GO_KEY}})
    cp = env.run("check")
    assert cp.returncode == 10, (cp.stdout, cp.stderr)
    msg = cp.stdout + cp.stderr
    assert PLAN in msg
    assert "multiagents auth login opencode-deepinfra" in msg
    assert "Deep Infra" in msg
    assert_no_key(cp, GO_KEY, KEY)


def test_di_r3_a_zai_only_store_is_10_for_deepinfra(env):
    env.store({"zai-coding-plan": {"type": "api", "key": ZAI_KEY}})
    env.bin = _exe(env.root / "opencode", "echo '1 credentials'\n")
    cp = env.run("check")
    assert cp.returncode == 10, (cp.stdout, cp.stderr)
    assert "multiagents auth login opencode-deepinfra" in cp.stdout + cp.stderr
    assert_no_key(cp, ZAI_KEY)


def test_di_r3_check_missing_store_exits_10(env):
    cp = env.run("check")
    assert cp.returncode == 10, (cp.stdout, cp.stderr)
    assert (cp.stdout + cp.stderr).strip()


def test_di_r3_the_three_failures_are_told_apart(env):
    env.store("{ this is not json " + KEY)
    bad = env.run("check")
    assert bad.returncode == 10, (bad.stdout, bad.stderr)
    assert "Traceback" not in bad.stdout + bad.stderr
    assert_no_key(bad)
    env.store({"opencode-go": {"key": GO_KEY}})
    entry_missing = env.run("check")
    shutil.rmtree(env.data / "opencode")
    store_missing = env.run("check")
    assert entry_missing.returncode == store_missing.returncode == 10
    msgs = {(r.stdout + r.stderr).strip() for r in (bad, entry_missing, store_missing)}
    assert len(msgs) == 3, f"each cause needs its own message: {msgs}"


@pytest.mark.parametrize("entry", [
    {"type": "api", "key": ""},
    {"type": "api"},
    {"type": "api", "key": None},
    "just-a-string",
    None,
])
def test_di_r3_check_entry_without_a_usable_key_exits_10(env, entry):
    env.store({PLAN: entry, "opencode-go": {"key": GO_KEY}})
    cp = env.run("check")
    assert cp.returncode == 10, (cp.stdout, cp.stderr)
    assert "Traceback" not in cp.stdout + cp.stderr


def test_di_r3_check_non_object_store_exits_10(env):
    env.store("[1, 2, 3]")
    cp = env.run("check")
    assert cp.returncode == 10, (cp.stdout, cp.stderr)
    assert "Traceback" not in cp.stdout + cp.stderr


def test_di_r3_check_is_repeatable(env):
    env.di_store()
    a, b = env.run("check"), env.run("check")
    assert (a.returncode, a.stdout) == (b.returncode, b.stdout) and a.returncode == 0


def test_di_r3_check_makes_no_network_call(env):
    env.di_store()
    env.run("check")
    assert env.curl_calls == []


def test_di_r3_login_prints_one_line_then_runs_providers_login(env):
    env.bin = _exe(env.root / "opencode",
                   f'echo "ARGS:$*" > "{env.calls}"; echo FAKE-LOGIN-RAN\n')
    cp = env.run("login")
    assert cp.returncode == 0, (cp.stdout, cp.stderr)
    assert env.calls.read_text().strip() == "ARGS:providers login"
    before = cp.stdout.split("FAKE-LOGIN-RAN")[0].strip().splitlines()
    assert len(before) == 1, f"exactly one instruction line first, got {before}"
    assert "deep infra" in before[0].lower()


def test_di_r3_login_passes_on_the_login_exit_status(env):
    env.bin = _exe(env.root / "opencode", "exit 7\n")
    cp = env.run("login")
    assert cp.returncode == 7 and "deep infra" in cp.stdout.lower()


# -- unset or zai plan: exactly as today --------------------------------------

def test_di_r3_unset_plan_check_ignores_the_store_and_uses_providers_list(env):
    env.di_store()
    env.bin = _exe(env.root / "opencode", "echo '0 credentials'\n")
    cp = env.run("check", plan=None)
    assert cp.returncode == 10 and "no stored credentials" in cp.stdout


def test_di_r3_unset_plan_check_with_credentials_is_0(env):
    env.store({})
    env.bin = _exe(env.root / "opencode", "echo 'x 2 credentials'\n")
    cp = env.run("check", plan=None)
    assert cp.returncode == 0 and "2 stored credential" in cp.stdout


def test_di_r3_zai_plan_check_ignores_a_deepinfra_entry(env):
    env.di_store()                      # deepinfra only: no zai-coding-plan entry
    cp = env.run("check", plan="zai-coding-plan")
    assert cp.returncode == 10, (cp.stdout, cp.stderr)
    assert "opencode-zai" in cp.stdout + cp.stderr
    assert "opencode-deepinfra" not in cp.stdout + cp.stderr


def test_di_r3_zai_plan_check_still_passes_with_its_own_entry(env):
    env.store({"zai-coding-plan": {"type": "api", "key": ZAI_KEY}})
    assert env.run("check", plan="zai-coding-plan").returncode == 0


@pytest.mark.parametrize("plan", [None, "", "zai-coding-plan"])
def test_di_r3_login_other_plans_do_not_mention_deepinfra(env, plan):
    env.bin = _exe(env.root / "opencode", "exit 0\n")
    cp = env.run("login", plan=plan)
    assert cp.returncode == 0
    assert "deep infra" not in cp.stdout.lower()


def test_di_r3_unset_plan_login_is_the_go_text(env):
    env.bin = _exe(env.root / "opencode", "echo FAKE-LOGIN-RAN\n")
    cp = env.run("login", plan=None)
    assert "opencode sign-in." in cp.stdout and "OpenCode Go" in cp.stdout


# ===========================================================================
# DI-R4: budget for a metered provider
# ===========================================================================

def _budget(env, **kw):
    cp = env.run("budget", **kw)
    assert cp.returncode == 0, (cp.returncode, cp.stdout, cp.stderr)
    return cp, json.loads(cp.stdout)


def _assert_unknown_no_windows(b):
    assert b.get("known") is False, b
    assert not b.get("windows"), b
    assert b.get("headroom") is None, f"headroom must be unknown, not a number: {b}"
    assert "exhaust" not in json.dumps(b).lower(), b


def test_di_r4_budget_with_a_key_is_exit_0_no_windows_and_never_calls_curl(env):
    env.di_store()
    cp, b = _budget(env)
    _assert_unknown_no_windows(b)
    assert env.curl_calls == [], f"budget made a network call: {env.curl_calls}"
    assert_no_key(cp)


def test_di_r4_budget_without_a_store_is_the_same_and_never_calls_curl(env):
    cp, b = _budget(env)
    _assert_unknown_no_windows(b)
    assert env.curl_calls == []


def test_di_r4_budget_with_a_go_key_in_the_store_does_not_probe_go(env):
    # The branch is taken BEFORE the Go probe: a Go key must not be sent anywhere.
    env.store({"opencode-go": {"type": "api", "key": GO_KEY},
               PLAN: {"type": "api", "key": KEY}})
    cp, b = _budget(env)
    _assert_unknown_no_windows(b)
    assert env.curl_calls == []
    assert_no_key(cp, GO_KEY, KEY)


@pytest.mark.parametrize("store", ["{ not json", "[1,2]", {PLAN: {"key": ""}}])
def test_di_r4_budget_with_a_bad_store_is_still_exit_0_unknown(env, store):
    env.store(store)
    _, b = _budget(env)
    _assert_unknown_no_windows(b)
    assert env.curl_calls == []


def test_di_r4_budget_prints_one_json_object_and_is_repeatable(env):
    env.di_store()
    a, _ = _budget(env)
    b, _ = _budget(env)
    assert a.stdout == b.stdout and len(a.stdout.strip().splitlines()) == 1


def test_di_r4_budget_has_a_note_that_is_a_string(env):
    env.di_store()
    _, b = _budget(env)
    assert isinstance(b.get("note"), str) and b["note"]


def test_di_r4_unset_plan_budget_still_probes_go(env):
    # regression guard for the branch: without the plan variable the Go probe runs
    env.store({"opencode-go": {"type": "api", "key": GO_KEY}})
    reply = env.root / "go-reply.json"
    reply.write_text('{"usage": {"weekly": {"percent": 40, "resetsAt": "2026-10-06T00:00:00Z"}}}')
    env.set_curl(f'cat >/dev/null; echo CALLED >> "{env.curl_log}"; cat "{reply}"\n')
    _, b = _budget(env, plan=None)
    assert b["known"] is True and b["headroom"] == 0.6
    assert env.curl_calls == ["CALLED"]


# -- the router and budget_status ---------------------------------------------

def _install_script(tmp_path):
    """The router finds provider scripts in the global config dir first."""
    dest = global_config_dir() / "providers" / "opencode.sh"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(SCRIPT, dest)


def _authed_host(tmp_path, monkeypatch) -> Env:
    """The process runs with a temporary HOME holding a Deep Infra credential
    and a PATH `curl` that records any use."""
    (tmp_path / "e").mkdir()
    e = Env(tmp_path / "e")
    e.di_store()
    monkeypatch.setenv("HOME", str(e.home))
    monkeypatch.setenv("XDG_DATA_HOME", str(e.data))
    monkeypatch.setenv("PATH", f"{e.bindir}{os.pathsep}/usr/bin{os.pathsep}/bin")
    _install_script(tmp_path)
    return e


def test_di_r4_the_router_admits_an_enabled_instance_with_no_windows(tmp_path, monkeypatch):
    e = _authed_host(tmp_path, monkeypatch)
    argv_file = tmp_path / "argv.txt"
    r = _deepinfra_runner(tmp_path, monkeypatch, MODEL, argv_file)
    result = _start(r)
    assert argv_file.exists(), f"an unknown-headroom metered provider was refused: {result}"
    assert result.get("provider", NAME) == NAME, result
    assert e.curl_calls == [], f"admission made a network call: {e.curl_calls}"
    node = next(iter(r.tree.read()["nodes"].values()))
    assert node["status"] != "refused" and node["provider"] == NAME


def test_di_r4_by_model_shows_full_id_real_cost_and_no_plan_marker(tmp_path, monkeypatch):
    argv_file = tmp_path / "argv.txt"
    r = _deepinfra_runner(tmp_path, monkeypatch, MODEL, argv_file)
    _start(r)
    paths: ProjectPaths = r.paths
    monkeypatch.setenv("MULTIAGENTS_PROJECT", str(paths.root))
    monkeypatch.chdir(paths.root)
    monkeypatch.setattr(server.budget_mod, "read_all", lambda *a, **k: {})
    # make the server see the project's providers: enabled shipped instance
    cfg = paths.root / ".multiagents" / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "providers.yaml").write_text(yaml.safe_dump({"providers": {
        n: {"enabled": False} for n in _shipped_raw()} | {NAME: {"enabled": True}}}))
    server._reset()
    try:
        status = server.budget_status()
    finally:
        server._reset()
    if isinstance(status, str):
        status = json.loads(status)
    rows = [row for row in status.get("by_model") or [] if row.get("provider") == NAME]
    assert len(rows) == 1, status.get("by_model")
    row = rows[0]
    assert row["model"] == MODEL, row
    assert row["cost_usd"] == pytest.approx(0.00029121, abs=1e-6), row
    assert row.get("billing") != "plan", f"real billed USD must not be marked plan: {row}"


# ===========================================================================
# DI-R6: no regression
# ===========================================================================

def test_di_r6_other_shipped_providers_are_not_given_the_plan_variable():
    ps = _load(_shipped_raw())
    assert ps.get(NAME) is not None
    for name, p in ps.items():
        if name in (NAME, "opencode-zai", "opencode-zen"):
            continue
        assert (p.env or {}).get("MULTIAGENTS_OPENCODE_PLAN") is None, name
    assert ps["opencode-zai"].env["MULTIAGENTS_OPENCODE_PLAN"] == "zai-coding-plan"
    assert ps["opencode-zen"].env["MULTIAGENTS_OPENCODE_PLAN"] == "zen"      # OZ-R1


def test_di_r6_shipped_roster_never_uses_the_instance():
    agents = yaml.safe_load((DEFAULTS / "agents.yaml").read_text())["agents"]
    text = json.dumps(agents)
    assert NAME not in text and "deepinfra/" not in text, "DI is 'just available'"


def test_di_r6_unset_plan_budget_without_a_store_keeps_its_message(env):
    cp = env.run("budget", plan=None)
    assert cp.returncode == 0
    assert json.loads(cp.stdout) == {
        "known": False, "note": "no opencode auth store; run `opencode auth login`"}
    assert env.curl_calls == []


def test_di_r6_unset_plan_usage_is_unchanged(env):
    cp = env.run("usage", plan=None,
                 extra={"MULTIAGENTS_BUDGET": json.dumps({"known": False, "note": "free tier"})})
    assert cp.returncode == 0 and cp.stdout.strip() == "free tier"
