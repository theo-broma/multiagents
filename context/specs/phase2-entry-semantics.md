# Phase 2 contract — what an allowlist entry means

The interface contract for `BRIEF.md` phase 2 item 3: **F10**, and — deliberately
bundled — **F11** and the surviving half of the adversary's fuzz claim. Written
by the orchestrator after a consult.

Read them with `read_finding("F10")` and `read_finding("F11")`.
`list_tickets` is not reachable from an agent; do not try it.

---

## Why three findings are one contract

I have refused to bundle findings all week, so this needs justifying rather than
asserting.

These are not three defects. They are one missing boundary seen from three
angles:

- **F10** — an entry `com` admits every `.com` host, because the generated line
  anchors on `(^|\.)` and accepts any prefix. The *structural* view.
- **The adversary's fuzz remainder** — nothing validates that an entry is a
  hostname at all. A non-hostname string is accepted in silence and becomes part
  of the egress boundary. Phase 2 item 1 made it inert rather than dangerous; it
  did not make it *noticed*. The *adversarial* view.
- **F11** — the operator gets a dead entry with **no signal that it is dead**.
  Item 1 fixed two of its seven reproductions and left five: a leading dot, a
  trailing dot, surrounding whitespace, a `:port` suffix, a full URL. The
  *operational* view, and the one that says what the other two are missing.

Fixing them separately means three passes over the same validation code. The
ledger stays honest because each id is closed on its own evidence, below.

---

## R14 — an entry with no dot matches exactly

**Observable behaviour required.** An `egress_allowlist` entry containing no dot
admits that host and nothing else.

Today `com` generates `(^|\.)com$` and admits `example.com`, `evil.com`, and
every other `.com` host in the world. That is F10, and it is a typo away from any
operator who meant `example.com`.

**What must be true afterwards:**

- `com` admits the literal host `com` and refuses `example.com`.
- `localhost`, `redis`, and any other single-label internal host still work as
  exact matches. **This is why the fix is not "reject dotless entries".**
  Container networks legitimately carry single-label names.
- **An entry containing a dot keeps exactly today's suffix behaviour.**
  `googleapis.com` must still admit `storage.googleapis.com`. Every entry in the
  shipped and live allowlists is multi-label, so this rule changes nothing that
  anyone currently relies on — verified against both files before writing this.

**Do not reach for `*` as a suffix marker.** Phase 2 item 1 landed an hour ago
and made `*` a literal character; `*.example.com` now matches the literal host
`*.example.com`. Introducing a wildcard syntax would mean carving an exception
out of the escaping that was just fixed, and that is a larger decision than this
contract.

**What this does NOT close, stated plainly.** A multi-label public suffix —
`co.uk`, `com.au`, `github.io` — still behaves as a suffix and still admits
every host beneath it. Distinguishing those from ordinary domains needs a public
suffix list, which is a dependency and a design decision this contract does not
take. F10's title says "bare **or short** generic suffix"; this closes the bare
case. Say so in your result rather than letting the finding read as fully
resolved.

---

## R15 — a malformed entry is refused, loudly, before anything starts

**Observable behaviour required.** An entry that cannot function as a hostname
stops the environment from starting, with a message naming the entry and what is
wrong with it.

The five F11 reproductions are the cases: a leading dot (`.example.com`), a
trailing dot (`example.com.`), leading or trailing whitespace, a `:port` suffix,
and a full URL with scheme and path. Each of these today produces a filter line
that matches nothing, and says nothing.

**Fail closed and fail loudly, not warn-and-skip.** Skipping a malformed entry
leaves the operator with a narrower allowlist than they wrote; the agent then
fails with a network error and the operator debugs container routing for hours
having missed a startup warning. An invalid security configuration should refuse
to start.

**What must be true afterwards:**

- Each of the five forms raises at configuration load, naming the offending
  entry.
- The message says what is wrong, not merely that something is. "`example.com.`
  has a trailing dot" is actionable; "invalid allowlist entry" is not.
- A valid allowlist is unaffected. Both the shipped and the live configuration
  must load unchanged — check them.

**This is the signal F11 is actually asking for.** Its complaint was never really
about what a dead entry matches; it is that the operator is not told. R14 alone
would leave `example.com.` inert and silent, which is the same defect in a new
costume.

---

## What is NOT in scope

- **F2 and F55** — a non-string entry and a null list, both raising uncaught
  exceptions. They are adjacent and they are type validation rather than value
  validation. If your change makes them fall out naturally, say so and I will
  close them; do not go looking.
- **F50–F54**, the tinyproxy directives — item 2, landing before you start.
- **The `(^|\.)` prefix for dotted entries.** It is correct and every real entry
  depends on it.

---

## What "done" looks like

Baseline: **985 passed, 1 failed** on a host, the failure being
`test_the_claude_script_uses_the_container_profile_only_where_it_should` —
finding F130, phase 3, and only red where `docker` is present. Green inside a
container. Item 2 will have added to that count by the time this starts.

Run the suite as `uv run --frozen python -m pytest`, never `uv run --frozen pytest`.

**Expect characterization tests to go red**, and expect them in the F11 group
specifically — the five reproductions above assert that malformed entries
silently match nothing, and after R15 they will raise instead. That is correct.
The implementer does not touch them; a test engineer inverts them afterwards,
renaming each and naming the finding in a comment, as has been done three times
in this phase already.

R14 and R15 are done when a dotless entry matches exactly, every malformed form
is refused with a message naming it, both real allowlists still load, and the
suite is green apart from the inversions.
