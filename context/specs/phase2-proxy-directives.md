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

---

# Amendment — after the first R13 run

The first run was interrupted before writing the guards, but it corrected this
contract three times and built the foundation the next one needs. Its handoff is
at `HANDOFF-R13.md`, and `tests/support/c1_harness.py` now carries
`parse_tinyproxy_conf` and `proxy_config` — a parser that reads the config the
way tinyproxy's grammar does (keyword lower-cased, `#` stripped, quoted values
unquoted, every value for a keyword kept so a contradicting second line is
visible) and a `directive(name)` accessor that fails unless the keyword appears
exactly once.

## Correction 1 — the directive is `Filter`, not `FilterFile`

Both this contract and F54's text say `FilterFile`. The production code emits
`Filter "/etc/tinyproxy/filter"` (`src/multiagents/executor/docker.py:743`),
which is tinyproxy's real keyword. Verified by inspection. **The mutation check
must alter that line**, not one that does not exist.

## Correction 2 — this contract's premise was wrong

It said the suite "validates the directives not at all". That was inherited from
F50's text and it stopped being true the moment the finding was filed:
`tests/test_adversary_allowlist_mutation.py` (commit `38f3786`, the adversary run
that *found* these defects) already contains five substring guards —
`assert "FilterDefaultDeny Yes" in conf` and four siblings, at lines 26, 35, 43,
52 and 65.

So a plain deletion already goes red today. **What the remaining gap actually
is**, demonstrated rather than asserted:

```
'FilterDefaultDeny Yes' in '# FilterDefaultDeny Yes'          -> True   (commented out, inert)
'FilterDefaultDeny Yes' in 'XFilterDefaultDeny Yes'           -> True   (keyword misspelled, ignored)
'FilterDefaultDeny Yes' in 'FilterDefaultDeny Yes\nFilterDefaultDeny No'
                                                              -> True   (contradicted, last line wins)
```

Each of those three leaves the substring guard green and the directive without
effect. **That is R13's real content**, and it is narrower and sharper than what
this contract originally described. Write the precise guards in a **new** file
and leave `test_adversary_allowlist_mutation.py` untouched: it is the record of
the finding.

My own instruction in the section above — *"assert the directive and its value,
not a substring of the file"* — turns out to name the live gap exactly. Keep it.

## Correction 3 — F54 cannot be settled where I asked

"The `Filter` directive names the path the patterns are actually written to" is
not checkable inside `write_proxy_config`. The directive names a **container**
path; the patterns are written to a **host** path. The only thing making them one
file is the bind mount at `docker.py:775`.

So F54 needs a second test that reaches `ensure_proxy` with `_run` monkeypatched
— the `tests/test_core.py:5454` idiom, inspecting argv rather than running
docker — asserting the `-v` argument maps the written filter file onto the
directive's value. `HANDOFF-R13.md` carries the stub shape and the
`stdout "absent"` trick that keeps `ensure_proxy` on its happy path.

## F56 is already closed, by the run that filed it

`test_filter_file_ends_with_newline` exists at line 80 of the adversary's own
file and passes. The adversary filed F56 and wrote its test in the same commit,
and the ledger never knew. Marked `fixed`; do not write it again.

## A tooling gap that affected this run

The first run reported that neither `read_finding` nor `consult("dev-advisor")`
was available to it — only the generic `Agent` tool. It recovered F50–F56 from
`context/review/REPORT.md` instead, which was enough, but no consult was
possible at any point.

That is the second instruction of this kind I have got wrong: every phase 1 task
told its agent to read tickets with `list_tickets`, which subagents also cannot
call. **Point agents at files in the tree, not at orchestrator tools.** The
findings are readable at `context/review/REPORT.md` and in the per-context files
under `context/review/`.
