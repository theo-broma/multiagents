"""C21: the monitor never waits for quota reads, and reads run in parallel.

Contract: context/specs/c21-fast-quota-reads.md, FQ-R1..R4. Test names carry
the requirement id.

Black box, at the seams the C18/C19 suites already use:

* the readers are fakes plugged in at ``scripts.run_action`` (the ``budget``
  action of every provider), so ``budget.read_all`` runs for real, with its
  cache, its shared-source rule (PS-R5) and its projection; no credential, no
  network, no docker. A fake reader sleeps or blocks on a gate we control.
* ``budget.read_all`` is called directly for FQ-R1/R4;
* the monitor is a real ``ThreadingHTTPServer`` on an ephemeral port with the
  token header, as in the C19/C20 suites, and the TUI is ``Screen.poll``.
* ``budget_status`` is called as the MCP tool, over fake budget scripts.

ASSUMPTIONS the contract leaves open (each is a NEED_INFO in the run report):

* "marked loading" is looked for as the word ``loading`` (any case) in the
  row's serialised text, as the C18 tests look for the install states; a row
  still exists for every provider, with ``budget.known`` not true.
* "marked with its age" after a failed refresh is looked for as one of
  ``stale`` / ``ago`` / ``age`` / ``old`` in the row's serialised text, while
  the last good ``headroom`` and ``windows`` stay in ``budget``.
* A refresh FAILS when ``read_all`` raises. That is injected by wrapping
  ``read_all`` where ``snapshot`` and ``budget`` bind it (both are wrapped, so
  the test does not care which the implementation calls). A reader that merely
  answers "unknown" is a reading, not a failure (that is the existing
  behaviour and is not changed).
* When the monitor starts a refresh is not fixed by the contract beyond "in the
  background, one at a time". The refresh tests therefore set
  ``budget._CACHE_TTL`` small and wait it out, which makes a refresh allowed
  under any reading of "never more often than the cache allows".
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

from multiagents import budget as budget_mod  # noqa: E402
from multiagents import scripts as scripts_mod  # noqa: E402
from multiagents.config import Config  # noqa: E402
from multiagents.monitor import server, snapshot as snap  # noqa: E402
from multiagents.paths import ProjectPaths  # noqa: E402
from multiagents.providers import load_providers  # noqa: E402
from multiagents.tree import Node, Tree  # noqa: E402

pytestmark = pytest.mark.real_providers   # providers_view / read_all are under test

# The real function, captured before any test replaces it.
REAL_READ_ALL = budget_mod.read_all


# --------------------------------------------------------------------------
# fake readers


def payload(i: int) -> dict:
    return {"known": True, "headroom": round(0.9 - i * 0.1, 2), "source": "script",
            "windows": {f"w{i}": {"percent": 10.0 + i}}}


class Readers:
    """Replaces ``scripts.run_action``: answers every ``budget`` action.

    ``delay[name]`` sleeps that long; ``gate`` (an Event), when set, blocks
    every reader until it is opened (bounded, so a bug cannot hang the suite);
    ``fail[name]`` is a callable returning the (code, out, err) to answer.
    Everything else (``usage``, ``identity``...) is "unimplemented" (64).
    """

    def __init__(self, bodies: dict[str, dict] | None = None):
        self.bodies = bodies or {}
        self.delay: dict[str, float] = {}
        self.fail: dict[str, object] = {}
        self.gate: threading.Event | None = None
        self.calls: list[tuple[str, float | None]] = []
        self.lock = threading.Lock()
        self.inflight = 0

    def __call__(self, name, provider, executor, action, config_dir,
                 project_config=None, timeout=20, extra_env=None, cwd=None):
        if action != "budget":
            return 64, "", ""
        with self.lock:
            self.calls.append((name, timeout))
            self.inflight += 1
        try:
            if self.gate is not None:
                self.gate.wait(8)
            time.sleep(self.delay.get(name, 0.0))
            if name in self.fail:
                return self.fail[name]()
            return 0, json.dumps(self.bodies[name]), ""
        finally:
            with self.lock:
                self.inflight -= 1

    def count(self, name: str | None = None) -> int:
        with self.lock:
            return sum(1 for n, _ in self.calls if name is None or n == name)

    def idle(self, seconds: float = 8.0) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            with self.lock:
                if not self.inflight:
                    return True
            time.sleep(0.02)
        return False


def make_providers(*names: str, deps: dict[str, str] | None = None) -> dict:
    raw = {n: {"bin": "sh"} for n in names}
    for dep, owner in (deps or {}).items():
        raw[dep] = {"bin": "sh", "budget_from": owner}
    return load_providers(raw)


def local(_name=None):
    return SimpleNamespace(kind="local")


_n = iter(range(10 ** 6))


def uniq(count: int, prefix: str = "q") -> list[str]:
    """Provider names no other test used (the budget cache is per process)."""
    tag = next(_n)
    return [f"{prefix}{tag}x{i}" for i in range(count)]


@pytest.fixture(autouse=True)
def clean_cache():
    budget_mod.invalidate_cache()
    yield
    budget_mod.invalidate_cache()


def read(providers, tmp_path, **kw):
    return budget_mod.read_all(providers, lambda n: local(), tmp_path / "g",
                               tmp_path / "proj-config", **kw)


def as_dicts(out: dict) -> dict:
    return {n: b.to_dict() for n, b in out.items()}


# --------------------------------------------------------------------------
# FQ-R1: provider reads run concurrently


def test_fq_r1_five_one_second_readers_cold_read_is_well_under_serial(
        tmp_path, monkeypatch):
    names = uniq(5)
    readers = Readers({n: payload(i) for i, n in enumerate(names)})
    readers.delay = {n: 1.0 for n in names}
    monkeypatch.setattr(scripts_mod, "run_action", readers)
    started = time.monotonic()
    out = read(make_providers(*names), tmp_path)
    elapsed = time.monotonic() - started
    assert elapsed < 2.5, f"cold read took {elapsed:.2f}s; serial would be 5s"
    assert list(out) == names
    assert all(b.known for b in out.values())
    assert readers.count() == 5


def test_fq_r1_the_result_equals_a_serial_read_in_content_and_order(
        tmp_path, monkeypatch):
    names = uniq(5)
    bodies = {n: payload(i) for i, n in enumerate(names)}
    providers = make_providers(*names)
    cooldowns = {names[1]: {"until": time.time() + 3600}}
    spend = {names[2]: {"tokens": 7}}

    # Reference: no delays at all (nothing for concurrency to reorder).
    reference = Readers(bodies)
    monkeypatch.setattr(scripts_mod, "run_action", reference)
    expected = read(providers, tmp_path, cooldowns=cooldowns,
                    spend_by_provider=spend)
    budget_mod.invalidate_cache()

    # Completion order is the REVERSE of declaration order.
    readers = Readers(bodies)
    readers.delay = {n: 0.1 * (5 - i) for i, n in enumerate(names)}
    monkeypatch.setattr(scripts_mod, "run_action", readers)
    got = read(providers, tmp_path, cooldowns=cooldowns, spend_by_provider=spend)

    assert list(got) == list(expected) == names
    assert as_dicts(got) == as_dicts(expected)
    assert got[names[1]].cooldown_until == expected[names[1]].cooldown_until > 0
    assert got[names[2]].spent == expected[names[2]].spent == {"tokens": 7}


def test_fq_r1_a_disabled_provider_is_still_skipped(tmp_path, monkeypatch):
    names = uniq(3)
    raw = {n: {"bin": "sh"} for n in names}
    raw[names[1]]["enabled"] = False
    readers = Readers({n: payload(i) for i, n in enumerate(names)})
    monkeypatch.setattr(scripts_mod, "run_action", readers)
    out = read(load_providers(raw), tmp_path)
    assert list(out) == [names[0], names[2]]
    assert readers.count(names[1]) == 0


@pytest.mark.parametrize("flags", [{}, {"use_cache": False}, {"force": True},
                                   {"use_cache": False, "force": True}])
def test_fq_r1_a_shared_source_with_two_dependents_is_fetched_once(
        tmp_path, monkeypatch, flags):
    owner, dep1, dep2, other = uniq(4)
    providers = make_providers(owner, other, deps={dep1: owner, dep2: owner})
    readers = Readers({owner: payload(0), other: payload(3)})
    # The owner is slow and the dependents are declared right behind it: the
    # race a naive pool loses is every dependent fetching the source itself.
    readers.delay = {owner: 0.5, other: 0.5}
    monkeypatch.setattr(scripts_mod, "run_action", readers)
    out = read(providers, tmp_path, **flags)
    assert readers.count(owner) == 1, readers.calls
    assert set(out) == {owner, dep1, dep2, other}
    assert readers.count() == 2, "only the owner and the independent provider"
    assert out[dep1].known and out[dep2].known


def test_fq_r1_dependents_resolve_to_the_same_answers_as_a_serial_read(
        tmp_path, monkeypatch):
    owner, dep1, dep2 = uniq(3)
    providers = make_providers(owner, deps={dep1: owner, dep2: owner})
    bodies = {owner: payload(2)}
    monkeypatch.setattr(scripts_mod, "run_action", Readers(bodies))
    expected = read(providers, tmp_path)
    budget_mod.invalidate_cache()
    slow = Readers(bodies)
    slow.delay = {owner: 0.4}
    monkeypatch.setattr(scripts_mod, "run_action", slow)
    got = read(providers, tmp_path)
    assert as_dicts(got) == as_dicts(expected)


def test_fq_r1_per_provider_timeouts_are_still_passed_to_the_reader(
        tmp_path, monkeypatch):
    # The budget action's own timeout (10 s today, `_from_script`) is what
    # `scripts.run_action` enforces; the concurrent path must keep passing it.
    names = uniq(4)
    readers = Readers({n: payload(i) for i, n in enumerate(names)})
    monkeypatch.setattr(scripts_mod, "run_action", readers)
    read(make_providers(*names), tmp_path)
    assert sorted(n for n, _ in readers.calls) == sorted(names)
    assert {t for _, t in readers.calls} == {10}


def test_fq_r1_a_reader_that_times_out_costs_only_itself(tmp_path, monkeypatch):
    names = uniq(4)
    readers = Readers({n: payload(i) for i, n in enumerate(names)})
    hung = names[1]
    readers.delay = {hung: 1.5}
    readers.fail = {hung: lambda: (124, "", "timed out after 10s")}
    monkeypatch.setattr(scripts_mod, "run_action", readers)
    started = time.monotonic()
    out = read(make_providers(*names), tmp_path)
    assert time.monotonic() - started < 3.0
    assert out[hung].known is False and "timed out" in out[hung].note
    assert [out[n].known for n in names if n != hung] == [True, True, True]


def test_fq_r1_a_reader_that_raises_is_unknown_and_spares_the_others(
        tmp_path, monkeypatch):
    names = uniq(4)
    readers = Readers({n: payload(i) for i, n in enumerate(names)})

    def boom():
        raise RuntimeError("reader exploded")
    readers.fail = {names[2]: boom}
    monkeypatch.setattr(scripts_mod, "run_action", readers)
    out = read(make_providers(*names), tmp_path)
    assert list(out) == names
    assert out[names[2]].known is False and "RuntimeError" in out[names[2]].note
    assert all(out[n].known for n in names if n != names[2])


def test_fq_r1_bp_r1_one_parse_per_config_file_for_the_whole_concurrent_call(
        tmp_path, monkeypatch):
    from test_bp_budget_read_cost import Project, _spy

    project = Project(tmp_path, monkeypatch)
    parses, loads = _spy(monkeypatch)
    out = project.read()
    assert set(out) == {"alpha", "beta", "gamma"}
    assert sum(loads) <= 1
    assert parses and max(parses.values()) <= 1, dict(parses)


# --------------------------------------------------------------------------
# FQ-R4: readers never run more often than the cache allows


def test_fq_r4_a_second_read_inside_the_ttl_runs_no_reader(tmp_path, monkeypatch):
    names = uniq(4)
    readers = Readers({n: payload(i) for i, n in enumerate(names)})
    monkeypatch.setattr(scripts_mod, "run_action", readers)
    providers = make_providers(*names)
    first = read(providers, tmp_path)
    for _ in range(3):
        again = read(providers, tmp_path)
    assert readers.count() == 4
    assert as_dicts(again).keys() == as_dicts(first).keys()
    assert {n: b.headroom for n, b in again.items()} == \
           {n: b.headroom for n, b in first.items()}


def test_fq_r4_concurrent_cold_callers_run_each_reader_once(tmp_path, monkeypatch):
    names = uniq(3)
    readers = Readers({n: payload(i) for i, n in enumerate(names)})
    readers.delay = {n: 0.4 for n in names}
    monkeypatch.setattr(scripts_mod, "run_action", readers)
    providers = make_providers(*names)
    with ThreadPoolExecutor(6) as pool:
        results = list(pool.map(lambda _i: read(providers, tmp_path), range(6)))
    assert all(list(r) == names and all(b.known for b in r.values())
               for r in results)
    assert readers.count() == 3, readers.calls


def test_fq_r4_use_cache_false_still_bypasses_the_cache(tmp_path, monkeypatch):
    names = uniq(2)
    readers = Readers({n: payload(i) for i, n in enumerate(names)})
    monkeypatch.setattr(scripts_mod, "run_action", readers)
    providers = make_providers(*names)
    read(providers, tmp_path)
    read(providers, tmp_path, use_cache=False)
    assert readers.count() == 4


def test_fq_r4_after_the_ttl_each_reader_runs_exactly_once_more(
        tmp_path, monkeypatch):
    names = uniq(3)
    readers = Readers({n: payload(i) for i, n in enumerate(names)})
    monkeypatch.setattr(scripts_mod, "run_action", readers)
    monkeypatch.setattr(budget_mod, "_CACHE_TTL", 0.3)
    providers = make_providers(*names)
    read(providers, tmp_path)
    time.sleep(0.45)
    read(providers, tmp_path)
    assert readers.count() == 6
    read(providers, tmp_path)
    assert readers.count() == 6, "the refreshed entry is cached again"


# --------------------------------------------------------------------------
# the monitor fixture: real server, real snapshot, real read_all, fake readers


class Monitor:
    def __init__(self, tmp_path, monkeypatch, names, readers, tree_agents=True):
        import multiagents.executor as executor_mod
        from http.server import ThreadingHTTPServer

        self.readers = readers
        self.names = names
        self.paths = ProjectPaths(tmp_path)
        self.paths.ensure()
        self.tree = Tree(self.paths.tree_file, self.paths.events_file)
        if tree_agents:
            self.tree.add(Node(id="ag-seed01", agent="worker", provider=names[0],
                               model="m", parent=None, depth=1,
                               task="the seeded task", status="running"))
        self.config = Config(project={"executor": {"kind": "local"}}, providers={},
                             agents={}, models={}, instruction_dirs=[])
        providers = make_providers(*names)
        monkeypatch.setattr(snap, "load_providers", lambda _raw: providers)
        monkeypatch.setattr(executor_mod, "executor_for",
                            lambda *a: (lambda name: local()))
        monkeypatch.setattr(scripts_mod, "run_action", readers)
        monkeypatch.setattr(server, "load_config", lambda _paths: self.config)

        self.refresh_failures = 0
        self.fail_refresh = False

        def read_all(*a, **k):
            if self.fail_refresh:
                self.refresh_failures += 1
                raise RuntimeError("the refresh failed")
            return REAL_READ_ALL(*a, **k)

        # Wherever the implementation binds it from.
        monkeypatch.setattr(snap, "read_all", read_all)
        monkeypatch.setattr(budget_mod, "read_all", read_all)

        self.token = "tok-" + base64.b32encode(os.urandom(10)).decode().lower()
        monkeypatch.setattr(server.Handler, "paths", self.paths)
        monkeypatch.setattr(server.Handler, "token", self.token)
        monkeypatch.setattr(server.Handler, "bound_host", "127.0.0.1")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        if self.readers.gate is not None:
            self.readers.gate.set()
        self.readers.idle(10)        # no background refresh outlives the test
        self.httpd.shutdown()
        self.httpd.server_close()

    def get(self, path, timeout=5):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            began = time.monotonic()
            try:
                conn.request("GET", path, headers={"X-Monitor-Token": self.token})
                resp = conn.getresponse()
                body = resp.read()
            except TimeoutError:
                pytest.fail(f"GET {path} still blocked after {timeout}s: the poll "
                            f"waited on a quota read")
            took = time.monotonic() - began
        finally:
            conn.close()
        assert resp.status == 200, (path, resp.status, body[:300])
        return json.loads(body), took

    def state(self):
        return self.get("/api/state")

    def quota(self):
        data, took = self.get("/api/quota")
        return {r["name"]: r for r in data["providers"]}, took

    def rows(self):
        data, took = self.state()
        return {r["name"]: r for r in data["providers"]}, took, data

    def wait_for(self, done, seconds=8.0):
        end = time.monotonic() + seconds
        while True:
            rows, _took, _data = self.rows()
            if done(rows) or time.monotonic() > end:
                return rows
            time.sleep(0.05)


@pytest.fixture
def monitor(tmp_path, monkeypatch):
    made = []

    def build(count=2, delay=None, gated=True, **kw):
        names = uniq(count, "m")
        readers = Readers({n: payload(i) for i, n in enumerate(names)})
        if gated:
            readers.gate = threading.Event()
        if delay:
            readers.delay = {n: delay for n in names}
        fx = Monitor(tmp_path, monkeypatch, names, readers, **kw)
        made.append(fx)
        return fx

    yield build
    for fx in made:
        fx.close()


def text(row) -> str:
    return json.dumps(row).lower()


def loading(row) -> bool:
    return "loading" in text(row)


def has_reading(row, i) -> bool:
    b = row["budget"]
    return bool(b.get("known")) and b.get("headroom") == payload(i)["headroom"] \
        and b.get("windows") == payload(i)["windows"]


# --------------------------------------------------------------------------
# FQ-R2: the monitor never blocks a poll on a quota read


def test_fq_r2_the_first_state_answers_at_once_with_loading_rows(monitor):
    fx = monitor(count=2)               # every reader blocks until released
    rows, took, data = fx.rows()
    assert took < 1.0, f"first /api/state took {took:.2f}s"
    assert sorted(rows) == sorted(fx.names), "a row per provider, even loading"
    for name, row in rows.items():
        assert loading(row), row
        assert row["budget"].get("known") is not True
        assert not row["budget"].get("headroom")
    assert fx.readers.gate is not None and not fx.readers.gate.is_set()


def test_fq_r2_a_cold_poll_still_carries_the_agents_tree_and_alerts(monitor):
    fx = monitor(count=2)
    data, took = fx.state()
    assert took < 1.0
    assert [r["id"] for r in data["running"]] == ["ag-seed01"]
    assert data["counts"]["nodes"] == 1 and data["counts"]["running"] == 1
    assert isinstance(data["history"], list) and data["history"]
    assert "alerts" in data and isinstance(data["alerts"], list)
    assert data["project"]["name"] == fx.paths.root.name
    assert "totals" in data and "pause" in data


def test_fq_r2_the_details_page_data_answers_at_once_with_loading_rows(monitor):
    fx = monitor(count=2)
    rows, took = fx.quota()
    assert took < 1.0, f"/api/quota took {took:.2f}s"
    assert sorted(rows) == sorted(fx.names)
    assert all(loading(r) and r["budget"].get("known") is not True
               for r in rows.values())


def test_fq_r2_a_later_poll_after_the_refresh_carries_the_reading(monitor):
    fx = monitor(count=2)
    fx.rows()                           # cold: starts the background refresh
    fx.readers.gate.set()               # the readers answer now
    rows = fx.wait_for(lambda rs: all(has_reading(rs[n], i)
                                      for i, n in enumerate(fx.names)))
    for i, name in enumerate(fx.names):
        assert has_reading(rows[name], i), rows[name]
        assert not loading(rows[name])
    quota, took = fx.quota()
    assert took < 1.0
    assert all(has_reading(quota[n], i) and not loading(quota[n])
               for i, n in enumerate(fx.names))


def test_fq_r2_the_refresh_itself_runs_the_readers_in_parallel(monitor):
    fx = monitor(count=5, delay=1.0, gated=False)
    began = time.monotonic()
    _rows, took, _ = fx.rows()
    assert took < 1.0
    fx.wait_for(lambda rs: all(not loading(r) for r in rs.values()), seconds=6)
    assert time.monotonic() - began < 3.5, "five 1 s readers, not 5 s of them"
    rows = fx.rows()[0]
    assert all(has_reading(rows[n], i) for i, n in enumerate(fx.names))


def test_fq_r2_sequential_polls_while_a_refresh_runs_start_no_second_one(monitor):
    fx = monitor(count=2)
    for _ in range(15):
        _rows, took, _ = fx.rows()
        assert took < 1.0
    fx.quota()
    time.sleep(0.3)
    assert fx.readers.count() <= 2
    fx.readers.gate.set()
    fx.wait_for(lambda rs: all(not loading(r) for r in rs.values()))
    assert fx.readers.count() == 2, fx.readers.calls


def test_fq_r2_concurrent_polls_trigger_exactly_one_refresh(monitor):
    fx = monitor(count=3)
    barrier = threading.Barrier(12)

    def poll(i):
        barrier.wait(5)
        if i % 3 == 0:
            return fx.quota()[1]
        return fx.state()[1]

    with ThreadPoolExecutor(12) as pool:
        times = list(pool.map(poll, range(12)))
    assert max(times) < 1.5, times
    fx.readers.gate.set()
    rows = fx.wait_for(lambda rs: all(has_reading(rs[n], i)
                                      for i, n in enumerate(fx.names)))
    assert all(not loading(r) for r in rows.values())
    assert fx.readers.idle()
    assert fx.readers.count() == 3, fx.readers.calls


def test_fq_r2_a_warm_poll_serves_the_last_reading_while_a_refresh_runs(
        monitor, monkeypatch):
    fx = monitor(count=2)
    fx.rows()
    fx.readers.gate.set()
    fx.wait_for(lambda rs: all(has_reading(rs[n], i)
                               for i, n in enumerate(fx.names)))
    assert fx.readers.idle()

    # Stale, and the next refresh blocks on a closed gate.
    fx.readers.gate = threading.Event()
    monkeypatch.setattr(budget_mod, "_CACHE_TTL", 0.2)
    time.sleep(0.35)
    for _ in range(4):
        rows, took, data = fx.rows()
        assert took < 1.0, "a poll never waits on the blocked refresh"
        assert not any(loading(r) for r in rows.values()), \
            "a warm panel is not blanked back to loading"
        assert all(has_reading(rows[n], i) for i, n in enumerate(fx.names))
        assert data["running"]
    fx.readers.gate.set()
    assert fx.readers.idle()


def test_fq_r2_a_failed_refresh_keeps_the_last_good_reading_with_its_age(
        monitor, monkeypatch):
    fx = monitor(count=2)
    fx.rows()
    fx.readers.gate.set()
    fx.wait_for(lambda rs: all(has_reading(rs[n], i)
                               for i, n in enumerate(fx.names)))
    assert fx.readers.idle()

    fx.fail_refresh = True
    monkeypatch.setattr(budget_mod, "_CACHE_TTL", 0.2)
    time.sleep(0.35)
    end = time.monotonic() + 6
    while fx.refresh_failures == 0 and time.monotonic() < end:
        fx.rows()
        time.sleep(0.05)
    assert fx.refresh_failures >= 1, "the refresh was never attempted"
    time.sleep(0.3)                     # let the failure land
    for _ in range(3):
        rows, took, _ = fx.rows()
        assert took < 1.0
        for i, name in enumerate(fx.names):
            assert has_reading(rows[name], i), rows[name]
            assert not loading(rows[name])
            assert any(word in text(rows[name])
                       for word in ("stale", "ago", "age", "old")), rows[name]
        time.sleep(0.1)
    quota, _ = fx.quota()
    assert all(has_reading(quota[n], i) for i, n in enumerate(fx.names))


def test_fq_r2_a_failed_refresh_does_not_stop_later_ones(monitor, monkeypatch):
    fx = monitor(count=2)
    fx.rows()
    fx.readers.gate.set()
    fx.wait_for(lambda rs: all(has_reading(rs[n], i)
                               for i, n in enumerate(fx.names)))
    assert fx.readers.idle()
    monkeypatch.setattr(budget_mod, "_CACHE_TTL", 0.2)
    fx.fail_refresh = True
    time.sleep(0.35)
    end = time.monotonic() + 6
    while fx.refresh_failures == 0 and time.monotonic() < end:
        fx.rows()
        time.sleep(0.05)
    assert fx.refresh_failures >= 1
    # The source recovers with a new reading; the monitor picks it up.
    fx.fail_refresh = False
    for i, n in enumerate(fx.names):
        fx.readers.bodies[n] = {**payload(i), "headroom": 0.01}
    budget_mod.invalidate_cache()
    rows = fx.wait_for(lambda rs: all(r["budget"].get("headroom") == 0.01
                                      for r in rs.values()), seconds=8)
    assert all(r["budget"].get("headroom") == 0.01 for r in rows.values())


def test_fq_r2_the_tui_poll_does_not_wait_for_the_readers(monitor, monkeypatch):
    from multiagents.monitor import tui

    fx = monitor(count=2)
    monkeypatch.setattr(tui, "load_config", lambda _paths: fx.config)
    screen = tui.Screen(None, fx.paths)
    began = time.monotonic()
    screen.poll(force=True)
    took = time.monotonic() - began
    assert took < 1.0, f"Screen.poll took {took:.2f}s"
    assert screen.state, screen.message
    rows = {r["name"]: r for r in screen.state["providers"]}
    assert sorted(rows) == sorted(fx.names)
    assert all(loading(r) for r in rows.values())
    assert [r["id"] for r in screen.state["running"]] == ["ag-seed01"]

    fx.readers.gate.set()
    end = time.monotonic() + 8
    while time.monotonic() < end:
        screen.poll(force=True)
        if all(has_reading(r, i) for i, r in enumerate(
                {x["name"]: x for x in screen.state["providers"]}[n]
                for n in fx.names)):
            break
        time.sleep(0.05)
    rows = {r["name"]: r for r in screen.state["providers"]}
    assert all(has_reading(rows[n], i) for i, n in enumerate(fx.names))


# --------------------------------------------------------------------------
# FQ-R3: budget_status keeps blocking and keeps its output


def test_fq_r3_budget_status_returns_the_complete_reading_in_parallel_time(
        tmp_path, monkeypatch):
    import yaml
    from multiagents import server as mcp_server
    import c3_harness as h3
    from test_codex_engine_models_budget import (NAME, Project, _executable,
                                                 _shipped_disabled)

    count = 5
    names = [f"fq{i}" for i in range(count)]
    p = Project(tmp_path, monkeypatch,
                {n: {"bin": NAME, "spawn": {"args": ["x"]}} for n in names})
    bodies = {n: payload(i) for i, n in enumerate(names)}
    for n in names:
        _executable(p.scripts / f"{n}.sh",
                    "#!/bin/sh\ncase \"$1\" in budget) sleep 1; cat <<'EOF'\n"
                    + json.dumps(bodies[n]) + "\nEOF\n;; *) exit 64;; esac\n")
    h3.as_root(monkeypatch)
    monkeypatch.setenv("MULTIAGENTS_PROJECT", str(p.root))
    mcp_server._reset()
    try:
        began = time.monotonic()
        status = mcp_server.budget_status()
        took = time.monotonic() - began
    finally:
        mcp_server._reset()
    if isinstance(status, str):
        status = json.loads(status)

    assert took < 2.5, f"budget_status took {took:.2f}s; serial is 5s"
    # Blocking and complete: every reading is in the answer, not "loading".
    assert [n for n in status["providers"] if n in names] == names
    for i, n in enumerate(names):
        entry = status["providers"][n]
        used = entry.pop("used_percent")
        assert used == pytest.approx((1 - bodies[n]["headroom"]) * 100)
        assert entry == {
            "provider": n, "known": True, "severity": "normal", "source": "script",
            "usable": True, "headroom": bodies[n]["headroom"],
            "windows": bodies[n]["windows"]}, entry
        assert "lines" not in entry and "lines_from" not in entry
        assert "loading" not in json.dumps(entry).lower()
    # The envelope is unchanged.
    for key in ("tree_usage", "by_model", "deferred_tasks", "spend", "advice",
                "context"):
        assert key in status, key
    assert set(status["context"]) == {"known", "tokens", "wind_down_at",
                                      "compact_at"}


def test_fq_r3_budget_status_reads_afresh_after_the_cache_is_invalidated(
        tmp_path, monkeypatch):
    """It blocks on a real read, never answering from a monitor-style
    "last known" copy: a changed reader is seen once the cache is dropped."""
    from multiagents import server as mcp_server
    import c3_harness as h3
    from test_codex_engine_models_budget import (NAME, Project, _executable)

    p = Project(tmp_path, monkeypatch, {"fqx": {"bin": NAME, "spawn": {"args": ["x"]}}})
    script = p.scripts / "fqx.sh"

    def write(headroom):
        _executable(script, "#!/bin/sh\ncase \"$1\" in budget) cat <<'EOF'\n"
                    + json.dumps({"known": True, "headroom": headroom,
                                  "source": "script", "windows": {}})
                    + "\nEOF\n;; *) exit 64;; esac\n")

    h3.as_root(monkeypatch)
    monkeypatch.setenv("MULTIAGENTS_PROJECT", str(p.root))

    def status():
        mcp_server._reset()
        try:
            out = mcp_server.budget_status()
        finally:
            mcp_server._reset()
        return json.loads(out) if isinstance(out, str) else out

    write(0.6)
    assert status()["providers"]["fqx"]["headroom"] == 0.6
    write(0.3)
    budget_mod.invalidate_cache()
    assert status()["providers"]["fqx"]["headroom"] == 0.3
