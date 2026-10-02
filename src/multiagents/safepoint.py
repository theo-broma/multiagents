"""CW-R2: the safe stop between the driver and the orchestrator's server.

Before the driver terminates the orchestrator's CLI for a compaction, the
server that CLI owns must stop admitting launches and let the transitions it
already admitted finish — a launch between its claim and its pid, a steer
between stopping its predecessor and starting the replacement, a consult
refreshing its worktree. Only then is the CLI stopped.

The two sides share no channel: the driver holds the CLI, the CLI holds the
server's stdio. And the server's event loop can be frozen in a slow `Popen`
for exactly the window that matters. So the handshake is files, polled from
both sides, and a frozen server simply does not answer — the driver's bound
runs out and the compaction is cancelled, which is the safe direction.

Files, in a host-only directory under the state root, keyed by project, with
no link followed on the way (kept as defence in depth). The driver resolves it
once and hands it to the CLI it launches in `MULTIAGENTS_SAFEPOINT_DIR`.

What makes a file count is not where it is but who wrote it (CW-R2b). The
driver makes a fresh 256-bit key for its run and gives it to the CLI in
`MULTIAGENTS_SAFEPOINT_KEY`; the CLI's root server takes it out of its own
environment at startup, so nothing it starts inherits it. Every record —
registration, request, commit, cancellation, acknowledgement — carries an
HMAC-SHA256 over a canonical encoding of all its fields, including a record
type. A record that does not verify is ignored: it can delay or deny a
compaction, never authorise a stop or reopen admission.

- `safepoint-server-<pid>.json`: a root server's signed registration (pid,
  start time, session), written before it serves anything.
- `safepoint-barrier-<session key>.json`: the driver's signed request, then
  its commit or cancellation, bound to a nonce, the driver and the CLI.
- `safepoint-server-<pid>.safe`: a server's signed acknowledgement.

A server closes admission at the first quiet moment after it sees a fresh
request that applies to it, and acknowledges in the same step. Once it has,
admission stays closed until an authenticated cancellation of that request or
the confirmed death of the driver that made it — a deleted, replaced or
malformed file changes nothing. A commit latches until the server exits.

Everything on the driver's side fails closed: an error writing the request,
listing the servers or reading an acknowledgement cancels the compaction, and
no registered server counts as "all acknowledged" only when `/proc` shows no
unregistered server of the session beneath the CLI either.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import re
import time
from pathlib import Path

from . import procs
from .paths import state_root

ENV = "MULTIAGENTS_SAFEPOINT_DIR"
KEY_ENV = "MULTIAGENTS_SAFEPOINT_KEY"
# The driver's own identity, `<pid>:<start>`, given to its CLI with the key: a
# server that starts while that driver lives admits nothing until the driver
# has granted it (review r9).
DRIVER_ENV = "MULTIAGENTS_SAFEPOINT_DRIVER"
SERVER_MODULE = "multiagents.server"

# How often both sides look. Small: the driver is holding a stop while it
# waits, and the server is refusing launches.
POLL_SECONDS = 0.1
# How much older than the bound a commit may be and still close a server
# that registers late: the driver's terminate-then-kill takes about twenty
# seconds.
COMMITTED_GRACE_SECONDS = 120.0
# How long nothing must have been in flight before the server says so: the
# last transition's tool result is written to the CLI after the Runner is
# done with it, and the CLI must have read it before it is stopped.
QUIET_SECONDS = 0.3
FILE_MAX_BYTES = 64 * 1024

STOPPING = ("this server is stopping so the session can be compacted; {what} "
            "was not started. It is resumed in a moment — try again then.")


def directory(paths) -> Path:
    """Where this project's handshake lives: the driver's resolution when it
    launched this process's CLI, else the state root's (the same function,
    run by the driver)."""
    explicit = os.environ.get(ENV)
    if explicit:
        return Path(explicit)
    return state_root() / "safepoints" / paths.slug


def _key(session: str) -> str:
    return hashlib.sha256(session.encode()).hexdigest()[:16]


def _open_dir(paths, create: bool = False) -> int:
    """A descriptor for the handshake directory, its last two components
    (`safepoints`, the project key) opened without following a link: one
    planted there would redirect the handshake somewhere a container can
    write, so it is refused (`OSError`), never written through or replaced.
    What lies above is the state root, judged by `exposed` on resolved paths.
    """
    where = directory(paths)
    base, parts = where.parent.parent, (where.parent.name, where.name)
    if create:
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def _write(paths, name: str, record: dict) -> None:
    fd = _open_dir(paths, create=True)
    try:
        tmp = f".{name}.{os.getpid()}.{os.urandom(6).hex()}.tmp"
        out = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                      0o600, dir_fd=fd)
        try:
            os.write(out, json.dumps(record).encode())
        finally:
            os.close(out)
        try:
            os.replace(tmp, name, src_dir_fd=fd, dst_dir_fd=fd)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp, dir_fd=fd)
            raise
    finally:
        os.close(fd)


def _read(paths, name: str) -> dict | None:
    """The record, or None when there is none. Raises `OSError` when it
    cannot be read for another reason — a link on the way included."""
    try:
        fd = _open_dir(paths)
    except FileNotFoundError:
        return None
    try:
        try:
            handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        except FileNotFoundError:
            return None
        try:
            raw = os.read(handle, FILE_MAX_BYTES + 1)
        finally:
            os.close(handle)
    finally:
        os.close(fd)
    if len(raw) > FILE_MAX_BYTES:
        return None                      # no record of ours is this large
    try:
        record = json.loads(raw)
    except Exception:                    # malformed, undecodable, too deep
        return None
    return record if isinstance(record, dict) else None


def _unlink(paths, name: str) -> bool:
    try:
        fd = _open_dir(paths)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    try:
        os.unlink(name, dir_fd=fd)
    except FileNotFoundError:
        pass
    except OSError:
        return False
    finally:
        os.close(fd)
    return True


def _names(paths) -> list[str]:
    try:
        fd = _open_dir(paths)
    except FileNotFoundError:
        return []
    try:
        return os.listdir(fd)
    finally:
        os.close(fd)


def _rmdir(paths) -> None:
    """The project's handshake directory, if nothing is left in it."""
    where = directory(paths)
    try:
        fd = _open_dir(paths)
        os.close(fd)                         # it is a real directory, not a link
        os.rmdir(where)
    except OSError:
        pass


