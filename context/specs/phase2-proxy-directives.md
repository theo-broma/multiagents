# Phase 2 contract — making the proxy suite load-bearing

The interface contract for `BRIEF.md` phase 2 item 2: **F50**, and with it
**F51**, **F52**, **F53** and **F54**. Written by the orchestrator, after a
consult.

Read them with `read_finding("F50")` and so on. `list_tickets` is not reachable
from an agent; do not try it.

---

## The finding, and why it is the review's heaviest

`write_proxy_config` generates two things: a set of ERE filter **patterns**, and
the tinyproxy **directives** that decide what those patterns mean.

The 48-test characterization suite validates the patterns thoroughly — the
adversary found that every mutation to the regex construction is caught by
between 9 and 43 tests. It validates the directives **not at all**.

| id | severity | mutation that survives every test |
|---|---|---|
| **F50** | critical | delete `FilterDefaultDeny Yes` — the proxy inverts from allow-list to **open relay** |
| **F51** | medium | `FilterType ere` → `regex` — BRE instead of ERE, so the patterns change meaning |
| **F52** | medium | `FilterCaseSensitive Off` → `On` — DNS is case-insensitive; hosts stop matching |
| **F53** | medium | `FilterURLs Off` → `On` — patterns match a host, not a URL |
| **F54** | medium | change the `FilterFile` path — tinyproxy reads no filter file at all |

F50 is the one to hold in mind. **A single deleted line turns the sandbox into
an open relay and the suite stays green.** The adversary's verdict was
`VERDICT(rejected, 4)`: the suite is not load-bearing for the security-critical
configuration it generates.

---

## R13 — the generated configuration is asserted, not assumed

**Observable behaviour required.** Every directive that determines what the
filter patterns *mean* is asserted present and correct by the suite, so that
altering any one of them turns a test red.

**What must be true afterwards, one per finding:**

- **F50** — a test asserts `FilterDefaultDeny Yes` is present in the generated
  configuration.
- **F51** — a test asserts `FilterType ere`.
- **F52** — a test asserts `FilterCaseSensitive Off`.
- **F53** — a test asserts `FilterURLs Off`.
- **F54** — a test asserts the `FilterFile` directive names the path the patterns
  are actually written to. Not that it names *a* path: that the two agree.

**Assert the directive, not a substring of the file.** A test that greps the
whole config for `"Deny"` passes against a config that says
`FilterDefaultDeny No`. Each assertion must pin the directive and its value.

**This is the change, and it is mostly in the tests.** The production code
already emits these directives correctly — the defect is that nothing checks
them. If you find yourself substantially rewriting `write_proxy_config` to make
this testable, stop and say why: a large production change here would mean the
function has a structural problem the finding did not name, and that is worth
knowing before it is fixed.

---

## Why one agent holds both sides

This runs on **`implementer-deep`**, which carries `readonly_paths: []` scoped to
this phase specifically so it can change `write_proxy_config` and its tests on
one branch. Everything else in this phase keeps the normal tester/implementer
separation; this is the exception the brief argued for, because splitting a
function from the 48 tests that characterise it costs a handoff per iteration.

**The obvious objection is that an agent holding both sides marks its own
homework** — and that this is the shape that produced F50, since the
characterizer that wrote those 48 tests also could not see the directives.

The answer is the verification below, which is not done by that agent.

---

## The mutation check is mine, once, and is not a test

**Do not write a permanent test that mutates production source and asserts a
failure.** That is fragile and it is not what proves anything here. The
permanent guard is the plain assertion — `FilterDefaultDeny Yes` is in the
generated config — and nothing more.

**I will verify it from outside, once, after the branch merges:** delete or alter
each of the five directives in turn and confirm a test goes red each time.
Green-after-your-change is not evidence, because green was already wrong — that
is the entire finding.

Say in your result which test guards which directive, so that check is a list to
walk rather than a hunt.

---

## What is NOT in scope

- **F10** — bare generic suffixes acting as wildcards. Phase 2 item 3, separate
  on purpose: the defect there is the anchor, not the directives.
- **F1, F12, F13, F14** — the ERE escaping. Phase 2 item 1, landing before you
  start, so the function you open will already carry that change.
- **F2, F55** — non-string and null entries raising uncaught exceptions. Neither
  item.
- **The proxy's runtime behaviour.** tinyproxy is not installed here and the
  harness re-derives its documented semantics with Python `re`. You are asserting
  what the configuration *says*, not what tinyproxy does with it.

**One thing you may close if it falls out naturally: F56**, the filter file's
trailing newline, currently untested. If pinning the file's shape is already in
the neighbourhood of what you are writing, cover it and say so. Do not go out of
your way.

---

## What "done" looks like

The suite is green — 967 passed, 3 skipped, 0 failed in a container — plus
whatever phase 2 item 1 has added by the time you start. Run it as
`uv run --frozen python -m pytest`, never `uv run --frozen pytest`.

R13 is done when each of the five directives has a named test guarding it, the
suite is green, and your result lists which test guards which directive so the
mutation check can be walked rather than hunted.
