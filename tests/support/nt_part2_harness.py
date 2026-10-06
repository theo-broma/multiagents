"""Harness for the NT part 2 tests (NT-R3..R6, the pause of NT-R4, NT-R8's pause
clearing): a real host scheduler process, started with `--clock-file` so that
"now" is a file the test rewrites, publishing to a fake ntfy server on 127.0.0.1.

Only public surfaces: the project's `notify:` config, the scheduler socket
(`scheduler_status`, `wait_for_nodes`), the CLI, the tree's question API, and
what the fake server receives. Placeholders only (127.0.0.1 / example.invalid).
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nt_harness import FakeNtfy, TOKEN, leaks_of, token_file, wait_until  # noqa: E402,F401
from nc_fixture.clock import ClockWorld  # noqa: E402
from nc_fixture.gitworld import GitWorld  # noqa: E402
from nc_fixture.world import blocked_codes  # noqa: E402,F401
from multiagents.tree import Tree  # noqa: E402

TOPIC = "multiagents-test"
INTERVAL = 300            # min_interval_seconds in most tests: far above any real elapsed time
ARRIVE = 3.0              # a message that must be sent arrives within this many real seconds
QUIET = 1.0               # a message that must NOT be sent has not arrived after this long
HOST_TICK = 0.2
HOUR = 3600
QID = re.compile(r"\bq-[0-9a-f]{6}\b")


def accepted_bodies(fake: FakeNtfy, since: int = 0) -> list[str]:
    return [r.text() for r in fake.requests[since:]]


class NtWorld(ClockWorld, GitWorld):
    """A ClockWorld with a `notify:` section and helpers over the fake server."""

    def __init__(self, tmp_path, monkeypatch, fake: FakeNtfy, *, notify="default", events=None, scheduler=None, **kw):
        super().__init__(tmp_path, monkeypatch, now=datetime.now(timezone.utc),
                         tick_seconds=HOST_TICK,
                         scheduler={"anomaly_interval_seconds": 5, "anomaly_admission_seconds": 30,
                                    "anomaly_held_seconds": 30, "starvation_after_seconds": 10 ** 9,
                                    **(scheduler or {})}, **kw)
        self.fake = fake
        self.tmp_path = tmp_path
        self.pc = self.provider("pcfx", max_concurrent=1)
        self.agent("pcworker", "pcfx")
        if notify == "default":
            notify = {"ntfy_url": fake.url, "topic": TOPIC, "min_interval_seconds": INTERVAL}
            if events is not None:
                notify["events"] = events
        self.set_notify(notify)

    def set_notify(self, notify) -> None:
        if notify is None:
            self.project.pop("notify", None)
        else:
            self.project["notify"] = notify
        self.write_config()

    def tree(self) -> Tree:
        self.paths.ensure()
        return Tree(self.paths.tree_file, self.paths.events_file)

    # ---- raising events
    def question(self, topic="topic", text="a question") -> str:
        return self.tree().add_question("ag-0a0a0a", topic, text)["id"]

    def answer(self, qid: str) -> None:
        self.tree().answer_question(qid, "an answer")

    def held_node(self, filename="shared.txt") -> str:
        """A node held `input_conflict`: it takes two inputs that both wrote
        `filename`. Returns the held node's id; its hold detail names the file."""
        a = self.coder("A", {filename: "from A\n"})
        b = self.coder("B", {filename: "from B\n"})
        self.wait_state(a, "done", 15)
        self.wait_state(b, "done", 15)
        node = self.coder("C", {"c.txt": "c"}, inputs=[{"node": a}, {"node": b}])
        self.until(lambda: self.get(node).get("state") == "held", timeout=15, what="the node held")
        return node

    def done_node(self, tag="DONE") -> str:
        node = self.simple(tag, "worker")
        self.until(lambda: self.get(node).get("state") == "done", timeout=15, what="the node done")
        return node

    def relaunch(self, node: str) -> dict:
        rev = self.get(node)["revision"]
        return self.rpc("relaunch_node", {"id": node, "revision": rev})

    def release(self) -> None:
        """Let the rate limit's interval elapse."""
        self.advance(seconds=INTERVAL + 1)

    def blocked_pair(self) -> str:
        """A running holder and a node admission refuses (provider concurrency):
        past `anomaly_admission_seconds` the scheduler records an anomaly on it."""
        holder = self.simple("H", "pcworker", fx={"gate": "gH"})
        self.wait_running(holder)
        blocked = self.simple("B", "pcworker")
        self.until(lambda: "admission:provider_concurrency" in blocked_codes(self.get(blocked)),
                   what="B refused for provider concurrency")
        self.holder = holder
        return blocked

    # ---- time
    def hours(self, n: float) -> None:
        self.advance(hours=n)

    def advance_steps(self, seconds: float, step: float = 100) -> None:
        """Advance in steps, so that every intermediate instant is evaluated."""
        left = seconds
        while left > 0:
            self.advance(seconds=min(step, left))
            left -= step

    # ---- observing what the server received
    def got(self, n: int, timeout: float = ARRIVE) -> bool:
        return wait_until(lambda: len(self.fake.requests) >= n, timeout)

    def quiet(self, seconds: float = QUIET) -> None:        # noqa: D401 - shadows World.quiet on purpose
        time.sleep(seconds)

    def bodies(self, since: int = 0) -> list[str]:
        return [r.text() for r in self.fake.requests[since:]]

    def first_message(self, with_questions: int = 1) -> int:
        """Start the scheduler over `with_questions` open questions and wait for
        the activation summary; returns how many requests the server has seen."""
        self.seeded = [self.question(text=f"seed {i}") for i in range(with_questions)]
        self.start_scheduler()
        assert self.got(1), "no activation summary reached the server"
        return len(self.fake.requests)

    # ---- status
    def notify_status(self) -> dict:
        result = self.status().get("notify")
        assert isinstance(result, dict), f"scheduler_status has no `notify` mapping: {sorted(self.status())}"
        return result


def field(status: dict, *needles: str):
    """The value of the first key of `status` whose name holds any needle: the
    contract names the fields (pending count, last accepted, current failure) but
    not their keys."""
    for key, value in status.items():
        if any(n in key.lower() for n in needles):
            return value
    raise AssertionError(f"no key like {needles} in {sorted(status)}")


def process_output(world: NtWorld) -> str:
    """What the scheduler process printed, once it has exited."""
    chunks = []
    for proc in world._popen:
        if proc.poll() is not None and proc.stdout and not proc.stdout.closed:
            try:
                chunks.append(proc.stdout.read())
            except Exception:                              # noqa: BLE001
                pass
    return "".join(chunks)