def _me() -> dict:
    pid = os.getpid()
    return {"pid": pid, "start": procs.start_time(pid)}


def _pid(value) -> int:
    """A recorded pid, or 0 for anything that is not a plain positive int."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _alive(who) -> bool:
    """Is the recorded process still that process, and not a zombie?"""
    if not isinstance(who, dict) or not _pid(who.get("pid")):
        return False
    start = who.get("start")
    return procs.living(_pid(who.get("pid")), start if isinstance(start, str) else "")


# ------------------------------------------------------------------ key --

_secret: bytes | None = None
_driver: dict | None = None          # the server's: the driver that holds its key
# The driver's request counter for this key: strictly increasing, signed into
# every request and kept by its commit or cancellation. Records are ordered by
# it, never by a clock (review r8). A new key starts it again, and records
# signed with an old key do not verify.
_sequence = 0


def new_key() -> str:
    """The driver's: a fresh key for this run, as the hex the CLI is given."""
    global _secret, _sequence
    _secret = os.urandom(32)
    _sequence = 0
    return _secret.hex()


def adopt_key() -> None:
    """The server's, at startup: take the key out of the inherited
    environment, so no process it starts inherits it. Without a usable one it
    keeps a key nobody else has: its records then verify for no driver, and
    the driver, seeing it in `/proc` unregistered, does not stop it."""
    global _secret, _driver
    raw = os.environ.pop(KEY_ENV, "")
    who = os.environ.pop(DRIVER_ENV, "")
    try:
        key = bytes.fromhex(raw)
    except ValueError:
        key = b""
    _secret = key if len(key) == 32 else os.urandom(32)
    pid, _, start = who.partition(":")
    _driver = ({"pid": int(pid), "start": start}
               if len(key) == 32 and pid.isdigit() and start else None)


def driver_identity() -> str:
    """This process as the driver, for its CLI's environment."""
    pid = os.getpid()
    return f"{pid}:{procs.start_time(pid)}"


