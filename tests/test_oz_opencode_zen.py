"""Black-box contract for context/specs/opencode-zen-provider.md (OZ-R1..OZ-R4).

OZ-R1  the shipped `opencode-zen` provider instance (providers.yaml)
OZ-R2  breaker / cooldown / headroom are per family: go and zen never refuse each other
OZ-R3  `opencode.sh` with MULTIAGENTS_OPENCODE_PLAN=zen: budget, check, usage
OZ-R4  go serves `opencode-go/*` only, zen serves `opencode/*` only

Everything is synthetic. The script is driven as a subprocess with a temporary
HOME / XDG_DATA_HOME holding a fake auth.json (never a real credential file), a
fake `opencode` binary, a PATH `curl` that records every call, and a local fake
HTTP proxy (bound to port 0) that records any request made through the proxy
environment variables. The opencode-go usage URL is hardcoded in the script, so
"never contacted" is observed through those two recorders. Runner tests use
fake CLIs and stop everything they started in a fixture finalizer.

SILENCES in the contract (each assumption is the loosest reading, and is the
only place this file goes beyond the text):
- Zen's credential in opencode's auth.json. opencode names that provider
  `opencode` (go is `opencode-go`); the positive `check` tests store the key
  under `opencode`.
- A zen `check` with nothing stored: only "never exit 0" is asserted (10 and 20
  are both accepted); a zen `check` mechanism (auth store vs `providers list`)
  is not fixed, so positives set both signals consistently.
- What a go-only credential means to zen `check` is NOT asserted.
- "Unknown capacity" for `budget`: the existing form, exit 0 and a JSON object
  `{"known": false, "note": "..."}` with no numeric headroom and no windows
  (the form DI-R4 already uses).
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import os
import shutil
import socketserver
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import yaml

from multiagents.paths import global_config_dir, shipped_defaults_dir

sys.path.insert(0, str(Path(__file__).parent / "support"))
import c3_harness as h3  # noqa: E402

DEFAULTS = shipped_defaults_dir()
SCRIPT = DEFAULTS / "providers" / "opencode.sh"
GO = "opencode"
ZEN = "opencode-zen"
PLAN = "zen"
GO_URL = "opencode.ai/zen/go/v1/usage"
ZEN_KEY = "zen-test-1234567890abcdef.SECRETtail"
GO_KEY = "go-key-NEVER-SEND-UNDER-ZEN-9876"
GO_MODEL = "opencode-go/glm-5.1"
ZEN_MODEL = "opencode/space-bunny-free"
SAME_NAME_GO = "opencode-go/space-bunny-free"     # the outage: same model, two namespaces
RUN_TIMEOUT = 20          # seconds, upper bound on any run / script wait
GO_REPLY_USED_UP = '{"usage": {"weekly": {"percent": 100, "resetsAt": "2099-01-01T00:00:00Z"}}}'
GO_REPLY_40 = '{"usage": {"weekly": {"percent": 40, "resetsAt": "2099-01-01T00:00:00Z"}}}'


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------

def _exe(path: Path, body: str, shebang: str = "#!/bin/sh\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(shebang + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


class _ProxyRecorder:
    """A local fake HTTP endpoint: records the first line of every request it
    receives and answers 503. Used as the proxy, so a call that bypasses the
    curl shim (python urllib, wget...) and tries to reach opencode.ai is
    still visible."""

    def __init__(self):
        seen = self.seen = []

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                try:
                    line = self.rfile.readline(4096).decode("latin1", "replace").strip()
                    seen.append(line)
                    self.wfile.write(b"HTTP/1.1 503 no\r\nContent-Length: 0\r\n"
                                     b"Connection: close\r\n\r\n")
                except OSError:
                    pass

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def proxy(request):
    p = _ProxyRecorder()
    request.addfinalizer(p.stop)
    return p


class Env:
    """Temporary HOME/XDG_DATA_HOME, a fake `opencode`, a PATH `curl` that
    records every call (and, when `go_reply` is set, answers it as the go usage
    endpoint would), and a recording proxy in the proxy variables."""

    def __init__(self, tmp_path: Path, proxy: _ProxyRecorder):
        self.root = tmp_path
        self.proxy = proxy
        self.data = tmp_path / "xdg"
        self.home = tmp_path / "home"
        self.bindir = tmp_path / "pathbin"
        for d in (self.data, self.home, self.bindir):
            d.mkdir(parents=True, exist_ok=True)
        self.curl_log = tmp_path / "curl-calls.txt"
        self.bin_log = tmp_path / "bin-calls.txt"
        self.bin = self.set_bin("echo '1 credentials'")
        self.set_go_reply(None)

    def set_bin(self, body: str) -> Path:
        self.bin = _exe(self.root / "opencode", f'echo "ARGS:$*" >> "{self.bin_log}"\n{body}\n')
        return self.bin

    def set_go_reply(self, reply: str | None) -> None:
        """Without a reply curl fails (7) like an unreachable host."""
        out = "exit 7"
        if reply is not None:
            (self.root / "go-reply.json").write_text(reply)
            out = f'cat "{self.root / "go-reply.json"}"'
        _exe(self.bindir / "curl",
             f'echo "CURL $*" >> "{self.curl_log}"; cat >> "{self.curl_log}" 2>/dev/null; {out}\n')

    @property
    def curl_calls(self) -> list[str]:
        return self.curl_log.read_text().splitlines() if self.curl_log.exists() else []

    @property
    def go_contacts(self) -> list[str]:
        """Every observed attempt to reach the go usage endpoint."""
        hits = [c for c in self.curl_calls if "opencode.ai" in c or "/usage" in c]
        return hits + [s for s in self.proxy.seen if "opencode.ai" in s or "/usage" in s]

    @property
    def any_contacts(self) -> list[str]:
        return self.curl_calls + self.proxy.seen

    def store(self, entries) -> None:
        d = self.data / "opencode"
        d.mkdir(parents=True, exist_ok=True)
        (d / "auth.json").write_text(entries if isinstance(entries, str)
                                     else json.dumps(entries))

    def zen_store(self, **extra) -> None:
        self.store({"opencode": {"type": "api", "key": ZEN_KEY}, **extra})

    def env(self, plan=PLAN, extra=None) -> dict:
        env = {k: v for k, v in os.environ.items()
               if not k.lower().endswith("_proxy") and not k.startswith("MULTIAGENTS_")
               and k not in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME")}
        env.update(HOME=str(self.home), XDG_DATA_HOME=str(self.data), TZ="UTC",
                   PATH=f"{self.bindir}{os.pathsep}/usr/bin{os.pathsep}/bin",
                   MULTIAGENTS_BIN=str(self.bin),
                   http_proxy=self.proxy.url, https_proxy=self.proxy.url,
                   HTTP_PROXY=self.proxy.url, HTTPS_PROXY=self.proxy.url,
                   no_proxy="", NO_PROXY="")
        if plan is not None:
            env["MULTIAGENTS_OPENCODE_PLAN"] = plan
        env.update(extra or {})
        return env

    def run(self, action, *, plan=PLAN, extra=None, timeout=30):
        return subprocess.run(["sh", str(SCRIPT), action], env=self.env(plan, extra),
                              text=True, stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=timeout)


@pytest.fixture
def env(tmp_path, proxy):
    return Env(tmp_path, proxy)


def assert_no_key(cp, *keys):
    for k in keys or (ZEN_KEY,):
        assert k not in cp.stdout and k not in cp.stderr, (
            f"a key leaked:\nstdout={cp.stdout!r}\nstderr={cp.stderr!r}")


def _shipped_raw():
    return yaml.safe_load((DEFAULTS / "providers.yaml").read_text())["providers"]


def _load(raw):
    from multiagents.providers import load_providers
    return load_providers(copy.deepcopy(raw))


def _zen():
    p = _load(_shipped_raw()).get(ZEN)
    assert p is not None, "the shipped providers.yaml has no `opencode-zen` provider"
    return p


# ===========================================================================
# OZ-R1: the shipped provider
# ===========================================================================

def test_oz_r1_shipped_file_defines_the_provider_disabled_with_family_and_includes():
    p = _zen()
    assert p.enabled is False, "disabled by default, like the other opencode instances"
    assert p.family == ZEN
    assert list(p.models_include) == ["opencode/*"]
    assert p.env.get("MULTIAGENTS_OPENCODE_PLAN") == PLAN


def test_oz_r1_extends_opencode_so_it_inherits_spawn_stream_script_and_usage_mode():
    ps = _load(_shipped_raw())
    z, o = ps.get(ZEN), ps[GO]
    assert z is not None, "no `opencode-zen` provider"
    assert z.spawn == o.spawn and z.spawn.get("args")
    assert z.stream == o.stream and z.stream.get("rules")
    assert z.bin == o.bin
    assert z.auth == o.auth, "zen must use the same opencode.sh"
    assert z.usage_mode == o.usage_mode == "delta"


def test_oz_r1_the_project_override_reduces_to_enabled_true():
    raw = _shipped_raw()
    assert ZEN in raw, "no `opencode-zen` provider"
    raw[ZEN] = {**raw[ZEN], "enabled": True}      # what the project file now says
    p = _load(raw)[ZEN]
    assert p.enabled is True
    assert p.family == ZEN and list(p.models_include) == ["opencode/*"]
    assert p.env.get("MULTIAGENTS_OPENCODE_PLAN") == PLAN


def test_oz_r1_a_project_file_of_just_enabled_true_resolves_through_the_real_merge(
        tmp_path, monkeypatch):
    """The project override is `opencode-zen: {enabled: true}` and nothing else;
    the merged configuration must hold the shipped family, includes and plan."""
    from multiagents import config as config_mod
    from multiagents.paths import ProjectPaths
    h3.as_root(monkeypatch)
    paths = ProjectPaths(tmp_path)
    paths.ensure()
    paths.config.mkdir(parents=True, exist_ok=True)
    (paths.config / "providers.yaml").write_text(
        yaml.safe_dump({"providers": {ZEN: {"enabled": True}}}))
    cfg = config_mod.load(paths, seed=False)
    p = cfg.providers.get(ZEN)
    assert p is not None, "the shipped provider did not survive the project merge"
    raw = p if isinstance(p, dict) else None
    get = (lambda k: raw.get(k)) if raw else (lambda k: getattr(p, k, None))   # noqa: E731
    assert get("enabled") is True
    assert get("family") == ZEN
    assert list(get("models_include")) == ["opencode/*"]
    assert (get("env") or {}).get("MULTIAGENTS_OPENCODE_PLAN") == PLAN


def test_oz_r1_zen_is_its_own_failover_family():
    from multiagents.providers import families
    fam = families(_load(_shipped_raw()))
    assert fam.get(ZEN) == [ZEN], fam
    assert ZEN not in fam.get(GO, []), fam
    assert ZEN not in fam.get("opencode-zai", []) and ZEN not in fam.get("opencode-deepinfra", [])


def test_oz_r1_no_other_provider_gets_the_zen_plan_and_the_roster_never_pins_it():
    ps = _load(_shipped_raw())
    assert ps.get(ZEN) is not None
    for name, p in ps.items():
        if name != ZEN:
            assert (p.env or {}).get("MULTIAGENTS_OPENCODE_PLAN") != PLAN, name
    agents = yaml.safe_load((DEFAULTS / "agents.yaml").read_text())["agents"]
    assert ZEN not in json.dumps(agents), "a provider that ships disabled is not in the roster"


def test_oz_r1_other_opencode_instances_keep_their_own_settings():
    ps = _load(_shipped_raw())
    assert ps.get(ZEN) is not None
    z, d = ps["opencode-zai"], ps["opencode-deepinfra"]
    assert list(z.models_include) == ["zai-coding-plan/*"] and z.family == "opencode-zai"
    assert z.env["MULTIAGENTS_OPENCODE_PLAN"] == "zai-coding-plan" and z.enabled is False
    assert list(d.models_include) == ["deepinfra/*"] and d.family == "opencode-deepinfra"
    assert d.env["MULTIAGENTS_OPENCODE_PLAN"] == "deepinfra" and d.enabled is False
    assert "MULTIAGENTS_OPENCODE_PLAN" not in (ps[GO].env or {})
    assert ps[GO].family == GO and ps[GO].enabled is not False


# -- refresh-models: the catalogue splits cleanly (R1 + R4) --------------------

def _models_for(tmp_path, monkeypatch, listing, *, zen=True, go=True):
    from multiagents.models import refresh_models
    raw = _shipped_raw()
    assert ZEN in raw, "no `opencode-zen` provider in the shipped file"
    raw = {k: raw[k] for k in (GO, ZEN)}
    raw[ZEN]["enabled"] = zen
    raw[GO]["enabled"] = go
    bindir = tmp_path / "bin"
    _exe(bindir / "opencode", "[ \"$1\" = models ] && printf '%s\\n' " + listing + "\n")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}/usr/bin{os.pathsep}/bin")
    monkeypatch.setenv("HOME", str(tmp_path))
    target = tmp_path / "models.yaml"
    out = refresh_models(_load(raw), target, config_dir=tmp_path / "cfg")
    return yaml.safe_load(target.read_text())["models"], out


def test_oz_r1_r4_refresh_models_splits_zen_and_go_namespaces(tmp_path, monkeypatch):
    models, out = _models_for(
        tmp_path, monkeypatch,
        f"opencode/big-pickle {ZEN_MODEL} {GO_MODEL} {SAME_NAME_GO} deepinfra/x/y zai-coding-plan/g")
    ids = lambda n: sorted(m["id"] for m in models.get(n) or [])   # noqa: E731
    assert ids(ZEN) == sorted(["opencode/big-pickle", ZEN_MODEL]), (models, out)
    assert ids(GO) == sorted([GO_MODEL, SAME_NAME_GO]), (models, out)


def test_oz_r1_a_disabled_zen_is_not_refreshed_and_go_does_not_absorb_its_models(
        tmp_path, monkeypatch):
    models, _ = _models_for(tmp_path, monkeypatch, f"{ZEN_MODEL} {GO_MODEL}", zen=False)
    assert ZEN not in (models or {})
    assert [m["id"] for m in models.get(GO) or []] == [GO_MODEL], \
        "opencode/* models must not fall back to the go provider"


# ===========================================================================
# OZ-R4: allowlists, one direction each (provider surface)
# ===========================================================================

GO_IDS = [GO_MODEL, SAME_NAME_GO, "opencode-go/kimi-k2.7-code"]
ZEN_IDS = [ZEN_MODEL, "opencode/big-pickle"]
OTHER_IDS = ["deepinfra/Qwen/Qwen3.8-Max", "zai-coding-plan/glm-4.7", "claude/opus", "glm-5.1"]


def test_oz_r4_the_go_provider_model_set_is_exactly_opencode_go():
    assert _zen() is not None         # the instance must exist for the split to mean anything
    assert list(_load(_shipped_raw())[GO].models_include) == ["opencode-go/*"]


@pytest.mark.parametrize("model", GO_IDS)
def test_oz_r4_go_allows_go_models(model):
    assert _load(_shipped_raw())[GO].allows_model(model)


@pytest.mark.parametrize("model", ZEN_IDS + OTHER_IDS)
def test_oz_r4_go_refuses_zen_and_foreign_models(model):
    assert _load(_shipped_raw())[GO].allows_model(model) is False, model


@pytest.mark.parametrize("model", ZEN_IDS)
def test_oz_r4_zen_allows_zen_models(model):
    assert _zen().allows_model(model)


@pytest.mark.parametrize("model", GO_IDS + OTHER_IDS)
def test_oz_r4_zen_refuses_go_and_foreign_models(model):
    assert _zen().allows_model(model) is False, model


# ===========================================================================
# runner-level fixtures: a go provider and a zen provider over fake CLIs
# ===========================================================================

STEP_FINISH = {"type": "step_finish", "sessionID": "ses_oz1",
               "part": {"type": "step-finish", "reason": "stop", "cost": 0,
                        "tokens": {"input": 10, "output": 5, "reasoning": 0,
                                   "cache": {"read": 0, "write": 0}}}}
TEXT = {"type": "text", "sessionID": "ses_oz1", "part": {"type": "text", "text": "done"}}


def _fake_cli(path: Path, argv_file: Path, *, fail: bool = False) -> Path:
    """Records the argv of a run (one per line); `providers list` answers as a
    logged-in opencode. With `fail` a run exits 1 after one stderr line."""
    body = (
        "import json, sys\n"
        "if sys.argv[1:3] == ['providers', 'list']:\n"
        "    print('2 credentials'); raise SystemExit(0)\n"
        f"open({str(argv_file)!r}, 'a').write('\\n'.join(sys.argv[1:]) + '\\n--\\n')\n"
        + ("sys.stderr.write('Unexpected server error\\n'); raise SystemExit(1)\n" if fail else
           f"for e in {[TEXT, STEP_FINISH]!r}:\n    print(json.dumps(e)); sys.stdout.flush()\n"))
    return _exe(path, body, shebang=f"#!{sys.executable}\n")


class World:
    """One Runner serving both providers. Each provider has its own fake CLI
    and its own argv log, so which provider actually launched is observable."""

    def __init__(self, tmp_path, monkeypatch, proxy, *, go_fails=False, zen_fails=False):
        from multiagents.providers import resolve_inheritance
        self.tmp = tmp_path
        self.env = Env(tmp_path / "e", proxy)
        self.env.zen_store(**{"opencode-go": {"type": "api", "key": GO_KEY}})
        monkeypatch.setenv("HOME", str(self.env.home))
        monkeypatch.setenv("XDG_DATA_HOME", str(self.env.data))
        monkeypatch.setenv("PATH", f"{self.env.bindir}{os.pathsep}/usr/bin{os.pathsep}/bin")
        for var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            monkeypatch.setenv(var, proxy.url)
        dest = global_config_dir() / "providers" / "opencode.sh"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(SCRIPT, dest)
        raw = _shipped_raw()
        assert ZEN in raw, "no `opencode-zen` provider in the shipped file"
        resolved = resolve_inheritance(copy.deepcopy(raw))
        self.argv = {GO: tmp_path / "go-argv.txt", ZEN: tmp_path / "zen-argv.txt"}
        providers = {}
        for name, fail in ((GO, go_fails), (ZEN, zen_fails)):
            d = copy.deepcopy(resolved[name])
            d.pop("extends", None)
            d["bin"] = str(_fake_cli(tmp_path / "clis" / name, self.argv[name], fail=fail))
            d["bin_search"] = []
            d["enabled"] = True
            providers[name] = d
        agents = {
            "go_worker": h3.AgentSpec(name="go_worker", provider=GO, model=GO_MODEL),
            "zen_worker": h3.AgentSpec(name="zen_worker", provider=ZEN, model=ZEN_MODEL),
        }
        self.runner = h3.make_runner(tmp_path / "proj", monkeypatch, agents=agents,
                                     providers=providers)

    def launched(self, name) -> list[list[str]]:
        f = self.argv[name]
        if not f.exists():
            return []
        return [chunk.split() for chunk in f.read_text().split("--\n") if chunk.strip()]

    def start(self, agent, task="go", **kw):
        """`start`'s result, or {"raised": exc, "error": text} when it raises."""
        async def go():
            try:
                result = await self.runner.start(agent, task, **kw)
            except Exception as exc:                   # a refusal may raise
                return {"raised": exc, "error": str(exc)}
            run = self.runner.runs.get(result.get("agent_id", ""))
            if run is not None:
                await asyncio.wait_for(run.done.wait(), timeout=RUN_TIMEOUT)
            return result
        return asyncio.run(go())

    def close(self):
        async def stop():
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.runner.shutdown(detach=False), timeout=10)
        with contextlib.suppress(Exception):
            asyncio.run(stop())


