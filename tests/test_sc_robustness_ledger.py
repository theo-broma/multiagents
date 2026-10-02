"""SC-R2a persistence and SC-R3 exact admission boundaries."""
import datetime as dt
import json
import os
import random
import multiprocessing
import math

import pytest

from multiagents import spendcap as sc

AT = dt.datetime(2026, 9, 16, 12, tzinfo=dt.timezone.utc).timestamp()


def charge(ledger, key, usd=0.75, at=AT, caps=()):
    return ledger.charge(key=key, provider="acme", model="acme/m1",
                         agent="worker", node="ag-example", usd=usd,
                         at=at, caps=list(caps))


def test_short_successful_writes_do_not_silently_lose_a_charge(tmp_path, monkeypatch):
    ledger = sc.Ledger(tmp_path / "ledger.jsonl")
    real_write = os.write
    # POSIX write may succeed while writing fewer bytes than requested.
    with monkeypatch.context() as patch:
        patch.setattr(os, "write", lambda fd, data: real_write(fd, data[:17]))
        charge(ledger, "first")
    charge(ledger, "second", 0.25)
    recovered = sc.Ledger(ledger.path)
    recovered.refresh()
    assert recovered.spend("acme", "", "day", AT) == 1.0


@pytest.mark.parametrize("cap,spent", [(1e-10, 0.0), (1.0, 1.0 - 5e-10)])
def test_spend_strictly_below_a_valid_cap_is_admitted(tmp_path, cap, spent):
    ledger = sc.Ledger(tmp_path / "ledger.jsonl")
    charge(ledger, "below", spent)
    assert spent < cap
    assert not ledger.describe(sc.Cap("acme", "", cap, "day"), AT)["reached"]


def test_probe_checks_creation_of_an_absent_ledger(tmp_path, monkeypatch):
    ledger = sc.Ledger(tmp_path / "ledger.jsonl")
    real_open = os.open

    def cannot_create(path, flags, *args, **kwargs):
        if os.fspath(path) == os.fspath(ledger.path) and flags & os.O_CREAT:
            raise PermissionError("ledger creation denied")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", cannot_create)
    with pytest.raises(sc.LedgerError):
        ledger.probe()


@pytest.mark.parametrize("period,boundary", [
    ("day", dt.datetime(2026, 9, 17, tzinfo=dt.timezone.utc)),
    ("week", dt.datetime(2026, 9, 21, tzinfo=dt.timezone.utc)),
    ("month", dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)),
])
def test_last_representable_timestamp_before_reset_stays_in_old_period(period, boundary):
    edge = boundary.timestamp()
    just_before = math.nextafter(edge, -math.inf)
    assert just_before < edge
    assert sc.period_start(just_before, period) <= just_before
    assert sc.period_end(just_before, period) == edge


def test_generated_calendar_boundaries_and_replay_roundtrips(tmp_path):
    """Seed 8362: independent calendar oracle, exact edges, repeated refresh."""
    rng = random.Random(8362)
    ledger = sc.Ledger(tmp_path / "ledger.jsonl")
    events = []
    for i in range(250):
        day = dt.datetime(1999, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(
            days=rng.randrange(12000), seconds=rng.choice([0, 1, 86399]))
        at, usd = day.timestamp(), rng.randrange(1, 100) / 100
        key = json.dumps(["key|雪", i])
        events.append((day, usd))
        charge(ledger, key, usd, at)
        charge(ledger, key, usd, at + 86400)
    recovered = sc.Ledger(ledger.path)
    recovered.refresh()
    assert len(recovered.charges) == len(events)
    for day, _ in events:
        midnight = day.replace(hour=0, minute=0, second=0, microsecond=0)
        bounds = {
            "day": (midnight, midnight + dt.timedelta(days=1)),
            "week": (midnight - dt.timedelta(days=day.weekday()),
                     midnight + dt.timedelta(days=7 - day.weekday())),
            "month": (midnight.replace(day=1),
                      midnight.replace(day=1, year=day.year + (day.month == 12),
                                       month=day.month % 12 + 1)),
        }
        for period, (start, end) in bounds.items():
            assert sc.period_start(day.timestamp(), period) == start.timestamp()
            assert sc.period_end(day.timestamp(), period) == end.timestamp()
            expected = sum(cost for when, cost in events if start <= when < end)
            assert recovered.spend("acme", "", period, day.timestamp()) == pytest.approx(expected)


def test_generated_torn_tail_prefixes_keep_prior_and_next_charges(tmp_path):
    rng = random.Random(8362)
    tail = json.dumps({"kind": "charge", "key": "torn|雪", "ts": AT,
                       "provider": "acme", "model": "acme/m1", "usd": 19.0},
                      ensure_ascii=False).encode()
    cuts = sorted(set([1, len(tail) - 1] + [rng.randrange(1, len(tail)) for _ in range(80)]))
    for cut in cuts:
        ledger = sc.Ledger(tmp_path / f"ledger-{cut}.jsonl")
        charge(ledger, "first", 0.5)
        with ledger.path.open("ab") as stream:
            stream.write(tail[:cut])
        recovered = sc.Ledger(ledger.path)
        recovered.refresh()
        assert recovered.spend("acme", "", "day", AT) == 0.5
        charge(recovered, "next", 0.25)
        reader = sc.Ledger(ledger.path)
        reader.refresh()
        assert reader.spend("acme", "", "day", AT) == 0.75


def _append_worker(path, worker):
    ledger = sc.Ledger(path)
    for i in range(30):
        charge(ledger, f"worker:{worker}:event:{i}", 0.125)
        charge(ledger, f"worker:{worker}:event:{i}", 0.125)


def test_four_process_writers_keep_each_distinct_event_once(tmp_path):
    ctx = multiprocessing.get_context("fork")
    path = tmp_path / "ledger.jsonl"
    workers = [ctx.Process(target=_append_worker, args=(path, i)) for i in range(4)]
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(20)
            assert worker.exitcode == 0
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join()
    ledger = sc.Ledger(path)
    ledger.refresh()
    assert len(ledger.charges) == 120
    assert ledger.spend("acme", "", "day", AT) == 15.0
