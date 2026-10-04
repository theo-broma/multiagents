"""Spend caps for metered providers, and the ledger they are counted in.

Contract: `context/specs/spend-caps.md` (SC-R*). Two halves:

- The cap a provider declares under `spend_cap:` in providers.yaml (SC-R1),
  parsed and validated here from the provider's RAW block, so a provider that
  `extends` a capped one starts uncapped.
- The ledger (SC-R2/SC-R2a): one append-only JSON-lines file per project,
  `.multiagents/spend-ledger.jsonl`, written under an exclusive `flock` on a
  sibling lock file. Every line is one record — the ledger's creation, a
  charge, or a crossing — and deduplicating a charge, appending it and
  claiming the crossings it causes happen in one locked transaction. A
  crossing record is also the durable stop request every runner polls for
  (SC-R4a). The ledger is never the tree's event log.

Periods are UTC calendar periods: a day from 00:00, a week from Monday 00:00,
a month from the 1st at 00:00.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import fcntl
import fnmatch
import json
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

PERIODS = ("day", "week", "month")
CAUSE = "spend_cap"
UNREADABLE = "spend_cap_unreadable"
LEDGER_NAME = "spend-ledger.jsonl"
ACCOUNTING_NOTE = ("this project's accounting, not the provider's invoice: spend "
                   "on the same account from other projects is not seen")
# The ledger and its lock are never opened through a link (a link planted
# in their place is refused, and with a cap that refuses launches).
_OPEN = os.O_CLOEXEC | os.O_NOFOLLOW


# ------------------------------------------------------------------ config --

@dataclass(frozen=True)
class ModelCap:
    usd: float | None
    period: str | None


@dataclass(frozen=True)
class SpendCap:
    """A provider's `spend_cap` block, validated. `usd` None: no provider cap."""
    usd: float | None = None
    period: str = "day"
    models: dict[str, ModelCap] = field(default_factory=dict)

    def caps_for(self, provider: str, model: str) -> list[Cap]:
        """The caps that apply to `model` on `provider`: the provider's, and
        the model's own in addition, never instead (SC-R1)."""
        out = []
        if self.usd is not None:
            out.append(Cap(provider, "", self.usd, self.period))
        own = self.models.get(model or "")
        if own is not None and own.usd is not None:
            out.append(Cap(provider, model, own.usd, own.period or self.period))
        return out


@dataclass(frozen=True)
class Cap:
    """One cap in force: the provider's (`model` "") or one model's."""
    provider: str
    model: str
    usd: float
    period: str

    @property
    def scope(self) -> str:
        return f"model:{self.provider}:{self.model}" if self.model \
            else f"provider:{self.provider}"

    @property
    def label(self) -> str:
        return (f"{self.provider} model {self.model}" if self.model
                else self.provider)


def _usd(where: str, value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value) or value < 0:
        raise ValueError(f"{where}.usd: must be a finite number of US dollars, "
                         f"0 or more, or null for no cap — not {value!r}")
    return float(value)


def _period(where: str, value: Any, default: str | None) -> str | None:
    if value is None:
        return default
    if not isinstance(value, str) or value not in PERIODS:
        raise ValueError(f"{where}.period: must be one of {', '.join(PERIODS)}, "
                         f"not {value!r}")
    return value