@pytest.fixture
def world_factory(tmp_path, monkeypatch, proxy, request):
    made = []

    def make(**kw):
        w = World(tmp_path, monkeypatch, proxy, **kw)
        made.append(w)
        request.addfinalizer(w.close)
        return w
    return make


def _refused(result) -> bool:
    """Not admitted: a raised refusal, an `error`, or a deferral (`deferred`),
    in every case with no agent started."""
    return not result.get("agent_id") and not result.get("reply")


# ===========================================================================
# OZ-R4: routing, one direction each (runner surface)
# ===========================================================================

def test_oz_r4_a_go_provider_never_launches_a_zen_model_id(world_factory):
    w = world_factory()
    result = w.start("go_worker", model=ZEN_MODEL)
    assert _refused(result), f"the go provider accepted a zen model: {result}"
    assert w.launched(GO) == [] and w.launched(ZEN) == [], "something launched"


def test_oz_r4_zen_never_launches_a_go_model_id(world_factory):
    w = world_factory()
    result = w.start("zen_worker", model=GO_MODEL)
    assert _refused(result), f"the zen provider accepted a go model: {result}"
    assert w.launched(GO) == [] and w.launched(ZEN) == [], "something launched"


def test_oz_r4_the_same_model_name_in_the_wrong_namespace_is_refused_both_ways(world_factory):
    """The outage model: `space-bunny-free` exists under both prefixes."""
    w = world_factory()
    assert _refused(w.start("go_worker", model=ZEN_MODEL))
    assert _refused(w.start("zen_worker", model=SAME_NAME_GO))
    assert w.launched(GO) == [] and w.launched(ZEN) == []


