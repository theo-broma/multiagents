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
    # Further identical repeats (or cycle pairs) before doom_loop re-arms.
    # 0 means "use loop_repeats" — set in __post_init__, since a plain default
    # of loop_repeats here would freeze at the class default rather than
    # tracking whatever loop_repeats a caller actually passed.
    loop_rearm: int = 0
    # Whether this provider's stream rules tag events with a turn id. Read from
    # providers.yaml by the caller and handed in — never decided here by name.
    declares_turn: bool = False
    # Tool names whose reported arguments do not identify the call (SL-R6):
    # repeating one must never trip doom_loop by itself. silence/runaway_steps
    # /timeout are unaffected — only the loop-signature check is skipped.
    opaque_tools: frozenset[str] = field(default_factory=frozenset)
    # bug-8615db: like `opaque_tools`, but only for a call matching specific
    # argument values — see `Provider.opaque_tool_args`. Each entry is
    # `{"tool": <name>, "match": {<arg key>: [<opaque values>]}}`; a call is
    # opaque under this if its name is `tool` and, for every key in `match`,
    # the call's own argument value is one of the listed ones. Kept separate
    # from `opaque_tools` rather than folded in: that one is unconditional on
    # the tool name and a tuple of dicts cannot live in the same frozenset.
    opaque_tool_args: tuple[dict, ...] = field(default_factory=tuple)

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
    # What the working tree looked like the last time silence was considered.
    # Compared, not counted: see check_timers.
    progress_when_last_quiet: str | None = None
    # The last signature seen, and the step it belonged to: together they say
    # whether the next event is a NEW call or the same one reported again.
    last_digest: str = ""
    last_step: int | None = None
    # SL-R3/SL-R6: opaque tool calls skip the loop-signature tracking above
    # entirely (that is what keeps them from tripping doom_loop on their
    # own), so `last_digest` never moves for one. A node stuck on some other
    # signature still needs a way to see "the agent called something new" —
    # counted here instead, since an opaque call's own signature is unknowable.
    opaque_calls: int = 0
    # Distinct turn ids already counted as a step, so only the first sighting
    # of each one moves the counter.
    _seen_turns: set[str] = field(default_factory=set)
    # bug-1b2612: the step index carried by the first tagged event this
    # Supervisor ever sees. Providers such as agy number steps monotonically
    # across a resumed session, not from zero per run, so a steer that
    # respawns a run whose stream had already reached step 100 handed this
    # class an `event.step` of ~100 on its very first event — instantly
    # tripping runaway_steps against a `max_steps` sized for a single run.
    # Each run gets its own Supervisor (see `_supervisor`), so subtracting
    # the first step index seen makes THIS run start counting at 0, exactly
    # like a fresh one, while a genuinely fresh run (first step is 0) is
    # unaffected.
    _step_offset: int | None = None
    # runaway_steps and timeout are terminal: reported at most once per run.
    # doom_loop and silence are not latched here — they re-arm on their own
    # rules (see _check_loop and check_timers).
    _runaway_reported: bool = False
    _timeout_reported: bool = False
    # True from the moment a silence trip is reported until the next real
    # stream event arrives — a moving tree alone must not re-arm it.
    _silence_pending: bool = False

    def __post_init__(self) -> None:
        self.signatures = deque(maxlen=self.loop_window)
        if not self.loop_rearm:
            self.loop_rearm = self.loop_repeats

    # ------------------------------------------------------------- ingestion --

    def observe(self, event: Event) -> Trip | None:
        """Feed one event. Returns a Trip whenever a condition fires."""
        self.last_event = time.monotonic()
        # Any real stream event is proof of life: the next quiet spell gets a
        # fresh look before it can trip silence again.
        self._silence_pending = False
        self.progress_when_last_quiet = None

        if event.step is not None:
            if self._step_offset is None:
                self._step_offset = event.step
            self.steps = max(self.steps, event.step - self._step_offset + 1)
        elif self.declares_turn:
            # A step is one model turn, not one stream line. Untagged events —
            # including anything before the provider's first turn — never
            # count; only the first sighting of each turn id does.
            if event.turn and event.turn not in self._seen_turns:
                self._seen_turns.add(event.turn)
                self.steps += 1
        elif event.kind == "step":
            self.steps += 1

        trip: Trip | None = None
        is_opaque_call = event.kind == "tool" and (
            event.name in self.opaque_tools or self._matches_opaque_args(event))
        if is_opaque_call:
            self.opaque_calls += 1
        signature = None if is_opaque_call else event.loop_signature()
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
            return trip
        if not self._runaway_reported and self.steps > self.max_steps:
            self._runaway_reported = True
            return Trip("runaway_steps", f"{self.steps} steps exceeds max_steps={self.max_steps}")
        return None

    def _matches_opaque_args(self, event: Event) -> bool:
        """bug-8615db: does this call match one of `opaque_tool_args`?

        A poll like agy's `manage_task {Action: status, TaskId: X}` reports
        the SAME TaskId on every check of one task, so an ordinary wait for a
        background job to finish looks identical to a real doom loop under
        the plain tool-name-and-args signature. Matching is scoped to the
        argument values that make a call a poll (`Action: status` or `list`),
        not the tool as a whole — `Action: run` re-launching the same command
        must still trip, same as any other tool.
        """
        for rule in self.opaque_tool_args:
            if event.name != rule.get("tool"):
                continue
            match = rule.get("match") or {}
            if all(event.args.get(key) in values for key, values in match.items()):
                return True
        return False

    def note_progress(self, state: str) -> None:
        """Record the agent's working tree as it is right now.

        Sampled by the caller — this class stays synchronous and free of I/O,
        because it runs inside the loop that has to keep the process's pipe
        drained, and a blocking `git` call there is a deadlock waiting to
        happen.
        """
        self.current_progress = state

    @staticmethod
    def _suffix_run(recent: list[tuple[str, str]]) -> int:
        """Length of the tail run of identical (digest, progress) pairs."""
        last = recent[-1]
        n = 0
        for item in reversed(recent):
            if item != last:
                break
            n += 1
        return n

    @staticmethod
    def _cycle_suffix_run(recent: list[tuple[str, str]]) -> int:
        """Length of the tail run alternating strictly between exactly two
        digests, with the working tree uniformly still across it. Not
        truncated to even length — a dangling half-pair at the tail is
        reported as-is so the caller can tell an in-progress pair from a
        completed one."""
        n = len(recent)
        if n < 2:
            return 0
        x_digest, progress = recent[-1]
        y_digest, y_progress = recent[-2]
        if x_digest == y_digest or y_progress != progress:
            return 0
        pattern = (x_digest, y_digest)
        length = 0
        i = n - 1
        while i >= 0:
            digest, prog = recent[i]
            if prog != progress or digest != pattern[length % 2]:
                break
            length += 1
            i -= 1
        return length

    def _check_loop(self, event: Event) -> Trip | None:
        """Identical repeats, and simple alternating cycles.

        Both require the working tree to have stood still as well. That is
        the invariant that separates the two cases the signature alone cannot:
        an agent re-reading one file changes nothing on disk and is stuck; an
        agent editing and re-testing changes the tree every pass and is
        working, however identical its reported arguments look.

        Re-arms rather than latching: recomputed fresh from `signatures` on
        every call, so there is no separate streak counter to keep in sync. A
        trip fires once at `loop_repeats`, and again every further
        `loop_rearm` repeats — one pair at a time for a two-step cycle, since
        that is the unit that actually repeated.
        """
        if len(self.signatures) < self.loop_repeats:
            return None
        recent = list(self.signatures)

        single_len = self._suffix_run(recent)
        if single_len >= self.loop_repeats:
            position = single_len - self.loop_repeats + 1
            if (position - 1) % self.loop_rearm == 0:
                return Trip(
                    "doom_loop",
                    f"{event.name} called {self.loop_repeats}x with identical arguments "
                    f"and nothing changed on disk",
                )

        cycle_len = self._cycle_suffix_run(recent)
        if cycle_len % 2 == 0 and cycle_len >= self.loop_repeats * 2:
            pairs = cycle_len // 2
            position = pairs - self.loop_repeats + 1
            if (position - 1) % self.loop_rearm == 0:
                return Trip("doom_loop",
                            f"two-step cycle repeated {self.loop_repeats}x with nothing "
                            f"changing on disk (last: {event.name})")
        return None

    # --------------------------------------------------------------- polling --

    def check_timers(self) -> Trip | None:
        """Call periodically; detects conditions no event will announce.

        `runaway_steps` and `timeout` are terminal — reported at most once per
        run. `silence` is per episode: it re-arms only once a real stream
        event has arrived (see observe), never from tree movement alone.
        """
        now = time.monotonic()
        if not self._timeout_reported and now - self.started > self.wall_timeout:
            self._timeout_reported = True
            return Trip("timeout", f"exceeded {self.wall_timeout:.0f}s wall clock")
        if not self._silence_pending and now - self.last_event > self.silence_timeout:
            # "Said nothing" is not "did nothing". A single long tool call —
            # a test suite, an install, a large edit — streams nothing while it
            # runs, and this fired on seven opencode implementers in one night
            # that all merged, then twice more on 2026-09-14 at 181s and 244s
            # against a 180s threshold, both on runs that finished.
            #
            # So the working tree decides, which is the same evidence the
            # doom-loop guard uses and the same reason: a change on disk is
            # proof of work that no stream event announced. The caller samples
            # it only while the agent is quiet, so this costs nothing in the
            # normal case.
            #
            # NOT a threshold change. open-questions.md §3 says those must come
            # from re-measurement, not judgement, and this is deliberately a
            # different kind of fix. It also inherits §3's known limit: an agent
            # with `writes: false` never moves its tree, so for a specifier or a
            # critic this degrades to exactly the old behaviour.
            previous = self.progress_when_last_quiet
            self.progress_when_last_quiet = self.current_progress
            if previous is None:
                # First look. There is a reading but nothing to compare it to,
                # so the question cannot be answered yet — and answering it
                # wrongly here is the whole bug. Costs one poll interval before
                # a genuine stall is reported.
                return None
            if self.current_progress != previous:
                self.last_event = now              # it is working; start again
                return None
            quiet = now - self.last_event
            self._silence_pending = True
            return Trip("silence", f"no stream event for {quiet:.0f}s")
        return None

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