def strip_key(env: dict) -> dict:
    """`env` without the key or the driver's identity — for every environment
    built for anything but the orchestrator's own CLI (CW-R2b)."""
    env.pop(KEY_ENV, None)
    env.pop(DRIVER_ENV, None)
    return env


def _key_bytes() -> bytes:
    if _secret is None:
        adopt_key()
    return _secret


def _canonical(record: dict) -> bytes:
    return json.dumps({k: v for k, v in record.items() if k != "mac"},
                      sort_keys=True, separators=(",", ":")).encode()


def _sign(record: dict) -> dict:
    return {**record, "mac": hmac.new(_key_bytes(), _canonical(record),
                                      hashlib.sha256).hexdigest()}


_MAC = re.compile(r"[0-9a-f]{64}")


def _verified(record: dict | None, *kinds: str) -> dict | None:
    """`record` if it is one of `kinds` and its MAC verifies, else None.
    Never raises, whatever the record holds: malformed is invalid."""
    try:
        if not isinstance(record, dict) or record.get("type") not in kinds:
            return None
        mac = record.get("mac")
        if not isinstance(mac, str) or not _MAC.fullmatch(mac):
            return None
        expected = hmac.new(_key_bytes(), _canonical(record), hashlib.sha256).hexdigest()
        return record if hmac.compare_digest(mac, expected) else None
    except Exception:
        return None


# ------------------------------------------------------------ server side --

def register(paths, session: str) -> None:
    """Say this root server exists, before it serves anything. Raises
    `OSError` when it cannot: the caller retries, and meanwhile the driver,
    which can see an unregistered server in `/proc`, does not stop it."""
    _write(paths, f"safepoint-server-{os.getpid()}.json",
           _sign({"type": "registration", **_me(), "session": session}))


def await_grant(gate: "Gate") -> None:
    """A root server's first step, before it registers or serves anything:
    under a live driver it stays closed until that driver grants it. A
    deleted or replayed barrier cannot open it — only the grant, which the
    driver issues only while no safe point is in progress, so only to a
    server every enumeration of a later request includes (review r9)."""
    if _secret is None:
        adopt_key()
    if _driver is not None and _alive(_driver):
        gate.ungranted, gate.driver = True, _driver


def granted(paths, session: str) -> bool:
    """Has the driver granted this process, by pid, start time and session?"""
    try:
        record = _verified(_read(paths, f"safepoint-server-{os.getpid()}.grant"), "grant")
    except Exception:
        return False
    me = _me()
    return (record is not None and record.get("pid") == me["pid"]
            and bool(me["start"]) and record.get("start") == me["start"]
            and record.get("session") == session)


def unregister(paths) -> None:
    _unlink(paths, f"safepoint-server-{os.getpid()}.json")
    _unlink(paths, f"safepoint-server-{os.getpid()}.safe")
    _unlink(paths, f"safepoint-server-{os.getpid()}.grant")
    _rmdir(paths)


BARRIER_TYPES = ("request", "commit", "cancel")


def request_for(paths, session: str) -> dict | None:
    """The authenticated barrier record for this session, if it is for this
    process — its CLI is one of this process's ancestors, where that can be
    read at all. Raises `OSError` when it cannot be read."""
    record = _verified(_read(paths, f"safepoint-barrier-{_key(session)}.json"),
                       *BARRIER_TYPES)
    if record is None or record.get("session") != session \
            or not isinstance(record.get("nonce"), str) or not record["nonce"]:
        return None
    cli = _pid(record.get("cli").get("pid")) if isinstance(record.get("cli"), dict) else 0
    if cli <= 1 or procs.descends_from(os.getpid(), cli) is False:
        return None
    return record


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value == value else None      # not NaN


def _fresh(record: dict, limit: float) -> bool:
    """Written within `limit` seconds by the monotonic clock both sides share
    on this host: an old authenticated record replayed later is not a request
    now. A reading from the future is not comparable, so not fresh."""
    written = _number(record.get("monotonic"))
    if written is None:
        return False
    return 0 <= time.monotonic() - written <= limit