def test_oz_r4_each_provider_runs_its_own_namespace_with_the_exact_model_argument(world_factory):
    w = world_factory()
    ok_go = w.start("go_worker", model=SAME_NAME_GO)
    ok_zen = w.start("zen_worker", model=ZEN_MODEL)
    assert not _refused(ok_go) and not _refused(ok_zen), (ok_go, ok_zen)
    (go_argv,), (zen_argv,) = w.launched(GO), w.launched(ZEN)
    assert go_argv[go_argv.index("-m") + 1] == SAME_NAME_GO, go_argv
    assert zen_argv[zen_argv.index("-m") + 1] == ZEN_MODEL, zen_argv


def test_oz_r4_a_roster_model_from_the_wrong_namespace_never_launches(
        tmp_path, monkeypatch, proxy, world_factory):
    """Not an override: the agent's own configured model is a zen id on the go provider."""
    w = world_factory()
    w.runner.config.agents["misrouted"] = h3.AgentSpec(
        name="misrouted", provider=GO, model=ZEN_MODEL)
    assert _refused(w.start("misrouted")), "go launched a zen model from the roster"
    assert w.launched(GO) == []


# ===========================================================================
# OZ-R2: failures are per family
# ===========================================================================

def _trip(w: World, provider: str) -> None:
    """Trips the circuit breaker the way a run of failures does: through the
    tree's own breaker entry points."""
    tree = w.runner.tree
    trip = None
    for _ in range(3):
        trip = tree.note_run_outcome(provider, ok=False, threshold=3) or trip
    assert trip is not None, "the breaker did not trip after three failures"
    tree.set_cooldown(provider, time.time() + 1800, "3 runs in a row failed",
                      cause="provider_down")
    assert tree.cooldown(provider), "precondition: the breaker is open"


