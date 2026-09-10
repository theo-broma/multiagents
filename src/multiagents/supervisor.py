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
    loop_repeats: int = 5
    loop_window: int = 20

    started: float = field(default_factory=time.monotonic)
    last_event: float = field(default_factory=time.monotonic)
    steps: int = 0
    # (tool signature, worktree state) pairs. The second half is what makes the
    # first usable: a CLI often reports a write as {"TargetFile": "..."} with no
    # content, so three different edits to one file hash identically, and
    # edit -> test -> edit -> test is an A,B,A,B alternation — the correct
    # behaviour of a test agent, which this used to call a doom loop.
    signatures: deque[tuple[str, str]] = field(default_factory=lambda: deque(maxlen=20))
    current_progress: str = ""
    # The last signature seen, and the step it belonged to: together they say
    # whether the next event is a NEW call or the same one reported again.
    last_digest: str = ""
    last_step: int | None = None
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
            # ONE call, not one per lifecycle event. Providers report a tool
            # twice — agy sends state=ACTIVE and then state=DONE for the same
            # invocation, with the same name, arguments and step number — and
            # counting both halved the threshold without anyone deciding to.
            # Measured on a real project: doom_loop produced 53% of every
            # watchdog alert and 89% of those agents went on to merge, because
            # "called 5 times" was really two and a half.
            repeat = (digest == self.last_digest and event.step is not None
                      and event.step == self.last_step)
            self.last_digest, self.last_step = digest, event.step
            if not repeat:
                self.signatures.append((digest, self.current_progress))
                trip = self._check_loop(event)
                if trip:
                    return self._trip(trip)

        if self.steps > self.max_steps:
            return self._trip(Trip("runaway_steps", f"{self.steps} steps exceeds max_steps={self.max_steps}"))
        return None

    def note_progress(self, state: str) -> None:
        """Record the agent's working tree as it is right now.

        Sampled by the caller — this class stays synchronous and free of I/O,
        because it runs inside the loop that has to keep the process's pipe
        drained, and a blocking `git` call there is a deadlock waiting to
        happen.
        """
        self.current_progress = state

    def _stalled(self, window: list[tuple[str, str]]) -> bool:
        """Did the working tree stand still across this window?

        With no sampler wired the value is "" throughout, which reads as
        stalled — so the detector behaves exactly as it did before, rather than
        silently switching itself off where progress cannot be observed.
        """
        return len({progress for _, progress in window}) == 1

    def _check_loop(self, event: Event) -> Trip | None:
        """Identical repeats, and simple alternating cycles.

        Both now require the working tree to have stood still as well. That is
        the invariant that separates the two cases the signature alone cannot:
        an agent re-reading one file changes nothing on disk and is stuck; an
        agent editing and re-testing changes the tree every pass and is
        working, however identical its reported arguments look.
        """
        if len(self.signatures) < self.loop_repeats:
            return None
        recent = list(self.signatures)

        tail = recent[-self.loop_repeats:]
        if len({digest for digest, _ in tail}) == 1 and self._stalled(tail):
            return Trip(
                "doom_loop",
                f"{event.name} called {self.loop_repeats}x with identical arguments "
                f"and nothing changed on disk",
            )

        # A -> B -> A -> B ... repeated often enough to be a cycle, not a retry.
        span = self.loop_repeats * 2
        if len(recent) >= span:
            window = recent[-span:]
            digests = [digest for digest, _ in window]
            if len(set(digests)) == 2 and digests[::2].count(digests[0]) == len(digests[::2]) \
               and digests[1::2].count(digests[1]) == len(digests[1::2]) \
               and self._stalled(window):
                return Trip("doom_loop",
                            f"two-step cycle repeated {self.loop_repeats}x with nothing "
                            f"changing on disk (last: {event.name})")
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


def looks_like_quota_failure(status: str, stderr: str) -> bool:
    """Did the CLI itself report a quota failure?

    Deliberately reads only the run's own failure channels — the provider's
    status field and stderr — never the agent's output. An advisor asked to
    review this system wrote the word "quota" in its reply and was recorded as
    having exhausted its quota: the run was marked failed, its provider put on a
    cooldown, and the conversation lost. An agent discussing a topic is not
    evidence about the run that produced it.
    """
    blob = f"{status}\n{stderr}".lower()
    return any(marker in blob for marker in _QUOTA_MARKERS)