def acknowledge(paths, nonce: str, session: str) -> bool:
    try:
        _write(paths, f"safepoint-server-{os.getpid()}.safe",
               _sign({"type": "ack", **_me(), "nonce": nonce, "session": session}))
    except OSError:
        return False
    return True


def withdraw(paths) -> bool:
    """Remove this server's acknowledgement, before admission reopens."""
    return _unlink(paths, f"safepoint-server-{os.getpid()}.safe")


class Gate:
    """The Runner's side: whether it admits launches, and what is in flight.

    `enter` is the admission check and the count in one synchronous step, and
    `observe` closes it and acknowledges in another, so nothing can be
    admitted between the server seeing zero transitions and it saying so.
    """

    def __init__(self) -> None:
        self.nonce: str | None = None
        self.requester: dict | None = None   # the driver that asked
        self.committed = False
        self.transitions = 0
        self.settled_at = 0.0           # when the last transition ended
        self.spent: set[str] = set()    # nonces over: never acted on again
        self.newest = 0                 # `seq` of the newest request seen
        # Started under a live driver and not yet granted by it (review r9):
        # closed to admission whatever the barrier says, until a grant naming
        # this server, or that driver's death.
        self.ungranted = False
        self.driver: dict | None = None

    @property
    def closed(self) -> bool:
        return self.nonce is not None or self.ungranted

    def enter(self, what: str) -> "_Ticket":
        """Admit one transition, or refuse it while the server is stopping."""
        if self.closed:
            raise RuntimeError(STOPPING.format(what=what))
        self.transitions += 1
        return _Ticket(self)

    async def wait_granted(self, bound: float = 30.0) -> None:
        """Before an admission: a server that has just started under a live
        driver waits — briefly, the driver grants within a poll — rather
        than refuse. Past the bound, `enter` refuses as usual."""
        waited = 0.0
        while self.ungranted and self.nonce is None and waited < bound:
            await asyncio.sleep(POLL_SECONDS)
            waited += POLL_SECONDS

    async def enter_when_open(self) -> "_Ticket":
        """For a transition the Runner starts by itself (a retry, a wrap-up):
        not refused — it waits, and if the stop goes ahead the server's exit
        ends the wait with nothing started."""
        while self.closed:
            await asyncio.sleep(POLL_SECONDS)
        self.transitions += 1
        return _Ticket(self)

    def close(self, nonce: str, committed: bool, requester: dict | None = None) -> None:
        if self.committed and self.nonce != nonce:
            return
        if self.nonce != nonce:
            self.nonce, self.requester = nonce, requester
        if committed:
            self.committed = True

    def reopen(self) -> None:
        if self.nonce:
            self.spent.add(self.nonce)
        self.nonce, self.requester, self.committed = None, None, False


class _Ticket:
    def __init__(self, gate: Gate) -> None:
        self.gate, self.open = gate, True

    def end(self) -> None:
        if self.open:
            self.open = False
            self.gate.transitions -= 1
            self.gate.settled_at = time.monotonic()

    def __enter__(self) -> "_Ticket":
        return self

    def __exit__(self, *_) -> None:
        self.end()