def test_oz_r2_control_a_tripped_go_breaker_does_refuse_go(world_factory):
    """Guards the other R2 tests: if tripping did nothing they would pass for free."""
    w = world_factory()
    _trip(w, GO)
    assert _refused(w.start("go_worker")), "the tripped go provider was still admitted"
    assert w.launched(GO) == []


def test_oz_r2_a_tripped_go_breaker_still_admits_a_node_pinned_to_zen(world_factory):
    w = world_factory()
    _trip(w, GO)
    result = w.start("zen_worker")
    assert not _refused(result), f"zen was refused because go tripped: {result}"
    assert len(w.launched(ZEN)) == 1
    assert w.runner.tree.cooldown(ZEN) is None, "a go failure put zen into cooldown"


def test_oz_r2_a_tripped_zen_breaker_still_admits_a_node_pinned_to_go(world_factory):
    w = world_factory()
    _trip(w, ZEN)
    assert _refused(w.start("zen_worker")), "control: the tripped zen was admitted"
    result = w.start("go_worker")
    assert not _refused(result), f"go was refused because zen tripped: {result}"
    assert len(w.launched(GO)) == 1
    assert w.runner.tree.cooldown(GO) is None


def test_oz_r2_failure_counts_are_not_shared(world_factory):
    w = world_factory()
    tree = w.runner.tree
    for _ in range(2):
        tree.note_run_outcome(GO, ok=False, threshold=3)
    assert tree.note_run_outcome(ZEN, ok=False, threshold=3) is None
    health = tree.provider_health()
    assert health[GO]["consecutive_failures"] == 2
    assert health[ZEN]["consecutive_failures"] == 1
    # a zen success must not reset go's streak either
    tree.note_run_outcome(ZEN, ok=True, threshold=3)
    assert tree.provider_health()[GO]["consecutive_failures"] == 2


