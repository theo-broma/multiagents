"""Harness for the spend-cap contract tests (SC-R*), `context/specs/spend-caps.md`.

Everything goes through public surfaces: a real project on disk with
`.multiagents/config/*.yaml` that the test edits, the MCP tool functions of
`multiagents.server` called as a host would, and fake provider CLIs that print
opencode-style NDJSON (`step_finish` parts with a unique `part.id` and a USD
`part.cost`). No network. The state root and HOME are already redirected per
test by `conftest.py`.

The fake provider is a copy of the *shipped* `opencode` block (spawn args,
resume args, stream rules, `usage_mode`) with `bin` pointed at a script, so
whatever the implementation adds to the shipped stream rules to capture the
step id is inherited here rather than guessed. `billing` is left at the
default (`metered`) unless a test says `plan`.

The fake CLI is steered by a per-provider control file, rewritten between
runs by the test:

    {"steps": [{"cost": 0.4, "id": "prt_a", "sleep": 0.0}, ...],
     "text": "done", "exit": 0}

`id` defaults to a value unique per invocation and step. Every invocation
appends `{"argv": [...]}` to `<name>.calls`, so "the CLI was (not) spawned"
is observable as a file, not as a mock call count.

The clock. Period boundaries (UTC day / Monday week / month) need a chosen
"now", so `OffsetClock` moves `time.time` and `datetime.datetime.now/utcnow`
by a fixed offset from the real clock (it keeps ticking, so deadlines and
waits still work). It is the generic "what time is it" seam, not an internal.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import stat
import sys
import time as _time
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import c3_harness as h  # noqa: E402
from multiagents.paths import shipped_defaults_dir  # noqa: E402

UTC = _dt.timezone.utc


def utc(*args) -> float:
    return _dt.datetime(*args, tzinfo=UTC).timestamp()


# A Wednesday, mid-month, mid-day. Monday of that week is 2026-09-14.
WED = utc(2026, 9, 16, 12, 0, 0)


class OffsetClock:
    def __init__(self, monkeypatch, at: float = WED):
        self._real_time = _time.time
        self.offset = at - self._real_time()
        real_dt = _dt.datetime
        clock = self

        class _DT(real_dt):
            @classmethod
            def now(cls, tz=None):
                return real_dt.fromtimestamp(clock.now(), tz or None) if tz \
                    else real_dt.fromtimestamp(clock.now())

            @classmethod
            def utcnow(cls):
                return real_dt.fromtimestamp(clock.now(), UTC).replace(tzinfo=None)

            @classmethod
            def today(cls):
                return real_dt.fromtimestamp(clock.now())

        monkeypatch.setattr(_time, "time", self.now)
        monkeypatch.setattr(_time, "time_ns", lambda: int(self.now() * 1e9))
        monkeypatch.setattr(_dt, "datetime", _DT)
        for name, mod in list(sys.modules.items()):
            if name.startswith("multiagents") and getattr(mod, "datetime", None) is real_dt:
                monkeypatch.setattr(mod, "datetime", _DT)

    def now(self) -> float:
        return self._real_time() + self.offset

    def set(self, at: float) -> None:
        self.offset = at - self._real_time()


# ------------------------------------------------------------------ fake CLI

_SCRIPT = r'''#!{python}
import json, os, sys, time
ctl_path, calls_path, name = {ctl!r}, {calls!r}, {name!r}
argv = sys.argv[1:]
with open(calls_path, "a") as f:
    f.write(json.dumps({{"argv": argv, "cwd": os.getcwd()}}) + "\n")
n = sum(1 for _ in open(calls_path))
ctl = json.load(open(ctl_path))
_text = " ".join(argv)
for _key, _variant in ctl.get("variants", {{}}).items():
    if _key in _text:
        ctl = dict(ctl, **_variant)
        break
session = None
if "-s" in argv:
    session = argv[argv.index("-s") + 1]
session = session or "ses_%s_%d" % (name, os.getpid())
def emit(kind, part):
    event = {{"type": kind, "part": dict(part)}}
    if not ctl.get("no_session"):
        event["sessionID"] = session
        event["part"]["sessionID"] = session
    print(json.dumps(event))
    sys.stdout.flush()
for i, step in enumerate(ctl.get("steps", [])):
    emit("step_start", {{"id": "prt_s%d_%d" % (os.getpid(), i), "type": "step-start"}})
    if step.get("sleep"):
        time.sleep(step["sleep"])
    part = {{"type": "step-finish", "reason": "tool-calls",
            "cost": step.get("cost", 0),
            "tokens": {{"input": 100, "output": 10, "reasoning": 0,
                       "cache": {{"read": 0, "write": 0}}}}}}
    if not step.get("noid"):
        part["id"] = step.get("id") or "prt_%s_%d_%d" % (name, os.getpid(), i)
    emit("step_finish", part)
if ctl.get("tail_sleep"):
    time.sleep(ctl["tail_sleep"])
if ctl.get("silent"):
    sys.exit(ctl.get("exit", 0))
with open(calls_path + ".finished", "a") as f:
    f.write("%d\n" % n)
emit("text", {{"id": "prt_t%d" % n, "type": "text", "text": ctl.get("text", "done")}})
sys.exit(ctl.get("exit", 0))
'''


def shipped_opencode() -> dict[str, Any]:
    raw = yaml.safe_load((shipped_defaults_dir() / "providers.yaml").read_text())
    return json.loads(json.dumps(raw["providers"]["opencode"]))


def steps_of(costs, sleep: float = 0.0, ids: list[str] | None = None,
             noid: bool = False) -> list[dict]:
    return [{"cost": c, "sleep": sleep, **({"id": ids[i]} if ids else {}),
             **({"noid": True} if noid else {})} for i, c in enumerate(costs)]


class FakeProvider:
    """One fake opencode-style CLI. `.entry` is its providers.yaml block."""

    def __init__(self, tmp: Path, name: str, **extra: Any):
        self.name = name
        self.ctl = tmp / f"{name}.ctl.json"
        self.calls = tmp / f"{name}.calls"
        self.calls.write_text("")
        script = tmp / f"{name}-cli.py"
        script.write_text(_SCRIPT.format(python=sys.executable, ctl=str(self.ctl),
                                         calls=str(self.calls), name=name))
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        base = shipped_opencode()
        for key in ("auth", "mcp", "home_links", "bin_search", "models_cmd",
                    "models_parse", "models_include", "notes"):
            base.pop(key, None)
        base["bin"] = str(script)
        base.update(extra)
        self.entry = base
        self.script(steps=[])

    def script(self, steps=(), text="done", exit=0, tail_sleep=0.0,
               variants: dict | None = None, no_session=False, silent=False) -> None:
        """What the next invocations do. `variants` maps a substring of the
        argv (the task text, or the model) to overrides of these settings.
        `silent` ends the process without its final text line (a death with
        nothing to say, the shape the runner's free retry looks for)."""
        self.ctl.write_text(json.dumps({"steps": list(steps), "text": text,
                                        "exit": exit, "tail_sleep": tail_sleep,
                                        "variants": variants or {},
                                        "no_session": no_session, "silent": silent}))

    def costs(self, *costs: float, sleep: float = 0.0, ids: list[str] | None = None,
              **kw: Any) -> None:
        self.script(steps=steps_of(costs, sleep=sleep, ids=ids), **kw)

    def finished(self) -> int:
        """How many invocations ran to their natural end (printed their last
        line); a stopped one never does."""
        path = Path(str(self.calls) + ".finished")
        return len(path.read_text().split()) if path.is_file() else 0

    def spawns(self) -> int:
        return sum(1 for line in self.calls.read_text().splitlines() if line.strip())

    def argv_of_spawn(self, k: int = -1) -> list[str]:
        return json.loads(self.calls.read_text().splitlines()[k])["argv"]


# ------------------------------------------------------------------- project

class Project:
    """A real project directory whose config layer the test rewrites."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.root = tmp / "proj"
        h.make_git_repo(self.root)
        self.config = self.root / ".multiagents" / "config"
        (self.config / "agents").mkdir(parents=True)
        self.events = self.root / ".multiagents" / "events.jsonl"
        self._tick = _time.time()
        self.fakes: dict[str, FakeProvider] = {}
        self.providers: dict[str, Any] = {}
        self.agents: dict[str, Any] = {}
        self.project: dict[str, Any] = {"team": "", "limits": {"max_depth": 7},
                                        "budget": {"blind_cooldown_seconds": 1}}
        self.models: dict[str, Any] = {}

    def add_provider(self, name: str, **extra: Any) -> FakeProvider:
        spend_cap = extra.pop("spend_cap", None)
        fake = FakeProvider(self.tmp, name, **extra)
        self.fakes[name] = fake
        self.providers[name] = fake.entry
        if spend_cap is not None:
            self.providers[name]["spend_cap"] = spend_cap
        return fake

    def add_agent(self, name: str, provider: str, model: str, **extra: Any) -> None:
        self.agents[name] = {"provider": provider, "model": model, "writes": True, **extra}

    def cap(self, provider: str, cap: Any) -> None:
        if cap is None:
            self.providers[provider].pop("spend_cap", None)
        else:
            self.providers[provider]["spend_cap"] = cap
        self.write()

    def _write(self, rel: str, data: Any) -> None:
        path = self.config / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(data))
        self._tick += 10                      # never two writes in one mtime tick
        os.utime(path, (self._tick, self._tick))

    def write_raw(self, rel: str, text: str) -> None:
        path = self.config / rel
        path.write_text(text)
        self._tick += 10
        os.utime(path, (self._tick, self._tick))

    def write(self) -> None:
        self._write("providers.yaml", {"providers": self.providers})
        self._write("agents.yaml", {"agents": self.agents})
        self._write("project.yaml", self.project)
        self._write("models.yaml", {"models": self.models})

    def event_records(self) -> list[dict]:
        if not self.events.is_file():
            return []
        out = []
        for line in self.events.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out

    def events_of(self, kind: str) -> list[dict]:
        return [e for e in self.event_records()
                if e.get("kind") == kind or e.get("type") == kind or e.get("event") == kind]


# ---------------------------------------------------------------- observation

def strings(value):
    """Every key and scalar of a nested result, as strings."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from strings(v)
    elif value is not None:
        yield str(value)


def mentions(value, needle: str) -> bool:
    return any(needle in s for s in strings(value))


def leaves(value, path=()):
    """(path-tuple, scalar) for every scalar leaf; list indices are omitted."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield from leaves(v, path + (str(k),))
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from leaves(v, path)
    else:
        yield path, value


def scope_subtrees(result, name: str):
    """Sub-structures of `result` that are about `name`: the value stored under
    a dict key equal to `name`, or a dict one of whose values equals `name`
    (a row such as {"model": name, ...}). Names the contract does not fix are
    therefore never required."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == name:
                    found.append(v)
                walk(v)
            if any(v == name for v in node.values() if isinstance(v, str)):
                found.append(node)
        elif isinstance(node, (list, tuple)):
            for v in node:
                walk(v)

    walk(result)
    return found


def numbers_about(result, name: str) -> list[tuple[tuple, float]]:
    out = []
    for sub in scope_subtrees(result, name):
        for path, v in leaves(sub):
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out.append((path, float(v)))
    return out


def has_number(result, name: str, value: float, key_hint: str = "", tol=1e-6) -> bool:
    return any(abs(v - value) <= tol and (not key_hint or any(key_hint in p.lower() for p in path))
               for path, v in numbers_about(result, name))


def find_key(value, key: str):
    """Every value stored under a dict key equal to `key`, anywhere."""
    out = []
    if isinstance(value, dict):
        for k, v in value.items():
            if k == key:
                out.append(v)
            out.extend(find_key(v, key))
    elif isinstance(value, (list, tuple)):
        for v in value:
            out.extend(find_key(v, key))
    return out


def as_epoch(value) -> float | None:
    """A time given as epoch seconds or an ISO-8601 string, as UTC epoch."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            d = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return (d if d.tzinfo else d.replace(tzinfo=UTC)).timestamp()
    return None


def epochs_in(result) -> list[float]:
    out = []
    for _, v in leaves(result):
        e = as_epoch(v)
        if e is not None:
            out.append(e)
    return out


def has_time(result, expected: float, tol: float = 90.0) -> bool:
    return any(abs(e - expected) <= tol for e in epochs_in(result))


# ------------------------------------------------------------------ periods

def _d(ts: float) -> _dt.datetime:
    return _dt.datetime.fromtimestamp(ts, UTC)


def period_start(ts: float, period: str) -> float:
    d = _d(ts)
    day = d.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "day":
        return day.timestamp()
    if period == "week":
        return (day - _dt.timedelta(days=day.weekday())).timestamp()
    if period == "month":
        return day.replace(day=1).timestamp()
    raise ValueError(period)


def period_end(ts: float, period: str) -> float:
    d = _d(period_start(ts, period))
    if period == "day":
        return (d + _dt.timedelta(days=1)).timestamp()
    if period == "week":
        return (d + _dt.timedelta(days=7)).timestamp()
    nxt = d.replace(year=d.year + (d.month == 12), month=d.month % 12 + 1)
    return nxt.timestamp()


# ------------------------------------------------------------------- world

TERMINAL_OR_PAUSED = {"done", "failed", "cancelled", "discarded", "merged", "orphaned",
                      "limited", "truncated", "refused", "idle", "awaiting_user"}


class World:
    """A project with fake metered providers, a server pointed at it and an
    offset clock. Build providers/agents, call `up()`, then drive `server.*`.

        w = World(tmp_path, monkeypatch)
        acme = w.provider("acme", spend_cap={"usd": 1.0})
        w.agent("worker", "acme", "acme/m1")
        w.up()
    """

    def __init__(self, tmp_path: Path, monkeypatch, *, at: float | None = WED):
        from multiagents import server
        self.server = server
        self.monkeypatch = monkeypatch
        h.as_root(monkeypatch)
        self.clock = OffsetClock(monkeypatch, at) if at is not None else None
        self.p = Project(tmp_path)
        self.fakes = self.p.fakes

    # -- building ---------------------------------------------------------
    def provider(self, name: str, models: list[str] | None = None, **extra: Any) -> FakeProvider:
        extra.setdefault("models_include", [f"{name}/*"] if models is None else models)
        return self.p.add_provider(name, **extra)

    def agent(self, name: str, provider: str, model: str, **extra: Any) -> None:
        self.p.add_agent(name, provider, model, **extra)

    def up(self) -> None:
        self.p.write()
        self.monkeypatch.setenv("MULTIAGENTS_PROJECT", str(self.p.root))
        self.server._reset()

    def down(self) -> None:
        self.server._reset()

    def restart_server(self) -> None:
        """A new server process-equivalent: the runner object is rebuilt from disk."""
        self.server._reset()

    def reload(self) -> None:
        """Rewrite the config files from `self.p` and make the server see them."""
        self.p.write()
        self.server.list_agents()

    # -- driving ----------------------------------------------------------
    @property
    def runner(self):
        return self.server.runner()

    def tree(self):
        return self.runner.tree

    def node(self, agent_id: str):
        return self.tree().get(agent_id)

    def status(self, agent_id: str) -> str:
        return self.server.check_agent(agent_id).get("status", "?")

    async def start(self, agent: str, task: str = "do it", **kw: Any) -> dict:
        return await self.server.start_agent(agent, task, **kw)

    async def started(self, agent: str, task: str = "do it", **kw: Any) -> str:
        r = await self.start(agent, task, **kw)
        assert r.get("agent_id"), f"start was not admitted: {r}"
        return r["agent_id"]

    async def until(self, ids, timeout: float = 30.0, states=TERMINAL_OR_PAUSED) -> dict:
        import asyncio
        ids = [ids] if isinstance(ids, str) else list(ids)
        deadline = _time.monotonic() + timeout
        seen: dict[str, str] = {}
        while _time.monotonic() < deadline:
            seen = {i: self.status(i) for i in ids}
            if all(v in states for v in seen.values()):
                return seen
            await asyncio.sleep(0.15)
        raise AssertionError(f"still not settled after {timeout}s: {seen}")

    async def settle(self, timeout: float = 30.0) -> None:
        import asyncio
        for run in list(self.runner.runs.values()):
            await asyncio.wait_for(run.done.wait(), timeout)

    def spawns(self, provider: str) -> int:
        return self.fakes[provider].spawns()

    # -- observing --------------------------------------------------------
    def budget(self) -> dict:
        return self.server.budget_status()

    def deferred(self) -> list[dict]:
        r = self.server.list_deferred()
        return r["deferred"] if isinstance(r, dict) else r

    def agent_view(self, agent_id: str) -> dict:
        """Everything a caller can read about one agent, plus its events."""
        view: dict[str, Any] = {"check": self.server.check_agent(agent_id)}
        try:
            view["collect"] = self.server.collect_agent(agent_id)
        except Exception as exc:                                  # noqa: BLE001
            view["collect"] = {"error": str(exc)}
        view["events"] = [e for e in self.p.event_records() if e.get("agent") == agent_id]
        node = self.node(agent_id)
        view["node"] = {"status": node.status, "reason": node.reason} if node else {}
        return view

    def spend_events(self) -> list[dict]:
        return self.p.events_of("spend_cap")


# ----------------------------------------------- reading budget_status loosely

PERIODS = ("day", "week", "month")


def period_numbers(status: dict, scope: str, period: str) -> list[float]:
    """Numbers about `scope` stored on a path that names `period`."""
    return [v for path, v in numbers_about(status, scope)
            if any(period in part.lower() for part in path)]


def period_spend_is(status: dict, scope: str, period: str, expected: float, tol=1e-6) -> bool:
    return any(abs(v - expected) <= tol for v in period_numbers(status, scope, period))


def partial_periods(status: dict, scope: str) -> set[str]:
    """The periods reported as partial for `scope`: a truthy leaf whose path
    names `partial` and a period, or a `partial` leaf whose value names one."""
    out: set[str] = set()
    for sub in scope_subtrees(status, scope):
        for path, v in leaves(sub):
            joined = "/".join(path).lower()
            if "partial" not in joined:
                continue
            for period in PERIODS:
                if period in joined and v not in (False, None, 0, ""):
                    out.add(period)
            if isinstance(v, str) and v.lower() in PERIODS:
                out.add(v.lower())
    return out


def refused_flag(status: dict, scope: str) -> bool | None:
    """Is admission reported as refused for `scope`? None if nothing says."""
    for sub in scope_subtrees(status, scope):
        for path, v in leaves(sub):
            if not isinstance(v, bool):
                continue
            key = path[-1].lower() if path else ""
            if any(w in key for w in ("refus", "block")):
                return v
            if any(w in key for w in ("admit", "admitted", "avail")):
                return not v
    return None