def parse(provider_name: str, data: dict, models_include: list[str]) -> SpendCap | None:
    """SC-R1: the provider's raw `spend_cap`, validated, or None when absent.

    Every invalid value is a load error naming the key; nothing is ignored.
    `models` keys must match one of the provider's `models_include` patterns
    (a provider without the list allows any model id).
    """
    if "spend_cap" not in data or data["spend_cap"] is None:
        return None
    raw = data["spend_cap"]
    where = f"provider {provider_name!r}: spend_cap"
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: must be a mapping with `usd`, `period` "
                         f"and `models`, not {raw!r}")
    unknown = sorted(str(k) for k in raw if k not in ("usd", "period", "models"))
    if unknown:
        raise ValueError(f"{where}: unknown key(s) {', '.join(unknown)}; "
                         f"expected usd, period, models")
    usd = _usd(where, raw.get("usd"))
    period = _period(where, raw.get("period"), "day")
    models_raw = raw.get("models")
    if models_raw is None:
        models_raw = {}
    if not isinstance(models_raw, dict):
        raise ValueError(f"{where}.models: must be a mapping of model id to "
                         f"{{usd, period}}, not {models_raw!r}")
    models: dict[str, ModelCap] = {}
    for key, entry in models_raw.items():
        here = f"{where}.models[{key!r}]"
        if not isinstance(key, str) or not key:
            raise ValueError(f"{here}: a model id must be a non-empty string")
        if models_include and not any(fnmatch.fnmatch(key, pattern)
                                      for pattern in models_include):
            raise ValueError(f"{here}: {key!r} matches none of the provider's "
                             f"models_include patterns ({', '.join(models_include)})")
        if entry is None:
            entry = {}
        if not isinstance(entry, dict):
            raise ValueError(f"{here}: must be a mapping with `usd` and "
                             f"`period`, not {entry!r}")
        unknown = sorted(str(k) for k in entry if k not in ("usd", "period"))
        if unknown:
            raise ValueError(f"{here}: unknown key(s) {', '.join(unknown)}; "
                             f"expected usd, period")
        models[key] = ModelCap(_usd(here, entry.get("usd")),
                               _period(here, entry.get("period"), None))
    return SpendCap(usd=usd, period=period or "day", models=models)


# ----------------------------------------------------------------- periods --

def _utc(ts: float) -> _dt.datetime:
    # Whole seconds, floored: `fromtimestamp` ROUNDS to the microsecond, so
    # the last float before midnight would land in the next period (R-5).
    # Every boundary is a whole second, so flooring never changes a period.
    return _dt.datetime.fromtimestamp(math.floor(ts), _dt.timezone.utc)


def period_start(ts: float, period: str) -> float:
    day = _utc(ts).replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        day -= _dt.timedelta(days=day.weekday())
    elif period == "month":
        day = day.replace(day=1)
    return day.timestamp()


def period_end(ts: float, period: str) -> float:
    start = _utc(period_start(ts, period))
    if period == "day":
        return (start + _dt.timedelta(days=1)).timestamp()
    if period == "week":
        return (start + _dt.timedelta(days=7)).timestamp()
    return start.replace(year=start.year + (start.month == 12),
                         month=start.month % 12 + 1).timestamp()


def iso(ts: float | None) -> str | None:
    return None if ts is None else _utc(ts).isoformat().replace("+00:00", "Z")


# ------------------------------------------------------------------ ledger --

def crossing_id(scope: str, start: float, cap: float) -> str:
    """One crossing: its scope, its period's start and the cap value."""
    return f"{scope}@{start:.0f}@{cap!r}"


class LedgerError(OSError):
    """The ledger could not be read or written."""


@dataclass
class Charge:
    ts: float
    provider: str
    model: str
    usd: float