def test_oz_r2_real_go_failures_trip_go_only_and_zen_keeps_working(world_factory):
    """End to end, as in the outage: every go call ends in 'Unexpected server error'."""
    w = world_factory(go_fails=True)
    for _ in range(3):
        w.start("go_worker")
    assert w.runner.tree.cooldown(GO), "three failed go runs did not trip the go breaker"
    assert w.runner.tree.cooldown(ZEN) is None, "go's breaker cooled zen"
    result = w.start("zen_worker")
    assert not _refused(result), f"zen refused after go failed: {result}"
    assert len(w.launched(ZEN)) == 1


def test_oz_r2_real_zen_failures_trip_zen_only_and_go_keeps_working(world_factory):
    w = world_factory(zen_fails=True)
    for _ in range(3):
        w.start("zen_worker")
    assert w.runner.tree.cooldown(ZEN), "three failed zen runs did not trip the zen breaker"
    assert w.runner.tree.cooldown(GO) is None
    assert not _refused(w.start("go_worker"))
    assert len(w.launched(GO)) == 1


def test_oz_r2_both_breakers_open_each_refuses_independently_and_clearing_one_frees_only_it(
        world_factory):
    w = world_factory()
    _trip(w, GO)
    _trip(w, ZEN)
    assert _refused(w.start("go_worker")) and _refused(w.start("zen_worker"))
    assert w.runner.tree.clear_cooldown(ZEN)
    assert not _refused(w.start("zen_worker"))
    assert _refused(w.start("go_worker")), "clearing zen's breaker freed go's"