def observe(paths, session: str, gate: Gate, acked: list, limit: float) -> None:
    """One look at the barrier: latch and acknowledge, or reopen.

    The safe point is a quiet moment: nothing admitted in flight, and nothing
    for `QUIET_SECONDS`. Admission closes at that moment and the
    acknowledgement is written in the same step, so nothing can be admitted
    between the two; until then launches are still admitted and waited for.

    Only authenticated records count, and only fresh ones (`limit`: the
    driver's bound plus room) can close admission. Once closed, it reopens
    only for an authenticated cancellation of that request or the confirmed
    death of the driver that made it; the acknowledgement goes first, so a
    driver reading it late finds no yes the server no longer means.
    """
    if gate.ungranted and (not _alive(gate.driver) or granted(paths, session)):
        gate.ungranted = False
    record = request_for(paths, session)
    if record is not None:
        asked = _pid(record.get("seq"))             # a positive int, else 0
        nonce = record["nonce"]
        if not asked or nonce in gate.spent:
            record = None
        elif nonce != gate.nonce and asked < gate.newest:
            # Older than one already seen: superseded, whether or not its own
            # cancellation was ever observed. Monotonic, never reopened.
            gate.spent.add(nonce)
            record = None
        else:
            gate.newest = max(gate.newest, asked)
    kind = record.get("type") if record is not None else None
    if gate.nonce is not None:
        if record is not None and record["nonce"] != gate.nonce and not gate.committed:
            # A newer request of the driver's: the acknowledged one is over,
            # and spent — its cancellation, replayed later, reopens nothing.
            gate.spent.add(gate.nonce)
            gate.nonce, gate.requester = record["nonce"], record.get("requester")
        mine = record is not None and record["nonce"] == gate.nonce
        if mine and kind == "commit":
            gate.committed = True
        # Only a cancellation of the current request, or its driver's death.
        if (mine and kind == "cancel" and not gate.committed) \
                or not _alive(gate.requester):
            if withdraw(paths):
                gate.reopen()
            return
        if acked[:1] != [gate.nonce] and acknowledge(paths, gate.nonce, session):
            acked[:] = [gate.nonce]
        return
    if record is None or kind == "cancel" or not _alive(record.get("requester")):
        return
    committed = kind == "commit"
    if not _fresh(record, limit + (COMMITTED_GRACE_SECONDS if committed else 0)):
        return
    if committed and not _alive(record.get("cli")):
        return
    quiet = time.monotonic() - gate.settled_at >= QUIET_SECONDS
    if gate.transitions == 0 and (quiet or committed):
        gate.close(record["nonce"], committed, record.get("requester"))
        if acknowledge(paths, record["nonce"], session):
            acked[:] = [record["nonce"]]


# ------------------------------------------------------------ driver side --

def servers(paths, session: str, cli_pid: int) -> list[dict]:
    """The live root servers of this session registered beneath this CLI,
    as their authenticated registrations. Raises `OSError` when they cannot
    be listed. One that does not verify is not listed — and is then found
    unregistered in `/proc`, which blocks the stop."""
    names = _names(paths)
    found = []
    for name in names:
        if not (name.startswith("safepoint-server-") and name.endswith(".json")):
            continue
        record = _verified(_read(paths, name), "registration")
        if not record or record.get("session") != session or not _alive(record):
            continue
        if procs.descends_from(_pid(record.get("pid")), cli_pid) is False:
            continue
        found.append(record)
    return found


def _is_root_server(pid: int, session: str) -> bool | None:
    """Is this process a root multiagents server of `session`, from `/proc`?
    None when it exists and cannot be read: unknown, not no."""
    try:
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        environ = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except (FileNotFoundError, ProcessLookupError):
        return False                        # gone since it was listed
    except OSError:
        return None
    if SERVER_MODULE.encode() not in argv:
        return False
    env = dict(item.split(b"=", 1) for item in environ if b"=" in item)
    return (env.get(b"MULTIAGENTS_SESSION_ID", b"").decode(errors="replace") == session
            and not env.get(b"MULTIAGENTS_AGENT_ID"))


def unregistered(session: str, cli_pid: int, registered: list[dict]) -> int | None:
    """How many root servers of this session run beneath the CLI without a
    registration (or a registered one beneath them: `uv run` wraps the real
    one), from `/proc`. None where `/proc` cannot say."""
    below = procs.descendants(cli_pid)
    if below is None:
        return None
    known = {_pid(r.get("pid")) for r in registered}
    count = 0
    for pid in below:
        if pid in known:
            continue
        server = _is_root_server(pid, session)
        if server is None:
            return None
        if not server:
            continue
        under = procs.descendants(pid)
        if under is None:
            return None
        if not known.intersection(under):
            count += 1
    return count


def request(paths, session: str, cli_pid: int) -> str:
    """Ask this CLI's servers to reach a safe point. Returns the nonce.
    Raises `OSError` when the request cannot be written."""
    global _sequence
    nonce = os.urandom(16).hex()
    _sequence += 1
    _write(paths, f"safepoint-barrier-{_key(session)}.json", _sign({
        "type": "request", "session": session, "nonce": nonce, "state": "drain",
        "seq": _sequence, "at": time.time(), "monotonic": time.monotonic(),
        "requester": _me(),
        "cli": {"pid": cli_pid, "start": procs.start_time(cli_pid)}}))
    return nonce


