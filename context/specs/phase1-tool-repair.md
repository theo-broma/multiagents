# Phase 1 contract — repairing the tool

The interface contract for the first tranche of `BRIEF.md` phase 1. Written by
the orchestrator, because this project expresses requirements as tests and
findings rather than as specifications, and the per-phase contract is the one
document that has to exist anyway.

Scope here is **items 1 and 3 only** — `bug-cfdc71` and `bug-08f9b3`, the two the
brief orders first and in that order. The remaining six items of phase 1 get
their own contract once these land.

Both tickets carry a proposed fix and name the test that should have caught
them. Read them with `list_tickets`. **Write the test the ticket names, not
merely a test**: each was chosen because its absence is what let the defect
through, and a different test that happens to pass proves less.

---

## R1 — the workspace container reaps its orphaned children

**Ticket:** `bug-cfdc71`. Blocking, and first because everything else in this
phase runs inside the container it describes.

**Observable behaviour required.** The argv that creates the workspace container
carries an init process. Today it does not: `run_args()` builds
`argv + [image, "sleep", "infinity"]`, and `sleep` never calls `wait()`, so every
orphaned grandchild becomes a permanent zombie holding a pid slot. Measured
during the review: 509 processes, 505 of them zombies, against a `pids_limit` of
512 — after which every new agent died during its own startup with a
signal-shaped exit code and text blaming unrelated projects.

**What must be true afterwards.**

- The container-creation argv contains an init process, so a process reparented
  to PID 1 inside the container is reaped rather than accumulating.
- The existing `run_args()` behaviour is otherwise unchanged. Nine assertions in
  `tests/test_core.py` already pin its mounts, network mode, `pids_limit` and
  environment; none of them may change meaning.

**What is out of scope.** The ticket also proposes reporting pid exhaustion when
an agent dies within seconds with no output. That is worth having and is **not**
part of R1 — it is a separate behaviour with its own test, and folding it in
here makes both harder to review.

**The test the ticket names:** an assertion on `run_args()`'s output, alongside
the nine that already exist. It does not exist today.

---

## R2 — an agent that owns a file may revise it

**Ticket:** `bug-08f9b3`. Blocking, and second because the three later items
that must change existing tests depend on this mechanism being understood.

**Observable behaviour required.** An agent's own `readonly_paths` replaces the
project default rather than adding to it. Two agents are documented as owning
files they cannot actually modify:

- `harness` carries a comment claiming it is the one agent in its team without
  `readonly_paths`, and then **omits the key** — which inherits the default
  rather than opting out of it. It can create a test file and never revise it.
- `reporter` carries `readonly_paths: ["**"]`, making every existing file
  read-only to it, including `context/review/REPORT.md` — the one file it exists
  to produce.

**What must be true afterwards.**

- `harness` resolves to an empty `readonly_paths`.
- `reporter` can modify `context/review/REPORT.md` and cannot modify the product
  source or its test suite.
- The agent that already does this correctly — `tester`, with an explicit `[]` —
  is unchanged, and remains the precedent the fix follows.

**Evidence the fix works.** Both changes were applied to the *live* config during
the review and proved there: after the `harness` change, a run modified an
existing test file and the change survived the merge gate. The work here is
landing the same change in the shipped defaults, where every new project gets
it.

---

## R3 — the assertion states the contract instead of its negation

**Ticket:** `bug-08f9b3`, its second half. This one is a **contract error**, not
an implementation error, which is why it belongs to the test engineer by
definition rather than by exemption.

`tests/test_core.py:10592` currently reads:

```python
assert "readonly_paths" not in harness, \
    "the harness builder is the one review agent that may edit shared files"
```

The message states the intent. The assertion checks the opposite of what
produces it: absence of the key means the default list is inherited, so the test
passes precisely when the agent may **not** edit shared files. It encodes the
defect, and a fix to R2 makes it fail.

**What must be true afterwards.** The assertion expresses what its own message
claims — that the agent resolves to an empty list — so that it fails if R2 ever
regresses, and passes once R2 lands.

**Note for whoever writes it.** R3 is the only requirement here whose test is
expected to go from passing to failing and back. It passes today against the
broken config, must fail once corrected to state the real contract, and passes
again when R2 merges. Say so in the result rather than letting it look like a
flake.

---

## What "done" looks like

The suite's baseline on this branch is **`949 passed, 1 failed, 3 skipped`**, and
the one failure is `F100`
(`tests/test_c2_provider_harness.py::test_read_provider_caches_until_invalidated`),
scheduled separately as phase 1 item 8. Until that lands, **one red test is the
baseline, not a regression**. Three skips shell out to `docker`, deliberately
absent in the container.

Run it as `uv run --frozen python -m pytest` — not `uv run --frozen pytest`,
which picks up a stale shebang when run from the project root on the host.

R1, R2 and R3 are done when their tests are green and that baseline is otherwise
unchanged.