def test_oz_r2_exhausted_go_headroom_does_not_refuse_zen(world_factory):
    """Headroom is per family: go reports 100% used, zen has no quota surface."""
    w = world_factory()
    w.env.set_go_reply(GO_REPLY_USED_UP)
    assert _refused(w.start("go_worker")), "control: exhausted go quota did not stop go"
    result = w.start("zen_worker")
    assert not _refused(result), f"go's exhausted quota refused zen: {result}"
    assert len(w.launched(ZEN)) == 1


# ===========================================================================
# OZ-R3: opencode.sh under MULTIAGENTS_OPENCODE_PLAN=zen
# ===========================================================================

def _budget(env, **kw):
    cp = env.run("budget", **kw)
    assert cp.returncode == 0, (cp.returncode, cp.stdout, cp.stderr)
    return cp, json.loads(cp.stdout)


def _assert_unknown_capacity(b):
    assert b.get("known") is False, b
    assert b.get("headroom") is None, f"unknown capacity must not carry a number: {b}"
    assert not b.get("windows"), b
    assert isinstance(b.get("note"), str) and b["note"], b


def _no_go_numbers(text):
    low = text.lower()
    assert GO_URL not in low and "opencode.ai" not in low, text
    assert "40%" not in text and "weekly" not in low, f"go's figures surfaced: {text}"