def acknowledged(paths, pid: int, nonce: str) -> bool:
    """Has the registered server `pid` acknowledged this request? Only an
    authenticated acknowledgement naming the nonce, the server's session, its
    pid and its start time counts — and only while that process is still the
    one alive. Raises `OSError` when either file cannot be read."""
    registration = _verified(_read(paths, f"safepoint-server-{pid}.json"), "registration")
    record = _verified(_read(paths, f"safepoint-server-{pid}.safe"), "ack")
    if not registration or not record:
        return False
    start = str(registration.get("start") or "")
    # The start time must be READ now and equal: an unreadable one cannot
    # tell the server from a process that has taken its pid since.
    return (bool(start) and record.get("nonce") == nonce and record.get("pid") == pid
            and registration.get("pid") == pid
            and str(record.get("start") or "") == start
            and record.get("session") == registration.get("session")
            and procs.start_time(pid) == start and procs.living(pid, start))


def settle(paths, session: str, nonce: str, state: str) -> bool:
    """`commit` the stop, or withdraw it as `cancelled`, signed. Only our own
    authenticated request: a newer one is left alone. False when it could not
    be written."""
    name = f"safepoint-barrier-{_key(session)}.json"
    kind = "commit" if state == "commit" else "cancel"
    try:
        record = _verified(_read(paths, name), *BARRIER_TYPES)
        if record is None or record.get("nonce") != nonce:
            return False
        fields = {k: v for k, v in record.items() if k != "mac"}
        _write(paths, name, _sign({**fields, "type": kind, "state": state,
                                   "at": time.time(), "monotonic": time.monotonic()}))
    except Exception:                    # never let a record stop the CLI
        return False
    return True


def clear(paths, session: str, nonce: str) -> None:
    """After a committed stop, once the CLI has gone: remove the request, and
    the directory if nothing else is in it. A server that acknowledged stays
    closed until it exits with its CLI, as it would have anyway."""
    name = f"safepoint-barrier-{_key(session)}.json"
    try:
        record = _verified(_read(paths, name), *BARRIER_TYPES)
        if record is not None and record.get("nonce") == nonce:
            _unlink(paths, name)
        _rmdir(paths)
    except OSError:
        pass


class Granter:
    """The driver's: grants each server of its CLI that registers while no
    safe point is in progress (review r9). Polls on its own thread, because
    the driver's own poll is a minute long and a server waits for its grant
    before admitting anything. `hold` before a request stops granting, and
    a cancelled request `release`s it; a committed one never does."""

    def __init__(self, paths, session: str, cli: int):
        import threading
        self.paths, self.session, self.cli = paths, session, cli
        self._lock = threading.Lock()
        self._busy = False
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="safepoint-granter")

    def start(self) -> "Granter":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._done.set()

    def hold(self) -> None:
        with self._lock:
            self._busy = True

    def release(self) -> None:
        with self._lock:
            self._busy = False

    def tick(self) -> None:
        with self._lock:
            if self._busy:
                return
            try:
                registered = servers(self.paths, self.session, self.cli)
            except Exception:
                return
            for record in registered:
                who = (_pid(record.get("pid")), record.get("start"))
                if self._holds_grant(who):
                    continue
                try:
                    _write(self.paths, f"safepoint-server-{who[0]}.grant", _sign(
                        {"type": "grant", "pid": who[0], "start": who[1],
                         "session": self.session}))
                except OSError:
                    continue

    def _holds_grant(self, who: tuple) -> bool:
        """Is a valid grant for `who` on disk? Checked every tick, not
        remembered (review r10): a grant deleted or damaged before its server
        read it is written again, or that server would wait out its bound and
        refuse every admission for as long as this driver lives."""
        try:
            record = _verified(_read(self.paths, f"safepoint-server-{who[0]}.grant"), "grant")
        except Exception:
            return False
        return (record is not None and record.get("pid") == who[0]
                and record.get("start") == who[1] and record.get("session") == self.session)

    def _run(self) -> None:
        while not self._done.wait(POLL_SECONDS):
            self.tick()
