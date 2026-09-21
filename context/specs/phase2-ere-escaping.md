# Phase 2 contract — escaping the allowlist

The interface contract for `BRIEF.md` phase 2 item 1: **F13**, and with it
**F1**, **F12** and **F14**. Written by the orchestrator.

Read each with `read_finding("F1")` and so on. Their reproductions are in
`tests/test_c1_allowlist_characterization.py` and `tests/test_char_c1_allowlist.py`.

---

## One line, four findings

`write_proxy_config` in `src/multiagents/executor/docker.py` escapes an
`egress_allowlist` entry with `host.replace(".", r"\.")` and drops the result
into `(^|\.)<escaped>$`. The literal dot is the *entire* escaping strategy, so
every other regex metacharacter reaches POSIX ERE meaning exactly what ERE says
it means.

| id | severity | what the entry does |
|---|---|---|
| **F1** | critical | `evil.com\|.*` — alternation has ERE's lowest precedence, so the line matches **any host** |
| **F13** | high | an unbalanced `(` or `[` produces an invalid line that **crashes** tinyproxy's evaluation on every request |
| **F14** | high | balanced `(...)` or `[...]` are valid regex and silently match **something other than the literal entry** |
| **F12** | medium | `*`, `+`, `?`, `{n}` act as quantifiers rather than literal characters |

The brief's instruction, and it is the one thing I most want honoured:

> **Verify each id explicitly against its own reproduction; do not assume three
> went green because the fourth did.**

They share a cause, not a test. A change that fixes the crash and leaves
alternation working would look finished.

---

## R12 — every ERE metacharacter in an allowlist entry is a literal

**Observable behaviour required.** An `egress_allowlist` entry matches hosts by
its literal text. No character in it is interpreted as a regex operator.

**What must be true afterwards, stated per finding so each is checkable:**

- **F1** — an entry containing `|` admits only the literal host containing that
  character, and admits nothing else. `evil.com|.*` must not admit
  `anything.example`.
- **F13** — an entry containing an unbalanced `(` or `[` produces a filter line
  that evaluates without error. Whatever it then admits, it must not crash.
- **F14** — an entry containing balanced `(...)` or `[...]` admits the literal
  text. `a(b)c` admits the host `a(b)c` and does **not** admit `abc`;
  `a[bc]d` does not admit `abd`.
- **F12** — `a*b`, `a+b`, `a?b` and `a{2}b` each admit their literal selves and
  nothing else.

**And what must not change.** Ordinary entries keep working exactly as they do
today: an exact host matches itself, a subdomain of a listed host is admitted
through the `(^|\.)` prefix, an unrelated host is refused. That anchoring is
correct and is not what this contract touches.

---

## What is NOT in scope

- **F10 — bare generic suffixes.** An entry like `com` acting as a wildcard is
  phase 2 item 3, and it is separate **on purpose**: escaping metacharacters does
  not make `com` safe, because the defect there is the anchor rather than the
  escaping. Do not fold it in, and do not "improve" the anchor while you are
  here.
- **F50–F54 — the tinyproxy directives.** Phase 2 item 2. Different defect,
  different change.
- **F2 and F55 — non-string and null entries.** They raise uncaught exceptions
  rather than mis-matching. Adjacent and not this.

---

## The thing that will happen, so nobody mistakes it for a regression

**Several existing characterization tests pin the broken behaviour and will go
red.** That is correct and expected. They were written during the review to
record what the code does, flagging it as wrong at the same time — which is
exactly what a characterization suite is for.

This has now happened three times in this project — with F150, with the `--init`
flag, and with F100's own proof test — and each time the cost was entirely in
whether the next reader could tell a deliberate inversion from a silent
weakening. So:

- The **implementer does not touch them.** They are read-only to its tier and the
  merge gate reverts the change silently. A test it believes must change is
  `NEED_INFO(<test name>)` back to me.
- The **test engineer inverts them afterwards**, renaming each so the name states
  what is now true, with a comment naming the finding and saying the inversion is
  deliberate.
- A test that asserted a crash must not simply be deleted. What it asserted is
  now a different behaviour, and that behaviour is worth pinning.

---

## What "done" looks like

The suite is **green**: 967 passed, 3 skipped, 0 failed in a container; on a host
with `docker` present, 969 passed with 1 failed, that one being
`test_the_claude_script_uses_the_container_profile_only_where_it_should` —
finding F130, scheduled in phase 3.

Run it as `uv run --frozen python -m pytest`, never `uv run --frozen pytest`.

R12 is done when all four findings are independently verified against their own
reproductions, the inverted characterization tests state what is now true, and
that baseline is otherwise unchanged.
