# C3 — a defect found while testing the resume path

Reproduced by the test engineer while building the `consult()` case for R7, and
deliberately not acted on: it is not a resume-path defect and was outside that
contract. Recorded so the next review does not rediscover it, and because it is
the kind of failure that produces no error at all.

**F170** — `consult()` never returns when its run is silently retried
*Class:* bug
*Severity:* high
*Where:* `src/multiagents/runner.py`, `_consume`'s `relaunched` path and `consult()`'s await
*Evidence:* reproduction
*What happens:* On a free retry, `_consume` returns `relaunched=True` and therefore never sets the **first** `Run`'s `done` event. `consult()` is awaiting exactly that event; the replacement `Run` sets a different one. So a conversational agent whose turn dies cheaply and unexplained never returns to its caller, and never reaches the `no reply within Ns` path either — it waits out `wait_for`'s `limit + 30` and then reports a timeout that describes none of what happened.
*Disposition:* fix
*Reasoning:* The failure mode is silence, which is the worst shape for a defect in a conversational agent: the caller cannot distinguish "still thinking" from "will never answer", and the eventual timeout names the wrong cause. It was found only because the test engineer's fake CLI had to be made to emit a line before `consult()` would return at all — meaning the existing consult tests pass because their fixtures happen to avoid the retry path, not because the path works. Adjacent to the R7 family in that `consult()` and `steer()` keep diverging from each other, but a distinct defect with a distinct cause.