# -- budget --------------------------------------------------------------------

def test_oz_r3_budget_reports_unknown_capacity_and_never_contacts_go(env):
    env.zen_store()
    env.set_go_reply(GO_REPLY_40)       # if it asks, it would get a believable go answer
    cp, b = _budget(env)
    _assert_unknown_capacity(b)
    assert env.go_contacts == [], f"the go usage endpoint was contacted: {env.go_contacts}"
    assert env.any_contacts == [], f"budget made a network call: {env.any_contacts}"
    _no_go_numbers(cp.stdout)
    assert_no_key(cp)


def test_oz_r3_budget_with_a_go_key_in_the_store_sends_it_nowhere(env):
    env.store({"opencode-go": {"type": "api", "key": GO_KEY},
               "opencode": {"type": "api", "key": ZEN_KEY}})
    env.set_go_reply(GO_REPLY_40)
    cp, b = _budget(env)
    _assert_unknown_capacity(b)
    assert env.any_contacts == []
    assert_no_key(cp, GO_KEY, ZEN_KEY)


def test_oz_r3_budget_with_only_a_go_key_stored_is_still_unknown_and_silent(env):
    """The go key is the exact thing the fall-through used to send to the go URL."""
    env.store({"opencode-go": {"type": "api", "key": GO_KEY}})
    env.set_go_reply(GO_REPLY_40)
    cp, b = _budget(env)
    _assert_unknown_capacity(b)
    assert env.any_contacts == []
    assert_no_key(cp, GO_KEY)


def test_oz_r3_budget_without_a_store_is_unknown_and_silent(env):
    env.set_go_reply(GO_REPLY_40)
    _, b = _budget(env)
    _assert_unknown_capacity(b)
    assert env.any_contacts == []


@pytest.mark.parametrize("store", ["{ not json", "[1, 2]", '{"opencode": {"key": ""}}', "null"])
def test_oz_r3_budget_with_a_broken_store_is_still_exit_0_unknown(env, store):
    env.store(store)
    env.set_go_reply(GO_REPLY_40)
    _, b = _budget(env)
    _assert_unknown_capacity(b)
    assert env.any_contacts == []


def test_oz_r3_budget_is_one_json_object_and_repeatable(env):
    env.zen_store()
    a, _ = _budget(env)
    b, _ = _budget(env)
    assert a.stdout == b.stdout and len(a.stdout.strip().splitlines()) == 1
    assert env.any_contacts == []


def test_oz_r3_budget_ignores_a_go_looking_reply_even_if_every_endpoint_answers(env):
    """Whatever answers on the network, the zen reading is not derived from it."""
    env.zen_store()
    env.set_go_reply('{"usage": {"rolling": {"percent": 1}, "weekly": '
                     '{"percent": 2}, "monthly": {"percent": 3}}}')
    _, b = _budget(env)
    assert b.get("known") is False and b.get("headroom") is None, b
    assert "source" not in b or GO_URL not in str(b.get("source"))


def test_oz_r3_control_without_the_plan_the_same_setup_does_probe_go(env):
    """Proves the recorders can see a go call, so the silence above means something."""
    env.store({"opencode-go": {"type": "api", "key": GO_KEY}})
    env.set_go_reply(GO_REPLY_40)
    _, b = _budget(env, plan=None)
    assert b["known"] is True and b["headroom"] == 0.6
    assert any(GO_URL in c for c in env.curl_calls), env.curl_calls


def test_oz_r3_other_plan_values_still_get_the_go_behaviour(env):
    env.store({"opencode-go": {"type": "api", "key": GO_KEY}})
    env.set_go_reply(GO_REPLY_40)
    for plan in ("", "Zen", "ZEN", "zen ", "opencode-zen"):
        env.curl_log.unlink(missing_ok=True)
        _, b = _budget(env, plan=plan)
        assert b.get("known") is True, (plan, b)
        assert any(GO_URL in c for c in env.curl_calls), (plan, env.curl_calls)


# -- check ---------------------------------------------------------------------

def test_oz_r3_check_with_a_stored_credential_is_logged_in(env):
    env.zen_store()
    env.set_bin("echo '1 credentials'")
    cp = env.run("check")
    assert cp.returncode == 0, (cp.stdout, cp.stderr)
    assert_no_key(cp)
    assert env.any_contacts == [], "check must not use the network"


def test_oz_r3_check_with_nothing_stored_is_not_logged_in(env):
    env.set_bin("echo '0 credentials'")
    cp = env.run("check")
    assert cp.returncode in (10, 20), (cp.returncode, cp.stdout, cp.stderr)
    assert (cp.stdout + cp.stderr).strip()
    assert env.go_contacts == []


