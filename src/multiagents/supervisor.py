"""Watchdog over a running agent's event stream.

A subprocess agent is a black box unless you read its stream, which is why every
provider is configured to emit streaming JSON rather than a single result at
exit. With the stream in hand, four independent conditions can be detected while
the run is still alive:

* **silence** — no event for `silence_timeout`; a live agent emits step updates
  every few seconds, so quiet means a wedged tool or a dead connection
* **wall clock** — the run exceeded its budget
* **doom loop** — the same tool called with the same arguments over and over,
  which is the subprocess analogue of a circular delegation
* **runaway steps** — more steps than any sane task requires

A trip marks the run ``stuck`` and records why. It deliberately does **not**
kill the process: the parent decides whether to steer, extend or stop, and
killing on suspicion throws away work that was often nearly finished.
"""

from __future__ import annotations

import hashlib
import time
from collections import deque
from dataclasses import dataclass, field

from .providers import Event


@dataclass
class Trip:
    reason: str
    detail: str


@dataclass
class Supervisor:
    silence_timeout: float = 180.0
    wall_timeout: float = 900.0
    max_steps: int = 120
    loop_repeats: int = 3
    loop_window: int = 12

    started: float = field(default_factory=time.monotonic)
    last_event: float = field(default_factory=time.monotonic)
    steps: int = 0
    signatures: deque[str] = field(default_factory=lambda: deque(maxlen=12))
    tripped: Trip | None = None

    def __post_init__(self) -> None:
        self.signatures = deque(maxlen=self.loop_window)

    # ------------------------------------------------------------- ingestion --

    def observe(self, event: Event) -> Trip | None:
        """Feed one event. Returns a Trip the first time a condition fires."""
        self.last_event = time.monotonic()
        if event.step is not None:
            self.steps = max(self.steps, event.step + 1)
        elif event.kind == "step":
            self.steps += 1

        signature = event.loop_signature()
        if signature:
            digest = hashlib.sha1(signature.encode()).hexdigest()[:12]
            self.signatures.append(digest)
            trip = self._check_loop(event)
            if trip:
                return self._trip(trip)

        if self.steps > self.max_steps:
            return self._trip(Trip("runaway_steps", f"{self.steps} steps exceeds max_steps={self.max_steps}"))
        return None

    def _check_loop(self, event: Event) -> Trip | None:
        """Identical repeats, and simple alternating cycles."""
        if len(self.signatures) < self.loop_repeats:
            return None
        recent = list(self.signatures)

        tail = recent[-self.loop_repeats:]
        if len(set(tail)) == 1:
            return Trip(
                "doom_loop",
                f"{event.name} called {self.loop_repeats}x with identical arguments",
            )

        # A -> B -> A -> B ... repeated often enough to be a cycle, not a retry.
        span = self.loop_repeats * 2
        if len(recent) >= span:
            window = recent[-span:]
            if len(set(window)) == 2 and window[::2].count(window[0]) == len(window[::2]) \
               and window[1::2].count(window[1]) == len(window[1::2]):
                return Trip("doom_loop", f"two-step cycle repeated {self.loop_repeats}x (last: {event.name})")
        return None

    # --------------------------------------------------------------- polling --

    def check_timers(self) -> Trip | None:
        """Call periodically; detects conditions no event will announce."""
        if self.tripped:
            return None
        now = time.monotonic()
        if now - self.started > self.wall_timeout:
            return self._trip(Trip("timeout", f"exceeded {self.wall_timeout:.0f}s wall clock"))
        if now - self.last_event > self.silence_timeout:
            quiet = now - self.last_event
            return self._trip(Trip("silence", f"no stream event for {quiet:.0f}s"))
        return None

    def _trip(self, trip: Trip) -> Trip | None:
        if self.tripped:
            return None                  # report each run's first trip only
        self.tripped = trip
        return trip

    # ---------------------------------------------------------------- status --

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    @property
    def quiet_for(self) -> float:
        return time.monotonic() - self.last_event


# --------------------------------------------------------------------------
# Quota failures are the fourth trip, but they surface at exit rather than
# mid-stream, so they are classified from the final status and stderr.
# --------------------------------------------------------------------------

_QUOTA_MARKERS = (
    "resource_exhausted", "rate limit", "rate_limit", "quota", "429",
    "too many requests", "usage limit", "insufficient credit", "out of credit",
)


def looks_like_quota_failure(status: str, stderr: str, text: str = "") -> bool:
    blob = f"{status}\n{stderr}\n{text}".lower()
    return any(marker in blob for marker in _QUOTA_MARKERS)