class Ledger:
    """The project's durable spend ledger (SC-R2/SC-R2a).

    What it read is kept in memory and only the bytes appended since are read
    on the next look, under the lock; a file that shrank or was replaced is
    read again from the start. Only whole lines count: a torn last line is
    ignored, and cut off by the next writer before it appends.
    """

    def __init__(self, path: Path):
        self.path = path
        self.lock_path = path.with_name(path.name + ".lock")
        self._reset()

    def _reset(self) -> None:
        self._ident: tuple[int, int] | None = None
        self._offset = 0
        self.created: float | None = None
        self.keys: dict[str, float] = {}
        self.charges: list[Charge] = []
        self.crossings: dict[tuple[str, float, float], dict] = {}
        self.announced: set[str] = set()
        # SC-R4c: the runs recorded as stopped for each crossing id.
        self.stops: dict[str, list[str]] = {}

    @contextlib.contextmanager
    def _locked(self, exclusive: bool) -> Iterator[None]:
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | _OPEN, 0o644)
        except OSError as exc:
            raise LedgerError(f"spend ledger lock {self.lock_path}: {exc}") from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _absorb(self, record: dict) -> None:
        kind = record.get("kind")
        if kind == "created":
            ts = record.get("ts")
            if isinstance(ts, (int, float)) and not isinstance(ts, bool):
                self.created = ts if self.created is None else min(self.created, ts)
        elif kind == "charge":
            key, ts, usd = record.get("key"), record.get("ts"), record.get("usd")
            if (not isinstance(key, str) or isinstance(ts, bool) or isinstance(usd, bool)
                    or not isinstance(ts, (int, float))
                    or not isinstance(usd, (int, float)) or key in self.keys):
                return
            self.keys[key] = ts
            self.charges.append(Charge(ts, str(record.get("provider") or ""),
                                       str(record.get("model") or ""), float(usd)))
        elif kind == "crossing":
            try:
                ident = (str(record["scope"]), float(record["period_start"]),
                         float(record["cap"]))
            except (KeyError, TypeError, ValueError):
                return
            self.crossings.setdefault(ident, record)
        elif kind == "announced" and isinstance(record.get("id"), str):
            self.announced.add(record["id"])
        elif (kind == "stopped" and isinstance(record.get("id"), str)
              and isinstance(record.get("node"), str)):
            nodes = self.stops.setdefault(record["id"], [])
            if record["node"] not in nodes:
                nodes.append(record["node"])

    def _refresh(self) -> None:
        """Read what was appended since the last look. Caller holds the lock."""
        try:
            fd = os.open(self.path, os.O_RDONLY | _OPEN)
        except FileNotFoundError:
            self._reset()
            return
        except OSError as exc:
            raise LedgerError(f"spend ledger {self.path}: {exc}") from exc
        try:
            st = os.fstat(fd)
            ident = (st.st_dev, st.st_ino)
            if ident != self._ident or st.st_size < self._offset:
                self._reset()
                self._ident = ident
            if st.st_size == self._offset:
                return
            os.lseek(fd, self._offset, os.SEEK_SET)
            chunks = []
            while True:
                chunk = os.read(fd, 1 << 20)
                if not chunk:
                    break
                chunks.append(chunk)
        except OSError as exc:
            raise LedgerError(f"spend ledger {self.path}: {exc}") from exc
        finally:
            os.close(fd)
        data = b"".join(chunks)
        end = data.rfind(b"\n")
        if end < 0:
            return                          # nothing whole yet: a torn line
        for line in data[:end].split(b"\n"):
            try:
                record = json.loads(line)
            except ValueError:
                continue                    # a torn or foreign line: skipped
            if isinstance(record, dict):
                self._absorb(record)
        self._offset += end + 1

    def _repair(self) -> None:
        """Cut a torn last line off, so the next line appended is not glued
        onto it and lost with it (SC-R2a: a torn entry is ignored). Caller
        holds the exclusive lock, and refreshes after."""
        try:
            fd = os.open(self.path, os.O_RDWR | _OPEN)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise LedgerError(f"spend ledger {self.path}: {exc}") from exc
        try:
            size = os.fstat(fd).st_size
            if not size or os.pread(fd, 1, size - 1) == b"\n":
                return
            keep, pos = 0, size
            while pos > 0:
                step = min(pos, 1 << 16)
                pos -= step
                cut = os.pread(fd, step, pos).rfind(b"\n")
                if cut >= 0:
                    keep = pos + cut + 1
                    break
            os.ftruncate(fd, keep)
        except OSError as exc:
            raise LedgerError(f"spend ledger {self.path}: {exc}") from exc
        finally:
            os.close(fd)

    def _append(self, records: list[dict]) -> None:
        """Append whole lines, durably, or nothing. Caller holds the exclusive
        lock. A short write is continued until every byte is down (#9); one
        that fails part-way is cut back to where it began, so a failure never
        leaves half a record for the next reader."""
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | _OPEN, 0o644)
        except OSError as exc:
            raise LedgerError(f"spend ledger {self.path}: {exc}") from exc
        start = None
        try:
            start = os.fstat(fd).st_size
            payload = memoryview(b"".join(json.dumps(r, sort_keys=True).encode() + b"\n"
                                          for r in records))
            while payload:
                written = os.write(fd, payload)
                if written <= 0:
                    raise OSError("the write made no progress")
                payload = payload[written:]
            os.fsync(fd)
        except OSError as exc:
            if start is not None:
                with contextlib.suppress(OSError):
                    os.ftruncate(fd, start)
            raise LedgerError(f"spend ledger {self.path}: {exc}") from exc
        finally:
            os.close(fd)

    # -- the public surface ------------------------------------------------

    def probe(self) -> None:
        """Raise LedgerError unless the ledger can be both read and written
        (SC-R2a: fail closed when a cap applies)."""
        with self._locked(exclusive=True):
            self._refresh()
            # Opened for append, and created when it is missing (#4): a
            # missing ledger is only usable if it can be made.
            try:
                os.close(os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | _OPEN,
                                 0o644))
            except OSError as exc:
                raise LedgerError(f"spend ledger {self.path}: {exc}") from exc

    def charge(self, *, key: str, provider: str, model: str, agent: str,
               node: str, usd: float, caps: list[Cap], at: float | None = None,
               account: str | None = None
               ) -> tuple[list[dict], list[dict]]:
        """Record one cost event and claim the crossings it causes, in one
        locked transaction. Returns `(new_crossings, binding)`: the crossing
        records this call claimed, and every cap of `caps` whose period spend
        has reached it (each as a `describe` dict). A key already recorded
        adds nothing and keeps its first observation time."""
        at = time.time() if at is None else at
        with self._locked(exclusive=True):
            self._repair()
            self._refresh()
            records: list[dict] = []
            if self.created is None:
                records.append({"kind": "created", "ts": at})
            if key not in self.keys and usd:
                records.append({"kind": "charge", "ts": at, "key": key,
                                "provider": provider, "model": model,
                                "agent": agent, "node": node, "usd": usd,
                                **({"account": account} if account is not None else {})})
            for record in records:
                self._absorb(record)
            binding, new = [], []
            for cap in caps:
                state = self._describe(cap, at)
                if not state["reached"]:
                    continue
                ident = (cap.scope, state["period_start"], cap.usd)
                cid = crossing_id(cap.scope, state["period_start"], cap.usd)
                # SC-R4b: a charge from a period that has ended (a held one,
                # retried late) crosses that period's cap and stops nothing.
                current = period_start(time.time(), cap.period) == state["period_start"]
                if current:
                    binding.append(state)
                if current and node not in self.stops.get(cid, []):
                    # SC-R4c: the run this charge belongs to is stopped by
                    # the crossing, recorded in the same transaction.
                    stop = {"kind": "stopped", "id": cid, "node": node, "ts": at}
                    records.append(stop)
                    self._absorb(stop)
                if ident in self.crossings:
                    continue
                crossing = {"kind": "crossing", "ts": at, "scope": cap.scope,
                            "id": cid,
                            "provider": cap.provider, "model": cap.model,
                            "period": cap.period, "period_start": state["period_start"],
                            "until": state["resets_at"], "cap": cap.usd,
                            "spend": state["spend"], "by": node}
                records.append(crossing)
                self._absorb(crossing)
                new.append(crossing)
            if records:
                try:
                    self._append(records)
                    # Already absorbed: the next look skips what was just written.
                    st = os.stat(self.path)
                except OSError:
                    self._reset()          # memory must not hold what never landed
                    raise
                self._ident, self._offset = (st.st_dev, st.st_ino), st.st_size
            return new, binding

    def refresh(self) -> None:
        with self._locked(exclusive=False):
            self._refresh()

    def poll(self) -> None:
        """`refresh`, but only when the file changed since the last look: a
        stat, without the lock, is all an idle poll costs. A ledger that does
        not exist yet holds nothing to see."""
        try:
            st = os.stat(self.path, follow_symlinks=False)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise LedgerError(f"spend ledger {self.path}: {exc}") from exc
        if ((st.st_dev, st.st_ino) != self._ident or st.st_size != self._offset):
            self.refresh()

    def stop_request(self, provider: str, model: str, since: float,
                     at: float) -> dict | None:
        """SC-R4a: a crossing that stops a run of `model` on `provider`
        launched at `since`: on its scope, in the period current now, and
        recorded after the launch. Matched on what the crossing itself
        records — never on this process's cap value, which may be older than
        the one the crossing process used (#1). A run launched after the
        crossing was admitted under the cap as it then stood (raised, or a
        new value) and is not stopped by it."""
        for record in self.crossings.values():
            if (record.get("provider") != provider
                    or (record.get("model") and record.get("model") != model)):
                continue
            period, ts = record.get("period"), record.get("ts")
            if period not in PERIODS or not isinstance(ts, (int, float)):
                continue
            if period_start(at, period) == record.get("period_start") and ts >= since:
                return record
        return None

    def unannounced(self, before: float) -> list[dict]:
        """Crossings whose `spend_cap` event is not known to be recorded,
        claimed before `before` (SC-R4a, #13)."""
        out = []
        for (scope, start, cap), record in self.crossings.items():
            ident = record.get("id") or crossing_id(scope, start, cap)
            ts = record.get("ts")
            if ident not in self.announced and isinstance(ts, (int, float)) and ts < before:
                out.append(dict(record, id=ident))
        return out

    def record_stop(self, ident: str, node: str) -> None:
        """SC-R4c: `node` was stopped by crossing `ident`."""
        with self._locked(exclusive=True):
            self._repair()
            self._refresh()
            if node in self.stops.get(ident, []):
                return
            record = {"kind": "stopped", "id": ident, "node": node, "ts": time.time()}
            try:
                self._append([record])
                st = os.stat(self.path, follow_symlinks=False)
            except OSError:
                self._reset()
                raise
            self._absorb(record)
            self._ident, self._offset = (st.st_dev, st.st_ino), st.st_size

    def announce(self, ident: str, emit: Any) -> bool:
        """Record a crossing's event exactly once across processes: under the
        exclusive lock, `emit()` runs only if no announcement is recorded,
        and the announcement is appended only after it returned (SC-R4c): an
        `emit` that raises leaves the crossing unannounced, for a later
        watcher to retry. False if it already was announced. A crash between
        the two leaves it to `emit` to find its own event."""
        with self._locked(exclusive=True):
            self._repair()
            self._refresh()
            if ident in self.announced:
                return False
            emit()
            record = {"kind": "announced", "id": ident, "ts": time.time()}
            try:
                self._append([record])
                st = os.stat(self.path, follow_symlinks=False)
            except OSError:
                self._reset()
                raise
            self._absorb(record)
            self._ident, self._offset = (st.st_dev, st.st_ino), st.st_size
            return True

    def spend(self, provider: str, model: str, period: str, at: float) -> float:
        """Spend on `provider` (on one `model` of it when given) in the period
        containing `at`. Reads what is in memory: `refresh` first."""
        start, end = period_start(at, period), period_end(at, period)
        # `fsum`: the exactly rounded sum, so 0.7 + 0.1 + 0.2 reaches 1.0.
        return math.fsum(c.usd for c in self.charges
                   if c.provider == provider and (not model or c.model == model)
                   and start <= c.ts < end)

    def _describe(self, cap: Cap, at: float) -> dict:
        spent = self.spend(cap.provider, cap.model, cap.period, at)
        return {"provider": cap.provider, "model": cap.model, "usd": cap.usd,
                "period": cap.period, "spend": round(spent, 6),
                "remaining": round(max(0.0, cap.usd - spent), 6),
                "period_start": period_start(at, cap.period),
                "resets_at": period_end(at, cap.period),
                # SC-R3 "has reached": exact, so spend below a cap is never
                # refused, however small the cap (R-6).
                "reached": spent >= cap.usd}

    def describe(self, cap: Cap, at: float) -> dict:
        return self._describe(cap, at)

    def crossed(self, cap: Cap, at: float) -> dict | None:
        """The live stop request for `cap`: its crossing in the current period
        at the current cap value, or None."""
        return self.crossings.get((cap.scope, period_start(at, cap.period), cap.usd))

    def partial(self, period: str, at: float) -> bool:
        """SC-R2: a period that began before the ledger existed is partial."""
        created = self.created if self.created is not None else at
        return period_start(at, period) < created

    def providers_seen(self) -> set[str]:
        return {c.provider for c in self.charges}