def test_oz_r3_check_missing_store_and_a_binary_that_says_nothing_is_never_logged_in(env):
    """AU's rule: unknown is never reported as logged in."""
    env.set_bin("exit 0")                  # empty output, exit 0
    cp = env.run("check")
    assert cp.returncode != 0, (cp.stdout, cp.stderr)


def test_oz_r3_check_with_a_failing_binary_and_no_store_is_never_logged_in(env):
    env.set_bin("exit 1")
    cp = env.run("check")
    assert cp.returncode != 0, (cp.stdout, cp.stderr)


@pytest.mark.parametrize("store", ["{ garbage", "[1, 2, 3]", '{"opencode": {"key": ""}}',
                                   '{"opencode": null}', '{"opencode": "just-a-string"}'])
def test_oz_r3_check_with_an_unusable_store_and_unrecognisable_binary_is_never_logged_in(
        env, store):
    env.store(store)
    env.set_bin("echo 'something nobody agreed to parse'")
    cp = env.run("check")
    assert cp.returncode != 0, (cp.stdout, cp.stderr)
    assert "Traceback" not in cp.stdout + cp.stderr
    assert_no_key(cp)


def test_oz_r3_check_is_repeatable_and_never_contacts_go(env):
    env.zen_store()
    a, b = env.run("check"), env.run("check")
    assert (a.returncode, a.stdout) == (b.returncode, b.stdout)
    assert env.any_contacts == []


def test_oz_r3_the_go_check_is_unchanged_without_the_plan(env):
    env.set_bin("echo 'x 2 credentials'")
    cp = env.run("check", plan=None)
    assert cp.returncode == 0 and "2 stored credential" in cp.stdout
    env.set_bin("echo '0 credentials'")
    cp = env.run("check", plan=None)
    assert cp.returncode == 10 and "no stored credentials" in cp.stdout


# -- usage ---------------------------------------------------------------------

def test_oz_r3_usage_never_contacts_go_and_prints_none_of_its_numbers(env):
    env.zen_store()
    env.set_go_reply(GO_REPLY_40)
    cp = env.run("usage")
    assert cp.returncode in (0, 64), (cp.returncode, cp.stdout, cp.stderr)
    assert env.any_contacts == [], env.any_contacts
    _no_go_numbers(cp.stdout + cp.stderr)
    assert_no_key(cp, ZEN_KEY, GO_KEY)


def test_oz_r3_usage_fed_zens_own_unknown_budget_reports_no_capacity_figures(env):
    env.zen_store()
    env.set_go_reply(GO_REPLY_40)
    _, b = _budget(env)
    cp = env.run("usage", extra={"MULTIAGENTS_BUDGET": json.dumps(b)})
    assert cp.returncode in (0, 64), (cp.returncode, cp.stdout, cp.stderr)
    _no_go_numbers(cp.stdout)
    assert "%" not in cp.stdout, f"a percentage appeared for a provider with no quota: {cp.stdout}"
    assert env.any_contacts == []


def test_oz_r3_usage_with_a_go_credential_only_does_not_use_it(env):
    env.store({"opencode-go": {"type": "api", "key": GO_KEY}})
    env.set_go_reply(GO_REPLY_40)
    cp = env.run("usage")
    assert cp.returncode in (0, 64)
    assert env.any_contacts == []
    assert_no_key(cp, GO_KEY)


def test_oz_r3_usage_without_the_plan_is_unchanged(env):
    cp = env.run("usage", plan=None,
                 extra={"MULTIAGENTS_BUDGET": json.dumps({"known": False, "note": "free tier"})})
    assert cp.returncode == 0 and cp.stdout.strip() == "free tier"


# -- the other actions are untouched -------------------------------------------

def test_oz_r3_login_under_zen_runs_providers_login(env):
    env.set_bin("echo FAKE-LOGIN-RAN")
    cp = env.run("login")
    assert cp.returncode == 0 and "FAKE-LOGIN-RAN" in cp.stdout
    assert "ARGS:providers login" in env.bin_log.read_text()


def test_oz_r3_zai_and_deepinfra_plans_do_not_change(env):
    env.store({"opencode-go": {"key": GO_KEY}})
    env.set_go_reply(GO_REPLY_40)
    cp = env.run("budget", plan="deepinfra")
    assert cp.returncode == 0 and json.loads(cp.stdout).get("known") is False
    assert env.any_contacts == []
    cp = env.run("check", plan="zai-coding-plan")
    assert cp.returncode == 10 and "opencode-zai" in cp.stdout + cp.stderr
